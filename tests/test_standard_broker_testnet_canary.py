from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path

import pytest

from services.standard_broker_testnet_canary import (
    TestnetCanary,
    TestnetCanaryError,
    TestnetCanaryPlan,
    canary_plan_digest,
    market_fact_digest,
)
from services.park_confirmation import ParkConfirmationLedger


ACCOUNT_FINGERPRINT = "sha256:" + "a" * 64
RELEASE_SHA = "b" * 40


@dataclass
class FakeReceipt:
    order_id: str
    client_order_id: str
    broker_order_id: str
    state: str
    original_quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    reason: str | None = None

    broker_id: str = "hyperliquid"
    environment: str = "testnet"
    account_address: str = ACCOUNT_FINGERPRINT
    lifecycle_id: str = "canary-runtime-1"
    release_sha: str = RELEASE_SHA
    instrument_id: str = "BTC-USD-PERP"
    side: str = "buy"
    quantity: Decimal = Decimal("0.001")
    order_type: str = "limit"
    time_in_force: str = "gtc"
    limit_price: Decimal = Decimal("60000")

    class _Provenance:
        source = "fake.external_testnet"
        execution_scope = "hypercore:default"
        transport_state = "external_testnet"
        mapping_revision = "hyperliquid-testnet-runtime-v1"

    provenance = _Provenance()


class FakeCanaryBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.status = "resting"
        self.next_order = 1
        self.preflight_overrides: dict[str, object] = {}
        self.active_request = None
        self.allow_recovery = False
        self.market_fact_value = None

    def preflight(self) -> dict:
        self.calls.append(("preflight", None))
        market_fact = {
            "instrument_id": "BTC-USD-PERP",
            "contract_multiplier": "1",
            "price": "60000",
            "freshness": "fresh",
            "observed_at": "2026-08-23T00:59:30+00:00",
            "max_age_seconds": "120",
            "transport_state": "external_testnet",
            "source": "fake.external_testnet",
            "mapping_revision": "hyperliquid-testnet-runtime-v1",
        }
        market_fact["fact_digest"] = market_fact_digest(market_fact)
        self.market_fact_value = market_fact
        return {
            "canary_ready": True,
            "host_ready": True,
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "transport_state": "external_testnet",
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "runtime_id": "canary-runtime-1",
            "release_sha": RELEASE_SHA,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
            "network_io": True,
            "real_money_eligible": False,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
            "protection_ready": False,
            "protection_gap": "external_protection_unavailable",
            "market_fact": market_fact,
            **self.preflight_overrides,
        }

    def market_fact(self, *, instrument_id: str, now):
        del now
        value = dict(self.market_fact_value or {})
        value["instrument_id"] = instrument_id
        return value

    def submit(self, request):
        self.calls.append(("submit", request))
        self.active_request = request
        order_id = f"canary-order-{self.next_order}"
        self.next_order += 1
        return FakeReceipt(
            order_id=order_id,
            client_order_id=request.idempotency_key,
            broker_order_id=f"broker-{order_id}",
            state=self.status,
            original_quantity=request.quantity,
            remaining_quantity=request.quantity,
            average_fill_price=None,
            instrument_id=request.instrument_id,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            limit_price=request.limit_price,
        )

    def query(self, order_id: str):
        self.calls.append(("query", order_id))
        request = self.active_request
        return FakeReceipt(
            order_id=order_id,
            client_order_id=f"client-{order_id}",
            broker_order_id=f"broker-{order_id}",
            state=self.status,
            original_quantity=Decimal("0.001"),
            remaining_quantity=Decimal("0.001") if self.status == "resting" else Decimal("0"),
            instrument_id=request.instrument_id if request else "BTC-USD-PERP",
            side=request.side if request else "buy",
            quantity=request.quantity if request else Decimal("0.001"),
            order_type=request.order_type if request else "limit",
            time_in_force=request.time_in_force if request else "gtc",
            limit_price=request.limit_price if request else Decimal("60000"),
        )

    def query_by_idempotency_key(self, idempotency_key: str):
        if not self.allow_recovery:
            raise RuntimeError("recovery unavailable")
        return self.query("recovered-order")

    def replace(self, order_id: str, request):
        self.calls.append(("replace", request))
        self.active_request = request
        return self.query(order_id)

    def cancel(self, order_id: str):
        self.calls.append(("cancel", order_id))
        self.status = "canceled"
        return self.query(order_id)


