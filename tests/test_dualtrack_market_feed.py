from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.dualtrack_market_feed import DualTrackMarketFeed
from services.market_store import MarketStore


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

    def candles(self, **kwargs):
        assert kwargs["source"] == "binance_usdm_futures"
        assert kwargs["require_execution_venue"] is True
        return {
            "provider": "binance_usdm_futures",
            "source_mode": "binance_usdm_futures",
            "selected_source": "binance_usdm_futures",
            "selection_reason": "requested_or_default",
            "attempted_sources": ["binance_usdm_futures"],
            "instrument_id": "GOLD",
            "provider_symbol": "XAUUSDT",
            "quality_flags": ["execution_venue"],
            "is_synthetic": False,
            "access_issues": [],
            "candles": [
                {
                    "timestamp": "2026-07-15T02:00:00+00:00",
                    "open": 4060,
                    "high": 4062,
                    "low": 4059,
                    "close": 4061,
                    "volume": 12,
                    "quality_flags": ["execution_venue"],
                }
            ],
        }


def test_dualtrack_market_feed_consumes_datafeed_port_without_private_db(tmp_path: Path):
    config = _config()
    config["datafeed"] = {
        "enabled": True,
        "base_url": "http://datafeed.test",
        "source": "binance_usdm_futures",
        "asset_class": "commodity",
    }
    payload = DualTrackMarketFeed(
        market_db=tmp_path / "must-not-exist.db",
        config=config,
        datafeed_client=_FakeDatafeedClient(),
    ).snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=10,
        as_of="2026-07-15T02:01:00+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["source_mode"] == "binance_usdm_futures"
    assert payload["provider_symbol"] == "XAUUSDT"
    assert payload["latest_close"] == 4061
    assert payload["safety"]["reads_private_market_db"] is False


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
