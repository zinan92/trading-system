from datetime import UTC, datetime
from dataclasses import replace
from decimal import Decimal

import pytest

from standard_broker.adapters.hyperliquid.orders import HyperliquidExternalOrderAdapter
from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    build_hyperliquid_testnet_order_adapter,
    default_testnet_capabilities,
)
from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
)
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.models import BrokerEnvironment, Provenance
from standard_broker.orders import OrderFill, OrderIntent, OrderReceipt, OrderSide, OrderState, OrderType, TimeInForce
from standard_broker.runtime import ExternalEnvironmentApproval, RuntimeActivationPolicy
from standard_broker.runtime_facts import RuntimeFactLedger

from test_external_host import ACCOUNT, RELEASE_SHA
from test_external_profile import _profile_context, _profile_runtime, _profile_session
from standard_broker.adapters.hyperliquid.profile import build_hyperliquid_testnet_host
from test_hyperliquid_runtime_order_lifecycle import FakeOrderBackend


NOW = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
META = {"universe": [{"name": "SOL", "index": 5, "szDecimals": 2, "maxLeverage": 10}]}


class ExternalOrderBackend(FakeOrderBackend):
    local_only = False
    external_network = True

    def __init__(self, profile) -> None:
        super().__init__(profile)
        self.fill_rows: list[dict[str, object]] = []
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version=NAUTILUS_HYPERLIQUID_VERSION,
            commit=NAUTILUS_HYPERLIQUID_COMMIT,
            capabilities=profile,
        )

    def activate(self, *, release_sha: str | None) -> None:
        assert release_sha == RELEASE_SHA

    def invoke(self, port: str, operation: str, request: object) -> object:
        if operation == "fills":
            self.calls.append((port, operation, request))
            return {"fills": list(self.fill_rows)}
        return super().invoke(port, operation, request)


class LifecycleStub:
    def __init__(self, session) -> None:
        self.runtime_session = session
        self.local_only = False
        self.transport_state = "external_testnet"
        self.calls: list[tuple[str, object]] = []
        self.submit_result: OrderReceipt | None = None
        self.fill = OrderFill(
            fill_id="fill-1",
            order_id="order-1",
            broker_order_id="101",
            client_order_id="cloid-order-1",
            instrument_id="SOL-USD-PERP",
            side=OrderSide.BUY,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            occurred_at=NOW,
            environment=BrokerEnvironment.TESTNET,
            account_address=ACCOUNT,
            lifecycle_id=session.lifecycle_id,
            release_sha=RELEASE_SHA,
        )

    @staticmethod
    def _receipt(session, *, order_id: str = "order-1", state: OrderState = OrderState.RESTING, reason: str | None = None):
        return OrderReceipt(
            order_id=order_id,
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            client_order_id=f"cloid-{order_id}",
            state=state,
            original_quantity=Decimal("0.1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0.1"),
            broker_order_id="101",
            average_fill_price=None,
            reason=reason,
            provenance=Provenance(
                source="nautilus-hyperliquid.testnet",
                execution_scope=session.execution_scope,
                transport_state="external_testnet",
                mapping_revision=session.capabilities.revision,
                received_at=NOW,
            ),
            updated_at=NOW,
            broker_updated_at=NOW,
            broker_order_lineage=("101",),
            client_order_lineage=(f"cloid-{order_id}",),
            account_address=ACCOUNT,
            lifecycle_id=session.lifecycle_id,
            release_sha=RELEASE_SHA,
        )

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        self.calls.append(("submit", intent))
        return self.submit_result or self._receipt(self.runtime_session, order_id=intent.order_id)

    def recover(self, intent: OrderIntent, *, broker_order_id: str, state: str) -> OrderReceipt:
        self.calls.append(("recover", intent))
        return self._receipt(
            self.runtime_session,
            order_id=intent.order_id,
            state=OrderState(state),
        )

    def recover_client_order(self, intent: OrderIntent, *, client_order_id: str, state: str) -> OrderReceipt:
        self.calls.append(("recover_client", intent))
        return replace(
            self._receipt(
                self.runtime_session,
                order_id=intent.order_id,
                state=OrderState(state),
            ),
            client_order_id=client_order_id,
            client_order_lineage=(client_order_id,),
        )

    def cancel(self, order_id: str) -> OrderReceipt:
        self.calls.append(("cancel", order_id))
        return self._receipt(self.runtime_session, order_id=order_id, state=OrderState.CANCEL_PENDING)

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        self.calls.append(("modify", intent))
        return self._receipt(self.runtime_session, order_id=order_id, state=OrderState.MODIFY_PENDING)

    def query(self, order_id: str) -> OrderReceipt:
        self.calls.append(("query", order_id))
        return self._receipt(self.runtime_session, order_id=order_id)

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        self.calls.append(("open_orders", instrument_id))
        return (self._receipt(self.runtime_session),)

    @property
    def fills(self):
        return {self.fill.fill_id: self.fill}

    def query_fills(
        self,
        *,
        order_id: str | None = None,
        instrument_id: str | None = None,
    ) -> tuple[OrderFill, ...]:
        self.calls.append(("query_fills", order_id or instrument_id))
        values = (self.fill,)
        if order_id is not None:
            values = tuple(item for item in values if item.order_id == order_id)
        if instrument_id is not None:
            values = tuple(item for item in values if item.instrument_id == instrument_id)
        return values


