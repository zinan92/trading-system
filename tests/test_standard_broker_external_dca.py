from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_external_dca import (
    CanonicalDcaProvenance,
    ExternalDcaError,
    ExternalDcaLifecycle,
    ExternalDcaPlan,
    external_dca_plan_digest,
)
from services.standard_broker_testnet_canary import TestnetCanaryOrderRequest
from services.standard_broker_testnet_canary_facts import (
    CanaryAccountFact,
    CanaryFactBundle,
    CanaryFeeFact,
    CanaryFillFact,
    CanaryPositionFact,
    CanaryReconciliationFact,
    canary_fact_digest,
)
from standard_broker.protection import ProtectionLifecycleState


ACCOUNT_FINGERPRINT = "sha256:" + "a" * 64
RELEASE_SHA = "b" * 40
CAPABILITY = "hyperliquid-testnet-position-protection-runtime-v1"
NOW = "2026-08-23T01:00:00+00:00"


def _plan_values() -> dict[str, object]:
    values: dict[str, object] = {
        "plan_id": "dca-external-1",
        "plan_version": 1,
        "cycle_id": "cycle-1",
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-position-protection",
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "runtime_id": "dca-runtime-1",
        "release_sha": RELEASE_SHA,
        "capability_revision": CAPABILITY,
        "instrument_id": "BTC-USD-PERP",
        "direction": "long",
        "entry_levels": ["60000", "59000"],
        "entry_quantities": ["0.001", "0.001"],
        "contract_multiplier": "1",
        "target_price": "61000",
        "stop_price": "58000",
        "close_price": "60500",
        "time_in_force": "gtc",
        "quantity_step": "0.001",
        "price_tick": "1",
        "max_slippage": "10",
        "max_notional": "200",
        "max_leverage": "2",
        "account_equity": "100",
        "max_open_orders": 2,
        "max_open_positions": 1,
        "fee_budget_usd": "1",
        "max_loss_usd": "50",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    values["plan_digest"] = external_dca_plan_digest(values)
    return values


def _plan(**overrides: object) -> ExternalDcaPlan:
    values = {**_plan_values(), **overrides}
    if "plan_digest" not in overrides:
        values["plan_digest"] = external_dca_plan_digest(values)
    return ExternalDcaPlan.from_mapping(values, allow_expired=True)


def _confirmation(
    tmp_path: Path,
    plan: ExternalDcaPlan,
    *,
    confirmation_expires_at: str | None = None,
) -> tuple[Path, dict[str, object], Path]:
    output_root = tmp_path / "outputs"
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id="proposal-dca-1",
        strategy_session_id=plan.strategy_session_id,
        strategy_revision_id=plan.strategy_revision_id,
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={
            "strategy_session_id": plan.strategy_session_id,
            "strategy_revision_id": plan.strategy_revision_id,
        },
        now=1787446800,
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "expires_at": confirmation_expires_at or plan.expires_at,
    }
    path = tmp_path / "confirmation.json"
    path.write_text(json.dumps(confirmation), encoding="utf-8")
    return path, confirmation, output_root


@dataclass
class _Receipt:
    order_id: str
    state: str
    client_order_id: str
    broker_order_id: str


