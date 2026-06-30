from pathlib import Path

from schemas.asset import Asset
from schemas.market_data import Bar
from services.kline_client import KlineClient
from services.market_store import MarketStore


def test_gold_5m_uses_latest_official_broker_bars_without_public_snapshot(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "market.db"
    store = MarketStore(db_path)
    bars = [
        Bar(
            "GOLD",
            "5m",
            f"2026-05-26T01:{index:02d}:00+00:00",
            4570 + index,
            4571 + index,
            4569 + index,
            4570.5 + index,
            10,
            "mt5_csv",
            ["csv_import"],
        )
        for index in range(20)
    ]
    store.upsert_bars(bars)

    def fail_public_snapshot(*args, **kwargs):
        raise AssertionError("public gold API should not be called when latest local bar is official")

    monkeypatch.setattr("urllib.request.urlopen", fail_public_snapshot)
    client = KlineClient(
        "local_gold_api",
        fallback_to_mock=False,
        local_db_path=db_path,
        official_gold_5m_providers=["mt5_csv"],
    )

    fetched = client.fetch(Asset("GOLD", "Gold", "commodity", "high", "UTC"), timeframe="5m", limit=20)

    assert len(fetched) == 20
    assert fetched[-1].provider == "mt5_csv"
    assert fetched[-1].close == 4589.5


def test_gold_api_snapshot_is_saved_as_quote_when_it_would_create_gap(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "market.db"
    store = MarketStore(db_path)
    bars = [
        Bar(
            "GOLD",
            "5m",
            f"2026-05-26T01:{index:02d}:00+00:00",
            4570 + index,
            4571 + index,
            4569 + index,
            4570.5 + index,
            10,
            "yahoo_chart:GC=F",
            ["historical_5m"],
        )
        for index in range(20)
    ]
    store.upsert_bars(bars)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4538.4")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-26T03:00:00+00:00")
    client = KlineClient("local_gold_api", fallback_to_mock=False, local_db_path=db_path)

    fetched = client.fetch(Asset("GOLD", "Gold", "commodity", "high", "UTC"), timeframe="5m", limit=20)

    assert fetched[-1].timestamp == "2026-05-26T01:19:00+00:00"
    assert fetched[-1].provider == "yahoo_chart:GC=F"
    assert MarketStore(db_path).load_latest_quote("GOLD")["close"] == 4538.4
    assert MarketStore(db_path).load_bars("GOLD", "5m", 30)[-1].timestamp == "2026-05-26T01:19:00+00:00"


def test_existing_gold_api_live_snapshots_are_not_used_as_5m_bars(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "market.db"
    store = MarketStore(db_path)
    stable = [
        Bar("GOLD", "5m", f"2026-05-26T01:{index:02d}:00+00:00", 4570, 4571, 4569, 4570 + index, 10, "yahoo_chart:GC=F", ["historical_5m"])
        for index in range(20)
    ]
    store.upsert_bars(stable + [Bar("GOLD", "5m", "2026-05-26T03:00:00+00:00", 4538, 4538, 4538, 4538, 0, "gold-api.com", ["live_snapshot"])])
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4537.9")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-26T03:05:00+00:00")
    client = KlineClient("local_gold_api", fallback_to_mock=False, local_db_path=db_path)

    fetched = client.fetch(Asset("GOLD", "Gold", "commodity", "high", "UTC"), timeframe="5m", limit=30)

    assert fetched[-1].provider == "yahoo_chart:GC=F"
    assert all(bar.provider != "gold-api.com" for bar in fetched)


def test_gold_api_snapshot_is_quote_only_even_for_paper(monkeypatch, tmp_path: Path):
    """Spot snapshots (V=0, H=L=O=C) are point quotes, never real 5m bars.

    Even with allow_public_snapshot_bar_for_paper=True the live price must land
    in the quotes table only — writing it as a degenerate candle perpetuated as
    the latest row and injected chart/audit zigzag. Real OHLC comes from the
    binance_usdm proxy feed / yahoo backfill.
    """
    db_path = tmp_path / "market.db"
    store = MarketStore(db_path)
    stable = [
        Bar("GOLD", "5m", f"2026-05-26T01:{index:02d}:00+00:00", 4570, 4571, 4569, 4570 + index, 10, "yahoo_chart:GC=F", ["historical_5m"])
        for index in range(20)
    ]
    store.upsert_bars(stable)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4538.4")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-26T03:05:00+00:00")
    client = KlineClient(
        "local_gold_api",
        fallback_to_mock=False,
        local_db_path=db_path,
        allow_public_snapshot_bar_for_paper=True,
    )

    fetched = client.fetch(Asset("GOLD", "Gold", "commodity", "high", "UTC"), timeframe="5m", limit=30)

    # Latest bar stays the last real OHLC bar; no degenerate snapshot bar added.
    assert fetched[-1].provider == "yahoo_chart:GC=F"
    assert fetched[-1].close == 4589
    assert all(bar.provider == "yahoo_chart:GC=F" for bar in fetched)
    assert all("quote_derived_bar" not in bar.quality_flags for bar in fetched)
    # The live price is still captured — as a quote, not a bar.
    assert MarketStore(db_path).load_latest_quote("GOLD")["close"] == 4538.4
    assert len(MarketStore(db_path).load_bars("GOLD", "5m", 50)) == 20
