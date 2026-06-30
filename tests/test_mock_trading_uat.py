from pathlib import Path

from services.journal_store import load_json, write_json
from services.mock_trading_uat import MockTradingUAT


def _write_uat_fixture(root: Path, run_date: str) -> None:
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True, "latest_record_is_fresh": True, "latest_provider": "gold-api.com"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"asset": "GOLD", "timestamp": "2026-05-25T00:00:00+00:00", "close": 4530, "provider": "gold-api.com"}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m", "clean_rows": 1}])
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD", "direction": "long", "strength": 61}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "verdict": "supportive", "sample_size": 40}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [{"ticket_id": "t1", "asset": "GOLD"}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "status": "filled"}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"quantity": 1}}\n', encoding="utf-8")
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "status": "open"}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "t1", "decision_status": "executed_paper"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"net_pnl_marked": 0}}])
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 1, "closed_trades": 0, "attributed_open_trades": 1, "attributed_closed_trades": 0}}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "warn", "kill_switch_active": False, "allow_paper_auto_approve": True}])
    write_json(root / "health" / f"{run_date}.json", [{"status": "warn", "checks": [{"name": "paper_trade_attribution", "status": "ok"}]}])
    write_json(root / "data_archive" / f"{run_date}.json", [{"status": "pass", "present_file_count": 20, "file_count": 20, "missing_files": []}])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "mock_runtime" / f"{run_date}.json", [{"status": "pass", "mock_ready": True, "mock_running": True, "summary": {"failed": 0}}])
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")


def test_mock_trading_uat_passes_with_complete_closed_loop_evidence(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-25"
    _write_uat_fixture(root, run_date)

    result = MockTradingUAT(root).run(run_date)

    assert result["status"] == "pass"
    statuses = {item["name"]: item["status"] for item in result["checks"]}
    assert statuses["market_data"] == "pass"
    assert statuses["paper_execution"] == "pass"
    assert statuses["trade_attribution"] == "pass"
    assert statuses["risk_monitor"] == "pass"
    assert statuses["health_archive"] == "pass"
    assert result["evidence_paths"]["paper_orders"].endswith(f"paper_orders/{run_date}.json")
    assert result["evidence_paths"]["paper_trade_attribution"].endswith(f"paper_trade_attribution/{run_date}.json")
    assert result["evidence_paths"]["risk_monitor"].endswith(f"risk_monitor/{run_date}.json")
    assert load_json(root / "mock_uat" / "current.json")[0]["run_date"] == run_date
    assert (root / "mock_uat" / f"{run_date}.md").exists()


def test_mock_trading_uat_fails_when_journal_review_artifacts_are_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-25"
    _write_uat_fixture(root, run_date)
    (root / "review_notes" / f"{run_date}.md").unlink()
    (root / "learning_ledger" / f"{run_date}.json").unlink()

    result = MockTradingUAT(root).run(run_date)

    journal = next(item for item in result["checks"] if item["name"] == "journal_review")
    assert result["status"] == "fail"
    assert journal["status"] == "fail"
    assert "review_notes" in journal["evidence"]["missing_files"]
    assert "learning_ledger" in journal["evidence"]["empty_json"]


def test_mock_trading_uat_accepts_explicit_no_trade_strategy_path(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-25"
    _write_uat_fixture(root, run_date)
    write_json(
        root / "signals" / f"{run_date}.json",
        [{"asset": "GOLD", "direction": "watch", "status": "no_signal", "regime": "no_trade", "methods": ["no-trade"]}],
    )
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD", "verdict": "no_trade"}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    result = MockTradingUAT(root).run(run_date)

    strategy = next(item for item in result["checks"] if item["name"] == "strategy_evidence")
    assert result["status"] == "pass"
    assert strategy["status"] == "pass"
    assert strategy["evidence"]["backtest_verdict"] == "no_trade"


def test_mock_trading_uat_warns_when_trade_attribution_is_partial(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-25"
    _write_uat_fixture(root, run_date)
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 2, "closed_trades": 0, "attributed_open_trades": 1, "attributed_closed_trades": 0}}])

    result = MockTradingUAT(root).run(run_date)

    attribution = next(item for item in result["checks"] if item["name"] == "trade_attribution")
    assert result["status"] == "warn"
    assert attribution["status"] == "warn"
    assert "1/2" in attribution["summary"]
