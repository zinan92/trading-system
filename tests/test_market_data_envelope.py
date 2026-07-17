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


def test_envelope_collections_are_immutable_tuples() -> None:
    envelope = _map(_trusted_payload())

    assert isinstance(envelope.bars, tuple)
    assert isinstance(envelope.quality_flags, tuple)
    assert isinstance(envelope.access_issues, tuple)
    with pytest.raises(FrozenInstanceError):
        envelope.provider = "tampered"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema_version="kline-candles-v2"), "schema"),
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


def test_error_envelope_cannot_be_mapped_as_candles() -> None:
    payload = _fixture("upstream_error_v1.json")

    with pytest.raises(DatafeedContractError, match="schema"):
        _map(payload)
