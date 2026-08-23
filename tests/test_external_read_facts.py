from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from standard_broker.adapters.hyperliquid.external import default_testnet_capabilities
from standard_broker.adapters.hyperliquid.instruments import HyperliquidInstrumentAdapter
from standard_broker.adapters.hyperliquid.read_facts import HyperliquidExternalFactAdapter
from standard_broker.errors import BrokerCapabilityError, RuntimeBoundaryError
from standard_broker.market_data import FreshnessPolicy
from standard_broker.models import BrokerEnvironment, Provenance

from test_external_profile import _profile_context, _profile_session


NOW = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
META = {
    "universe": [
        {"name": "SOL", "index": 5, "szDecimals": 2, "maxLeverage": 10},
    ]
}


def _adapter() -> HyperliquidExternalFactAdapter:
    session = _profile_session()
    context = _profile_context(session)
    instruments = HyperliquidInstrumentAdapter.from_meta(
        META,
        revision=default_testnet_capabilities().revision,
    )
    return HyperliquidExternalFactAdapter(
        context=context,
        instruments=instruments,
        freshness_policy=FreshnessPolicy(timedelta(seconds=5)),
    )


def _provenance() -> Provenance:
    return Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope="hypercore:default",
        transport_state="external_testnet",
        mapping_revision="hyperliquid-testnet-runtime-v1",
        received_at=NOW,
    )


def test_ticker_fact_is_canonical_and_fresh() -> None:
    envelope = _adapter().map_ticker(
        request_id="ticker-1",
        broker_symbol="SOL",
        raw={
            "mid": "100",
            "bbo": {"coin": "SOL", "bbo": [{"px": "99"}, {"px": "101"}], "time": 1787461200000},
            "provenance": _provenance(),
        },
        now=NOW + timedelta(seconds=1),
    )

    assert envelope.fact_type == "market_data.ticker"
    assert envelope.data.data.instrument_id == "SOL-USD-PERP"
    assert envelope.data.data.bid == Decimal("99")
    assert envelope.data.freshness.value == "fresh"
    assert envelope.fact_digest.startswith("sha256:")


def test_ticker_without_complete_bbo_is_a_capability_gap() -> None:
    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        _adapter().map_ticker(
            request_id="ticker-incomplete",
            broker_symbol="SOL",
            raw={"mid": "100", "provenance": _provenance()},
            now=NOW,
        )


def test_instrument_fact_contains_precision_and_margin_rules() -> None:
    envelope = _adapter().map_instruments(request_id="instruments-1", raw={"meta": META, "provenance": _provenance()})

    instrument = envelope.data[0]
    assert envelope.fact_type == "instrument.catalog"
    assert instrument.canonical_symbol == "SOL-USD-PERP"
    assert instrument.quantity_step == Decimal("0.01")
    assert instrument.max_leverage == Decimal("10")
    assert instrument.collateral_currency == "USDC"


def test_position_reports_are_mapped_with_account_and_environment_identity() -> None:
    envelope = _adapter().map_positions(
        request_id="positions-1",
        broker_symbol="SOL",
        raw={
            "positions": [
                {
                    "instrument_id": "SOL-USD-PERP.HYPERLIQUID",
                    "position_side": "LONG",
                    "signed_decimal_qty": "0.20",
                    "report_id": "position-report-1",
                }
            ],
            "provenance": _provenance(),
        },
    )

    position = envelope.data[0]
    assert position.instrument_id == "SOL-USD-PERP"
    assert position.signed_quantity == Decimal("0.20")
    assert position.environment is BrokerEnvironment.TESTNET
    assert position.observation_id == "position-report-1"


def test_position_missing_quantity_or_conflicting_side_fails_closed() -> None:
    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        _adapter().map_positions(
            request_id="positions-missing-quantity",
            broker_symbol="SOL",
            raw={"positions": [{"instrument_id": "SOL-USD-PERP.HYPERLIQUID", "report_id": "p"}], "provenance": _provenance()},
        )
    with pytest.raises(ValueError, match="conflicts"):
        _adapter().map_positions(
            request_id="positions-conflict",
            broker_symbol="SOL",
            raw={
                "positions": [{"instrument_id": "SOL-USD-PERP.HYPERLIQUID", "signed_decimal_qty": "0.2", "position_side": "SHORT", "report_id": "p"}],
                "provenance": _provenance(),
            },
        )


def test_incomplete_account_report_is_a_capability_gap() -> None:
    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        _adapter().map_account(
            request_id="account-1",
            raw={"data": {"type": "AccountState", "account_id": "master"}, "provenance": _provenance()},
        )


def test_complete_clearinghouse_account_maps_to_canonical_snapshot() -> None:
    envelope = _adapter().map_account(
        request_id="account-complete-1",
        raw={
            "data": {
                "marginSummary": {
                    "accountValue": "100",
                    "totalRawUsd": "100",
                    "totalMarginUsed": "10",
                    "totalNtlPos": "20",
                },
                "withdrawable": "90",
                "timestamp": 1787461200000,
                "assetPositions": [
                    {
                        "position": {
                            "coin": "SOL",
                            "szi": "0.20",
                            "leverage": {"type": "cross", "value": "5"},
                            "positionId": "position-1",
                        }
                    }
                ],
            },
            "provenance": _provenance(),
        },
    )

    assert envelope.fact_type == "account.snapshot"
    assert envelope.data.environment is BrokerEnvironment.TESTNET
    assert envelope.data.equity == Decimal("100")
    assert envelope.data.positions[0].observation_id == "position-1"


def test_account_identity_mismatch_fails_closed() -> None:
    with pytest.raises(ValueError, match="account_identity_mismatch"):
        _adapter().map_account(
            request_id="account-wrong-identity",
            raw={
                "data": {
                    "marginSummary": {"accountValue": "100", "totalRawUsd": "100"},
                    "timestamp": 1787461200000,
                    "accountAddress": "0x" + "22" * 20,
                    "assetPositions": [],
                },
                "provenance": _provenance(),
            },
        )


def test_fact_provenance_mismatch_fails_closed() -> None:
    invalid = Provenance(
        source="unbound-source",
        execution_scope="hypercore:default",
        transport_state="external_testnet",
        mapping_revision="hyperliquid-testnet-runtime-v1",
        received_at=NOW,
    )
    with pytest.raises(RuntimeBoundaryError, match="fact_provenance_mismatch"):
        _adapter().map_instruments(request_id="instruments-invalid-provenance", raw={"meta": META, "provenance": invalid})


def test_fee_schedule_and_actual_fill_are_separate_canonical_facts() -> None:
    adapter = _adapter()
    schedule = adapter.map_fee_schedule(
        request_id="schedule-1",
        raw={
            "data": {"userAddRate": "0.0001", "userCrossRate": "0.0004"},
            "provenance": _provenance(),
        },
    )
    fill = adapter.map_fill(
        request_id="fill-1",
        broker_symbol="SOL",
        raw={
            "tid": "fill-1",
            "oid": "order-1",
            "coin": "SOL",
            "side": "A",
            "px": "101",
            "sz": "0.20",
            "time": 1787461200000,
            "fee": "0.00808",
            "feeToken": "USDC",
            "crossed": True,
            "provenance": _provenance(),
        },
    )

    assert schedule.data.maker_rate == Decimal("0.0001")
    assert schedule.data.state.value == "estimated"
    assert fill.data.fee.state.value == "actual"
    assert fill.data.fee.currency == "USDC"


def test_funding_is_an_explicit_gap_until_external_transport_exists() -> None:
    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        _adapter().map_funding(request_id="funding-1", raw={"provenance": _provenance()})
