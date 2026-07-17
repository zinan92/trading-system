import json
from pathlib import Path

from services.journal_store import write_json
from services.strategy_leaderboard import StrategyLeaderboard


def _namespace(root: Path, sid: str, run_date: str, *, starting, current, max_dd, win_rate, gold_first, gold_last) -> None:
    ns = root / "strategies" / sid
    write_json(ns / "equity_curve" / "current.json", [{
        "starting_equity": starting,
        "current_equity": current,
        "current_drawdown_pct": 0.0,
        "max_drawdown_pct": max_dd,
        "points": [{"equity": starting}, {"equity": (starting + current) / 2}, {"equity": current}],
    }])
    write_json(ns / "performance" / "current.json", [{
        "summary": {"win_rate": win_rate, "net_pnl_marked": current - starting, "profit_factor": 1.2, "closed_all_count": 2, "open_trade_count": 0},
    }])
    write_json(ns / "clean_bars" / run_date / "GOLD_5m.json", [
        {"timestamp": f"{run_date}T00:00:00+00:00", "close": gold_first},
        {"timestamp": f"{run_date}T00:05:00+00:00", "close": gold_last},
    ])


def test_leaderboard_ranks_by_return_and_computes_vs_gold(tmp_path: Path):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10500, max_dd=-1.0, win_rate=0.6, gold_first=4500, gold_last=4545)  # +5%, gold +1%
    _namespace(root, "beta", rd, starting=10000, current=9800, max_dd=-4.0, win_rate=0.3, gold_first=4500, gold_last=4545)   # -2%

    lb = StrategyLeaderboard(root).build(rd)

    assert lb["strategy_count"] == 2
    assert [r["strategy_id"] for r in lb["strategies"]] == ["alpha", "beta"]  # ranked by return desc
    alpha = lb["strategies"][0]
    assert alpha["rank"] == 1
    assert round(alpha["return_pct"], 2) == 5.0
    assert round(alpha["gold_return_pct"], 2) == 1.0
    assert round(alpha["vs_gold_pct"], 2) == 4.0   # 5% - 1%
    assert alpha["win_rate"] == 0.6
    assert alpha["position"]["summary"] == "flat"
    assert alpha["lab_expectation"]["paper_eligible"] is False
    assert alpha["lab_expectation"]["status"] == "missing"
    assert lb["strategies"][1]["rank"] == 2
    assert round(lb["strategies"][1]["return_pct"], 2) == -2.0
    # persisted artifact
    assert json.loads((root / "strategy_leaderboard" / "current.json").read_text())[0]["strategy_count"] == 2


def test_leaderboard_empty_when_no_namespaces(tmp_path: Path):
    root = tmp_path / "outputs"
    lb = StrategyLeaderboard(root).build("2026-05-29")
    assert lb["strategy_count"] == 0
    assert lb["strategies"] == []


def test_leaderboard_skips_namespace_without_account(tmp_path: Path):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10100, max_dd=0.0, win_rate=0.5, gold_first=4500, gold_last=4500)
    (root / "strategies" / "no_account").mkdir(parents=True)  # dir exists but no equity_curve

    lb = StrategyLeaderboard(root).build(rd)
    assert [r["strategy_id"] for r in lb["strategies"]] == ["alpha"]


def test_leaderboard_includes_mixed_position_summary(tmp_path: Path):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10010, max_dd=-0.2, win_rate=0.5, gold_first=4500, gold_last=4500)
    write_json(root / "strategies" / "alpha" / "paper_positions" / "current.json", {
        "GOLD": {
            "symbol": "GOLD",
            "side": "mixed",
            "quantity": 0.55,
            "net_side": "long",
            "net_quantity": 0.18,
            "unrealized_pnl": 2.5,
            "legs": {
                "long": {"quantity": 0.37, "avg_price": 4316},
                "short": {"quantity": 0.18, "avg_price": 4320},
            },
        }
    })

    lb = StrategyLeaderboard(root).build(rd)

    position = lb["strategies"][0]["position"]
    assert position["status"] == "open"
    assert position["side"] == "mixed"
    assert position["gross_quantity"] == 0.55
    assert position["net_side"] == "long"
    assert position["summary"] == "mixed gross 0.5500 / net long 0.1800"


