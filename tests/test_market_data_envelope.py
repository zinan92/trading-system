from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from schemas.market_data import MARKET_DATA_ENVELOPE_SCHEMA, MarketDataEnvelope
from services.datafeed_market_mapper import DatafeedContractError, map_candle_response


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _trusted_payload() -> dict:
    return _fixture("trusted_execution_candles_v1.json")


def _map(payload: dict, *, source: str = "binance_usdm_futures", execution: bool = True) -> MarketDataEnvelope:
    return map_candle_response(
        payload,
        expected_asset_class="commodity",
        expected_timeframe="1m",
        expected_source=source,
        require_execution_venue=execution,
    )


def test_trusted_execution_fixture_round_trips_without_losing_trust_fields() -> None:
    envelope = _map(_trusted_payload())

    assert envelope.schema_version == MARKET_DATA_ENVELOPE_SCHEMA
    assert envelope.upstream_schema_version == "kline-candles-v1"
    assert envelope.instrument_id == "GOLD"
    assert envelope.provider_symbol == "XAUUSDT"
    assert envelope.selected_source == "binance_usdm_futures"
    assert envelope.attempted_sources == ("binance_usdm_futures",)
    assert envelope.quality_flags == (
        "public_api",
        "usd_m_futures",
        "live",
        "execution_venue",
    )
    assert envelope.execution_venue is True
    assert envelope.fresh is True
    assert envelope.execution_ready is True
    assert [bar.close for bar in envelope.bars] == [4001.0, 4003.0]
    assert envelope.bars[0].quality_flags == [
        "public_api",
        "usd_m_futures",
        "live",
        "execution_venue",
    ]
    assert envelope.to_dict()["candles"][1]["timestamp"] == "2026-07-18T12:01:00+00:00"


def test_v2_continuous_market_preserves_explicit_session_truth() -> None:
    envelope = _map(_fixture("trusted_execution_candles_v2.json"))

    assert envelope.upstream_schema_version == "kline-candles-v2"
    assert envelope.continuous_market is True
    assert envelope.market_open is True
    assert envelope.session_status == "continuous"
    assert envelope.session_checked_at == "2026-07-18T12:00:05+00:00"
    assert envelope.current_session_end is None
    assert envelope.execution_ready is True


def test_v2_open_sessioned_execution_venue_can_be_execution_ready() -> None:
    envelope = _map(
        _fixture("sessioned_execution_candles_v2.json"),
        source="tiger_openapi_comex",
    )

    assert envelope.continuous_market is False
    assert envelope.market_open is True
    assert envelope.session_status == "open"
    assert envelope.current_session_end == "2026-07-18T20:00:00+00:00"
    assert envelope.execution_ready is True


@pytest.mark.parametrize(
    ("market_open", "session_status", "fresh"),
    [
        (False, "closed", None),
        (None, "unknown", None),
        (True, "open", False),
    ],
)
def test_v2_sessioned_execution_fails_closed_unless_open_and_fresh(
    market_open: bool | None,
    session_status: str,
    fresh: bool | None,
) -> None:
    payload = _fixture("sessioned_execution_candles_v2.json")
    payload["market_open"] = market_open
    payload["session_status"] = session_status
    payload["fresh"] = fresh
    if session_status in {"closed", "unknown"}:
        payload["max_age_seconds"] = None
        payload["current_session_end"] = None
    elif fresh is False:
        payload["age_seconds"] = 600.0
        payload["max_age_seconds"] = 180.0

    envelope = _map(payload, source="tiger_openapi_comex")

    assert envelope.execution_ready is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.pop("continuous_market"), "continuous_market"),
        (lambda payload: payload.pop("market_open"), "market_open"),
        (lambda payload: payload.pop("session_checked_at"), "session_checked_at"),
        (
            lambda payload: payload.update(
                continuous_market=False,
                market_open=True,
                session_status="continuous",
            ),
            "sessioned market",
        ),
        (
            lambda payload: payload.update(
                continuous_market=False,
                market_open=False,
                session_status="closed",
                fresh=None,
                max_age_seconds=None,
            ),
            "current_session_end=null",
        ),
    ],
)
def test_v2_rejects_missing_or_inconsistent_session_truth(mutate, message: str) -> None:
    payload = _fixture("sessioned_execution_candles_v2.json")
    mutate(payload)

    with pytest.raises(DatafeedContractError, match=message):
        _map(payload, source="tiger_openapi_comex")


def test_research_fixture_is_real_but_not_execution_ready() -> None:
    envelope = _map(
        _fixture("research_candles_v1.json"),
        source="yahoo_finance_futures",
        execution=False,
    )

    assert envelope.is_synthetic is False
    assert envelope.instrument_id == "GOLD"
    assert envelope.provider_symbol == "GC=F"
    assert envelope.execution_venue is False
    assert envelope.fresh is None
    assert envelope.execution_ready is False


def test_envelope_header_and_batch_membership_are_immutable() -> None:
    envelope = _map(_trusted_payload())

    assert isinstance(envelope.bars, tuple)
    assert isinstance(envelope.quality_flags, tuple)
    assert isinstance(envelope.access_issues, tuple)
    with pytest.raises(FrozenInstanceError):
        envelope.provider = "tampered"  # type: ignore[misc]
    envelope.bars[0].quality_flags.append("row-local-mutation")
    assert "row-local-mutation" not in envelope.quality_flags


