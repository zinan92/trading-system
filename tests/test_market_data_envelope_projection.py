from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from services.datafeed_market_mapper import map_candle_response
from services.market_data_envelope_projection import (
    compare_market_payloads,
    project_dualtrack_market_payload,
)


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


def _payload(name: str = "trusted_execution_candles_v2.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _envelope(payload: dict | None = None, *, source: str = "binance_usdm_futures"):
    return map_candle_response(
        payload or _payload(),
        expected_asset_class="commodity",
        expected_timeframe="1m",
        expected_source=source,
        require_execution_venue=True,
    )


def _project(envelope=None, *, checked_at: str = "2026-07-18T12:00:05+00:00") -> dict:
    return project_dualtrack_market_payload(
        envelope or _envelope(),
        requested={"symbol": "GOLD", "timeframe": "1m", "limit": 1},
        datafeed_url="http://datafeed.test",
        checked_at=datetime.fromisoformat(checked_at),
    )


def test_projection_preserves_dualtrack_shape_and_trust_receipt() -> None:
    projected = _project()

    assert projected["schema_version"] == "dualtrack-market-bars-v1"
    assert projected["status"] == "ready"
    assert projected["symbol"] == "GOLD"
    assert projected["provider_symbol"] == "XAUUSDT"
    assert projected["source_mode"] == "binance_usdm_futures"
    assert projected["bar_count"] == 1
    assert projected["latest_close"] == 4001.0
    assert projected["fresh"] is True
    assert projected["bars"][0]["provider_symbol"] == "XAUUSDT"
    assert projected["market_data_contract"]["execution_ready"] is True
    assert projected["market_data_contract"]["session_status"] == "continuous"


def test_projection_rechecks_latest_bar_age_at_the_consumer_boundary() -> None:
    projected = _project(checked_at="2026-07-18T12:04:00+00:00")

    assert projected["status"] == "stale"
    assert projected["fresh"] is False
    assert projected["age_minutes"] == 4.0
    assert projected["market_data_contract"]["execution_ready"] is True
    assert projected["market_data_contract"]["consumer_fresh"] is False


def test_historical_projection_is_trusted_for_display_but_not_fresh_for_execution() -> None:
    projected = project_dualtrack_market_payload(
        _envelope(),
        requested={"symbol": "GOLD", "timeframe": "1m", "limit": 1},
        datafeed_url="http://datafeed.test",
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        historical=True,
    )

    assert projected["status"] == "ready"
    assert projected["historical_page"] is True
    assert projected["trusted_history"] is True
    assert projected["fresh"] is False
    assert projected["market_data_contract"]["consumer_fresh"] is False


def test_historical_projection_rejects_non_execution_venue_display_trust() -> None:
    projected = project_dualtrack_market_payload(
        replace(_envelope(), execution_venue=False),
        requested={"symbol": "GOLD", "timeframe": "1m", "limit": 1},
        datafeed_url="http://datafeed.test",
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        historical=True,
    )

    assert projected["historical_page"] is True
    assert projected["trusted_history"] is False
    assert projected["fresh"] is False


def test_historical_projection_rejects_synthetic_display_trust() -> None:
    projected = project_dualtrack_market_payload(
        replace(_envelope(), is_synthetic=True),
        requested={"symbol": "GOLD", "timeframe": "1m", "limit": 1},
        datafeed_url="http://datafeed.test",
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        historical=True,
    )

    assert projected["historical_page"] is True
    assert projected["trusted_history"] is False
    assert projected["fresh"] is False


def test_session_closed_is_blocked_instead_of_mislabeled_stale() -> None:
    raw = _payload("sessioned_execution_candles_v2.json")
    raw.update(
        market_open=False,
        session_status="closed",
        current_session_end=None,
        fresh=None,
        max_age_seconds=None,
    )
    envelope = _envelope(raw, source="tiger_openapi_comex")

    projected = _project(envelope)

    assert projected["status"] == "blocked"
    assert projected["fresh"] is False
    assert projected["market_data_contract"]["session_status"] == "closed"


def test_sessioned_projection_rejects_an_expired_open_session_receipt() -> None:
    raw = _payload("sessioned_execution_candles_v2.json")
    raw["latest_timestamp"] = "2026-07-18T19:59:30+00:00"
    raw["session_checked_at"] = "2026-07-18T19:59:35+00:00"
    raw["current_session_end"] = "2026-07-18T20:00:00+00:00"
    raw["candles"][0]["timestamp"] = raw["latest_timestamp"]
    envelope = _envelope(raw, source="tiger_openapi_comex")

    projected = _project(envelope, checked_at="2026-07-18T20:00:01+00:00")

    assert projected["age_minutes"] == 0.52
    assert projected["status"] == "blocked"
    assert projected["market_data_contract"]["consumer_bar_fresh"] is True
    assert projected["market_data_contract"]["consumer_session_ready"] is False


def test_sessioned_projection_rejects_a_stale_session_check() -> None:
    raw = _payload("sessioned_execution_candles_v2.json")
    raw["latest_timestamp"] = "2026-07-18T12:09:59+00:00"
    raw["candles"][0]["timestamp"] = raw["latest_timestamp"]
    envelope = _envelope(raw, source="tiger_openapi_comex")

    projected = _project(envelope, checked_at="2026-07-18T12:10:00+00:00")

    assert projected["status"] == "blocked"
    assert projected["market_data_contract"]["consumer_bar_fresh"] is True
    assert projected["market_data_contract"]["consumer_session_ready"] is False


def test_exact_comparison_reports_pass_and_stable_digests() -> None:
    legacy = _project()
    candidate = deepcopy(legacy)
    legacy.pop("market_data_contract")

    receipt = compare_market_payloads(legacy, candidate)

    assert receipt["status"] == "pass"
    assert receipt["difference_count"] == 0
    assert receipt["differences"] == []
    assert receipt["digest_status"] == "computed"
    assert receipt["legacy_digest"] == receipt["candidate_digest"]


def test_exact_comparison_reports_all_semantic_drift_without_float_tolerance() -> None:
    legacy = _project()
    candidate = deepcopy(legacy)
    legacy.pop("market_data_contract")
    candidate["bars"][0]["close"] += 0.000001
    candidate["fresh"] = False

    receipt = compare_market_payloads(legacy, candidate)

    assert receipt["status"] == "drift"
    assert receipt["difference_count"] == 2
    assert {row["field"] for row in receipt["differences"]} == {
        "fresh",
        "bars[0].close",
    }
    assert receipt["legacy_digest"] != receipt["candidate_digest"]


def test_large_exact_comparison_skips_diagnostic_digests_without_skipping_fields() -> None:
    row = {
        "symbol": "GOLD",
        "provider_symbol": "XAUUSDT",
        "timeframe": "1m",
        "timestamp": "2026-07-18T12:00:00+00:00",
        "open": 4000.0,
        "high": 4002.0,
        "low": 3999.0,
        "close": 4001.0,
        "volume": 10.0,
        "provider": "binance_usdm_futures",
        "quality_flags": ["execution_venue"],
    }
    legacy = _project()
    candidate = _project()
    legacy.pop("market_data_contract")
    candidate.pop("market_data_contract")
    legacy["bars"] = [dict(row) for _ in range(5_001)]
    candidate["bars"] = [dict(row) for _ in range(5_001)]
    legacy["bar_count"] = candidate["bar_count"] = 5_001

    receipt = compare_market_payloads(legacy, candidate)

    assert receipt["status"] == "pass"
    assert receipt["comparison_count"] == 55_029
    assert receipt["digest_status"] == "skipped_large_batch"
    assert receipt["legacy_digest"] is None
    assert receipt["candidate_digest"] is None