def _plan_values() -> dict:
    values = {
        "canary_id": "canary-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-default",
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "runtime_id": "canary-runtime-1",
        "release_sha": RELEASE_SHA,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
        "instrument_id": "BTC-USD-PERP",
        "direction": "buy",
        "quantity": "0.001",
        "contract_multiplier": "1",
        "quantity_step": "0.001",
        "order_type": "limit",
        "entry_price": "60000",
        "price_tick": "1",
        "time_in_force": "gtc",
        "max_slippage": "25",
        "max_notional": "100",
        "max_leverage": "2",
        "account_equity": "63",
        "max_open_orders": 1,
        "max_open_positions": 1,
        "close_price": "59900",
        "fee_reserve_usd": "1",
        "entry_fee_estimate_usd": "0.25",
        "exit_fee_estimate_usd": "0.25",
        "explicit_loss_buffer_usd": "0.5",
        "max_loss_usd": "50",
        "close_mode": "ordinary_reduce_only_close",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    values["plan_digest"] = canary_plan_digest(values)
    return values


def _plan(**overrides) -> TestnetCanaryPlan:
    values = {**_plan_values(), **overrides}
    if "plan_digest" not in overrides:
        values["plan_digest"] = canary_plan_digest(values)
    return TestnetCanaryPlan.from_mapping(values)


def _confirmation(plan: TestnetCanaryPlan) -> dict:
    return {
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.canary_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "confirmation_id": "confirmation-1",
        "expires_at": plan.expires_at,
    }


def _authorized_canary(tmp_path: Path, broker: FakeCanaryBroker, plan: TestnetCanaryPlan, *, name: str = "outputs"):
    output_root = tmp_path / name
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal_id = f"proposal-{plan.canary_id}"
    binding = {
        "strategy_session_id": f"canary-session:{plan.canary_id}",
        "strategy_revision_id": f"canary-revision:{plan.plan_digest[7:23]}",
    }
    ledger.create_proposal(
        proposal_id=proposal_id,
        strategy_session_id=binding["strategy_session_id"],
        strategy_revision_id=binding["strategy_revision_id"],
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal_id,
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding=binding,
        now=1787446800,
    )
    confirmation = {
        **_confirmation(plan),
        "proposal_id": proposal_id,
        "receipt_digest": decision["receipt_digest"],
        "event": "confirmed",
        "park_user_id": "park",
    }
    return TestnetCanary(
        output_root,
        broker,
        confirmation_ledger=ledger,
        approved_market_sources={"fake.external_testnet"},
    ), confirmation


def test_canary_plan_preflight_and_entry_lifecycle_are_attended_and_idempotent(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    plan = _plan()
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)

    prepared = canary.prepare(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-23T01:00:00+00:00",
    )
    assert prepared["status"] == "AWAITING_ATTENDED_START"
    assert [name for name, _ in broker.calls] == ["preflight"]

    submitted = canary.submit_entry(
        plan,
        timestamp="2026-08-23T01:01:00+00:00",
        confirmation=confirmation,
    )
    assert submitted["status"] == "ENTRY_RECONCILED"
    assert [name for name, _ in broker.calls] == ["preflight", "submit", "query"]
    assert canary.submit_entry(
        plan,
        timestamp="2026-08-23T01:01:30+00:00",
        confirmation=confirmation,
    )["status"] == "ENTRY_RECONCILED"
    assert [name for name, _ in broker.calls].count("submit") == 1

    replaced = canary.replace_entry(
        plan,
        entry_price=Decimal("59980"),
        timestamp="2026-08-23T01:02:00+00:00",
        confirmation=confirmation,
    )
    assert replaced["status"] == "ENTRY_RECONCILED"
    assert [row["operation"] for row in replaced["lineage"]] == ["submit", "replace"]
    assert replaced["lineage"][0]["original_order_id"] == replaced["lineage"][1]["original_order_id"]

    canceled = canary.cancel_entry(
        plan,
        timestamp="2026-08-23T01:03:00+00:00",
        confirmation=confirmation,
    )
    assert canceled["status"] == "NO_FILL_RECONCILED"
    assert canary.snapshot(plan)["status"] == "NO_FILL_RECONCILED"
    encoded = json.dumps(canary.snapshot(plan), sort_keys=True)
    assert "native" not in encoded.lower()
    assert "fixture://" not in encoded


def test_canary_plan_rejects_missing_or_over_budget_fields_before_preflight(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    canary = TestnetCanary(
        tmp_path / "outputs",
        broker,
        approved_market_sources={"fake.external_testnet"},
    )

    with pytest.raises(TestnetCanaryError, match="plan_digest"):
        canary.prepare(
            _plan(plan_digest="sha256:" + "c" * 64),
            confirmation={},
            timestamp="2026-08-23T01:00:00+00:00",
        )

    with pytest.raises(TestnetCanaryError, match="max_loss_usd"):
        over_budget = _plan_values()
        over_budget["max_loss_usd"] = "50.01"
        over_budget["plan_digest"] = canary_plan_digest(over_budget)
        canary.prepare(
            over_budget,
            confirmation={},
            timestamp="2026-08-23T01:00:00+00:00",
        )

    assert broker.calls == []
    assert canary.snapshot()["status"] == "BLOCKED"


def test_canary_freezes_on_unknown_order_state_and_never_falls_back(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    broker.status = "unknown"
    plan = _plan()
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)

    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")
    with pytest.raises(TestnetCanaryError, match="unknown"):
        canary.submit_entry(
            plan,
            timestamp="2026-08-23T01:01:00+00:00",
            confirmation=confirmation,
        )

    assert canary.snapshot(plan)["status"] == "BLOCKED"
    assert all(name in {"preflight", "submit", "query"} for name, _ in broker.calls)


@pytest.mark.parametrize("status", ["bogus", ""])
def test_canary_freezes_on_any_noncanonical_order_state(tmp_path: Path, status: str) -> None:
    broker = FakeCanaryBroker()
    broker.status = status
    plan = _plan()
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")

    with pytest.raises(TestnetCanaryError, match="unknown"):
        canary.submit_entry(
            plan,
            timestamp="2026-08-23T01:01:00+00:00",
            confirmation=confirmation,
        )
    assert canary.snapshot(plan)["status"] == "BLOCKED"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"runtime_id": "drifted-runtime"}, "runtime_id mismatch"),
        ({"capability_revision": "drifted-capability"}, "capability_revision mismatch"),
        (
            {
                "market_fact": {
                    "instrument_id": "BTC-USD-PERP",
                    "contract_multiplier": "1",
                    "price": "60000",
                    "freshness": "stale",
                    "observed_at": "2026-08-23T00:00:00+00:00",
                    "transport_state": "external_testnet",
                }
            },
            "stale",
        ),
    ],
)
def test_canary_persists_preflight_blockers_and_never_submits(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    broker = FakeCanaryBroker()
    broker.preflight_overrides = override
    plan = _plan()
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)

    with pytest.raises(TestnetCanaryError, match=message):
        canary.prepare(
            plan,
            confirmation=confirmation,
            timestamp="2026-08-23T01:00:00+00:00",
        )

    blocked = canary.snapshot(plan)
    assert blocked["status"] == "BLOCKED"
    assert blocked["next_action"] == "notify_park_and_wait"
    assert [name for name, _ in broker.calls] == ["preflight"]


