from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.datafeed_market_client import DatafeedUnavailable
from services.dualtrack_market_feed import DualTrackMarketFeed
from services.market_store import MarketStore


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


def _trusted_v2_payload() -> dict:
    return json.loads(
        (FIXTURES / "trusted_execution_candles_v2.json").read_text(
            encoding="utf-8"
        )
    )


def _config() -> dict:
    return {
        "local_market_db": "data/market_data.db",
        "tiger_futures_feed": {
            "provider": "tiger_openapi:COMEX",
            "contract": "MGCmain",
            "output_symbol": "MGCmain",
            "timeframe": "1m",
        },
        "binance_usdm_1m_feed": {
            "provider": "binance_usdm",
            "output_symbol": "GOLD",
            "timeframe": "1m",
        },
    }


class _FakeDatafeedClient:
    base_url = "http://datafeed.test"

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or _trusted_v2_payload()
        self.calls = 0

    def candles(self, **kwargs):
        self.calls += 1
        assert kwargs["source"] == "binance_usdm_futures"
        assert kwargs["require_execution_venue"] is True
        return deepcopy(self.payload)


class _UnavailableDatafeedClient:
    base_url = "http://datafeed.test"

    def __init__(self) -> None:
        self.calls = 0

    def candles(self, **_kwargs):
        self.calls += 1
        raise DatafeedUnavailable("datafeed HTTP 502: upstream_error")


def test_dualtrack_market_feed_consumes_datafeed_port_without_private_db(tmp_path: Path):
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }
    client = _FakeDatafeedClient()
    payload = DualTrackMarketFeed(
        market_db=tmp_path / "must-not-exist.db",
        config=config,
        datafeed_client=client,
    ).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=10,
        as_of="2026-07-18T12:00:05+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["source_mode"] == "binance_usdm_futures"
    assert payload["provider_symbol"] == "XAUUSDT"
    assert payload["latest_close"] == 4001.0
    assert payload["safety"]["reads_private_market_db"] is False
    assert payload["market_data_contract_shadow"]["status"] == "pass"
    assert payload["market_data_contract_shadow"]["authoritative"] == "legacy"
    assert client.calls == 1


def test_datafeed_historical_page_is_trusted_but_never_fresh(
    tmp_path: Path,
) -> None:
    payload = _trusted_v2_payload()
    template = payload["candles"][0]
    payload["candles"] = [
        {**template, "timestamp": f"2026-07-18T11:{minute:02d}:00+00:00"}
        for minute in (57, 58, 59)
    ]
    payload.update(
        count=3,
        latest_timestamp="2026-07-18T11:59:00+00:00",
        fresh=False,
        age_seconds=60.0,
        cache_policy="require",
        quality_policy="standard",
        served_from="cache",
    )

    class HistoricalClient(_FakeDatafeedClient):
        def candles(self, **kwargs):
            self.kwargs = dict(kwargs)
            return super().candles(**kwargs)

    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }
    client = HistoricalClient(payload)

    result = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=2,
        end="2026-07-18T12:00:00+00:00",
        as_of="2026-07-21T12:00:00+00:00",
    )

    assert client.kwargs["end"] == "2026-07-18T12:00:00+00:00"
    assert client.kwargs["limit"] == 3
    assert client.kwargs["cache_policy"] == "require"
    assert client.kwargs["quality"] == "standard"
    assert result["status"] == "ready"
    assert result["fresh"] is False
    assert result["historical_page"] is True
    assert result["trusted_history"] is True
    assert [row["timestamp"] for row in result["bars"]] == [
        "2026-07-18T11:58:00+00:00",
        "2026-07-18T11:59:00+00:00",
    ]
    assert result["pagination"]["has_more"] is True
    assert result["pagination"]["next_before"] == "2026-07-18T11:58:00+00:00"


