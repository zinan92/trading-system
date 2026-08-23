from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest

from services.standard_broker_external_canary import (
    StandardBrokerExternalCanaryAdapter,
    StandardBrokerExternalCanaryError,
)
from services.standard_broker_testnet_canary import TestnetCanaryOrderRequest
from services.standard_broker_testnet_canary import TestnetCanary, market_fact_digest
from tests.test_standard_broker_testnet_canary import _authorized_canary, _plan


class FakeBinding:
    def __init__(self) -> None:
        self.requests = []

    def preflight(self):
        return {
            "canary_ready": True,
            "host_ready": True,
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "transport_state": "external_testnet",
            "account_fingerprint": "sha256:" + "a" * 64,
            "runtime_id": "canary-runtime-1",
            "release_sha": "b" * 40,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
            "network_io": True,
            "real_money_eligible": False,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
            "protection_ready": False,
            "protection_gap": "external_protection_unavailable",
        }

    def market_fact(self, *, instrument_id: str, now):
        result = {
            "instrument_id": instrument_id,
            "contract_multiplier": "1",
            "price": "60000",
            "freshness": "fresh",
            "observed_at": now.isoformat(),
            "max_age_seconds": "120",
            "source": "nautilus-hyperliquid.testnet",
            "transport_state": "external_testnet",
            "mapping_revision": "hyperliquid-testnet-runtime-v1",
        }
        result["fact_digest"] = market_fact_digest(result)
        return result

    def submit(self, intent):
        self.requests.append(intent)
        return SimpleNamespace(order_id=intent.order_id, intent=intent)

    def query(self, order_id):
        return SimpleNamespace(order_id=order_id)

    def query_by_idempotency_key(self, key):
        return SimpleNamespace(order_id=key.split(":")[0])

    def replace(self, order_id, intent):
        self.requests.append(intent)
        return SimpleNamespace(order_id=order_id, intent=intent)

    def cancel(self, order_id):
        return SimpleNamespace(order_id=order_id)

    def read_facts(self, **kwargs):
        raise RuntimeError("not exercised")


def _request(**overrides):
    values = {
        "order_id": "canary-1:entry",
        "instrument_id": "BTC-USD-PERP",
        "side": "buy",
        "quantity": Decimal("0.001"),
        "order_type": "limit",
        "limit_price": Decimal("60000"),
        "time_in_force": "gtc",
        "idempotency_key": "canary-1:entry:60000",
        **overrides,
    }
    return TestnetCanaryOrderRequest(**values)


def test_wrapper_maps_only_canonical_order_request_and_exposes_preflight() -> None:
    binding = FakeBinding()
    adapter = StandardBrokerExternalCanaryAdapter(binding)
    request = _request()

    assert adapter.preflight()["canary_ready"] is True
    adapter.submit(request)
    intent = binding.requests[0]
    assert intent.instrument_id == request.instrument_id
    assert intent.quantity == request.quantity
    assert not hasattr(intent, "oid")
    assert adapter.market_fact(instrument_id=request.instrument_id, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))["instrument_id"] == request.instrument_id


def test_wrapper_rejects_missing_public_binding() -> None:
    with pytest.raises(StandardBrokerExternalCanaryError, match="public"):
        StandardBrokerExternalCanaryAdapter(object())


def test_wrapper_can_pass_attended_canary_admission_without_order_mutation(tmp_path) -> None:
    binding = FakeBinding()
    adapter = StandardBrokerExternalCanaryAdapter(binding)
    plan = _plan(canary_id="wrapper-admission")
    canary, confirmation = _authorized_canary(tmp_path, binding, plan)
    canary = TestnetCanary(
        canary.output_root,
        adapter,
        confirmation_ledger=canary.confirmation_ledger,
        approved_market_sources={"nautilus-hyperliquid.testnet"},
    )
    prepared = canary.prepare(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-23T01:00:00+00:00",
    )
    assert prepared["status"] == "AWAITING_ATTENDED_START"
    assert binding.requests == []


def test_wrapper_projects_standard_typed_bundle_without_native_fields() -> None:
    from standard_broker import ExternalCanaryFactBundle
    from standard_broker.account import AccountSnapshot
    from standard_broker.fees import FeeEvent, FeeKind, FeeSource, FeeState
    from standard_broker.models import BrokerEnvironment, Provenance
    from standard_broker.orders import OrderFill, OrderSide

    now = datetime(2026, 8, 23, 1, 2, tzinfo=UTC)
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope="hypercore:default",
        transport_state="external_testnet",
        mapping_revision="hyperliquid-testnet-runtime-v1",
        received_at=now,
    )
    fill = OrderFill(
        fill_id="fill-1",
        order_id="entry-1",
        broker_order_id="broker-1",
        client_order_id="client-1",
        instrument_id="BTC-USD-PERP",
        side=OrderSide.BUY,
        price=Decimal("60000"),
        quantity=Decimal("0.001"),
        occurred_at=now,
        environment=BrokerEnvironment.TESTNET,
        account_address="0x" + "11" * 20,
        lifecycle_id="canary-runtime-1",
        release_sha="b" * 40,
    )
    fee = FeeEvent(
        fee_id="fee-1",
        broker_id="hyperliquid",
        kind=FeeKind.TAKER,
        amount=Decimal("0.2"),
        currency="USD",
        occurred_at=now,
        source=FeeSource.ACTUAL_FILL,
        state=FeeState.ACTUAL,
        provenance=provenance,
        instrument_id="BTC-USD-PERP",
        fill_id="fill-1",
        order_id="entry-1",
        environment=BrokerEnvironment.TESTNET,
    )
    account = AccountSnapshot(
        broker_id="hyperliquid",
        account_address="0x" + "11" * 20,
        equity=Decimal("62"),
        balance=Decimal("62"),
        withdrawable=Decimal("62"),
        margin_used=Decimal("1"),
        exposure=Decimal("60"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        positions=(),
        provenance=provenance,
        environment=BrokerEnvironment.TESTNET,
        observation_id="123",
    )
    identity = SimpleNamespace(
        account_address=account.account_address,
        lifecycle_id="canary-runtime-1",
        release_sha="b" * 40,
        capability_revision="hyperliquid-testnet-runtime-v1",
        runtime_identity=SimpleNamespace(transport_state="external_testnet"),
    )
    snapshot = SimpleNamespace(
        identity=identity,
        cursor=SimpleNamespace(value=123),
        observed_at=now,
        passed=True,
        evidence_digest="sha256:" + "e" * 64,
    )
    bundle = ExternalCanaryFactBundle(
        fills=(fill,),
        fees=(fee,),
        account=account,
        positions=(),
        open_orders=(),
        reconciliation=snapshot,
    )
    projected = StandardBrokerExternalCanaryAdapter._convert_bundle(bundle)
    assert projected.fills[0].fill_id == "fill-1"
    assert projected.fees[0].fee_state == "actual"
    assert projected.reconciliation.passed is True
    assert "native" not in str(projected).lower()
