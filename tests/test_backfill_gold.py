from pathlib import Path

from pipelines.backfill_gold import backfill_gold_5m
from schemas.market_data import Bar
from services.journal_store import load_json
from services.market_store import MarketStore


def test_backfill_gold_5m_writes_bars_and_log(tmp_path: Path, monkeypatch):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    def fake_fetch(self, yahoo_symbol: str, output_symbol: str, range_value: str, interval: str):
        return [
            Bar(output_symbol, interval, "2026-05-22T20:50:00+00:00", 4520, 4522, 4519, 4521, 100, f"yahoo_chart:{yahoo_symbol}", ["historical_5m", "futures_proxy"]),
            Bar(output_symbol, interval, "2026-05-22T20:55:00+00:00", 4521, 4524, 4520, 4523.2, 120, f"yahoo_chart:{yahoo_symbol}", ["historical_5m", "futures_proxy"]),
        ]

    monkeypatch.setattr("pipelines.backfill_gold.YahooChartClient.fetch_5m_bars", fake_fetch)

    result = backfill_gold_5m("2026-05-26", yahoo_symbol="GC=F", range_value="5d")

    assert result["imported_rows"] == 2
    assert result["provider"] == "yahoo_chart:GC=F"
    assert MarketStore(db_path).load_bars("GOLD", "5m", 10)[-1].close == 4523.2
    logs = load_json(output_root / "backfills" / "2026-05-26.json")
    assert logs[0]["last_timestamp"] == "2026-05-22T20:55:00+00:00"