def test_shadow_contract_mode_preserves_historical_page_metadata(
    tmp_path: Path,
) -> None:
    payload = _trusted_v2_payload()
    template = payload["candles"][0]
    payload["candles"] = [
        {**template, "timestamp": f"2026-07-18T11:{minute:02d}:00+00:00"}
        for minute in (57, 58, 59)
    ]
    payload.update(
        count=3,
        latest_timestamp="2026-07-18T11:59:00+00:00",
        fresh=False,
        cache_policy="require",
        quality_policy="standard",
        served_from="cache",
    )
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    result = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=_FakeDatafeedClient(payload),
    ).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=2,
        end="2026-07-18T12:00:00+00:00",
        as_of="2026-07-21T12:00:00+00:00",
    )

    assert result["market_data_contract_shadow"]["authoritative"] == "legacy"
    assert result["historical_page"] is True
    assert result["trusted_history"] is True
    assert result["fresh"] is False
    assert result["pagination"]["has_more"] is True
    assert [row["timestamp"] for row in result["bars"]] == [
        "2026-07-18T11:58:00+00:00",
        "2026-07-18T11:59:00+00:00",
    ]


def test_live_datafeed_retries_the_same_source_once_after_transient_failure(
    tmp_path: Path,
) -> None:
    class TransientClient(_FakeDatafeedClient):
        def candles(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise DatafeedUnavailable("transient timeout")
            return deepcopy(self.payload)

    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }
    client = TransientClient()

    result = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert client.calls == 2
    assert result["status"] == "ready"


def test_datafeed_contract_defaults_to_authoritative_after_cutover(
    tmp_path: Path,
) -> None:
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
    }
    client = _FakeDatafeedClient()

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert "market_data_contract_shadow" not in payload
    assert payload["market_data_contract"]["execution_ready"] is True
    assert payload["market_data_contract_comparison"]["authoritative"] == (
        "envelope"
    )
    assert client.calls == 1


def test_canonical_pipeline_selects_authoritative_market_envelope() -> None:
    pipeline = json.loads(
        (Path(__file__).parents[1] / "configs" / "pipeline.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert pipeline["datafeed"]["market_data_contract_mode"] == (
        "authoritative"
    )


def test_default_authority_blocks_upstream_failure_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
    }
    client = _UnavailableDatafeedClient()

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot()

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 0
    assert "market_data_contract_shadow" not in payload
    assert payload["market_data_contract"]["mode"] == "authoritative"
    assert "HTTP 502" in payload["market_data_contract"]["error"]
    assert client.calls == 2


def test_datafeed_live_attempts_can_be_bounded_for_read_only_consumers(tmp_path: Path) -> None:
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "live_request_attempts": 1,
    }
    client = _UnavailableDatafeedClient()

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert client.calls == 1


def test_datafeed_shadow_reports_drift_but_keeps_legacy_authoritative(tmp_path: Path) -> None:
    raw = _trusted_v2_payload()
    raw["candles"][0]["quality_flags"] = ["execution_venue"]
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": client.base_url,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "ready"
    assert payload["bars"][0]["quality_flags"] == ["execution_venue"]
    assert payload["market_data_contract_shadow"]["status"] == "drift"
    assert payload["market_data_contract_shadow"]["authoritative"] == "legacy"
    assert client.calls == 1


def test_datafeed_shadow_sandboxes_an_unexpected_comparison_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import services.dualtrack_market_feed as market_feed_module

    client = _FakeDatafeedClient()
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    def explode(*_args, **_kwargs):
        raise RuntimeError("comparison exploded")

    monkeypatch.setattr(market_feed_module, "compare_market_payloads", explode)
    payload = market_feed_module.DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "ready"
    assert payload["bar_count"] == 1
    assert payload["market_data_contract_shadow"]["status"] == "blocked"
    assert payload["market_data_contract_shadow"]["error"] == "comparison exploded"
    assert client.calls == 1


def test_datafeed_shadow_blocks_candidate_contract_without_changing_legacy(tmp_path: Path) -> None:
    raw = _trusted_v2_payload()
    raw.pop("schema_version")
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "ready"
    assert payload["bar_count"] == 1
    assert payload["market_data_contract_shadow"]["status"] == "blocked"
    assert "schema_version" in payload["market_data_contract_shadow"]["error"]
    assert client.calls == 1


