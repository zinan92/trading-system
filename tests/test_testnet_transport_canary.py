from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from services.standard_broker_testnet_canary import (
    TestnetCanaryOrderRequest,
    canary_plan_digest,
)
from services.standard_broker_testnet_canary_facts import (
    CanaryAccountFact,
    CanaryFactBundle,
    CanaryFeeFact,
    CanaryFillFact,
    CanaryPositionFact,
    CanaryReconciliationFact,
    canary_fact_digest,
)
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_transport_canary import (
    TRANSPORT_CANARY_SCHEMA,
    TestnetTransportCanary,
    TestnetTransportCanaryError,
)


ACCOUNT = "sha256:" + "a" * 64
RELEASE = "b" * 40
CAPABILITY = "hyperliquid-testnet-runtime-v1"
NOW = "2026-08-26T01:00:00+00:00"


@dataclass
class Receipt:
    order_id: str
    client_order_id: str
    broker_order_id: str
    state: str
    original_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal
    instrument_id: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    limit_price: Decimal
    broker_id: str = "hyperliquid"
    environment: str = "testnet"
    account_fingerprint: str = ACCOUNT
    lifecycle_id: str = "runtime-1"
    release_sha: str = RELEASE

    class Provenance:
        transport_state = "external_testnet"
        mapping_revision = CAPABILITY
        source = "fake.external_testnet"
        execution_scope = "hypercore:default"

    provenance = Provenance()