def test_source_mode_is_preserved_as_a_distinct_path_label() -> None:
    payload = _trusted_payload()
    payload["source_mode"] = "binance_usdm_realtime_path"

    envelope = _map(payload)

    assert envelope.selected_source == "binance_usdm_futures"
    assert envelope.source_mode == "binance_usdm_realtime_path"
    assert envelope.execution_ready is True


@pytest.mark.parametrize(
    ("age_seconds", "max_age_seconds"),
    [(181.0, 180.0), (None, 180.0), (5.0, None)],
)
def test_execution_readiness_cross_checks_the_reported_age_window(
    age_seconds: float | None,
    max_age_seconds: float | None,
) -> None:
    payload = _trusted_payload()
    payload["age_seconds"] = age_seconds
    payload["max_age_seconds"] = max_age_seconds

    assert _map(payload).execution_ready is False


def test_unknown_freshness_remains_fail_closed_for_a_sessioned_venue_contract() -> None:
    payload = _trusted_payload()
    payload["provider"] = "tiger_openapi"
    payload["source_mode"] = "tiger_comex_session"
    payload["requested_source"] = "tiger_openapi_comex"
    payload["selected_source"] = "tiger_openapi_comex"
    payload["attempted_sources"] = ["tiger_openapi_comex"]
    payload["provider_symbol"] = "MGC2608"
    payload["fresh"] = None
    payload["age_seconds"] = None
    payload["max_age_seconds"] = None
    for candle in payload["candles"]:
        candle["provider"] = "tiger_openapi"

    envelope = _map(payload, source="tiger_openapi_comex")

    assert envelope.execution_venue is True
    assert envelope.fresh is None
    assert envelope.execution_ready is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema_version="kline-candles-v3"), "schema"),
        (lambda payload: payload.update(count=99), "count"),
        (lambda payload: payload.update(instrument_id=""), "instrument_id"),
        (lambda payload: payload.update(asset_class="crypto"), "asset_class"),
        (lambda payload: payload.update(provider=""), "provider"),
        (lambda payload: payload.update(source_mode=""), "source_mode"),
        (lambda payload: payload.update(selected_source=""), "selected_source"),
        (lambda payload: payload.update(selected_source="other_source"), "selected_source"),
        (lambda payload: payload.update(is_synthetic=True), "synthetic"),
        (lambda payload: payload.update(reject_reason="stale"), "rejected"),
        (lambda payload: payload.update(execution_venue=False), "execution venue"),
        (lambda payload: payload.update(cache_policy="mystery"), "cache_policy"),
        (lambda payload: payload.update(quality_policy="relaxed"), "quality_policy"),
        (lambda payload: payload.update(fallback_policy="explicit"), "fallback_policy"),
        (lambda payload: payload.update(served_from="unknown"), "served_from"),
        (lambda payload: payload.update(attempted_sources=["other_source"]), "attempted_sources"),
    ],
)
def test_contract_metadata_failures_are_rejected(mutate, message: str) -> None:
    payload = _trusted_payload()
    mutate(payload)

    with pytest.raises(DatafeedContractError, match=message):
        _map(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("open", 0, "positive"),
        ("high", 3998, "high"),
        ("low", 4005, "low"),
        ("close", float("nan"), "finite"),
        ("volume", -1, "volume"),
    ],
)
def test_invalid_candle_geometry_fails_closed(field: str, value: float, message: str) -> None:
    payload = _trusted_payload()
    payload["candles"][0][field] = value

    with pytest.raises(DatafeedContractError, match=message):
        _map(payload)


def test_unordered_or_duplicate_timestamps_fail_closed() -> None:
    payload = _trusted_payload()
    payload["candles"][1]["timestamp"] = payload["candles"][0]["timestamp"]

    with pytest.raises(DatafeedContractError, match="chronological"):
        _map(payload)


def test_equivalent_latest_timestamp_encoding_is_accepted() -> None:
    payload = _trusted_payload()
    payload["latest_timestamp"] = "2026-07-18T12:01:00Z"

    envelope = _map(payload)

    assert envelope.latest_timestamp == "2026-07-18T12:01:00Z"
    assert envelope.execution_ready is True


def test_latest_timestamp_must_identify_the_final_candle() -> None:
    payload = _trusted_payload()
    payload["latest_timestamp"] = "2026-07-18T12:00:00+00:00"

    with pytest.raises(DatafeedContractError, match="latest_timestamp"):
        _map(payload)


def test_requested_timeframe_must_match_the_response() -> None:
    payload = _trusted_payload()
    payload["timeframe"] = "5m"

    with pytest.raises(DatafeedContractError, match="timeframe mismatch"):
        _map(payload)


def test_intraday_timestamps_must_include_timezone() -> None:
    payload = _trusted_payload()
    payload["candles"][0]["timestamp"] = "2026-07-18T12:00:00"

    with pytest.raises(DatafeedContractError, match="timezone"):
        _map(payload)