def test_datafeed_shadow_preserves_upstream_block_and_records_no_candidate(tmp_path: Path) -> None:
    client = _UnavailableDatafeedClient()
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot()

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 0
    assert payload["market_data_contract_shadow"]["status"] == "blocked"
    assert "HTTP 502" in payload["market_data_contract_shadow"]["error"]
    assert client.calls == 2


def test_datafeed_authoritative_mode_returns_envelope_projection(tmp_path: Path) -> None:
    client = _FakeDatafeedClient()
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": client.base_url,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "ready"
    assert payload["market_data_contract"]["execution_ready"] is True
    assert payload["market_data_contract_comparison"]["status"] == "pass"
    assert payload["market_data_contract_comparison"]["authoritative"] == "envelope"
    assert client.calls == 1


def test_authoritative_mode_bridges_known_exact_source_v1_identity_gap(
    tmp_path: Path,
) -> None:
    raw = json.loads(
        (FIXTURES / "trusted_execution_candles_v1.json").read_text(
            encoding="utf-8"
        )
    )
    for key in (
        "instrument_id",
        "provider_symbol",
        "selected_source",
        "attempted_sources",
        "selection_reason",
    ):
        raw.pop(key)
    for row in raw["candles"]:
        row["timestamp"] = row["timestamp"].removesuffix("+00:00")
    raw["latest_timestamp"] = raw["latest_timestamp"].removesuffix("+00:00")
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(
        symbol="GOLD",
        timeframe="1m",
        as_of="2026-07-18T12:01:05+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["symbol"] == "GOLD"
    assert payload["provider_symbol"] == "XAUUSDT"
    assert payload["market_data_contract"]["execution_ready"] is True


def test_exact_source_v1_bridge_refuses_source_mismatch(tmp_path: Path) -> None:
    raw = json.loads(
        (FIXTURES / "trusted_execution_candles_v1.json").read_text(
            encoding="utf-8"
        )
    )
    raw.pop("instrument_id")
    raw["source_mode"] = "unexpected_source"
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert payload["market_data_contract"]["status"] == "blocked"


def test_datafeed_authoritative_mode_never_falls_back_on_invalid_contract(tmp_path: Path) -> None:
    raw = _trusted_v2_payload()
    raw.pop("schema_version")
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 0
    assert payload["market_data_contract"]["status"] == "blocked"
    assert payload["market_data_contract"]["mode"] == "authoritative"
    assert client.calls == 1


def test_datafeed_authoritative_mode_fails_closed_on_projection_bug(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import services.dualtrack_market_feed as market_feed_module

    client = _FakeDatafeedClient()
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    def explode(*_args, **_kwargs):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(
        market_feed_module,
        "project_dualtrack_market_payload",
        explode,
    )
    payload = market_feed_module.DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 0
    assert payload["market_data_contract"]["status"] == "blocked"
    assert payload["market_data_contract"]["error"] == "projection exploded"
    assert client.calls == 1


def test_datafeed_shadow_has_exact_policy_parity_for_five_minute_bars(tmp_path: Path) -> None:
    raw = _trusted_v2_payload()
    raw["timeframe"] = "5m"
    raw["max_age_seconds"] = 900.0
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "shadow",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(
        symbol="GOLD",
        timeframe="5m",
        as_of="2026-07-18T12:00:05+00:00",
    )

    assert payload["max_age_minutes"] == 15.0
    assert payload["market_data_contract_shadow"]["status"] == "pass"
    assert client.calls == 1


def test_datafeed_authoritative_mode_blocks_a_closed_session(tmp_path: Path) -> None:
    raw = json.loads(
        (FIXTURES / "sessioned_execution_candles_v2.json").read_text(
            encoding="utf-8"
        )
    )
    raw.update(
        requested_source="binance_usdm_futures",
        selected_source="binance_usdm_futures",
        attempted_sources=["binance_usdm_futures"],
        market_open=False,
        session_status="closed",
        current_session_end=None,
        fresh=None,
        max_age_seconds=None,
    )
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 1
    assert payload["market_data_contract"]["session_status"] == "closed"
    assert client.calls == 1


def test_datafeed_authoritative_mode_blocks_an_unknown_session(tmp_path: Path) -> None:
    raw = json.loads(
        (FIXTURES / "sessioned_execution_candles_v2.json").read_text(
            encoding="utf-8"
        )
    )
    raw.update(
        requested_source="binance_usdm_futures",
        selected_source="binance_usdm_futures",
        attempted_sources=["binance_usdm_futures"],
        market_open=None,
        session_status="unknown",
        current_session_end=None,
        fresh=None,
        max_age_seconds=None,
    )
    client = _FakeDatafeedClient(raw)
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
        "market_data_contract_mode": "authoritative",
    }

    payload = DualTrackMarketFeed(
        market_db=tmp_path / "unused.db",
        config=config,
        datafeed_client=client,
    ).snapshot(as_of="2026-07-18T12:00:05+00:00")

    assert payload["status"] == "blocked"
    assert payload["bar_count"] == 1
    assert payload["market_data_contract"]["session_status"] == "unknown"
    assert client.calls == 1


def test_datafeed_rejects_unknown_market_contract_mode(tmp_path: Path) -> None:
    config = _config()
    config["datafeed"] = {"market_data_contract_mode": "sometimes"}

    with pytest.raises(ValueError, match="market_data_contract_mode"):
        DualTrackMarketFeed(market_db=tmp_path / "unused.db", config=config)


def _bars(symbol: str, provider: str, start_price: float, *, start: datetime | None = None) -> list[Bar]:
    start = start or datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    rows = []
    price = start_price
    for i in range(3):
        close = price + i + 1
        rows.append(
            Bar(
                symbol=symbol,
                timeframe="1m",
                timestamp=(start + timedelta(minutes=i)).isoformat(),
                open=price,
                high=close + 1,
                low=price - 1,
                close=close,
                volume=10,
                provider=provider,
                quality_flags=[],
            )
        )
        price = close
    return rows


def test_dualtrack_market_feed_uses_configured_binance_bars_not_tiger_cache(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars(_bars("GOLD", "binance_usdm", 4000))
    store.upsert_bars(_bars("MGCmain", "tiger_openapi:COMEX", 4100))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["source_mode"] == "binance_usdm"
    assert payload["symbol"] == "GOLD"
    assert payload["provider"] == "binance_usdm"
    assert payload["quality_flags"] == []
    assert payload["is_synthetic"] is False
    assert payload["fresh"] is True
    assert payload["bar_count"] == 2
    assert [row["close"] for row in payload["bars"]] == [4003.0, 4006.0]
    assert payload["safety"] == {
        "read_only": True,
        "writes_market_db": False,
        "opens_broker_clients": False,
        "opens_order_clients": False,
        "uses_browser_exchange_socket": False,
    }


def test_dualtrack_market_feed_does_not_switch_source_when_primary_is_absent(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    MarketStore(db).upsert_bars(_bars("MGCmain", "tiger_openapi:COMEX", 4100))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["status"] == "blocked"
    assert payload["source_mode"] == "unavailable"
    assert payload["bar_count"] == 0
    assert payload["is_synthetic"] is False


def test_dualtrack_market_feed_has_one_configured_default_source_not_a_fallback_chain(tmp_path: Path) -> None:
    feed = DualTrackMarketFeed(market_db=tmp_path / "market_data.db", config=_config())

    assert feed._candidates(symbol=None, timeframe=None) == [{
        "symbol": "GOLD",
        "timeframe": "1m",
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
    }]


def test_dualtrack_market_feed_surfaces_stale_primary_without_switching_source(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars(_bars("GOLD", "binance_usdm", 4000))
    store.upsert_bars(_bars("MGCmain", "tiger_openapi:COMEX", 4100, start=datetime(2026, 7, 6, 3, 28, tzinfo=timezone.utc)))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-06T03:30:00+00:00",
    )

    assert payload["status"] == "stale"
    assert payload["source_mode"] == "binance_usdm"
    assert payload["symbol"] == "GOLD"
    assert payload["provider"] == "binance_usdm"
    assert payload["fresh"] is False
    assert payload["is_synthetic"] is False


def test_dualtrack_market_feed_requested_gold_one_minute_returns_real_rows(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars(_bars("GOLD", "binance_usdm", 4000))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["source_mode"] == "requested_symbol"
    assert payload["symbol"] == "GOLD"
    assert payload["timeframe"] == "1m"
    assert payload["provider"] == "binance_usdm"
    assert payload["bar_count"] == 2
    assert payload["fresh"] is True
    assert payload["is_synthetic"] is False
    assert payload["requested"] == {"symbol": "GOLD", "timeframe": "1m", "limit": 2}


def test_dualtrack_market_feed_marks_one_minute_data_stale_after_three_minutes(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    MarketStore(db).upsert_bars(_bars("GOLD", "binance_usdm", 4000))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=2,
        as_of="2026-07-05T01:06:00+00:00",
    )

    assert payload["latest_timestamp"] == "2026-07-05T01:02:00+00:00"
    assert payload["age_minutes"] == 4.0
    assert payload["max_age_minutes"] == 3.0
    assert payload["status"] == "stale"
    assert payload["fresh"] is False


def test_dualtrack_market_feed_resolves_relative_config_db_from_repo_root(tmp_path: Path, monkeypatch) -> None:
    import services.dualtrack_market_feed as market_feed_module

    db = tmp_path / "data" / "market_data.db"
    MarketStore(db).upsert_bars(_bars("GOLD", "binance_usdm", 4000))
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    monkeypatch.setattr(market_feed_module, "ROOT", tmp_path)
    monkeypatch.chdir(other_cwd)

    payload = market_feed_module.DualTrackMarketFeed(config=_config()).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["market_db"] == str(db)
    assert payload["symbol"] == "GOLD"
    assert payload["provider"] == "binance_usdm"
    assert payload["bar_count"] == 2
    assert payload["is_synthetic"] is False


def test_dualtrack_market_feed_derives_context_timeframes_from_one_minute_bars(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    rows = []
    start = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    for index in range(60):
        price = 4000 + index
        rows.append(
            Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open=price,
                high=price + 2,
                low=price - 1,
                close=price + 1,
                volume=10,
                provider="binance_usdm",
                quality_flags=[],
            )
        )
    MarketStore(db).upsert_bars(rows)

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="15m",
        limit=4,
        as_of="2026-07-06T04:00:00+00:00",
    )

    assert payload["status"] == "derived"
    assert payload["source_mode"] == "derived_from_1m"
    assert payload["symbol"] == "GOLD"
    assert payload["timeframe"] == "15m"
    assert payload["provider"] == "derived:binance_usdm"
    assert payload["is_synthetic"] is False
    assert payload["bar_count"] == 4
    assert payload["bars"][0]["timestamp"] == "2026-07-06T03:00:00+00:00"
    assert payload["bars"][0]["open"] == 4000.0
    assert payload["bars"][0]["high"] == 4016.0
    assert payload["bars"][0]["low"] == 3999.0
    assert payload["bars"][0]["close"] == 4015.0
    assert payload["fresh"] is True
    assert "derived_source:GOLD:1m->15m" in payload["access_issues"]


def test_dualtrack_market_feed_can_derive_daily_strategy_bars(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    rows = []
    start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    for index in range(22 * 24 * 60):
        price = 4000 + (index % 1440) * 0.01 + (index // 1440)
        rows.append(
            Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + 0.25,
                volume=10,
                provider="binance_usdm",
                quality_flags=[],
            )
        )
    MarketStore(db).upsert_bars(rows)

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="1d",
        limit=20,
        as_of="2026-06-23T00:01:00+00:00",
    )

    assert payload["status"] == "derived"
    assert payload["timeframe"] == "1d"
    assert payload["source_mode"] == "derived_from_1m"
    assert payload["bar_count"] == 20
    assert payload["bars"][-1]["timestamp"] == "2026-06-22T00:00:00+00:00"
    assert payload["is_synthetic"] is False


def test_dualtrack_market_feed_keeps_stale_explicit_derived_symbol_instead_of_seed(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    rows = []
    start = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    for index in range(20):
        price = 4000 + index
        rows.append(
            Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open=price,
                high=price + 2,
                low=price - 1,
                close=price + 1,
                volume=10,
                provider="binance_usdm",
                quality_flags=[],
            )
        )
    MarketStore(db).upsert_bars(rows)

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="15m",
        limit=2,
        as_of="2026-07-06T05:00:00+00:00",
    )

    assert payload["status"] == "stale"
    assert payload["source_mode"] == "derived_from_1m"
    assert payload["symbol"] == "GOLD"
    assert payload["timeframe"] == "15m"
    assert payload["is_synthetic"] is False
    assert payload["bar_count"] == 2
    assert "derived_source:GOLD:1m->15m" in payload["access_issues"]


def test_dualtrack_market_feed_derives_from_the_configured_binance_source(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    rows = []
    start = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    for index in range(10):
        price = 4000 + index
        rows.append(
            Bar(
                symbol="GOLD",
                timeframe="1m",
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open=price,
                high=price + 2,
                low=price - 1,
                close=price + 1,
                volume=10,
                provider="binance_usdm",
                quality_flags=[],
            )
        )
    MarketStore(db).upsert_bars(rows)

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        timeframe="5m",
        limit=2,
        as_of="2026-07-06T03:10:00+00:00",
    )

    assert payload["status"] == "derived"
    assert payload["source_mode"] == "derived_from_1m"
    assert payload["symbol"] == "GOLD"
    assert payload["bar_count"] == 2
    assert payload["requested"] == {"symbol": "", "timeframe": "5m", "limit": 2}


def test_dualtrack_market_feed_prefers_fresh_same_provider_aggregation_over_stale_exact_timeframe(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    rows = []
    start = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    for index in range(15):
        price = 4000 + index
        rows.append(Bar(symbol="GOLD", timeframe="1m", timestamp=(start + timedelta(minutes=index)).isoformat(), open=price, high=price + 2, low=price - 1, close=price + 1, volume=10, provider="binance_usdm", quality_flags=[]))
    rows.append(Bar(symbol="GOLD", timeframe="5m", timestamp="2026-07-05T01:00:00+00:00", open=3900, high=3902, low=3899, close=3901, volume=10, provider="binance_usdm", quality_flags=[]))
    MarketStore(db).upsert_bars(rows)

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        symbol="GOLD",
        timeframe="5m",
        limit=3,
        as_of="2026-07-06T03:15:00+00:00",
    )

    assert payload["status"] == "derived"
    assert payload["fresh"] is True
    assert payload["source_mode"] == "derived_from_1m"
    assert payload["provider"] == "derived:binance_usdm"
    assert payload["latest_timestamp"] == "2026-07-06T03:10:00+00:00"
    assert "stale_exact_source:GOLD:5m:2026-07-05T01:00:00+00:00" in payload["access_issues"]


def test_dualtrack_market_feed_missing_db_blocks_without_creating_data(tmp_path: Path) -> None:
    db = tmp_path / "missing" / "market_data.db"

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=4,
        as_of="2026-07-05T01:03:59+00:00",
    )

    assert payload["status"] == "blocked"
    assert payload["source_mode"] == "unavailable"
    assert payload["is_synthetic"] is False
    assert payload["quality_flags"] == ["market_unavailable"]
    assert payload["bar_count"] == 0
    assert payload["latest_timestamp"] == ""
    assert "market_db_missing" in payload["access_issues"]
    assert payload["safety"]["writes_market_db"] is False
    assert not db.exists()
