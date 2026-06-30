from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_exit_decisions import PaperExitDecisionQueue


def test_paper_exit_decision_queue_flags_stop_touch_for_approval(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "paper_exit_monitor" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "status": "alert",
                "summary": {"open_trades": 1},
                "monitors": [
                    {
                        "trade_id": "tr1",
                        "ticket_id": "t1",
                        "symbol": "GOLD",
                        "side": "long",
                        "latest_price": 4495,
                        "latest_timestamp": "2026-05-26T00:05:00+00:00",
                        "stop_loss": 4490,
                        "target": 4570,
                        "stop_touched": True,
                        "target_touched": False,
                        "distance_to_stop_pct": 0.1,
                        "distance_to_target_pct": 1.6,
                        "unrealized_pnl": -15,
                    }
                ],
            }
        ],
    )

    result = PaperExitDecisionQueue(root).build(run_date)

    assert result["status"] == "alert"
    assert result["summary"]["high_priority"] == 1
    item = result["queue"][0]
    assert item["exit_type"] == "stop_touched"
    assert item["required_user_action"] == "approve_exit"
    assert item["suggested_exit_price"] == 4490
    assert load_json(root / "paper_exit_decisions" / "current.json")[0]["summary"]["open_items"] == 1


def test_paper_exit_decision_approve_closes_trade_and_updates_position(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True}])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True}})
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"timestamp": "2026-05-26T00:05:00+00:00", "close": 4550, "high": 4560, "low": 4540}])
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "tr1",
                "ticket_id": "t1",
                "symbol": "GOLD",
                "side": "long",
                "status": "open",
                "quantity": 2,
                "entry_price": 4510,
                "entry_total_cost": 0,
                "stop_loss": 4490,
                "target": 4570,
            }
        ],
    )
    write_json(root / "paper_positions" / "current.json", {"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 4510, "realized_pnl": 0, "unrealized_pnl": 0, "total_costs": 0, "risk_used_pct": 0.5}})
    write_json(
        root / "paper_exit_monitor" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "status": "pass",
                "summary": {"open_trades": 1},
                "monitors": [
                    {
                        "trade_id": "tr1",
                        "ticket_id": "t1",
                        "symbol": "GOLD",
                        "side": "long",
                        "latest_price": 4550,
                        "latest_timestamp": "2026-05-26T00:05:00+00:00",
                        "stop_loss": 4490,
                        "target": 4570,
                        "stop_touched": False,
                        "target_touched": False,
                        "distance_to_stop_pct": 1.3,
                        "distance_to_target_pct": 0.4,
                        "unrealized_pnl": 80,
                    }
                ],
            }
        ],
    )

    result = PaperExitDecisionQueue(root).record_decision(run_date, "tr1", "approve_exit", notes="manual test", exit_price=4550)

    assert result["decision"] == "approve_exit"
    assert result["closed_trade"]["status"] == "closed"
    assert result["closed_trade"]["gross_realized_pnl"] == 80
    assert result["closed_trade"]["realized_pnl"] < 80
    assert load_json(root / "paper_trades" / "current.json") == []
    assert load_json(root / "paper_positions" / "current.json") == {}
    assert load_json(root / "paper_exit_decisions" / f"{run_date}.decisions.json")[0]["closed_trade"]["manual_exit"] is True


def test_paper_exit_decision_hold_records_without_closing(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "paper_exit_monitor" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "status": "pass",
                "summary": {"open_trades": 1},
                "monitors": [
                    {
                        "trade_id": "tr1",
                        "ticket_id": "t1",
                        "symbol": "GOLD",
                        "side": "long",
                        "latest_price": 4530,
                        "stop_loss": 4490,
                        "target": 4570,
                        "stop_touched": False,
                        "target_touched": False,
                        "distance_to_stop_pct": 0.9,
                        "distance_to_target_pct": 0.9,
                        "unrealized_pnl": 40,
                    }
                ],
            }
        ],
    )
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "status": "open"}])

    result = PaperExitDecisionQueue(root).record_decision(run_date, "tr1", "hold", notes="let it work")

    assert result["decision"] == "hold"
    assert result["closed_trade"] is None
    assert load_json(root / "paper_trades" / "current.json")[0]["trade_id"] == "tr1"


def test_paper_exit_decision_queue_escalates_portfolio_drawdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(
        root / "paper_exit_monitor" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "status": "pass",
                "summary": {"open_trades": 2},
                "monitors": [
                    {
                        "trade_id": "tr1",
                        "ticket_id": "t1",
                        "symbol": "GOLD",
                        "side": "long",
                        "latest_price": 4518,
                        "stop_loss": 4480,
                        "target": 4750,
                        "stop_touched": False,
                        "target_touched": False,
                        "distance_to_stop_pct": 0.8,
                        "distance_to_target_pct": 5.0,
                        "unrealized_pnl": -90,
                    },
                    {
                        "trade_id": "tr2",
                        "ticket_id": "t2",
                        "symbol": "GOLD",
                        "side": "long",
                        "latest_price": 4518,
                        "stop_loss": 4480,
                        "target": 4750,
                        "stop_touched": False,
                        "target_touched": False,
                        "distance_to_stop_pct": 0.8,
                        "distance_to_target_pct": 5.0,
                        "unrealized_pnl": -88,
                    },
                ],
            }
        ],
    )
    write_json(
        root / "performance" / f"{run_date}.json",
        [
            {
                "summary": {"open_unrealized_r": -1.12},
                "open_trades": [
                    {"trade_id": "tr1", "unrealized_r": -0.56},
                    {"trade_id": "tr2", "unrealized_r": -0.55},
                ],
            }
        ],
    )

    result = PaperExitDecisionQueue(root).build(run_date)

    assert result["status"] == "alert"
    assert result["summary"]["high_priority"] == 2
    assert result["summary"]["needs_approval"] == 2
    assert result["queue"][0]["exit_type"] == "portfolio_risk_reduction"
    assert result["queue"][0]["required_user_action"] == "approve_exit"
    assert result["queue"][0]["portfolio_open_unrealized_r"] == -1.12