def _intent(*, order_id: str = "order-1", key: str = "cycle-1", close: bool = False) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instrument_id="SOL-USD-PERP",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.1"),
        limit_price=Decimal("100"),
        time_in_force=TimeInForce.GTC,
        idempotency_key=key,
        reduce_only=close,
        close_position=close,
    )


def _adapter() -> tuple[HyperliquidExternalOrderAdapter, LifecycleStub, object, object]:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(
        context=_profile_context(session),
        runtime=runtime,
    )
    lifecycle = LifecycleStub(session)
    return HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle), lifecycle, host, runtime


def test_external_order_facade_authorizes_each_canonical_operation() -> None:
    adapter, lifecycle, _, runtime = _adapter()

    submitted = adapter.submit(_intent())
    canceled = adapter.cancel(submitted.order_id)
    queried = adapter.query(submitted.order_id)
    replaced = adapter.replace(submitted.order_id, _intent(key="replace-1"))
    opened = adapter.open_orders("SOL-USD-PERP")
    fills = adapter.fills(order_id="order-1")

    assert submitted.state is OrderState.RESTING
    assert canceled.state is OrderState.CANCEL_PENDING
    assert queried.order_id == "order-1"
    assert replaced.state is OrderState.MODIFY_PENDING
    assert opened[0].order_id == "order-1"
    assert fills[0].fill_id == "fill-1"
    assert [call[0] for call in lifecycle.calls] == [
        "submit",
        "cancel",
        "query",
        "modify",
        "open_orders",
        "query_fills",
    ]
    assert [sorted(item["order_execution"]) for item in runtime.preflight_calls] == [
        ["submit"],
        ["cancel"],
        ["query"],
        ["replace"],
        ["open_orders"],
        ["fills"],
    ]
    assert runtime.invoke_calls == []


def test_external_order_facade_preserves_ambiguous_unknown_without_retry() -> None:
    adapter, lifecycle, _, _ = _adapter()
    lifecycle.submit_result = lifecycle._receipt(
        lifecycle.runtime_session,
        state=OrderState.UNKNOWN,
        reason="ambiguous_submit:timeout",
    )

    result = adapter.submit(_intent())

    assert result.state is OrderState.UNKNOWN
    assert result.reason == "ambiguous_submit:timeout"
    assert [call[0] for call in lifecycle.calls] == ["submit"]