class _Orders:
    def __init__(self, *, submit_state: str = "filled", query_state: str | None = None) -> None:
        self.requests: list[TestnetCanaryOrderRequest] = []
        self.receipts: dict[str, _Receipt] = {}
        self.submit_state = submit_state
        self.query_state = query_state if query_state is not None else submit_state
        self.cancelled: set[str] = set()

    def preflight(self):
        return {
            "canary_ready": True,
            "environment": "testnet",
            "transport_state": "external_testnet",
            "transport_profile": "hyperliquid-testnet-position-protection",
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "runtime_id": "dca-runtime-1",
            "release_sha": RELEASE_SHA,
            "capability_revision": CAPABILITY,
            "network_io": True,
            "real_money_eligible": False,
        }

    def market_fact(self, *, instrument_id: str, now: datetime):
        value = {
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
        from services.standard_broker_testnet_canary import market_fact_digest
        value["fact_digest"] = market_fact_digest(value)
        return value

    def submit(self, request: TestnetCanaryOrderRequest):
        self.requests.append(request)
        receipt = _Receipt(
            order_id=request.order_id,
            state=self.submit_state,
            client_order_id=request.idempotency_key,
            broker_order_id=f"broker:{request.order_id}",
        )
        self.receipts[request.order_id] = receipt
        return receipt

    def query(self, order_id: str):
        receipt = self.receipts[order_id]
        receipt.state = "canceled" if order_id in self.cancelled else self.query_state
        return receipt

    def query_by_idempotency_key(self, idempotency_key: str):
        for receipt in self.receipts.values():
            if receipt.client_order_id == idempotency_key:
                receipt.state = self.query_state
                return receipt
        raise KeyError(idempotency_key)

    def recover_client_order(self, request: TestnetCanaryOrderRequest, *, client_order_id: str, state: str):
        receipt = self.receipts[request.order_id]
        receipt.client_order_id = client_order_id
        receipt.state = state
        return receipt

    def cancel(self, order_id: str):
        receipt = self.receipts[order_id]
        self.cancelled.add(order_id)
        receipt.state = "canceled"
        return receipt

    def open_orders(self, instrument_id: str):
        del instrument_id
        return ()


class _Protection:
    def __init__(self, *, state: ProtectionLifecycleState = ProtectionLifecycleState.ACTIVE) -> None:
        self.state = state
        self.calls: list[str] = []
        self.protection_capabilities = SimpleNamespace(
            profile_id="hyperliquid-testnet-position-protection-v1"
        )
        self.runtime_session = SimpleNamespace(
            account=SimpleNamespace(address="0x" + "11" * 20),
            lifecycle_id="dca-runtime-1",
            capability_revision=CAPABILITY,
        )

    def _receipt(self, operation: str, group):
        self.calls.append(operation)
        observation = SimpleNamespace(
            state=self.state,
            covered_quantity=group.quantity if self.state is ProtectionLifecycleState.ACTIVE else Decimal("0"),
            observation_digest="sha256:" + "c" * 64,
        )
        return SimpleNamespace(
            operation=operation,
            accepted=self.state is not ProtectionLifecycleState.UNKNOWN,
            observation=observation,
        )

    def submit(self, group):
        return self._receipt("submit", group)

    def reconcile(self, group):
        return self._receipt("query", group)

    def replace(self, group):
        return self._receipt("replace", group)

    def cancel(self, group):
        previous = self.state
        self.state = ProtectionLifecycleState.CANCELED
        try:
            return self._receipt("cancel", group)
        finally:
            self.state = previous


def _bundle(
    plan: ExternalDcaPlan,
    *,
    order_id: str,
    position: Decimal,
    side: str,
    price: Decimal,
    fill_quantity: Decimal | None = None,
    client_order_id: str | None = None,
) -> CanaryFactBundle:
    occurred = "2026-08-23T01:00:30+00:00"
    identity = {
        "account_fingerprint": plan.account_fingerprint,
        "runtime_id": plan.runtime_id,
        "release_sha": plan.release_sha,
        "capability_revision": plan.capability_revision,
    }
    fill = CanaryFillFact(
        fill_id=f"fill:{order_id}",
        order_id=order_id,
        broker_order_id=f"broker:{order_id}",
        client_order_id=client_order_id
        or (f"{plan.plan_id}:entry:0" if "flatten" not in order_id else f"{plan.plan_id}:flatten:{position}"),
        instrument_id=plan.instrument_id,
        side=side,
        price=price,
        quantity=fill_quantity if fill_quantity is not None else (abs(position) if position else plan.entry_quantities[0]),
        occurred_at=occurred,
        **identity,
        cursor=f"cursor:{order_id}",
    )
    fill = replace(fill, fact_digest=canary_fact_digest(fill))
    fee = CanaryFeeFact(
        fee_id=f"fee:{order_id}",
        fill_id=fill.fill_id,
        amount_usd=Decimal("0.1"),
        currency="USDC",
        occurred_at=occurred,
        **identity,
        cursor=fill.cursor,
        fee_source="actual_fill",
        fee_state="actual",
    )
    fee = replace(fee, fact_digest=canary_fact_digest(fee))
    account = CanaryAccountFact(
        equity_usd=Decimal("99"),
        cursor=fill.cursor,
        observed_at=occurred,
        **identity,
    )
    account = replace(account, fact_digest=canary_fact_digest(account))
    positions = () if position == 0 else (
        CanaryPositionFact(
            instrument_id=plan.instrument_id,
            signed_quantity=position,
            cursor=fill.cursor,
            observed_at=occurred,
            **identity,
        ),
    )
    positions = tuple(replace(item, fact_digest=canary_fact_digest(item)) for item in positions)
    reconciliation = CanaryReconciliationFact(
        coherent=True,
        freshness="fresh",
        cursor=fill.cursor,
        open_order_ids=(),
        signed_position_quantity=position,
        observed_at=occurred,
        canonical_schema="ExternalReconciliationSnapshot",
        evidence_digest="sha256:" + "e" * 64,
        **identity,
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


class _Facts:
    def __init__(self, plan: ExternalDcaPlan) -> None:
        self.plan = plan

    def read_facts(
        self,
        *,
        order_id: str,
        instrument_id: str,
        now: datetime,
        client_order_id: str | None = None,
    ) -> CanaryFactBundle:
        del instrument_id, now
        if "flatten" in order_id:
            return _bundle(
                self.plan,
                order_id=order_id,
                position=Decimal("0"),
                side="sell",
                price=Decimal("60500"),
                client_order_id=client_order_id,
            )
        position = self.plan.entry_quantities[0] if order_id.endswith(":0") else sum(self.plan.entry_quantities)
        price = self.plan.entry_levels[0] if order_id.endswith(":0") else self.plan.entry_levels[1]
        return _bundle(
            self.plan,
            order_id=order_id,
            position=position,
            side="buy",
            price=price,
            fill_quantity=self.plan.entry_quantities[0],
            client_order_id=client_order_id,
        )

    def read_account_state(self, *, instrument_id: str, now: datetime) -> CanaryFactBundle:
        del instrument_id, now
        return replace(
            _bundle(
                self.plan,
                order_id=f"{self.plan.plan_id}:clean-state",
                position=Decimal("0"),
                side="buy",
                price=self.plan.entry_levels[0],
            ),
            fills=(),
            fees=(),
        )


def _lifecycle(tmp_path: Path, *, protection_state: ProtectionLifecycleState = ProtectionLifecycleState.ACTIVE):
    plan = _plan()
    _path, confirmation, output_root = _confirmation(tmp_path, plan)
    orders = _Orders()
    protection = _Protection(state=protection_state)
    lifecycle = ExternalDcaLifecycle(
        output_root,
        orders,
        _Facts(plan),
        protection,
    )
    return plan, confirmation, lifecycle, orders, protection


def _provenance(plan: ExternalDcaPlan) -> CanonicalDcaProvenance:
    return CanonicalDcaProvenance.from_mapping(
        {
            "source_strategy_plan_id": "strategy-plan-paper-dca-1",
            "source_strategy_plan_digest": "sha256:" + "d" * 64,
            "canonical_semantics": {
                "direction": plan.direction,
                "notional_per_addition": "100",
                "max_additions": len(plan.entry_levels),
                "loop_enabled": False,
                "aggregate_take_profit": "fixed_price",
            },
            "source_market": {
                "provider": "nautilus-hyperliquid.testnet",
                "symbol": plan.instrument_id,
            },
            "execution_market_source": {
                "source_id": "nautilus-hyperliquid.testnet",
                "broker_id": plan.broker_id,
                "environment": plan.environment,
                "instrument_id": plan.instrument_id,
                "execution_venue": True,
            },
        }
    )


def test_external_dca_requires_opt_in_profile_and_exact_plan_digest() -> None:
    values = _plan_values()
    values["profile_id"] = "hyperliquid-testnet-default"
    values["plan_digest"] = external_dca_plan_digest(values)
    with pytest.raises(ExternalDcaError, match="protection_profile"):
        ExternalDcaPlan.from_mapping(values)


def test_external_dca_prepare_facts_protection_then_next_entry(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, protection = _lifecycle(tmp_path)

    prepared = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert prepared["status"] == "ENTRY_FILLED_PENDING_FACTS"
    first = lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[0].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert first["status"] == "PROTECTION_ACTIVE"
    second = lifecycle.submit_next_entry(plan, confirmation=confirmation, timestamp=NOW)
    assert second["status"] == "ENTRY_FILLED_PENDING_FACTS"
    final = lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[1].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert final["status"] == "PROTECTION_ACTIVE"
    assert protection.calls == ["submit", "query", "replace", "query"]
    assert len(orders.requests) == 2
    encoded = json.dumps(lifecycle.snapshot(), sort_keys=True).lower()
    assert all(token not in encoded for token in ("cloid", "tid", "hash", "coin", '"px"', '"sz"', '"oid"'))


def test_external_dca_canonical_start_persists_source_and_normalized_facts(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    provenance = _provenance(plan)

    prepared = lifecycle.prepare(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        canonical_provenance=provenance,
        require_canonical=True,
    )
    state = lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(
            order_id=orders.requests[0].order_id,
            instrument_id=plan.instrument_id,
            now=datetime.now(UTC),
        ),
        confirmation=confirmation,
        timestamp=NOW,
    )

    assert prepared["source_strategy_plan_id"] == provenance.source_strategy_plan_id
    assert prepared["source_strategy_plan_digest"] == provenance.source_strategy_plan_digest
    assert prepared["execution_market_source"]["instrument_id"] == plan.instrument_id
    assert prepared["clean_state_facts"]["reconciliation"]["signed_position_quantity"] == "0"
    assert prepared["clean_state_facts_digest"].startswith("sha256:")
    assert state["entry_facts"]["account"]["account_fingerprint"] == plan.account_fingerprint
    assert state["entry_facts"]["reconciliation"]["freshness"] == "fresh"
    assert state["entry_facts"]["reconciliation"]["cursor"] == f"cursor:{orders.requests[0].order_id}"
    assert state["entry_facts"]["provenance"]["runtime_id"] == plan.runtime_id
    assert state["entry_facts_digest"].startswith("sha256:")


def test_external_dca_canonical_start_requires_source_provenance(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, _orders, _protection = _lifecycle(tmp_path)
    state = lifecycle.prepare(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        require_canonical=True,
    )
    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "canonical_strategy_plan_required"


def test_external_dca_expired_plan_persists_identity_blocker_without_submit(tmp_path: Path) -> None:
    plan = _plan(expires_at="2020-01-01T00:00:00+00:00")
    _path, confirmation, output_root = _confirmation(
        tmp_path,
        plan,
        confirmation_expires_at="2099-01-01T00:00:00+00:00",
    )
    orders = _Orders()
    lifecycle = ExternalDcaLifecycle(output_root, orders, _Facts(plan), _Protection())

    state = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)

    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "plan_expired"
    assert state["plan_id"] == plan.plan_id
    assert state["plan_digest"] == plan.plan_digest
    assert orders.requests == []


def test_external_dca_expired_confirmation_persists_identity_blocker_without_submit(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    expired_confirmation = {**confirmation, "expires_at": "2020-01-01T00:00:00+00:00"}

    state = lifecycle.prepare(plan, confirmation=expired_confirmation, timestamp=NOW)

    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "confirmation_expired"
    assert state["plan_id"] == plan.plan_id
    assert state["plan_digest"] == plan.plan_digest
    assert orders.requests == []


def test_external_dca_expired_resting_entry_can_be_cancelled_and_reconciled(tmp_path: Path) -> None:
    plan = _plan(expires_at="2026-08-23T02:00:00+00:00")
    _path, confirmation, output_root = _confirmation(
        tmp_path,
        plan,
        confirmation_expires_at="2099-01-01T00:00:00+00:00",
    )
    orders = _Orders(query_state="resting")
    lifecycle = ExternalDcaLifecycle(output_root, orders, _Facts(plan), _Protection())
    prepared = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert prepared["status"] == "WAITING_ENTRY"

    state = lifecycle.reconcile_expired_entry(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-24T01:00:00+00:00",
    )

    assert state["status"] == "EXPIRED_RECONCILED"
    assert state["blocker"] == "plan_expired"
    assert state["next_action"] == "record_expired_entry_reconciliation"
    assert orders.cancelled == {orders.requests[0].order_id}


def test_external_dca_expired_fill_blocks_then_allows_explicit_unprotected_flatten(tmp_path: Path) -> None:
    plan = _plan(expires_at="2026-08-23T01:01:00+00:00")
    _path, confirmation, output_root = _confirmation(
        tmp_path,
        plan,
        confirmation_expires_at="2099-01-01T00:00:00+00:00",
    )
    orders = _Orders(query_state="filled")
    protection = _Protection()
    lifecycle = ExternalDcaLifecycle(output_root, orders, _Facts(plan), protection)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)

    expired = lifecycle.reconcile_expired_entry(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-23T01:02:00+00:00",
    )
    assert expired["status"] == "EXPIRED_POSITION_BLOCKED"
    assert expired["next_action"] == "attended_flatten_expired_position"

    flat = lifecycle.flatten(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-23T01:02:00+00:00",
    )
    assert flat["status"] == "FLAT_RECONCILED"
    assert flat["protection"]["status"] == "not_present"
    assert protection.calls == []


def test_external_dca_fresh_process_recovery_never_resubmits_ambiguous_intent(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, protection = _lifecycle(tmp_path)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    state = lifecycle.snapshot()
    state["status"] = "ENTRY_SUBMIT_INTENT_RESERVED"
    state["entry_order_id"] = orders.requests[0].order_id
    state["entry_order_ids"] = [orders.requests[0].order_id]
    lifecycle._save(state)

    recovered_orders = _Orders(query_state="resting")
    recovered_orders.receipts[orders.requests[0].order_id] = _Receipt(
        order_id=orders.requests[0].order_id,
        state="resting",
        client_order_id=orders.requests[0].idempotency_key,
        broker_order_id=f"broker:{orders.requests[0].order_id}",
    )
    recovered = ExternalDcaLifecycle(
        lifecycle.output_root,
        recovered_orders,
        _Facts(plan),
        protection,
    )

    state = recovered.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert state["status"] == "RECOVERY_REQUIRED"
    assert recovered_orders.requests == []
    reconciled = recovered.reconcile_entry(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert reconciled["status"] == "WAITING_ENTRY"
    assert recovered_orders.requests == []


def test_external_dca_clean_state_gate_blocks_existing_position_before_submit(tmp_path: Path) -> None:
    plan = _plan()
    _path, confirmation, output_root = _confirmation(tmp_path, plan)
    protection = _Protection()

    class _NonFlatFacts(_Facts):
        def read_account_state(self, *, instrument_id: str, now: datetime) -> CanaryFactBundle:
            del instrument_id, now
            return _bundle(
                self.plan,
                order_id=f"{self.plan.plan_id}:clean-state",
                position=self.plan.entry_quantities[0],
                side="buy",
                price=self.plan.entry_levels[0],
            )

    orders = _Orders()
    lifecycle = ExternalDcaLifecycle(output_root, orders, _NonFlatFacts(plan), protection)
    state = lifecycle.prepare(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        canonical_provenance=_provenance(plan),
        require_canonical=True,
    )
    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "clean_state_not_flat"
    assert orders.requests == []


def test_external_dca_adopts_existing_position_in_isolated_journal_before_flatten(tmp_path: Path) -> None:
    plan, confirmation, _unused_lifecycle, _unused_orders, protection = _lifecycle(tmp_path)
    _path, confirmation, output_root = _confirmation(tmp_path / "adopt", plan)

    class _ExistingFacts(_Facts):
        def read_account_state(self, *, instrument_id: str, now: datetime) -> CanaryFactBundle:
            del instrument_id, now
            return _bundle(
                self.plan,
                order_id=f"{self.plan.plan_id}:existing",
                position=self.plan.entry_quantities[0],
                side="buy",
                price=self.plan.entry_levels[0],
            )

    orders = _Orders()
    lifecycle = ExternalDcaLifecycle(
        output_root,
        orders,
        _ExistingFacts(plan),
        protection,
        journal_id="cleanup-existing-1",
    )
    adopted = lifecycle.adopt_existing_position(
        plan,
        bundle=_ExistingFacts(plan).read_account_state(
            instrument_id=plan.instrument_id,
            now=datetime.now(UTC),
        ),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert adopted["status"] == "EXPIRED_POSITION_BLOCKED"
    assert adopted["existing_state"] == "adopted_external_canary"
    assert lifecycle.current_path.name == "current.json"
    assert "cleanup-existing-1" in str(lifecycle.current_path)

    flattened = lifecycle.flatten(plan, confirmation=confirmation, timestamp=NOW)
    assert flattened["status"] == "FLAT_RECONCILED"
    assert orders.requests[-1].close_position is True
    assert protection.calls == []


def test_external_dca_reconciles_unknown_flatten_without_resubmitting(tmp_path: Path) -> None:
    plan, confirmation, _unused_lifecycle, _unused_orders, protection = _lifecycle(tmp_path)
    _path, confirmation, output_root = _confirmation(tmp_path / "reconcile-flatten", plan)

    class _ExistingFacts(_Facts):
        def read_account_state(self, *, instrument_id: str, now: datetime) -> CanaryFactBundle:
            del instrument_id, now
            return _bundle(
                self.plan,
                order_id=f"{self.plan.plan_id}:existing",
                position=self.plan.entry_quantities[0],
                side="buy",
                price=self.plan.entry_levels[0],
            )

    orders = _Orders()
    lifecycle = ExternalDcaLifecycle(
        output_root,
        orders,
        _ExistingFacts(plan),
        protection,
        journal_id="cleanup-reconcile-flatten",
    )
    lifecycle.adopt_existing_position(
        plan,
        bundle=_ExistingFacts(plan).read_account_state(instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    state = lifecycle.snapshot()
    state["status"] = "BLOCKED"
    state["blocker"] = "flatten_submit_receipt_unknown"
    state["receipts"] = [
        {
            "operation": "flatten_submit",
            "order_id": f"{plan.plan_id}:flatten",
            "client_order_id": f"{plan.plan_id}:flatten:0.001:{plan.close_price}",
            "broker_order_id": "",
            "state": "unknown",
            "quantity": str(plan.entry_quantities[0]),
            "price": str(plan.close_price),
            "side": "sell",
            "instrument_id": plan.instrument_id,
            "environment": "testnet",
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": plan.capability_revision,
            "observed_at": NOW,
        }
    ]
    lifecycle._save(state)

    recovered_orders = _Orders(query_state="filled")
    recovered_orders.receipts[f"{plan.plan_id}:flatten"] = _Receipt(
        order_id=f"{plan.plan_id}:flatten",
        state="filled",
        client_order_id=f"{plan.plan_id}:flatten:{plan.entry_quantities[0]}:{plan.close_price}",
        broker_order_id=f"broker:{plan.plan_id}:flatten",
    )
    recovered = ExternalDcaLifecycle(
        output_root,
        recovered_orders,
        _Facts(plan),
        protection,
        journal_id="cleanup-reconcile-flatten",
    )
    reconciled = recovered.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert reconciled["status"] == "FLAT_RECONCILED"
    assert recovered_orders.requests == []


def test_external_dca_reconciles_expired_plan_by_explicit_broker_identity_without_mutation(tmp_path: Path) -> None:
    plan = _plan(expires_at="2020-01-01T00:00:00+00:00")
    _path, confirmation, output_root = _confirmation(tmp_path / "expired-broker-recovery", plan)
    protection = _Protection()

    class _BrokerOrders(_Orders):
        def recover(self, request: TestnetCanaryOrderRequest, *, broker_order_id: str, state: str):
            self.receipts[request.order_id] = _Receipt(
                order_id=request.order_id,
                state=state,
                client_order_id=request.idempotency_key,
                broker_order_id=broker_order_id,
            )

    orders = _BrokerOrders(query_state="unknown")
    lifecycle = ExternalDcaLifecycle(
        output_root,
        orders,
        _Facts(plan),
        protection,
        journal_id="expired-broker-recovery",
    )
    state = lifecycle._new_state(plan, NOW)
    state.update(
        {
            "status": "BLOCKED",
            "blocker": "flatten_submit_receipt_unknown",
            "position_quantity": str(plan.entry_quantities[0]),
            "average_entry_price": str(plan.entry_levels[0]),
            "receipts": [
                {
                    "operation": "flatten_submit",
                    "order_id": f"{plan.plan_id}:flatten",
                    "client_order_id": f"{plan.plan_id}:flatten:{plan.entry_quantities[0]}:{plan.close_price}",
                    "broker_order_id": "",
                    "state": "unknown",
                    "quantity": str(plan.entry_quantities[0]),
                    "price": str(plan.close_price),
                    "side": "sell",
                    "instrument_id": plan.instrument_id,
                    "environment": "testnet",
                    "account_fingerprint": plan.account_fingerprint,
                    "runtime_id": plan.runtime_id,
                    "release_sha": plan.release_sha,
                    "capability_revision": plan.capability_revision,
                    "observed_at": NOW,
                }
            ],
        }
    )
    lifecycle._save(state)

    blocked = lifecycle.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "plan_expired"
    assert orders.requests == []

    reconciled = lifecycle.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        broker_order_id=f"broker:{plan.plan_id}:flatten",
    )

    assert reconciled["status"] == "FLAT_RECONCILED"
    assert reconciled["flatten_outcome"] == "explicit_broker_identity_causal_fill_reconciled"
    assert orders.requests == []
    assert orders.receipts[f"{plan.plan_id}:flatten"].broker_order_id == f"broker:{plan.plan_id}:flatten"

    mismatch = lifecycle.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        broker_order_id="broker:wrong-close",
    )
    assert mismatch["status"] == "BLOCKED"
    assert mismatch["blocker"] == "flatten_reconcile_broker_identity_mismatch"
    assert lifecycle.snapshot()["status"] == "FLAT_RECONCILED"

    replay = lifecycle.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
        broker_order_id=f"broker:{plan.plan_id}:flatten",
    )
    assert replay["status"] == "FLAT_RECONCILED"
    assert replay["blocker"] is None


def test_external_dca_facts_require_fill_identity_and_exact_flatten_quantity(tmp_path: Path) -> None:
    plan = _plan()
    _path, confirmation, output_root = _confirmation(tmp_path / "facts-identity", plan)
    lifecycle = ExternalDcaLifecycle(
        output_root,
        _Orders(),
        _Facts(plan),
        _Protection(),
    )
    bundle = _bundle(
        plan,
        order_id=f"{plan.plan_id}:flatten",
        position=Decimal("0"),
        side="sell",
        price=plan.close_price,
        fill_quantity=plan.entry_quantities[0],
    )

    with pytest.raises(ExternalDcaError, match="fill_client_identity_mismatch"):
        lifecycle._validate_facts(
            plan,
            bundle,
            order_id=f"{plan.plan_id}:flatten",
            timestamp=NOW,
            require_flat=True,
            expected_client_order_id="client:wrong",
            expected_fill_quantity=plan.entry_quantities[0],
        )

    with pytest.raises(ExternalDcaError, match="flatten_fill_quantity_mismatch"):
        lifecycle._validate_facts(
            plan,
            bundle,
            order_id=f"{plan.plan_id}:flatten",
            timestamp=NOW,
            require_flat=True,
            expected_client_order_id=bundle.fills[0].client_order_id,
            expected_fill_quantity=Decimal("0.002"),
        )


def test_external_dca_facts_require_actual_usdc_fees(tmp_path: Path) -> None:
    plan = _plan()
    _path, confirmation, output_root = _confirmation(tmp_path / "facts-fee", plan)
    lifecycle = ExternalDcaLifecycle(
        output_root,
        _Orders(),
        _Facts(plan),
        _Protection(),
    )
    bundle = _bundle(
        plan,
        order_id=f"{plan.plan_id}:flatten",
        position=Decimal("0"),
        side="sell",
        price=plan.close_price,
        fill_quantity=plan.entry_quantities[0],
    )
    bad_fee = replace(
        bundle.fees[0],
        amount_usd=Decimal("-100"),
        currency="BTC",
        fee_source="estimate",
        fee_state="estimated",
    )
    bad_fee = replace(bad_fee, fact_digest=canary_fact_digest(bad_fee))
    bad_bundle = replace(bundle, fees=(bad_fee,))

    with pytest.raises(ExternalDcaError, match="fee_fact_invalid"):
        lifecycle._validate_facts(
            plan,
            bad_bundle,
            order_id=f"{plan.plan_id}:flatten",
            timestamp=NOW,
            require_flat=True,
            expected_client_order_id=bundle.fills[0].client_order_id,
            expected_fill_quantity=plan.entry_quantities[0],
        )


def test_external_dca_persists_blocked_final_facts_when_causal_fill_is_missing(tmp_path: Path) -> None:
    plan, confirmation, _unused_lifecycle, _unused_orders, protection = _lifecycle(tmp_path)
    _path, confirmation, output_root = _confirmation(tmp_path / "reconcile-blocked-final-facts", plan)

    class _NoCausalFacts(_Facts):
        def read_facts(
            self,
            *,
            order_id: str,
            instrument_id: str,
            now: datetime,
            client_order_id: str | None = None,
        ) -> CanaryFactBundle:
            bundle = super().read_facts(
                order_id=order_id,
                instrument_id=instrument_id,
                now=now,
                client_order_id=client_order_id,
            )
            return replace(bundle, fills=(), fees=())

    class _ExistingFacts(_Facts):
        def read_account_state(self, *, instrument_id: str, now: datetime) -> CanaryFactBundle:
            del instrument_id, now
            return _bundle(
                self.plan,
                order_id=f"{self.plan.plan_id}:existing",
                position=self.plan.entry_quantities[0],
                side="buy",
                price=self.plan.entry_levels[0],
            )

    seed_orders = _Orders()
    seed_lifecycle = ExternalDcaLifecycle(
        output_root,
        seed_orders,
        _ExistingFacts(plan),
        protection,
        journal_id="cleanup-reconcile-blocked-final-facts",
    )
    seed_lifecycle.adopt_existing_position(
        plan,
        bundle=_ExistingFacts(plan).read_account_state(instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    state = seed_lifecycle.snapshot()
    state["status"] = "BLOCKED"
    state["blocker"] = "flatten_submit_receipt_unknown"
    client_order_id = f"{plan.plan_id}:flatten:{plan.entry_quantities[0]}:{plan.close_price}"
    state["receipts"] = [
        {
            "operation": "flatten_submit",
            "order_id": f"{plan.plan_id}:flatten",
            "client_order_id": client_order_id,
            "broker_order_id": "",
            "state": "unknown",
            "quantity": str(plan.entry_quantities[0]),
            "price": str(plan.close_price),
            "side": "sell",
            "instrument_id": plan.instrument_id,
            "environment": "testnet",
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": plan.capability_revision,
            "observed_at": NOW,
        }
    ]
    seed_lifecycle._save(state)

    recovered_orders = _Orders(query_state="unknown")
    recovered_orders.receipts[f"{plan.plan_id}:flatten"] = _Receipt(
        order_id=f"{plan.plan_id}:flatten",
        state="unknown",
        client_order_id=client_order_id,
        broker_order_id="",
    )
    recovered = ExternalDcaLifecycle(
        output_root,
        recovered_orders,
        _NoCausalFacts(plan),
        protection,
        journal_id="cleanup-reconcile-blocked-final-facts",
    )

    blocked = recovered.reconcile_flatten(plan, confirmation=confirmation, timestamp=NOW)

    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "canonical_fill_missing"
    assert blocked["final_facts"]["fills"] == []
    assert blocked["final_facts"]["fees"] == []
    assert blocked["final_facts"]["positions"] == []
    assert blocked["final_facts_digest"].startswith("sha256:")
    assert recovered_orders.requests == []


@pytest.mark.parametrize(
    ("query_state", "expected_status", "expected_outcome"),
    [
        ("resting", "WAITING_ENTRY", "resting"),
        ("rejected", "BLOCKED", "rejected"),
        ("canceled", "BLOCKED", "canceled"),
        ("unknown", "BLOCKED", "unknown"),
    ],
)
def test_external_dca_entry_outcomes_are_normalized(
    tmp_path: Path,
    query_state: str,
    expected_status: str,
    expected_outcome: str,
) -> None:
    plan, confirmation, _unused_lifecycle, _unused_orders, protection = _lifecycle(tmp_path)
    output_root = tmp_path / f"outputs-{query_state}"
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id=f"proposal-{query_state}",
        strategy_session_id=plan.strategy_session_id,
        strategy_revision_id=plan.strategy_revision_id,
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={
            "strategy_session_id": plan.strategy_session_id,
            "strategy_revision_id": plan.strategy_revision_id,
        },
        now=1787446800,
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "expires_at": plan.expires_at,
    }
    orders = _Orders(query_state=query_state)
    lifecycle = ExternalDcaLifecycle(
        output_root,
        orders,
        _Facts(plan),
        protection,
    )
    state = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert state["status"] == expected_status
    assert state["entry_outcome"] == expected_outcome
    assert len(orders.requests) == 1


def test_external_dca_partial_fill_protects_only_owned_quantity(tmp_path: Path) -> None:
    plan, confirmation, _lifecycle_unused, _orders_unused, protection = _lifecycle(tmp_path)
    output_root = tmp_path / "outputs-partial"
    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    proposal = ledger.create_proposal(
        proposal_id="proposal-partial",
        strategy_session_id=plan.strategy_session_id,
        strategy_revision_id=plan.strategy_revision_id,
        plan_digest=plan.plan_digest,
        risk_digest=plan.plan_digest,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park",
        command_text=f"confirm {plan.plan_digest}",
        current_binding={
            "strategy_session_id": plan.strategy_session_id,
            "strategy_revision_id": plan.strategy_revision_id,
        },
        now=1787446800,
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "canary_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "operator_id": "park",
        "proposal_id": proposal["proposal_id"],
        "receipt_digest": decision["receipt_digest"],
        "expires_at": plan.expires_at,
    }
    orders = _Orders(query_state="partially_filled")
    lifecycle = ExternalDcaLifecycle(output_root, orders, _Facts(plan), protection)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    partial = Decimal("0.0005")
    state = lifecycle.on_entry_facts(
        plan,
        bundle=_bundle(
            plan,
            order_id=orders.requests[0].order_id,
            position=partial,
            side="buy",
            price=plan.entry_levels[0],
            fill_quantity=partial,
        ),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert state["status"] == "PROTECTION_ACTIVE"
    assert state["position_quantity"] == str(partial)
    assert protection.calls == ["submit", "query"]


def test_external_dca_unknown_protection_blocks_new_entries(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(
        tmp_path,
        protection_state=ProtectionLifecycleState.UNKNOWN,
    )
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    state = lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[0].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert state["status"] == "BLOCKED"
    assert lifecycle.snapshot()["next_action"] == "notify_park_and_wait"


def test_external_dca_flatten_requires_canonical_flat_reconciliation(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[0].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    state = lifecycle.flatten(
        plan,
        bundle=_Facts(plan).read_facts(order_id=f"{plan.plan_id}:flatten", instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert state["status"] == "FLAT_RECONCILED"
    assert orders.requests[-1].reduce_only is True
    assert orders.requests[-1].close_position is True


def test_external_dca_rejects_flat_claim_without_causal_close_fill(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[0].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    malformed = _Facts(plan).read_facts(
        order_id=f"{plan.plan_id}:flatten",
        instrument_id=plan.instrument_id,
        now=datetime.now(UTC),
    )
    malformed = replace(malformed, fills=(), fees=())
    state = lifecycle.flatten(plan, bundle=malformed, confirmation=confirmation, timestamp=NOW)
    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "flatten_fill_quantity_incomplete"


def test_external_dca_rechecks_confirmation_before_next_entry(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    lifecycle.on_entry_facts(
        plan,
        bundle=_Facts(plan).read_facts(order_id=orders.requests[0].order_id, instrument_id=plan.instrument_id, now=datetime.now(UTC)),
        confirmation=confirmation,
        timestamp=NOW,
    )
    expired = {**confirmation, "expires_at": "2020-01-01T00:00:00+00:00"}
    state = lifecycle.submit_next_entry(plan, confirmation=expired, timestamp=NOW)
    assert state["status"] == "BLOCKED"
    assert state["blocker"] == "confirmation_expired"
    assert len(orders.requests) == 1


def test_external_dca_reconcile_entry_reads_facts_and_activates_protection(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, protection = _lifecycle(tmp_path)

    prepared = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert prepared["status"] == "ENTRY_FILLED_PENDING_FACTS"
    state = lifecycle.reconcile_entry(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )

    assert state["status"] == "PROTECTION_ACTIVE"
    assert state["next_action"] == "submit_next_entry_only_after_attended_price_gate"
    assert protection.calls == ["submit", "query"]
    assert any(row["operation"] == "reconcile_query" for row in state["receipts"])


def test_external_dca_persists_slippage_block_and_allows_attended_flatten_without_retry(
    tmp_path: Path,
) -> None:
    plan = _plan(max_slippage="1")
    _path, confirmation, output_root = _confirmation(tmp_path, plan)

    class _SlippageFacts(_Facts):
        def read_facts(
            self,
            *,
            order_id: str,
            instrument_id: str,
            now: datetime,
            client_order_id: str | None = None,
        ) -> CanaryFactBundle:
            del instrument_id, now
            if "flatten" in order_id:
                return super().read_facts(
                    order_id=order_id,
                    instrument_id=self.plan.instrument_id,
                    now=datetime.now(UTC),
                    client_order_id=client_order_id,
                )
            return _bundle(
                self.plan,
                order_id=order_id,
                position=self.plan.entry_quantities[0],
                side="buy",
                price=self.plan.entry_levels[0] + Decimal("20"),
                fill_quantity=self.plan.entry_quantities[0],
                client_order_id=client_order_id,
            )

    orders = _Orders()
    protection = _Protection()
    lifecycle = ExternalDcaLifecycle(output_root, orders, _SlippageFacts(plan), protection)
    prepared = lifecycle.prepare(plan, confirmation=confirmation, timestamp=NOW)
    assert prepared["status"] == "ENTRY_FILLED_PENDING_FACTS"

    blocked = lifecycle.on_entry_facts(
        plan,
        bundle=_SlippageFacts(plan).read_facts(
            order_id=orders.requests[0].order_id,
            instrument_id=plan.instrument_id,
            now=datetime.now(UTC),
        ),
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "entry_fill_slippage_exceeded"
    assert blocked["next_action"] == "attended_flatten_after_risk_block"
    assert blocked["position_quantity"] == str(plan.entry_quantities[0])
    assert blocked["entry_facts_digest"]
    assert protection.calls == []

    recovered = lifecycle.reconcile_entry(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert recovered["status"] == "BLOCKED"
    assert recovered["blocker"] == "entry_fill_slippage_exceeded"
    assert len(orders.requests) == 1
    assert any(row["operation"] == "reconcile_query" for row in recovered["receipts"])

    flattened = lifecycle.flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )
    assert flattened["status"] == "FLAT_RECONCILED"
    assert len(orders.requests) == 2
    assert orders.requests[-1].reduce_only is True
    assert orders.requests[-1].limit_price == Decimal("59999")
    assert flattened["flatten_recovery_reason"] == "risk_block_market_guard"
    assert protection.calls == []


def test_emergency_flatten_price_respects_hyperliquid_significant_figures() -> None:
    plan = _plan(
        direction="short",
        entry_levels=["4630", "4660"],
        entry_quantities=["0.06", "0.06"],
        target_price="4580",
        stop_price="4680",
        close_price="4580",
        max_slippage="5",
        max_notional="560",
        max_leverage="10",
        price_tick="0.001",
    )

    assert ExternalDcaLifecycle._emergency_flatten_price(
        plan,
        Decimal("4682.550"),
    ) == Decimal("4687.5")


def test_external_dca_unknown_flatten_does_not_retry_after_ambiguous_submit(
    tmp_path: Path,
) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    state = lifecycle._new_state(plan, NOW)
    state.update(
        {
            "status": "BLOCKED",
            "blocker": "flatten_unknown",
            "position_quantity": str(plan.entry_quantities[0]),
            "entry_order_ids": [],
        }
    )
    lifecycle._save(state)

    blocked = lifecycle.flatten(plan, confirmation=confirmation, timestamp=NOW)

    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "flatten_not_available"
    assert orders.requests == []


def test_emergency_flatten_price_blocks_when_precision_cannot_fit_slippage() -> None:
    plan = _plan(
        max_slippage="1",
        max_leverage="10",
        price_tick="0.001",
    )

    with pytest.raises(ExternalDcaError, match="emergency_flatten_slippage_unavailable"):
        ExternalDcaLifecycle._emergency_flatten_price(plan, Decimal("123456"))


def test_reconcile_flatten_reuses_persisted_dynamic_emergency_price(tmp_path: Path) -> None:
    plan, confirmation, lifecycle, orders, _protection = _lifecycle(tmp_path)
    state = lifecycle._new_state(plan, NOW)
    client_order_id = f"{plan.plan_id}:flatten:{plan.entry_quantities[0]}:{plan.close_price}"
    flatten_order_id = f"{plan.plan_id}:flatten"
    dynamic_price = Decimal("59999")
    state.update(
        {
            "status": "BLOCKED",
            "blocker": "flatten_not_filled",
            "position_quantity": str(plan.entry_quantities[0]),
            "flatten_recovery_price": str(dynamic_price),
            "flatten_recovery_reason": "risk_block_market_guard",
            "receipts": [
                {
                    "operation": "flatten_submit",
                    "order_id": flatten_order_id,
                    "client_order_id": client_order_id,
                    "broker_order_id": "broker:dynamic-flatten",
                    "state": "filled",
                    "quantity": str(plan.entry_quantities[0]),
                    "price": str(dynamic_price),
                    "side": "sell",
                    "instrument_id": plan.instrument_id,
                    "reduce_only": True,
                    "close_position": True,
                    "environment": "testnet",
                    "account_fingerprint": plan.account_fingerprint,
                    "runtime_id": plan.runtime_id,
                    "release_sha": plan.release_sha,
                    "capability_revision": plan.capability_revision,
                    "observed_at": NOW,
                }
            ],
        }
    )
    lifecycle._save(state)
    orders.receipts[flatten_order_id] = _Receipt(
        order_id=flatten_order_id,
        state="filled",
        client_order_id=client_order_id,
        broker_order_id="broker:dynamic-flatten",
    )

    reconciled = lifecycle.reconcile_flatten(
        plan,
        confirmation=confirmation,
        timestamp=NOW,
    )

    assert reconciled["status"] == "FLAT_RECONCILED"
