from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from services.standard_broker_testnet_canary_facts import (
    CanaryAccountFact,
    CanaryFactBundle,
    CanaryFeeFact,
    CanaryFillFact,
    CanaryPositionFact,
    CanaryReconciliationFact,
    TestnetCanaryFillFlat,
    canary_fact_digest,
)
from services.standard_broker_testnet_canary import TestnetCanaryError
from tests.test_standard_broker_testnet_canary import (
    ACCOUNT_FINGERPRINT,
    RELEASE_SHA,
    FakeCanaryBroker,
    _authorized_canary,
    _plan,
)


class FilledBroker(FakeCanaryBroker):
    def __init__(self) -> None:
        super().__init__()
        self.status = "filled"

    def submit(self, request):
        receipt = super().submit(request)
        receipt.filled_quantity = request.quantity
        receipt.remaining_quantity = Decimal("0")
        receipt.average_fill_price = request.limit_price
        return receipt

    def query(self, order_id: str):
        receipt = super().query(order_id)
        receipt.filled_quantity = receipt.original_quantity
        receipt.remaining_quantity = Decimal("0")
        receipt.average_fill_price = receipt.limit_price
        return receipt


class FakeFacts:
    def __init__(self, plan, *, final_open_orders: tuple[str, ...] = ()) -> None:
        self.plan = plan
        self.calls: list[str] = []
        self.final_open_orders = final_open_orders
        self.bundle = None
        self.final_bundle = None

    def read_facts(self, *, order_id: str, instrument_id: str) -> CanaryFactBundle:
        self.calls.append(order_id)
        final = len(self.calls) > 1
        if self.bundle is not None:
            return self.bundle
        if not final:
            return _entry_bundle(self.plan, order_id)
        if self.final_bundle is not None:
            return self.final_bundle
        return _final_bundle(self.plan, order_id, open_orders=self.final_open_orders)


def _identity(plan):
    return {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "runtime_id": plan.runtime_id,
        "release_sha": RELEASE_SHA,
        "capability_revision": plan.capability_revision,
        "transport_state": "external_testnet",
    }


def _with_digest(fact):
    return replace(fact, fact_digest=canary_fact_digest(fact))


def _entry_bundle(plan, order_id: str) -> CanaryFactBundle:
    identity = _identity(plan)
    fill = _with_digest(CanaryFillFact(
        fill_id="fill-entry-1",
        order_id=order_id,
        instrument_id=plan.instrument_id,
        side="buy",
        price=Decimal("60000"),
        quantity=plan.quantity,
        occurred_at="2026-08-23T01:01:30+00:00",
        **identity,
        broker_order_id="broker-canary-order-1",
        client_order_id="client-canary-order-1",
        cursor="cursor-entry",
    ))
    fee = _with_digest(CanaryFeeFact(
        fee_id="fee-entry-1",
        fill_id=fill.fill_id,
        amount_usd=Decimal("0.2"),
        currency="USD",
        occurred_at=fill.occurred_at,
        **identity,
        cursor="cursor-entry",
        fee_source="actual_fill",
        fee_state="actual",
    ))
    account = _with_digest(CanaryAccountFact(
        equity_usd=Decimal("62.8"),
        cursor="cursor-entry",
        observed_at=fill.occurred_at,
        **identity,
    ))
    position = _with_digest(CanaryPositionFact(
        instrument_id=plan.instrument_id,
        signed_quantity=plan.quantity,
        cursor=account.cursor,
        observed_at=fill.occurred_at,
        **identity,
    ))
    reconciliation = _with_digest(CanaryReconciliationFact(
        coherent=True,
        freshness="fresh",
        cursor=account.cursor,
        open_order_ids=(),
        signed_position_quantity=plan.quantity,
        observed_at=fill.occurred_at,
        evidence_digest="sha256:" + "e" * 64,
        **identity,
    ))
    return CanaryFactBundle(
        fills=(fill,),
        fees=(fee,),
        account=account,
        positions=(position,),
        open_orders=(),
        reconciliation=reconciliation,
    )


def _final_bundle(plan, order_id: str, *, open_orders: tuple[str, ...] = ()) -> CanaryFactBundle:
    identity = _identity(plan)
    fill = _with_digest(CanaryFillFact(
        fill_id="fill-close-1",
        order_id=order_id,
        instrument_id=plan.instrument_id,
        side="sell",
        price=Decimal("59900"),
        quantity=plan.quantity,
        occurred_at="2026-08-23T01:02:30+00:00",
        **identity,
        broker_order_id="broker-canary-order-2",
        client_order_id="client-canary-order-2",
        cursor="cursor-final",
    ))
    fee = _with_digest(CanaryFeeFact(
        fee_id="fee-close-1",
        fill_id=fill.fill_id,
        amount_usd=Decimal("0.2"),
        currency="USD",
        occurred_at=fill.occurred_at,
        **identity,
        cursor="cursor-final",
        fee_source="actual_fill",
        fee_state="actual",
    ))
    account = _with_digest(CanaryAccountFact(
        equity_usd=Decimal("62.6"),
        cursor="cursor-final",
        observed_at=fill.occurred_at,
        **identity,
    ))
    reconciliation = _with_digest(CanaryReconciliationFact(
        coherent=True,
        freshness="fresh",
        cursor=account.cursor,
        open_order_ids=open_orders,
        signed_position_quantity=Decimal("0"),
        observed_at=fill.occurred_at,
        evidence_digest="sha256:" + "f" * 64,
        **identity,
    ))
    return CanaryFactBundle(
        fills=(fill,),
        fees=(fee,),
        account=account,
        positions=(),
        open_orders=open_orders,
        reconciliation=reconciliation,
    )


