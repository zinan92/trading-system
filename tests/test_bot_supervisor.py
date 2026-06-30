from datetime import datetime, timezone
from pathlib import Path

from services.bot_supervisor import BotSupervisor
from services.journal_store import load_json, write_json


def test_bot_supervisor_passes_fresh_mock_loop(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    journal = root / "journals" / f"{run_date}.md"
    review = root / "review_notes" / f"{run_date}.md"
    report = root / "reports" / f"{run_date}.md"
    for path in [journal, review, report]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(f'{{"run_date":"{run_date}","state":"ok","interval_seconds":300,"finished_at":"{now}"}}\n', encoding="utf-8")
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_price": 4529.4, "latest_provider": "gold-api.com", "latest_record_age_minutes": 1, "price_sanity": {"passes": True}}])
    write_json(root / "mock_runtime" / "current.json", [{"mock_ready": True, "mock_running": True}])
    write_json(root / "daily_review_runs" / "current.json", [{"run_date": run_date, "status": "warn", "artifacts": {"journal": str(journal), "review_notes": str(review), "report": str(report)}}])
    write_json(root / "operation_runbooks" / "current.json", [{"status": "paper_manual_only", "permissions": {"paper_manual_review": True, "live_trading": False}}])

    result = BotSupervisor(root).run(run_date)

    assert result["status"] == "pass"
    assert result["mock_bot_running"] is True
    assert result["live_trading_allowed"] is False
    assert result["summary"]["latest_price"] == 4529.4
    assert load_json(root / "bot_supervisor" / "current.json")[0]["mock_bot_running"] is True


def test_bot_supervisor_warns_on_stale_runner(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    journal = root / "journals" / f"{run_date}.md"
    review = root / "review_notes" / f"{run_date}.md"
    report = root / "reports" / f"{run_date}.md"
    for path in [journal, review, report]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(f'{{"run_date":"{run_date}","state":"ok","interval_seconds":300,"finished_at":"2026-05-26T00:00:00+00:00"}}\n', encoding="utf-8")
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_price": 4529.4, "latest_provider": "gold-api.com", "price_sanity": {"passes": True}}])
    write_json(root / "mock_runtime" / "current.json", [{"mock_ready": True, "mock_running": True}])
    write_json(root / "daily_review_runs" / "current.json", [{"run_date": run_date, "status": "warn", "artifacts": {"journal": str(journal), "review_notes": str(review), "report": str(report)}}])
    write_json(root / "operation_runbooks" / "current.json", [{"status": "paper_manual_only", "permissions": {"paper_manual_review": True, "live_trading": False}}])

    result = BotSupervisor(root).run(run_date)

    assert result["status"] == "warn"
    assert result["mock_bot_running"] is False
    assert next(item for item in result["checks"] if item["name"] == "runner_5m")["status"] == "warn"