def test_external_order_facade_recovers_persisted_client_identity_without_submit() -> None:
    adapter, lifecycle, _, runtime = _adapter()

    result = adapter.recover_client_order(
        _intent(),
        client_order_id="0xclient-recovered",
        state="unknown",
    )

    assert result.state is OrderState.UNKNOWN
    assert result.client_order_id == "0xclient-recovered"
    assert [call[0] for call in lifecycle.calls] == ["recover_client"]
    assert runtime.invoke_calls == []


def test_reduce_only_close_intent_remains_explicit_at_external_boundary() -> None:
    adapter, lifecycle, _, _ = _adapter()

    adapter.submit(_intent(close=True))

    submitted_intent = lifecycle.calls[0][1]
    assert isinstance(submitted_intent, OrderIntent)
    assert submitted_intent.reduce_only is True
    assert submitted_intent.close_position is True


def test_external_order_facade_rejects_receipt_identity_conflict() -> None:
    adapter, lifecycle, _, _ = _adapter()
    lifecycle.submit_result = replace(
        lifecycle._receipt(lifecycle.runtime_session),
        account_address="0x" + "22" * 20,
    )

    with pytest.raises(RuntimeBoundaryError, match="external_order_receipt_identity_mismatch"):
        adapter.submit(_intent())


def test_external_order_facade_rejects_local_fixture_lifecycle() -> None:
    session = _profile_session()
    host = build_hyperliquid_testnet_host(
        context=_profile_context(session),
        runtime=_profile_runtime(session),
    )
    lifecycle = LifecycleStub(session)
    lifecycle.local_only = True

    with pytest.raises(RuntimeBoundaryError, match="external_order_fixture_forbidden"):
        HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle)


def test_factory_binds_real_runtime_fixture_to_external_facade_without_network() -> None:
    session = _profile_session()
    backend = ExternalOrderBackend(default_testnet_capabilities())
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=NAUTILUS_HYPERLIQUID_VERSION,
            expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
            policy=RuntimeActivationPolicy(
                testnet_approval=ExternalEnvironmentApproval(
                    environment=BrokerEnvironment.TESTNET,
                    approval_id="external-order-fixture-approval",
                    release_sha=RELEASE_SHA,
                    approved_by="park",
                    approved_at=NOW,
                    account_address=ACCOUNT,
                    lifecycle_id=session.lifecycle_id,
                )
            ),
            expected_release_sha=RELEASE_SHA,
        ),
    )
    runtime.start()
    instruments = HyperliquidInstrumentAdapter.from_meta(META, revision=session.capabilities.revision)
    adapter = build_hyperliquid_testnet_order_adapter(
        context=_profile_context(session),
        runtime=runtime,
        instruments=instruments,
        ledger=RuntimeFactLedger(),
    )

    receipt = adapter.submit(_intent())
    backend.fill_rows = [
        {
            "oid": receipt.broker_order_id,
            "cloid": receipt.client_order_id,
            "coin": "SOL",
            "tid": "501",
            "side": "B",
            "px": "100",
            "sz": "0.04",
            "time": 1787461200000,
        }
    ]
    fills = adapter.fills(instrument_id="SOL-USD-PERP")
    duplicate_fills = adapter.fills(instrument_id="SOL-USD-PERP")

    assert adapter.local_only is False
    assert adapter.transport_state == "external_testnet"
    assert receipt.state is OrderState.RESTING
    assert fills[0].fill_id == "501"
    assert duplicate_fills == fills
    assert [call[1] for call in backend.calls] == ["submit", "fills", "fills"]

    backend.submit_timeout = True
    unknown = adapter.submit(_intent(order_id="order-2", key="cycle-2"))
    with pytest.raises(RuntimeBoundaryError, match="reconciliation_required"):
        adapter.cancel(unknown.order_id)
    assert [call[1] for call in backend.calls][-1:] == ["submit"]
