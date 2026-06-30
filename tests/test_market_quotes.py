from pathlib import Path

from schemas.market_data import Bar
from services.market_store import MarketStore


def test_market_store_writes_and_loads_latest_quote(tmp_path: Path):
    store = MarketStore(tmp_path / "market.db")
    quote = Bar("GOLD", "5m", "2026-05-26T02:21:45+00:00", 4538.4, 4538.4, 4538.4, 4538.4, 0, "gold-api.com", ["live_quote"])

    store.upsert_quote(quote)

    latest = store.load_latest_quote("GOLD")
    assert latest["symbol"] == "GOLD"
    assert latest["close"] == 4538.4
    assert latest["provider"] == "gold-api.com"
    assert latest["record_type"] == "quote"


def test_market_store_loads_bar_at_or_before_timestamp(tmp_path: Path):
    store = MarketStore(tmp_path / "market.db")
    store.upsert_bars([
        Bar("GOLD", "1m", "2026-06-25T00:00:00+00:00", 3999, 4002, 3998, 4000, 1, "binance_usdm", []),
        Bar("GOLD", "1m", "2026-06-25T00:01:00+00:00", 4000, 4004, 3999, 4003, 1, "binance_usdm", []),
        Bar("GOLD", "1m", "2026-06-25T00:02:00+00:00", 4003, 4005, 4001, 4004, 1, "binance_usdm", []),
    ])

    row = store.load_bar_at_or_before("GOLD", "1m", "2026-06-25T00:01:30+00:00")

    assert row["timestamp"] == "2026-06-25T00:01:00+00:00"
    assert row["close"] == 4003