def test_leaderboard_includes_classification_and_daily_execution_status(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    monkeypatch.setattr(
        "services.strategy_leaderboard.load_strategy_config",
        lambda: {
            "alpha": {
                "symbol": "GOLD",
                "timeframe": "1m",
                "engine": "chan",
                "classification": {
                    "family": "chan",
                    "family_label": "缠论",
                    "style": "second_buy",
                    "style_label": "二买/二卖",
                    "directionality": "long_short",
                    "frequency_bucket": "low",
                    "role": "test",
                },
            },
            "beta": {"symbol": "GOLD", "timeframe": "1m", "engine": "macd"},
        },
    )
    _namespace(root, "alpha", rd, starting=10000, current=10500, max_dd=-1.0, win_rate=0.6, gold_first=4500, gold_last=4545)
    _namespace(root, "beta", rd, starting=10000, current=9900, max_dd=-1.0, win_rate=0.6, gold_first=4500, gold_last=4545)
    write_json(root / "strategies" / "alpha" / "signals" / f"{rd}.json", [{"signal_id": "s1", "direction": "long"}])
    alpha_rows = [{"ticket_id": f"t{index}"} for index in range(1, 6)]
    write_json(root / "strategies" / "alpha" / "trade_tickets" / f"{rd}.json", alpha_rows)
    write_json(root / "strategies" / "alpha" / "paper_orders" / f"{rd}.json", alpha_rows)
    write_json(root / "strategies" / "beta" / "signals" / f"{rd}.json", [{"signal_id": "s1", "direction": "watch"}])
    write_json(root / "strategies" / "beta" / "paper_orders" / f"{rd}.json", [{"ticket_id": "only_one"}])

    lb = StrategyLeaderboard(root).build(rd)
    alpha = next(row for row in lb["strategies"] if row["strategy_id"] == "alpha")
    beta = next(row for row in lb["strategies"] if row["strategy_id"] == "beta")

    assert alpha["classification"]["family"] == "chan"
    assert alpha["classification"]["family_label"] == "缠论"
    assert alpha["engine"] == "chan"
    assert alpha["timeframe"] == "1m"
    assert alpha["daily_execution"]["executed_trade_count"] == 5
    assert alpha["daily_execution"]["status"] == "effective"
    assert alpha["effective_today"] is True
    assert beta["classification"]["family"] == "momentum"
    assert beta["daily_execution"]["executed_trade_count"] == 1
    assert beta["daily_execution"]["status"] == "low_volume"
    assert beta["effective_today"] is False


def test_leaderboard_includes_lab_expectation_when_present(tmp_path: Path):
    from services.lab_registry import LabRegistry

    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10500, max_dd=-1.0, win_rate=0.6, gold_first=4500, gold_last=4545)
    registry = LabRegistry(root)
    registry.start({"hypothesis": "h", "family": "macd", "strategy_ref": {"strategy_id": "alpha"}}, exp_id="e1")
    registry.finalize("e1", status="valid", results={"objective": {"walkforward": {"passed": True}, "holdout": {"passed": True}}})
    registry.record_holdout_consumption("e1", {"start": "2026-01-01", "end": "2026-02-01"})

    alpha = StrategyLeaderboard(root).build(rd)["strategies"][0]

    assert alpha["lab_expectation"]["paper_eligible"] is False
    assert "execution_candidate_identity_missing" in alpha["lab_expectation"]["blockers"]
    assert alpha["lab_expectation"]["source_exp_id"] == "e1"


def test_leaderboard_does_not_count_blocked_demo_request_as_execution(tmp_path: Path):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10000, max_dd=0, win_rate=0, gold_first=4500, gold_last=4500)
    write_json(root / "strategies" / "alpha" / "signals" / f"{rd}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(root / "strategies" / "alpha" / "trade_tickets" / f"{rd}.json", [{"ticket_id": "t1"}])
    write_json(
        root / "strategies" / "alpha" / "demo_order_requests" / f"{rd}.json",
        [{"ticket_id": "t1", "status": "blocked", "guard": {"block_reason": "demo position already open"}}],
    )

    lb = StrategyLeaderboard(root).build(rd)
    alpha = lb["strategies"][0]

    assert alpha["daily_execution"]["executed_trade_count"] == 0
    assert alpha["daily_execution"]["status"] == "inactive"


def test_leaderboard_counts_emergency_closed_demo_entry_as_execution(tmp_path: Path):
    root = tmp_path / "outputs"
    rd = "2026-05-29"
    _namespace(root, "alpha", rd, starting=10000, current=10000, max_dd=0, win_rate=0, gold_first=4500, gold_last=4500)
    write_json(root / "strategies" / "alpha" / "signals" / f"{rd}.json", [{"signal_id": "s1", "direction": "long"}])
    write_json(root / "strategies" / "alpha" / "trade_tickets" / f"{rd}.json", [{"ticket_id": "t1"}])
    write_json(
        root / "strategies" / "alpha" / "demo_order_requests" / f"{rd}.json",
        [{
            "ticket": {"ticket_id": "t1"},
            "receipt": {"status": "protective_order_missing_closed"},
            "broker_response": {"emergency_close": {"status": "closed"}},
        }],
    )

    lb = StrategyLeaderboard(root).build(rd)
    alpha = lb["strategies"][0]

    assert alpha["daily_execution"]["executed_trade_count"] == 1
    assert alpha["daily_execution"]["status"] == "low_volume"