class FilledBroker:
    def __init__(self, *, unknown_query: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self.unknown_query = unknown_query
        self._active: Receipt | None = None

    def preflight(self) -> dict:
        return {
            "canary_ready": True,
            "host_ready": True,
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "transport_state": "external_testnet",
            "account_fingerprint": ACCOUNT,
            "runtime_id": "runtime-1",
            "release_sha": RELEASE,
            "capability_revision": CAPABILITY,
            "network_io": True,
            "real_money_eligible": False,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
            "protection_ready": True,
            "account_read_ready": True,
            "order_execution_ready": True,
        }

    def market_fact(self, *, instrument_id: str, now) -> dict:
        value = {
            "instrument_id": instrument_id,
            "contract_multiplier": "1",
            "price": "60000",
            "freshness": "fresh",
            "observed_at": now.isoformat(),
            "max_age_seconds": "120",
            "source": "fake.external_testnet",
            "transport_state": "external_testnet",
            "mapping_revision": CAPABILITY,
        }
        from services.standard_broker_testnet_canary import market_fact_digest

        value["fact_digest"] = market_fact_digest(value)
        return value

    def submit(self, request: TestnetCanaryOrderRequest) -> Receipt:
        self.calls.append(("submit", request))
        receipt = Receipt(
            order_id=request.order_id,
            client_order_id=request.idempotency_key,
            broker_order_id=f"broker-{request.order_id}",
            state="filled",
            original_quantity=request.quantity,
            filled_quantity=request.quantity,
            remaining_quantity=Decimal("0"),
            average_fill_price=request.limit_price,
            instrument_id=request.instrument_id,
            side=request.side,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            quantity=request.quantity,
            limit_price=request.limit_price,
        )
        self._active = receipt
        return receipt

    def query(self, order_id: str) -> Receipt:
        self.calls.append(("query", order_id))
        if self.unknown_query:
            return replace(self._active or _receipt(order_id), state="unknown")
        return self._active or _receipt(order_id)

    def query_by_idempotency_key(self, idempotency_key: str) -> Receipt:
        self.calls.append(("query_by_idempotency_key", idempotency_key))
        return self._active or _receipt(idempotency_key.split(":")[0])

    def replace(self, order_id: str, request: TestnetCanaryOrderRequest) -> Receipt:
        self.calls.append(("replace", request))
        return self._active or _receipt(order_id)

    def cancel(self, order_id: str) -> Receipt:
        self.calls.append(("cancel", order_id))
        return replace(self._active or _receipt(order_id), state="canceled")


def _receipt(order_id: str) -> Receipt:
    return Receipt(
        order_id=order_id,
        client_order_id=f"client-{order_id}",
        broker_order_id=f"broker-{order_id}",
        state="filled",
        original_quantity=Decimal("0.001"),
        filled_quantity=Decimal("0.001"),
        remaining_quantity=Decimal("0"),
        average_fill_price=Decimal("60000"),
        instrument_id="BTC-USD-PERP",
        side="buy",
        order_type="limit",
        time_in_force="gtc",
        quantity=Decimal("0.001"),
        limit_price=Decimal("60000"),
    )


class FilledFacts:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.calls: list[str] = []

    def read_facts(self, *, order_id: str, instrument_id: str) -> CanaryFactBundle:
        self.calls.append(order_id)
        final = len(self.calls) > 1
        timestamp = "2026-08-26T00:59:00+00:00" if not final else "2026-08-26T00:59:30+00:00"
        cursor = "cursor-final" if final else "cursor-entry"
        side = "sell" if final else "buy"
        client_order_id = (
            f"{self.plan.canary_id}:entry:60000"
            if not final
            else f"{self.plan.canary_id}:close:0.001:59900"
        )
        fill = CanaryFillFact(
            fill_id=f"fill-{order_id}",
            order_id=order_id,
            broker_order_id=f"broker-{order_id}",
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            side=side,
            price=Decimal("59900" if final else "60000"),
            quantity=self.plan.quantity,
            occurred_at=timestamp,
            account_fingerprint=ACCOUNT,
            runtime_id="runtime-1",
            release_sha=RELEASE,
            capability_revision=CAPABILITY,
            cursor=cursor,
        )
        fill = replace(fill, fact_digest=canary_fact_digest(fill))
        fee = CanaryFeeFact(
            fee_id=f"fee-{order_id}",
            fill_id=fill.fill_id,
            amount_usd=Decimal("0.2"),
            currency="USD",
            occurred_at=timestamp,
            account_fingerprint=ACCOUNT,
            runtime_id="runtime-1",
            release_sha=RELEASE,
            capability_revision=CAPABILITY,
            cursor=cursor,
            fee_source="actual_fill",
            fee_state="actual",
        )
        fee = replace(fee, fact_digest=canary_fact_digest(fee))
        account = CanaryAccountFact(
            account_fingerprint=ACCOUNT,
            equity_usd=Decimal("999.8"),
            cursor=cursor,
            observed_at=timestamp,
            runtime_id="runtime-1",
            release_sha=RELEASE,
            capability_revision=CAPABILITY,
        )
        account = replace(account, fact_digest=canary_fact_digest(account))
        positions = () if final else (
            CanaryPositionFact(
                instrument_id=instrument_id,
                signed_quantity=self.plan.quantity,
                cursor=cursor,
                observed_at=timestamp,
                account_fingerprint=ACCOUNT,
                runtime_id="runtime-1",
                release_sha=RELEASE,
                capability_revision=CAPABILITY,
                fact_digest="",
            ),
        )
        positions = tuple(replace(item, fact_digest=canary_fact_digest(item)) for item in positions)
        reconciliation = CanaryReconciliationFact(
            coherent=True,
            freshness="fresh",
            cursor=cursor,
            open_order_ids=(),
            signed_position_quantity=Decimal("0") if final else self.plan.quantity,
            observed_at=timestamp,
            account_fingerprint=ACCOUNT,
            runtime_id="runtime-1",
            release_sha=RELEASE,
            capability_revision=CAPABILITY,
            evidence_digest="sha256:" + ("f" if final else "e") * 64,
        )
        reconciliation = replace(reconciliation, fact_digest=canary_fact_digest(reconciliation))
        return CanaryFactBundle(
            fills=(fill,),
            fees=(fee,),
            account=account,
            positions=positions,
            open_orders=(),
            reconciliation=reconciliation,
        )


class FakeProtection:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[dict] = []

    def preflight(self) -> dict:
        return {
            "ready": self.ready,
            "environment": "testnet",
            "real_money_eligible": False,
            "position_coverage": self.ready,
            "reduce_only": self.ready,
            "capability_revision": CAPABILITY,
        }

    def ensure_position_coverage(self, **request) -> dict:
        self.calls.append(dict(request))
        if not self.ready:
            return {"status": "blocked", "covered": False, "reason": "coverage_gap"}
        return {
            "status": "active",
            "covered": True,
            "covered_quantity": str(request["filled_quantity"]),
            "reduce_only": True,
            "instrument_id": request["instrument_id"],
            "parent_order_id": request["parent_order_id"],
            "capability_revision": CAPABILITY,
        }


def _plan(**overrides) -> dict:
    values = {
        "canary_id": "btc-transport-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-default",
        "account_fingerprint": ACCOUNT,
        "runtime_id": "runtime-1",
        "release_sha": RELEASE,
        "capability_revision": CAPABILITY,
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
        "account_equity": "1000",
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
    values.update(overrides)
    values["plan_digest"] = canary_plan_digest(values)
    return values


def _confirmation(plan: dict) -> dict:
    return {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan["canary_id"],
        "plan_digest": plan["plan_digest"],
        "operator_id": "park",
        "confirmation_id": "confirmation-1",
        "expires_at": plan["expires_at"],
    }


def test_transport_canary_completes_canonical_fill_protection_and_flat_reconcile(tmp_path: Path) -> None:
    from services.park_confirmation import ParkConfirmationLedger
    from services.standard_broker_testnet_canary import TestnetCanaryPlan

    raw_plan = _plan()
    plan = TestnetCanaryPlan.from_mapping(raw_plan)
    ledger = ParkConfirmationLedger(tmp_path / "outputs", park_user_id="park")
    ledger.create_proposal(
        proposal_id="proposal-1",
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id="proposal-1",
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"},
        now=1787446800,
    )
    confirmation = {**_confirmation(raw_plan), "proposal_id": "proposal-1", "receipt_digest": decision["receipt_digest"], "park_user_id": "park"}
    broker = FilledBroker()
    protection = FakeProtection()
    runner = TestnetTransportCanary(
        tmp_path / "outputs",
        broker,
        FilledFacts(plan),
        protection,
        confirmation_ledger=ledger,
    )

    result = runner.run(plan, confirmation=confirmation, timestamp="2026-08-26T01:00:00+00:00")

    assert result["schema_version"] == TRANSPORT_CANARY_SCHEMA
    assert result["status"] == "FLAT_RECONCILED"
    assert result["protection"]["covered"] is True
    assert result["protection"]["covered_quantity"] == "0.001"
    assert result["canary"]["status"] == "FLAT_RECONCILED"
    assert [name for name, _ in broker.calls].count("submit") == 2
    assert len(protection.calls) == 1


def test_transport_canary_blocks_protection_gap_before_submit(tmp_path: Path) -> None:
    from services.park_confirmation import ParkConfirmationLedger
    from services.standard_broker_testnet_canary import TestnetCanaryPlan

    raw_plan = _plan(canary_id="gap")
    plan = TestnetCanaryPlan.from_mapping(raw_plan)
    ledger = ParkConfirmationLedger(tmp_path / "outputs", park_user_id="park")
    ledger.create_proposal(
        proposal_id="proposal-gap",
        strategy_session_id="session-gap",
        strategy_revision_id="revision-gap",
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id="proposal-gap",
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={"strategy_session_id": "session-gap", "strategy_revision_id": "revision-gap"},
        now=1787446800,
    )
    confirmation = {**_confirmation(raw_plan), "proposal_id": "proposal-gap", "receipt_digest": decision["receipt_digest"], "park_user_id": "park"}
    broker = FilledBroker()
    runner = TestnetTransportCanary(
        tmp_path / "outputs",
        broker,
        FilledFacts(plan),
        FakeProtection(ready=False),
        confirmation_ledger=ledger,
    )

    with pytest.raises(TestnetTransportCanaryError, match="protection"):
        runner.run(plan, confirmation=confirmation, timestamp="2026-08-26T01:00:00+00:00")

    assert broker.calls == []
    assert runner.snapshot()["status"] == "BLOCKED"


def test_coordinator_canary_requires_selected_btc_slice_and_persists_result(tmp_path: Path) -> None:
    from tests.test_testnet_candidate_selection import _candidate, _policy, _snapshot

    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs", clock=lambda: NOW)
    activation = {
        "strategy_family": "dca",
        "strategy_session_id": "session-btc-dca",
        "strategy_revision_id": "revision-btc-dca-1",
        "plan_digest": "sha256:" + "d" * 64,
        "account_fingerprint": ACCOUNT,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-1",
        "release_sha": RELEASE,
        "capability_revision": CAPABILITY,
    }
    coordinator.activate(activation, command_id="activate-1")
    coordinator.command(
        "select_candidate",
        {"candidates": [_candidate("BTC", rank=1)], "snapshot": _snapshot(), "policy": _policy()},
        command_id="select-1",
    )
    raw_plan = _plan(canary_id="coordinator-canary")
    plan = __import__("services.standard_broker_testnet_canary", fromlist=["TestnetCanaryPlan"]).TestnetCanaryPlan.from_mapping(raw_plan)
    ledger = __import__("services.park_confirmation", fromlist=["ParkConfirmationLedger"]).ParkConfirmationLedger(tmp_path / "outputs", park_user_id="park")
    ledger.create_proposal(
        proposal_id="proposal-coordinator",
        strategy_session_id="session-coordinator",
        strategy_revision_id="revision-coordinator",
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id="proposal-coordinator",
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={"strategy_session_id": "session-coordinator", "strategy_revision_id": "revision-coordinator"},
        now=1787446800,
    )
    confirmation = {**_confirmation(raw_plan), "proposal_id": "proposal-coordinator", "receipt_digest": decision["receipt_digest"], "park_user_id": "park"}
    result = coordinator.run_transport_canary(
        plan,
        confirmation=confirmation,
        broker=FilledBroker(),
        facts=FilledFacts(plan),
        protection=FakeProtection(),
        confirmation_ledger=ledger,
        timestamp=NOW,
    )

    assert result["event"] == "transport_canary_completed"
    assert result["status"] == "canary_completed"
    assert result["execution_enabled"] is False
    assert result["canary"]["status"] == "FLAT_RECONCILED"
    assert coordinator.status()["selected_asset"] == "BTC"
