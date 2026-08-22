from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from services.broker_composition import (
    BrokerBuildContext,
    build_broker_execution_port,
)
from services.broker_port import BrokerCancelRequest, BrokerCapability
from services.standard_broker_testnet import StandardBrokerTestnetHostError


def _testnet_config(backend: object, approval: object) -> dict:
    return {
        "provider": "standard_broker",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "environment_fingerprint": "hyperliquid:testnet:fingerprint",
        "account_id": "testnet-account",
        "credential_source": "HL_TESTNET_CREDENTIAL",
        "runtime_id": "runtime-testnet",
        "ledger_namespace": "ledger.standard-broker.testnet",
        "release_sha": "a" * 40,
        "execution_scope": "hypercore:default",
        "backend": backend,
        "testnet_approval": approval,
        "instrument_meta": {
            "universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]
        },
        "nautilus_expected_version": "1.230.0",
        "nautilus_expected_commit": "order-lifecycle-commit",
    }


def _approval():
    from standard_broker import BrokerEnvironment, ExternalEnvironmentApproval

    return ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id="testnet-approval-host",
        release_sha="a" * 40,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address="testnet-account",
        lifecycle_id="runtime-testnet",
    )


class FixtureBackend:
    local_only = True

    def __init__(self, capabilities) -> None:
        from standard_broker.adapters.hyperliquid import NautilusAdapterMetadata

        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version="1.230.0",
            commit="order-lifecycle-commit",
            capabilities=capabilities,
        )
        self.calls: list[tuple[str, str, object]] = []
        self.last_cloid: str | None = None

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        if operation == "submit":
            self.last_cloid = str(request["cloid"])
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 101}}]},
                },
            }
        if operation in {"cancel", "replace"}:
            return {"status": "ok"}
        if operation == "query":
            return {
                "status": "open",
                "oid": 101,
                "cloid": self.last_cloid,
                "timestamp": 1787313661000,
            }
        if operation == "open_orders":
            return {
                "orders": [
                    {"status": "open", "oid": 101, "cloid": self.last_cloid}
                ]
            }
        return {"status": "unknown"}


def _context(tmp_path: Path, backend: object) -> BrokerBuildContext:
    from standard_broker import CapabilityDescriptor, BrokerEnvironment

    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={
            "order_execution": frozenset(
                {"submit", "cancel", "replace", "query", "open_orders"}
            )
        },
        revision="testnet-order-v1",
    )
    backend.metadata = type(backend.metadata)(
        package=backend.metadata.package,
        version=backend.metadata.version,
        commit=backend.metadata.commit,
        capabilities=profile,
    )
    return BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config=_testnet_config(backend, _approval()),
    )


def test_testnet_composition_is_explicit_local_fixture_and_ready(tmp_path: Path) -> None:
    from standard_broker import CapabilityDescriptor, BrokerEnvironment

    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={
            "order_execution": frozenset(
                {"submit", "cancel", "replace", "query", "open_orders"}
            )
        },
        revision="testnet-order-v1",
    )
    backend = FixtureBackend(profile)
    adapter = build_broker_execution_port(_context(tmp_path, backend))

    preflight = adapter.preflight()

    assert adapter.name == "standard_broker_testnet"
    assert preflight["ready"] is True
    assert preflight["environment"] == "testnet"
    assert preflight["network_io"] is False
    assert preflight["external_network"] is True
    assert preflight["real_money_eligible"] is False
    assert preflight["transport_state"] == "local_fixture"
    assert adapter.fact_ledger.session_key.startswith(
        "ledger.standard-broker.testnet:hyperliquid:testnet:"
    )
    assert adapter.capabilities.supports(BrokerCapability.REPLACE_ORDER)
    assert adapter.capabilities.supports(BrokerCapability.ORDER_RECONCILIATION)


def test_testnet_composition_maps_canonical_order_lifecycle(tmp_path: Path) -> None:
    from standard_broker import (
        CapabilityDescriptor,
        BrokerEnvironment,
        OrderIntent,
        OrderSide,
        OrderState,
        OrderType,
        TimeInForce,
    )

    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={
            "order_execution": frozenset(
                {"submit", "cancel", "replace", "query", "open_orders"}
            )
        },
        revision="testnet-order-v1",
    )
    backend = FixtureBackend(profile)
    adapter = build_broker_execution_port(_context(tmp_path, backend))
    intent = OrderIntent(
        order_id="testnet-order-1",
        instrument_id="BTC-USD-PERP",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.1"),
        limit_price=Decimal("65000"),
        time_in_force=TimeInForce.GTC,
        idempotency_key="testnet-order-1",
    )

    first = adapter.request("order_execution", "submit", intent)
    duplicate = adapter.request("order_execution", "submit", intent)
    queried = adapter.request("order_execution", "query", first.order_id)
    open_orders = adapter.request("order_execution", "open_orders", "BTC-USD-PERP")
    canceled = adapter.cancel_order(
        BrokerCancelRequest(
            run_date="2026-08-22",
            asset="BTC-USD-PERP",
            client_order_id=first.client_order_id,
        )
    )

    assert first == duplicate
    assert first.environment is BrokerEnvironment.TESTNET
    assert first.account_address == "testnet-account"
    assert first.lifecycle_id == "runtime-testnet"
    assert first.release_sha == "a" * 40
    assert queried.state is OrderState.RESTING
    assert open_orders[0].order_id == first.order_id
    assert canceled.state is OrderState.CANCEL_PENDING
    assert [call[1] for call in backend.calls] == ["submit", "query", "open_orders", "cancel"]
    with pytest.raises(StandardBrokerTestnetHostError, match="contradictory"):
        adapter.cancel_order(
            BrokerCancelRequest(
                run_date="2026-08-22",
                asset="BTC-USD-PERP",
                client_order_id=first.client_order_id,
                broker_order_id="unknown-broker-order",
            )
        )


def test_testnet_missing_fixture_fails_without_fallback(tmp_path: Path) -> None:
    from standard_broker import CapabilityDescriptor, BrokerEnvironment

    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={"order_execution": frozenset({"submit"})},
        revision="testnet-order-v1",
    )
    backend = FixtureBackend(profile)
    config = _testnet_config(backend, _approval())
    config.pop("backend")
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config=config,
    )

    with pytest.raises(StandardBrokerTestnetHostError, match="missing backend"):
        build_broker_execution_port(context)


def test_testnet_rejects_secret_like_credential_source(tmp_path: Path) -> None:
    from standard_broker import CapabilityDescriptor, BrokerEnvironment

    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={"order_execution": frozenset({"submit"})},
        revision="testnet-order-v1",
    )
    backend = FixtureBackend(profile)
    config = _testnet_config(backend, _approval())
    config["credential_source"] = "testnet-secret-token-123"
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config=config,
    )

    with pytest.raises(StandardBrokerTestnetHostError, match="credential_source"):
        build_broker_execution_port(context)
