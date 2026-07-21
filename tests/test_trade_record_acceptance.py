from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.trade_record_acceptance import TRADE_RECORD_ACCEPTANCE_VERSION, TradeRecordAcceptanceAudit


def _closed_trade(idx: int, *, strategy_id: str = "gold_1m_breakout", realized_pnl: float = 20.0) -> dict:
    return {
        "trade_id": f"trade_{idx}",
        "order_id": f"order_{idx}",
        "ticket_id": f"ticket_{idx}",
        "signal_id": f"signal_{idx}",
        "strategy_id": strategy_id,
        "symbol": "GOLD",
        "side": "long",
        "status": "closed",
        "quantity": 2,
        "entry_price": 100,
        "stop_loss": 95,
        "target": 110,
        "opened_at": "2026-06-23T00:00:00+00:00",
        "closed_at": "2026-06-23T01:00:00+00:00",
        "exit_price": 110,
        "exit_reason": "target",
        "entry_reason": "range breakout confirmed",
        "gross_realized_pnl": 20,
        "total_cost": 0,
        "realized_pnl": realized_pnl,
    }


def _open_trade(idx: int, *, symbol: str = "GOLD") -> dict:
    return {
        "trade_id": f"open_trade_{idx}",
        "order_id": f"open_order_{idx}",
        "ticket_id": f"open_ticket_{idx}",
        "signal_id": f"open_signal_{idx}",
        "strategy_id": "gold_1m_breakout",
        "symbol": symbol,
        "side": "short",
        "status": "open",
        "quantity": 2,
        "entry_price": 100,
        "stop_loss": 105,
        "target": 90,
        "opened_at": "2026-06-24T00:00:00+00:00",
        "entry_reason": "breakdown confirmed",
    }


def test_trade_record_acceptance_samples_across_strategies_and_dates(tmp_path: Path):
    root = tmp_path / "outputs"
    market_db = tmp_path / "market.db"
    MarketStore(market_db)
    write_json(root / "strategies" / "gold_1m_breakout" / "paper_trades" / "closed" / "2026-06-23.json", [_closed_trade(1)])
    write_json(root / "strategies" / "gold_1m_macd" / "paper_trades" / "closed" / "2026-06-24.json", [_closed_trade(2, strategy_id="gold_1m_macd")])

    result = TradeRecordAcceptanceAudit(root, market_db=market_db, sample_size=5).run("2026-06-24")

    assert result["schema_version"] == TRADE_RECORD_ACCEPTANCE_VERSION
    assert result["status"] == "pass"
    assert result["sample_size_actual"] == 2
    assert result["strategy_count"] == 2
    assert {row["strategy_id"] for row in result["samples"]} == {"gold_1m_breakout", "gold_1m_macd"}
    assert all(row["pnl_check"]["status"] == "pass" for row in result["samples"])
    assert load_json(root / "trade_record_acceptance" / "current.json")[0]["status"] == "pass"


def test_trade_record_acceptance_fails_when_hand_check_pnl_mismatches(tmp_path: Path):
    root = tmp_path / "outputs"
    market_db = tmp_path / "market.db"
    MarketStore(market_db)
    write_json(root / "strategies" / "gold_1m_breakout" / "paper_trades" / "closed" / "2026-06-23.json", [
        _closed_trade(1, realized_pnl=999.0)
    ])

    result = TradeRecordAcceptanceAudit(root, market_db=market_db, sample_size=5).run("2026-06-24", persist=False)

    assert result["status"] == "fail"
    assert result["samples"][0]["status"] == "fail"
    assert result["samples"][0]["pnl_check"]["reason"] == "net_pnl_math_mismatch"


def test_trade_record_acceptance_uses_latest_market_price_for_open_trade_pnl(tmp_path: Path):
    root = tmp_path / "outputs"
    market_db = tmp_path / "market.db"
    MarketStore(market_db).upsert_bars([
        Bar("GOLD", "1m", "2026-06-24T00:01:00+00:00", 98, 99, 97, 98, 10, "test", ["fresh"]),
    ])
    write_json(root / "strategies" / "gold_1m_breakout" / "paper_trades" / "current.json", [
        _open_trade(1, symbol="XAUUSDT")
    ])

    result = TradeRecordAcceptanceAudit(root, market_db=market_db, sample_size=5).run("2026-06-24", persist=False)

    assert result["status"] == "pass"
    assert result["samples"][0]["status"] == "pass"
    assert result["samples"][0]["display"]["pnl"] == "unrealized=4.0 R=0.4"