def test_canary_persists_malformed_plan_blocker_without_inference(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    canary = TestnetCanary(tmp_path / "outputs", broker)
    values = _plan_values()
    del values["quantity"]

    with pytest.raises(TestnetCanaryError, match="quantity"):
        canary.prepare(
            values,
            confirmation={},
            timestamp="2026-08-23T01:00:00+00:00",
        )

    blocked = canary.snapshot()
    assert blocked["status"] == "BLOCKED"
    assert blocked["plan"].get("quantity") is None
    assert broker.calls == []


@pytest.mark.parametrize(
    "missing_field",
    ["contract_multiplier", "entry_fee_estimate_usd", "exit_fee_estimate_usd", "explicit_loss_buffer_usd", "quantity_step", "price_tick"],
)
def test_canary_blocks_when_risk_contract_is_incomplete(tmp_path: Path, missing_field: str) -> None:
    broker = FakeCanaryBroker()
    canary = TestnetCanary(tmp_path / missing_field, broker)
    values = _plan_values()
    values.pop(missing_field)

    with pytest.raises(TestnetCanaryError, match=missing_field):
        canary.prepare(values, confirmation={}, timestamp="2026-08-23T01:00:00+00:00")
    assert canary.snapshot()["status"] == "BLOCKED"
    assert broker.calls == []


def test_canary_rejects_ambiguous_market_identity_before_confirmation(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    broker.preflight_overrides = {
        "market_fact": {
            "instrument_id": "ETH-USD-PERP",
            "price": "60000",
            "freshness": "fresh",
            "observed_at": "2026-08-23T00:59:30+00:00",
            "transport_state": "external_testnet",
        }
    }
    plan = _plan()
    canary = TestnetCanary(tmp_path / "outputs", broker)

    with pytest.raises(TestnetCanaryError, match="instrument identity"):
        canary.prepare(
            plan,
            confirmation={},
            timestamp="2026-08-23T01:00:00+00:00",
        )

    assert canary.snapshot(plan)["status"] == "BLOCKED"


def test_canary_rejects_old_market_fact_even_if_provider_labels_it_fresh(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    stale = {
        "instrument_id": "BTC-USD-PERP",
        "contract_multiplier": "1",
        "price": "60000",
        "freshness": "fresh",
        "observed_at": "2020-01-01T00:00:00+00:00",
        "max_age_seconds": "120",
        "transport_state": "external_testnet",
        "source": "hyperliquid.external_testnet",
        "mapping_revision": "hyperliquid-testnet-runtime-v1",
    }
    stale["fact_digest"] = market_fact_digest(stale)
    broker.preflight_overrides = {"market_fact": stale}
    plan = _plan()
    canary = TestnetCanary(tmp_path / "outputs", broker)

    with pytest.raises(TestnetCanaryError, match="stale"):
        canary.prepare(plan, confirmation={}, timestamp="2026-08-23T01:00:00+00:00")
    assert canary.snapshot(plan)["status"] == "BLOCKED"


def test_canary_persists_expiry_and_confirmation_blockers(tmp_path: Path) -> None:
    expired_broker = FakeCanaryBroker()
    expired = TestnetCanary(tmp_path / "expired", expired_broker)
    expired_values = _plan_values()
    expired_values["expires_at"] = "2026-08-23T00:59:00+00:00"
    expired_values["plan_digest"] = canary_plan_digest(expired_values)

    with pytest.raises(TestnetCanaryError, match="expired"):
        expired.prepare(
            expired_values,
            confirmation={},
            timestamp="2026-08-23T01:00:00+00:00",
        )
    assert expired.snapshot()["status"] == "BLOCKED"
    assert expired_broker.calls == []

    confirmation_broker = FakeCanaryBroker()
    plan = _plan()
    confirmation = TestnetCanary(
        tmp_path / "confirmation",
        confirmation_broker,
        approved_market_sources={"fake.external_testnet"},
    )
    with pytest.raises(TestnetCanaryError, match="confirmation"):
        confirmation.prepare(
            plan,
            confirmation={"execution_authorized": False},
            timestamp="2026-08-23T01:00:00+00:00",
        )
    blocked = confirmation.snapshot(plan)
    assert blocked["status"] == "BLOCKED"
    assert [name for name, _ in confirmation_broker.calls] == ["preflight"]

    forged = TestnetCanary(
        tmp_path / "forged",
        FakeCanaryBroker(),
        approved_market_sources={"fake.external_testnet"},
    )
    forged_plan = _plan(canary_id="forged-canary")
    with pytest.raises(TestnetCanaryError, match="durable confirmation"):
        forged.prepare(
            forged_plan,
            confirmation=_confirmation(forged_plan),
            timestamp="2026-08-23T01:00:00+00:00",
        )
    assert forged.snapshot(forged_plan)["status"] == "BLOCKED"


def test_canary_redacts_unsafe_receipt_fields(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    plan = _plan()
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")

    class UnsafeReceipt(FakeReceipt):
        reason = "native://signed-payload"
        broker_order_id = "{\"native\":\"payload\"}"

    broker.submit = lambda request: UnsafeReceipt(
        order_id="canary-order-unsafe",
        client_order_id=request.idempotency_key,
        broker_order_id="{\"native\":\"payload\"}",
        state="resting",
        original_quantity=request.quantity,
        remaining_quantity=request.quantity,
    )
    with pytest.raises(TestnetCanaryError, match="broker_order_id"):
        canary.submit_entry(
            plan,
            confirmation=confirmation,
            timestamp="2026-08-23T01:01:00+00:00",
        )
    encoded = json.dumps(canary.snapshot(plan), sort_keys=True)
    assert "native" not in encoded.lower()
    assert "signed-payload" not in encoded


def test_canary_rejects_receipt_identity_drift(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    plan = _plan(canary_id="identity-drift")
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")
    original_query = broker.query

    def drifted_query(order_id: str):
        receipt = original_query(order_id)
        receipt.release_sha = "c" * 40
        return receipt

    broker.query = drifted_query
    with pytest.raises(TestnetCanaryError, match="release identity"):
        canary.submit_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:01:00+00:00")
    assert canary.snapshot(plan)["status"] == "BLOCKED"


def test_canary_never_labels_partial_cancel_as_no_fill(tmp_path: Path) -> None:
    broker = FakeCanaryBroker()
    plan = _plan(canary_id="partial-cancel")
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")
    canary.submit_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:01:00+00:00")
    original_query = broker.query
    canceled = {"value": False}

    def partial_query(order_id: str):
        receipt = original_query(order_id)
        if canceled["value"]:
            receipt.state = "canceled"
            receipt.filled_quantity = Decimal("0.0005")
            receipt.remaining_quantity = Decimal("0")
        return receipt

    broker.query = partial_query
    original_cancel = broker.cancel

    def mark_cancel(order_id: str):
        canceled["value"] = True
        return original_cancel(order_id)

    broker.cancel = mark_cancel
    with pytest.raises(TestnetCanaryError, match="partial fill"):
        canary.cancel_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:02:00+00:00")
    assert canary.snapshot(plan)["status"] == "BLOCKED"
    assert canary.snapshot(plan)["status"] != "NO_FILL_RECONCILED"


def test_canary_persists_submit_intent_and_never_resubmits_after_journal_failure(tmp_path: Path, monkeypatch) -> None:
    broker = FakeCanaryBroker()
    plan = _plan(canary_id="crash-canary")
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")

    original_save = canary._save
    save_calls = {"count": 0}

    def fail_after_reservation(state):
        save_calls["count"] += 1
        if save_calls["count"] == 2:
            raise OSError("simulated journal crash")
        original_save(state)

    monkeypatch.setattr(canary, "_save", fail_after_reservation)
    with pytest.raises(OSError, match="journal crash"):
        canary.submit_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:01:00+00:00")
    monkeypatch.setattr(canary, "_save", original_save)

    recovered = canary.snapshot(plan)
    assert recovered["status"] == "ENTRY_SUBMIT_INTENT_RESERVED"
    submit_count = len([name for name, _ in broker.calls if name == "submit"])
    with pytest.raises(TestnetCanaryError, match="outcome unknown"):
        canary.submit_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:02:00+00:00")
    assert len([name for name, _ in broker.calls if name == "submit"]) == submit_count
    assert canary.snapshot(plan)["status"] == "BLOCKED"