def _ready(tmp_path: Path):
    broker = FilledBroker()
    plan = _plan(canary_id="fill-flat")
    canary, confirmation = _authorized_canary(tmp_path, broker, plan)
    canary.prepare(plan, confirmation=confirmation, timestamp="2026-08-23T01:00:00+00:00")
    entry = canary.submit_entry(plan, confirmation=confirmation, timestamp="2026-08-23T01:01:00+00:00")
    assert entry["status"] == "ENTRY_FILLED"
    return broker, plan, canary, confirmation


def test_fill_to_flat_requires_typed_facts_and_attended_reduce_only_close(tmp_path: Path) -> None:
    broker, plan, canary, confirmation = _ready(tmp_path)
    facts = FakeFacts(plan)
    coordinator = TestnetCanaryFillFlat(canary, facts)

    result = coordinator.complete(
        plan,
        confirmation=confirmation,
        timestamp="2026-08-23T01:03:00+00:00",
    )
    assert result["status"] == "FLAT_RECONCILED"
    assert result["close_state"] == "filled"
    close_requests = [request for name, request in broker.calls if name == "submit"][1:]
    assert len(close_requests) == 1
    assert close_requests[0].reduce_only is True
    assert close_requests[0].close_position is True
    assert result["plan"]["close_mode"] == "ordinary_reduce_only_close"
    encoded = json.dumps(result, sort_keys=True)
    assert "native" not in encoded.lower()
    assert coordinator.complete(plan, confirmation=confirmation, timestamp="2026-08-23T01:04:00+00:00") == result
    assert len([name for name, _ in broker.calls if name == "submit"]) == 2


@pytest.mark.parametrize("variant", ["unknown_bundle", "fee_missing", "conflicting_fill", "not_flat"])
def test_fill_to_flat_freezes_unknown_or_incoherent_facts(tmp_path: Path, variant: str) -> None:
    broker, plan, canary, confirmation = _ready(tmp_path)
    facts = FakeFacts(plan, final_open_orders=("foreign-order",) if variant == "not_flat" else ())
    if variant == "unknown_bundle":
        facts.bundle = None
        facts.read_facts = lambda **kwargs: None
    elif variant == "fee_missing":
        facts.bundle = replace(_entry_bundle(plan, "canary-order-1"), fees=())
    elif variant == "conflicting_fill":
        entry = _entry_bundle(plan, "canary-order-1")
        conflicting = replace(entry.fills[0], quantity=Decimal("0.0005"))
        facts.bundle = replace(entry, fills=(entry.fills[0], conflicting))

    coordinator = TestnetCanaryFillFlat(canary, facts)
    with pytest.raises(TestnetCanaryError):
        coordinator.complete(plan, confirmation=confirmation, timestamp="2026-08-23T01:03:00+00:00")
    assert coordinator.snapshot(plan)["status"] == "BLOCKED"
    assert len([name for name, _ in broker.calls if name == "submit"]) == (2 if variant == "not_flat" else 1)


def test_fill_to_flat_blocks_when_final_reconciliation_is_not_flat(tmp_path: Path) -> None:
    broker, plan, canary, confirmation = _ready(tmp_path)
    facts = FakeFacts(plan, final_open_orders=("foreign-order",))
    coordinator = TestnetCanaryFillFlat(canary, facts)

    with pytest.raises(TestnetCanaryError, match="not flat"):
        coordinator.complete(plan, confirmation=confirmation, timestamp="2026-08-23T01:03:00+00:00")
    assert coordinator.snapshot(plan)["status"] == "BLOCKED"


def test_fill_to_flat_requires_terminal_close_receipt(tmp_path: Path) -> None:
    broker, plan, canary, confirmation = _ready(tmp_path)
    broker.status = "resting"
    facts = FakeFacts(plan)
    coordinator = TestnetCanaryFillFlat(canary, facts)

    with pytest.raises(TestnetCanaryError, match="terminal"):
        coordinator.complete(plan, confirmation=confirmation, timestamp="2026-08-23T01:03:00+00:00")
    assert coordinator.snapshot(plan)["status"] == "BLOCKED"


def test_fill_to_flat_requires_causal_close_fill(tmp_path: Path) -> None:
    broker, plan, canary, confirmation = _ready(tmp_path)
    facts = FakeFacts(plan)
    final = _final_bundle(plan, "canary-order-2")
    unrelated = _with_digest(
        replace(
            final.fills[0],
            fill_id="unrelated-entry-fill",
            order_id="canary-order-1",
            side="buy",
            broker_order_id="broker-canary-order-1",
            client_order_id="client-canary-order-1",
        )
    )
    facts.final_bundle = replace(final, fills=(unrelated,))
    coordinator = TestnetCanaryFillFlat(canary, facts)

    with pytest.raises(TestnetCanaryError, match="lineage"):
        coordinator.complete(plan, confirmation=confirmation, timestamp="2026-08-23T01:03:00+00:00")
    assert coordinator.snapshot(plan)["status"] == "BLOCKED"
