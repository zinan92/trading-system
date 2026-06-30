from pathlib import Path

from services.alert_delivery_drill import AlertDeliveryDrill
from services.alert_notifier import AlertNotifier
from services.health_check import HealthCheck
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_health_check_ok_with_required_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-17"
    store = MarketStore(db_path)
    store.upsert_bars(
        [
            Bar("GOLD", "5m", f"2026-05-17T00:{index:02d}:00+00:00", 4500, 4501, 4499, 4500 + index, 1, "broker_csv", [])
            for index in range(20)
        ]
    )
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4510}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD"}])
    write_json(root / "journal_decisions" / f"{run_date}.json", [])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"net_pnl_marked": 0, "open_unrealized_r": 0, "expectancy_r": 0, "profit_factor": 0}}])
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 0, "closed_trades": 0, "attributed_open_trades": 0, "attributed_closed_trades": 0}}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text("{}\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    write_json(root / "broker_preflight" / "current.json", [{"ready": True, "block_reason": "paper mode"}])
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    write_json(root / "broker_receipts" / "summary_current.json", [{"errors": [], "total_receipts": 1}])
    write_json(root / "mt5_bridge_smoke" / "current.json", [{"status": "pass", "order": {"order_id": "o1"}}])
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok"}\n', encoding="utf-8")

    result = HealthCheck(root, db_path).run(run_date)

    assert result["status"] in {"ok", "warn"}
    assert any(item["name"] == "market_db" for item in result["checks"])
    assert any(item["name"] == "oanda_feed" and item["status"] == "ok" and "inactive" in item["message"] for item in result["checks"])
    assert any(item["name"] == "broker_receipts" and item["status"] == "ok" for item in result["checks"])
    assert any(item["name"] == "paper_performance" and item["status"] == "ok" for item in result["checks"])
    assert any(item["name"] == "paper_trade_attribution" and item["status"] == "ok" for item in result["checks"])
    journal = next(item for item in result["checks"] if item["name"] == "journal_review")
    assert journal["status"] == "ok"
    assert "learning_ledger" in journal["details"]
    assert load_json(root / "health" / "current.json")[0]["run_date"] == run_date


def test_health_check_errors_when_required_artifacts_missing(tmp_path: Path):
    result = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db").run("2026-05-18")

    assert result["status"] == "error"
    assert any(item["name"] == "market_db" and item["status"] == "error" for item in result["checks"])


def test_health_check_accepts_active_running_runner(tmp_path: Path):
    root = tmp_path / "outputs"
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "running", "interval_seconds": 300}\n', encoding="utf-8")

    check = HealthCheck(root, tmp_path / "missing.db")._runner_check()

    assert check["status"] == "ok"
    assert check["message"] == "runner active"


def test_active_demo_reconciliation_check_errors_on_drift(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "reconciled": False,
            "error": "",
            "drift_count": 1,
            "drifts": [{"reason": "exchange position has no local record"}],
        }],
    )

    check = HealthCheck(root, tmp_path / "missing.db")._active_demo_reconciliation_check()

    assert check["status"] == "error"
    assert check["name"] == "active_demo_reconciliation"
    assert "exchange position has no local record" in check["message"]


def test_active_demo_reconciliation_check_ok_when_flat(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"run_date": run_date, "reconciled": True, "error": "", "drift_count": 0, "drifts": []}],
    )

    check = HealthCheck(root, tmp_path / "missing.db")._active_demo_reconciliation_check()

    assert check["status"] == "ok"
    assert "reconciles" in check["message"]


def test_active_demo_reconciliation_check_errors_on_cannot_confirm_unknown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "reconciled": False,
            "confirmation_status": "cannot_confirm",
            "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
            "reason_code": "venue_state_unknown",
            "error": "TimeoutError: venue read timed out",
            "drift_count": 0,
            "drifts": [],
        }],
    )
    health = HealthCheck(root, tmp_path / "missing.db")
    health.config["demo_trading"] = {"enabled": True, "active_strategy_id": "gold_1m_chan"}

    check = health._active_demo_reconciliation_check()

    assert check["status"] == "error"
    assert "cannot confirm venue state" in check["message"]
    assert check["details"]["reconciliation"]["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"


def test_active_demo_reconciliation_check_names_suspected_naked_position(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{
            "run_date": run_date,
            "reconciled": False,
            "confirmation_status": "confirmed_drift",
            "system_state": "BLOCKED_NAKED_POSITION_SUSPECTED",
            "reason_code": "naked_position_suspected",
            "suspected_naked_position": True,
            "escalation_action": "halt_new_orders_and_verify_or_flatten_suspected_naked_position",
            "error": "",
            "drift_count": 1,
            "drifts": [{"reason": "suspected naked position: exchange position exists while local order is still submitting"}],
        }],
    )
    health = HealthCheck(root, tmp_path / "missing.db")
    health.config["demo_trading"] = {"enabled": True, "active_strategy_id": "gold_1m_chan"}

    check = health._active_demo_reconciliation_check()

    assert check["status"] == "error"
    assert "suspected naked position" in check["message"]
    assert check["details"]["reconciliation"]["reason_code"] == "naked_position_suspected"


