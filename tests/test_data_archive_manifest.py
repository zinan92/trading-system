from pathlib import Path

from schemas.market_data import Bar
from services.data_archive_manifest import DataArchiveManifest, run_data_archive_manifest
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


def _write_required_outputs(root: Path, run_date: str) -> None:
    write_json(root / "raw_snapshots" / run_date / "GOLD_5m.json", [{"close": 4530}])
    write_json(root / "raw_snapshots" / run_date / "quote_snapshots.json", [{"symbol": "GOLD", "close": 4530, "record_type": "quote"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4530}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "trade_tickets" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_positions" / "current.json", {"GOLD": {"quantity": 0}})
    write_json(root / "paper_reconciliation" / f"{run_date}.json", [{"status": "pass"}])
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 0, "closed_trades": 0}}])
    write_json(root / "paper_exit_monitor" / f"{run_date}.json", [{"status": "empty", "summary": {"open_trades": 0}}])
    write_json(root / "paper_exit_decisions" / f"{run_date}.json", [{"status": "empty", "summary": {"open_items": 0}}])
    write_json(root / "paper_exit_decisions" / f"{run_date}.decisions.json", [])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {}}])
    write_json(root / "equity_curve" / f"{run_date}.json", [{"status": "pass", "points": []}])
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    write_json(root / "strategy_learning_actions" / f"{run_date}.json", [{"status": "collecting_evidence", "actions": []}])
    write_json(root / "strategy_experiments" / f"{run_date}.json", [{"status": "blocked", "paper_only": True, "auto_apply": False}])
    write_json(root / "strategy_improvement_plan" / f"{run_date}.json", [{"status": "collecting_evidence", "paper_only": True, "auto_apply": False}])
    write_json(root / "strategy_promotion_gate" / f"{run_date}.json", [{"status": "blocked", "promotion_allowed": False, "auto_apply": False}])
    write_json(root / "strategy_guardrails" / f"{run_date}.json", [{"status": "warn", "allow_new_paper_order": True}])
    write_json(root / "risk_monitor" / f"{run_date}.json", [{"status": "warn", "kill_switch_active": False}])
    write_json(root / "paper_risk_action_plan" / f"{run_date}.json", [{"status": "empty", "summary": {"action_count": 0}}])
    write_json(root / "paper_auto_approval_gate" / f"{run_date}.json", [{"status": "skipped", "allow_auto_approve": False}])
    write_json(root / "bot_supervisor" / f"{run_date}.json", [{"status": "pass", "mock_bot_running": True}])
    write_json(root / "bot_checkpoints" / f"{run_date}.json", [{"status": "ready", "mock_recoverable": True}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"status": "warn", "truth_level": "public_snapshot"}])
    write_json(root / "data_trust" / f"{run_date}.json", [{"status": "warn", "display_mode": "PAPER_PUBLIC"}])
    write_json(root / "data_integrity" / f"{run_date}.json", [{"status": "pass", "summary": {"passed": 4, "failed": 0}}])
    write_json(root / "official_feed_receipts" / f"{run_date}.json", [{"status": "warn", "official_rows": 0}])
    write_json(root / "operation_runbooks" / f"{run_date}.json", [{"status": "paper_manual_only"}])
    write_json(root / "live_switch_plan" / f"{run_date}.json", [{"status": "blocked"}])
    write_json(root / "live_submission_safety" / f"{run_date}.json", [{"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}])
    write_json(root / "live_dry_run_drill" / f"{run_date}.json", [{"status": "blocked", "safe_to_submit_live_order": False}])
    write_json(root / "live_broker_preflight" / f"{run_date}.json", [{"status": "blocked", "real_submit_blocked": True}])


def test_data_archive_manifest_records_local_files_and_db_hash(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    _write_required_outputs(root, run_date)
    MarketStore(db_path).upsert_bars([Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 1, 2, 1, 1.5, 1, "gold-api.com", ["test"])])

    result = DataArchiveManifest(root, db_path).run(run_date)

    assert result["status"] == "pass"
    assert result["gold_5m_rows"] == 1
    assert result["market_db"]["exists"] is True
    assert len(result["market_db"]["sha256"]) == 64
    assert result["present_file_count"] == result["file_count"]
    assert any(item["path"].endswith(f"raw_snapshots/{run_date}/quote_snapshots.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_trade_attribution/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_exit_monitor/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_exit_decisions/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_exit_decisions/{run_date}.decisions.json") for item in result["files"])
    assert any(item["path"].endswith(f"bot_supervisor/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"bot_checkpoints/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"data_trust/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_auto_approval_gate/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"paper_risk_action_plan/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"strategy_learning_actions/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"strategy_experiments/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"strategy_improvement_plan/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"strategy_promotion_gate/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"live_dry_run_drill/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"live_submission_safety/{run_date}.json") for item in result["files"])
    assert any(item["path"].endswith(f"live_broker_preflight/{run_date}.json") for item in result["files"])
    assert all(item["sha256"] for item in result["files"])
    assert result["snapshot"]["status"] == "pass"
    assert result["snapshot"]["snapshot_path"].endswith(f"{run_date}.tar.gz")
    assert len(result["snapshot"]["snapshot_sha256"]) == 64
    assert (root / "data_archive" / f"{run_date}.md").exists()
    assert load_json(root / "data_archive" / "current.json")[0]["status"] == "pass"


def test_data_archive_manifest_warns_when_required_file_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    run_date = "2026-05-26"
    _write_required_outputs(root, run_date)
    (root / "signals" / f"{run_date}.json").unlink()
    MarketStore(db_path).upsert_bars([Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 1, 2, 1, 1.5, 1, "gold-api.com", ["test"])])

    result = run_data_archive_manifest(run_date, root, db_path)

    assert result["status"] == "warn"
    assert any(path.endswith("/signals/2026-05-26.json") for path in result["missing_files"])
