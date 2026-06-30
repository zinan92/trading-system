from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_performance import PaperPerformanceAnalyzer


def test_paper_performance_builds_r_metrics_for_open_and_closed_trades(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 102.0}])
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "t_open",
                "order_id": "o_open",
                "symbol": "GOLD",
                "side": "long",
                "quantity": 2,
                "entry_price": 100,
                "entry_total_cost": 1,
                "stop_loss": 95,
                "target": 110,
                "status": "open",
                "signal_regime": "trend_following",
            }
        ],
    )
    write_json(
        root / "paper_trades" / "closed" / f"{run_date}.json",
        [
            {
                "trade_id": "t_closed",
                "order_id": "o_closed",
                "symbol": "GOLD",
                "side": "long",
                "quantity": 1,
                "entry_price": 100,
                "stop_loss": 95,
                "target": 110,
                "exit_price": 110,
                "exit_reason": "target",
                "realized_pnl": 10,
                "status": "closed",
                "signal_regime": "pullback_long",
            }
        ],
    )
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"last_price": 102, "unrealized_pnl": 4}}\n', encoding="utf-8")

    result = PaperPerformanceAnalyzer(root).build(run_date)

    summary = result["summary"]
    assert summary["open_trade_count"] == 1
    assert summary["closed_today_count"] == 1
    assert summary["win_rate"] == 1
    assert summary["realized_pnl_today"] == 10
    assert summary["unrealized_pnl"] == 4
    assert summary["open_costs"] == 1
    assert summary["total_execution_costs"] == 1
    assert summary["open_by_signal_regime"] == {"trend_following": 1}
    assert summary["closed_by_signal_regime"] == {"pullback_long": 1}
    assert summary["open_risk_amount"] == 10
    assert summary["open_unrealized_r"] == 0.3
    assert summary["avg_realized_r"] == 2
    assert result["open_trades"][0]["distance_to_stop_pct"] > 0
    assert load_json(root / "performance" / "current.json")[0]["summary"]["net_pnl_marked"] == 14
