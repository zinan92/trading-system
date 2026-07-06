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


def test_dualtrack_market_feed_prefers_tiger_bars_over_binance_cache(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars(_bars("GOLD", "binance_usdm", 4000))
    store.upsert_bars(_bars("MGCmain", "tiger_openapi:COMEX", 4100))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["status"] == "ready"
    assert payload["source_mode"] == "tiger_openapi"
    assert payload["symbol"] == "MGCmain"
    assert payload["provider"] == "tiger_openapi:COMEX"
    assert payload["quality_flags"] == []
    assert payload["is_synthetic"] is False
    assert payload["fresh"] is True
    assert payload["bar_count"] == 2
    assert [row["close"] for row in payload["bars"]] == [4103.0, 4106.0]
    assert payload["safety"] == {
        "read_only": True,
        "writes_market_db": False,
        "opens_broker_clients": False,
        "opens_order_clients": False,
        "uses_browser_exchange_socket": False,
    }


def test_dualtrack_market_feed_falls_back_to_cached_binance_when_tiger_absent(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    MarketStore(db).upsert_bars(_bars("GOLD", "binance_usdm", 4000))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-05T01:03:00+00:00",
    )

    assert payload["status"] == "fallback"
    assert payload["source_mode"] == "binance_usdm_fallback"
    assert payload["symbol"] == "GOLD"
    assert payload["bar_count"] == 2
    assert payload["is_synthetic"] is False


def test_dualtrack_market_feed_skips_stale_tiger_and_uses_fresh_binance(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars(_bars("MGCmain", "tiger_openapi:COMEX", 4100))
    store.upsert_bars(_bars("GOLD", "binance_usdm", 4000, start=datetime(2026, 7, 6, 3, 28, tzinfo=timezone.utc)))

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=2,
        as_of="2026-07-06T03:30:00+00:00",
    )

    assert payload["status"] == "fallback"
    assert payload["source_mode"] == "binance_usdm_fallback"
    assert payload["symbol"] == "GOLD"
    assert payload["provider"] == "binance_usdm"
    assert payload["fresh"] is True
    assert payload["is_synthetic"] is False
    assert any("stale_source:MGCmain:1m" in item for item in payload["access_issues"])


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


def test_dualtrack_market_feed_missing_db_uses_seed_without_creating_db(tmp_path: Path) -> None:
    db = tmp_path / "missing" / "market_data.db"

    payload = DualTrackMarketFeed(market_db=db, config=_config()).snapshot(
        limit=4,
        as_of="2026-07-05T01:03:59+00:00",
    )

    assert payload["status"] == "seeded"
    assert payload["source_mode"] == "synthetic_fallback"
    assert payload["is_synthetic"] is True
    assert "synthetic_seed" in payload["quality_flags"]
    assert payload["bar_count"] == 4
    assert payload["latest_timestamp"] == "2026-07-05T01:03:00+00:00"
    assert payload["safety"]["writes_market_db"] is False
    assert not db.exists()