def test_active_demo_reconciliation_errors_on_unresolved_protective_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"run_date": run_date, "reconciled": True, "error": "", "drift_count": 0, "drifts": []}],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "demo_order_requests" / f"{run_date}.json",
        [{
            "order_id": "demo_order_1",
            "receipt": {"status": "protective_order_missing"},
            "broker_response": {
                "protective_status": "failed",
                "emergency_close": {"status": "failed", "local_mirror": {"closed": False}},
            },
        }],
    )
    health = HealthCheck(root, tmp_path / "missing.db")
    health.config["demo_trading"] = {"enabled": True, "active_strategy_id": "gold_1m_chan"}

    check = health._active_demo_reconciliation_check()

    assert check["status"] == "error"
    assert "protective orders missing" in check["message"]
    assert check["details"]["request"]["receipt"]["status"] == "protective_order_missing"


def test_active_demo_protective_missing_fires_alert(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"run_date": run_date, "reconciled": True, "error": "", "drift_count": 0, "drifts": []}],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "demo_order_requests" / f"{run_date}.json",
        [{
            "order_id": "demo_order_1",
            "receipt": {"status": "protective_order_missing"},
            "broker_response": {"protective_status": "partial", "emergency_close": {"status": "failed"}},
        }],
    )
    health = HealthCheck(root, tmp_path / "missing.db")
    health.config["demo_trading"] = {"enabled": True, "active_strategy_id": "gold_1m_chan"}
    check = health._active_demo_reconciliation_check()

    class Sender:
        configured = True
        channel = "test"

        def send(self, text: str):
            return {"ok": True, "channel": "test"}

    alert = AlertNotifier(root, sender=Sender()).run(run_date, {"status": "error", "checks": [check]})

    assert alert["alert_count"] == 1
    assert alert["events"][0]["kind"] == "firing"
    assert alert["events"][0]["name"] == "active_demo_reconciliation"


def test_active_demo_reconciliation_ok_after_protective_failure_emergency_close(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    write_json(
        root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json",
        [{"run_date": run_date, "reconciled": True, "error": "", "drift_count": 0, "drifts": []}],
    )
    write_json(
        root / "strategies" / "gold_1m_chan" / "demo_order_requests" / f"{run_date}.json",
        [{
            "order_id": "demo_order_1",
            "receipt": {"status": "protective_order_missing_closed"},
            "broker_response": {
                "protective_status": "failed",
                "emergency_close": {"status": "closed", "local_mirror": {"closed": True}},
            },
        }],
    )
    health = HealthCheck(root, tmp_path / "missing.db")
    health.config["demo_trading"] = {"enabled": True, "active_strategy_id": "gold_1m_chan"}

    check = health._active_demo_reconciliation_check()

    assert check["status"] == "ok"
    assert "reconciles" in check["message"]


def test_alert_delivery_warns_when_telegram_not_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_ALERT_CHANNEL", "telegram")
    monkeypatch.delenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_SECRET", raising=False)

    check = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()

    assert check["status"] == "warn"
    assert check["details"]["channel"] == "log"
    assert "TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN" in check["details"]["required_env"]


def test_alert_delivery_warns_until_successful_drill_when_telegram_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_ALERT_CHANNEL", "telegram")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_FEISHU_SECRET", raising=False)

    check = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()

    assert check["status"] == "warn"
    assert check["details"]["channel"] == "telegram"
    assert "no successful delivery drill" in check["message"]

    class Sender:
        configured = True

        def send(self, text: str):
            return {"ok": True, "channel": "telegram", "message_id": "m1"}

    AlertDeliveryDrill(tmp_path / "outputs", sender=Sender()).run("2026-06-23")
    passed = HealthCheck(tmp_path / "outputs", tmp_path / "missing.db")._alert_delivery_check()
    assert passed["status"] == "ok"


