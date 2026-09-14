from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from standard_broker import (
    ExternalCursorKind,
    ExternalFactEnvelope,
    ExternalReconciliationCursor,
    ExternalReconciliationObservation,
    ExternalReconciliationOutcome,
    ExternalReconciliationSnapshot,
)
from standard_broker.account import AccountSnapshot, PositionFact, PositionSide
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.external_host import digest_canonical
from standard_broker.fees import FeeEvent, FeeKind, FeeSource, FeeState, FundingPayment
from standard_broker.instruments import MarginMode
from standard_broker.models import BrokerEnvironment, Provenance
from standard_broker.orders import OrderFill, OrderReceipt, OrderSide, OrderState

from test_external_host import ACCOUNT, RELEASE_SHA
from test_external_profile import _profile_context, _profile_session


NOW = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)


def _provenance() -> Provenance:
    return Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope="hypercore:default",
        transport_state="external_testnet",
        mapping_revision="hyperliquid-testnet-runtime-v1",
        received_at=NOW,
    )


def _facts() -> dict[str, object]:
    provenance = _provenance()
    position = PositionFact(
        instrument_id="SOL-USD-PERP",
        signed_quantity=Decimal("0.1"),
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("5"),
        margin_mode=MarginMode.CROSS,
        liquidation_price=Decimal("80"),
        margin_used=Decimal("2"),
        position_value=Decimal("10"),
        unrealized_pnl=Decimal("0.2"),
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        observation_id="position-1",
        provenance=provenance,
    )
    account = AccountSnapshot(
        broker_id="hyperliquid",
        account_address=ACCOUNT,
        equity=Decimal("100"),
        balance=Decimal("100"),
        withdrawable=Decimal("90"),
        margin_used=Decimal("2"),
        exposure=Decimal("10"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0.2"),
        positions=(position,),
        provenance=provenance,
        environment=BrokerEnvironment.TESTNET,
        observation_id="account-1",
    )
    order = OrderReceipt(
        order_id="order-1",
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        client_order_id="cloid-1",
        state=OrderState.RESTING,
        original_quantity=Decimal("0.1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("0.1"),
        broker_order_id="101",
        average_fill_price=None,
        reason=None,
        provenance=provenance,
        updated_at=NOW,
        broker_updated_at=NOW,
        broker_order_lineage=("101",),
        client_order_lineage=("cloid-1",),
        account_address=ACCOUNT,
        lifecycle_id="profile-lifecycle-1",
        release_sha=RELEASE_SHA,
    )
    fill = OrderFill(
        fill_id="fill-1",
        order_id="order-1",
        broker_order_id="101",
        client_order_id="cloid-1",
        instrument_id="SOL-USD-PERP",
        side=OrderSide.BUY,
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        occurred_at=NOW,
        environment=BrokerEnvironment.TESTNET,
        account_address=ACCOUNT,
        lifecycle_id="profile-lifecycle-1",
        release_sha=RELEASE_SHA,
    )
    fee = FeeEvent(
        fee_id="fee-1",
        broker_id="hyperliquid",
        kind=FeeKind.TAKER,
        amount=Decimal("0.04"),
        currency="USDC",
        occurred_at=NOW,
        source=FeeSource.ACTUAL_FILL,
        state=FeeState.ACTUAL,
        provenance=provenance,
        instrument_id="SOL-USD-PERP",
        fill_id="fill-1",
        order_id="order-1",
        liquidity="taker",
        environment=BrokerEnvironment.TESTNET,
    )
    funding_fee = replace(fee, fee_id="funding-fee-1", kind=FeeKind.FUNDING, amount=Decimal("0.01"))
    funding = FundingPayment(
        funding_id="funding-1",
        broker_id="hyperliquid",
        instrument_id="SOL-USD-PERP",
        amount=Decimal("0.01"),
        currency="USDC",
        funding_rate=Decimal("0.0001"),
        signed_quantity=Decimal("0.1"),
        occurred_at=NOW,
        fee=funding_fee,
        provenance=provenance,
        environment=BrokerEnvironment.TESTNET,
    )
    return {
        "account": account,
        "positions": (position,),
        "open_orders": (order,),
        "fills": (fill,),
        "fees": (fee,),
        "funding": (funding,),
    }


def _observations(*, cursor_value: int = 10, cursor_time: datetime = NOW):
    session = _profile_session()
    context = _profile_context(session)
    cursor = ExternalReconciliationCursor(
        kind=ExternalCursorKind.WATERMARK,
        value=cursor_value,
        observed_at=cursor_time,
    )
    result = {}
    for name, data in _facts().items():
        fact = ExternalFactEnvelope.create(
            context=context,
            fact_type=f"reconciliation.{name}",
            data=data,
            request_id=f"request-{name}",
            provenance=_provenance(),
            raw_payload={"fixture_fact": name},
        )
        result[name] = ExternalReconciliationObservation(
            fact=fact,
            cursor=cursor,
            receipt_digest=fact.fact_digest,
        )
    return result


def _replace_fact(fact: ExternalFactEnvelope, **changes) -> ExternalFactEnvelope:
    candidate = replace(fact, **changes)
    return replace(
        candidate,
        fact_digest=digest_canonical(
            {
                "fact_type": candidate.fact_type,
                "data": candidate.data,
                "request_digest": candidate.request_digest,
                "raw_payload_digest": candidate.raw_payload_digest,
                "broker_id": candidate.broker_id,
                "environment": candidate.environment,
                "account_scope": candidate.account_scope,
                "account_address": candidate.account_address,
                "signer_kind": candidate.signer_kind,
                "execution_scope": candidate.execution_scope,
                "lifecycle_id": candidate.lifecycle_id,
                "release_sha": candidate.release_sha,
                "runtime_identity": candidate.runtime_identity,
                "capability_revision": candidate.capability_revision,
                "provenance": candidate.provenance,
            }
        ),
    )


def _assemble(**overrides):
    values = _observations()
    values.update(overrides)
    return ExternalReconciliationSnapshot.assemble(
        account=values.get("account"),
        positions=values.get("positions"),
        open_orders=values.get("open_orders"),
        fills=values.get("fills"),
        fees=values.get("fees"),
        funding=values.get("funding"),
        funding_applicable=values.get("funding_applicable", True),
        now=values.get("now", NOW),
        stale_after=values.get("stale_after"),
        max_observation_skew=values.get("max_observation_skew"),
    )


def test_external_reconciliation_snapshot_is_coherent_and_digest_bound() -> None:
    snapshot = _assemble()

    assert snapshot.passed is True
    assert snapshot.outcome is ExternalReconciliationOutcome.COHERENT
    assert snapshot.cursor.value == 10
    assert snapshot.observed_at == NOW
    assert snapshot.order_ids == ("order-1",)
    assert snapshot.fill_ids == ("fill-1",)
    assert snapshot.fee_ids == ("fee-1",)
    assert snapshot.funding_ids == ("funding-1",)
    assert snapshot.account_ids == ("account-1",)
    assert snapshot.position_ids == ("position-1",)
    assert snapshot.request_digests
    assert snapshot.receipt_digests
    assert snapshot.raw_payload_digests
    assert not any(field.name == "raw_payload" for field in fields(snapshot))
    assert snapshot.require_coherent() is snapshot


def test_fee_payload_keeps_read_provenance_when_observation_timestamp_differs() -> None:
    observations = _observations()
    fee = _facts()["fees"][0]
    fee = replace(fee, provenance=replace(fee.provenance, received_at=NOW + timedelta(seconds=1)))
    fee_fact = _replace_fact(observations["fees"].fact, data=(fee,))
    observations["fees"] = replace(
        observations["fees"],
        fact=fee_fact,
        receipt_digest=fee_fact.fact_digest,
    )

    snapshot = _assemble(**observations)

    assert snapshot.passed is True


def test_mixed_cursor_fails_closed_as_explicit_non_pass() -> None:
    other = _observations(cursor_value=11)["fills"]
    snapshot = _assemble(fills=other)

    assert snapshot.passed is False
    assert snapshot.outcome is ExternalReconciliationOutcome.DRIFT
    assert "cursor_conflict" in snapshot.failure_reasons
    with pytest.raises(RuntimeBoundaryError, match="external_reconciliation_not_coherent"):
        snapshot.require_coherent()


def test_unregistered_open_orders_remain_visible_in_a_non_coherent_snapshot() -> None:
    observations = _observations()
    known = observations["open_orders"].fact.data[0]
    external = replace(
        known,
        order_id="external:202",
        client_order_id="0xunregistered-2",
        state=OrderState.UNKNOWN,
        broker_order_id="202",
        reason="unregistered_exchange_order",
        broker_order_lineage=("202",),
        client_order_lineage=("0xunregistered-2",),
    )
    fact = _replace_fact(observations["open_orders"].fact, data=(known, external))
    observations["open_orders"] = replace(
        observations["open_orders"],
        fact=fact,
        receipt_digest=fact.fact_digest,
    )

    snapshot = _assemble(**observations)

    assert snapshot.passed is False
    assert snapshot.failure_reasons == ("unregistered_open_orders",)
    assert snapshot.unregistered_open_order_count == 1
    assert snapshot.unregistered_open_orders == (external,)
    with pytest.raises(RuntimeBoundaryError, match="unregistered_open_orders"):
        snapshot.require_coherent()


def test_mismatched_account_identity_fails_closed() -> None:
    observations = _observations()
    wrong = _replace_fact(observations["positions"].fact, account_address="0x" + "22" * 20)
    observations["positions"] = replace(
        observations["positions"],
        fact=wrong,
        receipt_digest=wrong.fact_digest,
    )
    snapshot = _assemble(**observations)

    assert snapshot.passed is False
    assert snapshot.outcome is ExternalReconciliationOutcome.IDENTITY_CONFLICT
    assert "identity_conflict" in snapshot.failure_reasons


def test_stale_unknown_and_incomplete_states_are_not_passes() -> None:
    stale = _assemble(
        **_observations(cursor_time=NOW - timedelta(hours=1)),
        now=NOW,
        stale_after=timedelta(minutes=5),
    )
    assert stale.outcome is ExternalReconciliationOutcome.STALE

    observations = _observations()
    unknown_order = replace(observations["open_orders"].fact.data[0], state=OrderState.UNKNOWN)
    unknown_fact = _replace_fact(observations["open_orders"].fact, data=(unknown_order,))
    unknown = _assemble(
        **{
            **observations,
            "open_orders": replace(
                observations["open_orders"],
                fact=unknown_fact,
                receipt_digest=unknown_fact.fact_digest,
            ),
        }
    )
    assert unknown.outcome is ExternalReconciliationOutcome.UNKNOWN

    incomplete = _assemble(funding=None, funding_applicable=True)
    assert incomplete.outcome is ExternalReconciliationOutcome.INCOMPLETE
    assert "missing_funding" in incomplete.failure_reasons


def test_raw_or_illegal_fact_cannot_enter_the_reconciliation_envelope() -> None:
    observations = _observations()
    raw_fact = _replace_fact(observations["fills"].fact, data=({"tid": "raw-provider-fill"},))
    raw_observation = replace(
        observations["fills"],
        fact=raw_fact,
        receipt_digest=raw_fact.fact_digest,
    )

    with pytest.raises(RuntimeBoundaryError, match="external_reconciliation_fact_invalid"):
        _assemble(fills=raw_observation)


def test_snapshot_digest_and_outcome_are_bound_on_direct_construction() -> None:
    snapshot = _assemble()

    with pytest.raises(ValueError, match="evidence_digest"):
        replace(
            snapshot,
            outcome=ExternalReconciliationOutcome.INCOMPLETE,
            failure_reasons=("missing_funding",),
        )
