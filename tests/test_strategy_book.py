from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_book import STRATEGY_BOOK_VERSION, StrategyBook


def _config() -> dict:
    return {
        "engine": "breakout",
        "timeframe": "1m",
        "starting_equity": 10000,
        "trader_id": "trader_breakout",
        "portfolio_id": "portfolio_breakout",
        "strategy_variant": "range_breakout_v1",
        "classification": {"family": "breakout", "style": "range_breakout"},
    }


def _write_book_fixture(root: Path, run_date: str, *, current_equity: float = 10025.0) -> None:
    write_json(
        root / "performance" / "current.json",
        [{
            "summary": {
                "realized_pnl_all": 20.0,
                "unrealized_pnl": 5.0,
                "open_trade_count": 1,
                "closed_all_count": 2,
            }
        }],
    )
    write_json(
        root / "equity_curve" / "current.json",
        [{
            "starting_equity": 10000.0,
            "current_equity": current_equity,
            "current_drawdown_pct": -0.1,
            "max_drawdown_pct": -0.5,
            "points": [{"run_date": run_date, "equity": current_equity}],
        }],
    )
    write_json(
        root / "paper_positions" / "current.json",
        {
            "GOLD": {
                "symbol": "GOLD",
                "side": "long",
                "quantity": 0.2,
                "avg_price": 4100,
                "unrealized_pnl": 5.0,
            }
        },
    )
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "open_1", "status": "open", "symbol": "GOLD"}])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [{"trade_id": "closed_1", "status": "closed"}])
    write_json(root / "paper_reconciliation" / "current.json", [{"status": "pass", "summary": {"failed": 0}}])


def test_strategy_book_passes_when_nav_positions_and_reconciliation_match(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "gold_1m_breakout"
    run_date = "2026-06-23"
    _write_book_fixture(root, run_date)

    book = StrategyBook(root, strategy_id="gold_1m_breakout", strategy_config=_config()).build(run_date)

    assert book["schema_version"] == STRATEGY_BOOK_VERSION
    assert book["identity"]["trader_id"] == "trader_breakout"
    assert book["identity"]["portfolio_id"] == "portfolio_breakout"
    assert book["identity"]["strategy_family"] == "breakout"
    assert book["accounting"]["expected_equity"] == 10025.0
    assert book["accounting"]["equity_drift"] == 0.0
    assert book["positions"]["gross_notional"] == 820.0
    assert book["trades"]["open_trade_count"] == 1
    assert book["audit"]["status"] == "pass"
    assert load_json(root / "strategy_book" / "current.json")[0]["audit"]["status"] == "pass"


def test_strategy_book_fails_when_nav_does_not_match_realized_plus_unrealized(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "gold_1m_breakout"
    run_date = "2026-06-23"
    _write_book_fixture(root, run_date, current_equity=10010.0)

    book = StrategyBook(root, strategy_id="gold_1m_breakout", strategy_config=_config()).build(run_date)

    assert book["accounting"]["expected_equity"] == 10025.0
    assert book["accounting"]["equity_drift"] == -15.0
    assert book["audit"]["status"] == "fail"
    assert "accounting_invariant" in book["audit"]["failures"]


def test_strategy_book_reports_corrupt_artifact_instead_of_showing_green(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "gold_1m_breakout"
    run_date = "2026-06-23"
    _write_book_fixture(root, run_date)
    path = root / "performance" / "current.json"
    path.write_text("{not valid json", encoding="utf-8")

    book = StrategyBook(root, strategy_id="gold_1m_breakout", strategy_config=_config()).build(run_date)

    assert book["audit"]["status"] == "fail"
    assert "artifact_read_error" in book["audit"]["failures"]
    assert book["audit"]["read_errors"][0]["path"].endswith("performance/current.json")