def test_health_check_warns_when_trade_attribution_is_incomplete(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-19"
    write_json(root / "paper_trade_attribution" / f"{run_date}.json", [{"status": "pass", "summary": {"open_trades": 2, "closed_trades": 0, "attributed_open_trades": 1, "attributed_closed_trades": 0}}])

    check = HealthCheck(root, tmp_path / "missing.db")._paper_trade_attribution_check(run_date)

    assert check["status"] == "warn"
    assert "1/2" in check["message"]


def _write_cycle_artifacts(root: Path, run_date: str) -> None:
    """Write every per-5min cycle artifact for run_date (no evening artifacts)."""
    write_json(root / "signals" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "backtests" / f"{run_date}.json", [{"asset": "GOLD"}])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4510}])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD"}])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "strategy_snapshots" / f"{run_date}.json", [{"config_hash": "abc123def456"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"net_pnl_marked": 0, "open_unrealized_r": 0, "expectancy_r": 0, "profit_factor": 0}}])
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")


def test_pipeline_artifacts_ok_without_evening_only_attribution(tmp_path: Path):
    """A mid-day cycle has every per-cycle artifact but no paper_trade_attribution
    (an evening-only artifact). The per-cycle artifacts check must not error."""
    root = tmp_path / "outputs"
    run_date = "2026-05-20"
    _write_cycle_artifacts(root, run_date)
    assert not (root / "paper_trade_attribution" / f"{run_date}.json").exists()

    check = HealthCheck(root, tmp_path / "missing.db")._pipeline_artifacts_check(run_date)

    assert check["status"] == "ok"


def test_trade_attribution_ok_when_pending_evening_review(tmp_path: Path):
    """Before the evening review runs, a missing attribution is "not yet due" (ok),
    so the system-status badge stays green instead of firing a false red."""
    root = tmp_path / "outputs"
    run_date = "2026-05-20"
    assert not (root / "daily_review_runs" / f"{run_date}.json").exists()

    check = HealthCheck(root, tmp_path / "missing.db")._paper_trade_attribution_check(run_date)

    assert check["status"] == "ok"
    assert "pending evening review" in check["message"]


def test_trade_attribution_errors_when_missing_after_evening_review(tmp_path: Path):
    """Once the evening review has completed for the date, the attribution
    artifact must exist; a missing file is then a genuine error."""
    root = tmp_path / "outputs"
    run_date = "2026-05-20"
    write_json(root / "daily_review_runs" / f"{run_date}.json", [{"run_date": run_date, "status": "pass"}])

    check = HealthCheck(root, tmp_path / "missing.db")._paper_trade_attribution_check(run_date)

    assert check["status"] == "error"


def test_health_rollup_not_error_intraday_when_only_evening_artifacts_pending(tmp_path: Path):
    """End-to-end: a mid-day cycle (all per-cycle artifacts, healthy db/runner, no
    evening artifacts, no daily review receipt) must roll up to ok/warn — never
    error — and expose zero hard-failing checks for the dashboard badge."""
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-20"
    store = MarketStore(db_path)
    store.upsert_bars(
        [
            Bar("GOLD", "5m", f"2026-05-20T00:{index:02d}:00+00:00", 4500, 4501, 4499, 4500 + index, 1, "broker_csv", [])
            for index in range(20)
        ]
    )
    _write_cycle_artifacts(root, run_date)
    write_json(root / "journal_decisions" / f"{run_date}.json", [])
    write_json(root / "journal_pending" / f"{run_date}.json", [])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text("{}\n", encoding="utf-8")
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    write_json(root / "broker_preflight" / "current.json", [{"ready": True, "block_reason": "paper mode"}])
    write_json(root / "oanda_feed" / "current.json", [{"status": "pass", "imported_rows": 50}])
    write_json(root / "broker_receipts" / "summary_current.json", [{"errors": [], "total_receipts": 1}])
    write_json(root / "mt5_bridge_smoke" / "current.json", [{"status": "pass", "order": {"order_id": "o1"}}])
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok"}\n', encoding="utf-8")
    # No paper_trade_attribution, no daily_review_runs receipt → evening still pending.

    result = HealthCheck(root, db_path).run(run_date)

    # The bug: missing evening-only attribution forced an "error" rollup all day,
    # turning the dashboard #systemStatus badge red and firing a spurious alert.
    # After the fix the rollup is never "error" intraday and no check is hard-
    # failing, so the badge renders green/yellow (tone(): ok->green, warn->yellow)
    # and activeIssues (checks in error/fail/block) is 0 → AlertNotifier stays
    # quiet. Remaining warns (reconciliation, guardrails, daily_review pending)
    # are correct intraday and pre-date this fix.
    assert result["status"] in {"ok", "warn"}
    hard_failing = [c for c in result["checks"] if c["status"] in {"error", "fail", "block"}]
    assert hard_failing == []
    attribution = next(c for c in result["checks"] if c["name"] == "paper_trade_attribution")
    assert attribution["status"] == "ok"
    artifacts = next(c for c in result["checks"] if c["name"] == "pipeline_artifacts")
    assert artifacts["status"] == "ok"
