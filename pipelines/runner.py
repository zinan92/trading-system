from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from services.run_date import utc_run_date

from pipelines.bot import run_bot_cycle
from services.alert_notifier import AlertNotifier
from services.config_loader import ROOT, load_pipeline_config
from services.health_check import HealthCheck
from services.mock_runtime import MockTradingRuntime
from services.run_history import RunHistory
from services.runner_status import RunnerStatusStore


def _output_root() -> Path:
    config = load_pipeline_config()
    return Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))


def run_runner_once(run_date: str, paper_auto_approve: bool, interval_seconds: int = 300) -> dict:
    output_root = _output_root()
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    status = {
        "state": "running",
        "started_at": started_at.isoformat(),
        "interval_seconds": interval_seconds,
        "paper_auto_approve": paper_auto_approve,
    }
    store = RunnerStatusStore(output_root)
    store.record(run_date, status)
    try:
        result = run_bot_cycle(run_date, paper_auto_approve=paper_auto_approve)
        finished_at = datetime.now(timezone.utc).replace(microsecond=0)
        status.update(
            {
                "state": "ok",
                "finished_at": finished_at.isoformat(),
                "next_run_at": (finished_at + timedelta(seconds=interval_seconds)).isoformat(),
                "collector_count": len(result.get("collector_records", [])),
                "binance_feed_status": result.get("binance_feed", {}).get("status", ""),
                "binance_feed_ready": result.get("binance_feed", {}).get("ready"),
                "binance_feed_imported_rows": result.get("binance_feed", {}).get("imported_rows", 0),
                "binance_feed_latest_timestamp": result.get("binance_feed", {}).get("latest_timestamp", ""),
                "binance_feed_latest_price": result.get("binance_feed", {}).get("latest_price"),
                "oanda_feed_status": result.get("oanda_feed", {}).get("status", ""),
                "oanda_feed_ready": result.get("oanda_feed", {}).get("ready"),
                "oanda_feed_imported_rows": result.get("oanda_feed", {}).get("imported_rows", 0),
                "oanda_feed_missing_env": result.get("oanda_feed", {}).get("missing_env", []),
                "broker_feed_new_files": result.get("broker_feed", {}).get("new_files", 0),
                "broker_feed_imported_rows": result.get("broker_feed", {}).get("imported_rows", 0),
                "broker_receipt_new_count": result.get("broker_receipts", {}).get("new_receipts", 0),
                "broker_receipt_total_count": result.get("broker_receipts", {}).get("total_receipts", 0),
                "data_source_status": result.get("data_source_preflight", {}).get("status", ""),
                "data_source_lineage_status": result.get("data_source_lineage", {}).get("status", ""),
                "data_truth_level": result.get("data_source_lineage", {}).get("truth_level", ""),
                "data_lineage_ready_for_live": result.get("data_source_lineage", {}).get("ready_for_live"),
                "official_5m_rows": result.get("data_source_lineage", {}).get("provider_groups", {}).get("official", {}).get("rows", 0),
                "public_5m_rows": result.get("data_source_lineage", {}).get("provider_groups", {}).get("public", {}).get("rows", 0),
                "live_submission_safety_status": result.get("live_submission_safety", {}).get("status", ""),
                "live_submission_blocked_by_gate": result.get("live_submission_safety", {}).get("blocked_by_activation_gate"),
                "live_submission_network_call_attempted": result.get("live_submission_safety", {}).get("network_call_attempted"),
                "audit_status": result.get("audit", {}).get("status", ""),
                "mock_runtime_status": result.get("mock_runtime", {}).get("status", ""),
                "risk_monitor_status": result.get("risk_monitor", {}).get("status", ""),
                "risk_kill_switch_active": result.get("risk_monitor", {}).get("kill_switch_active"),
                "risk_allow_paper_auto_approve": result.get("risk_monitor", {}).get("allow_paper_auto_approve"),
                "risk_monitor_blocks": result.get("risk_monitor", {}).get("summary", {}).get("blocks", 0),
                "risk_monitor_warnings": result.get("risk_monitor", {}).get("summary", {}).get("warnings", 0),
                "paper_auto_gate_status": result.get("paper_auto_approval_gate", {}).get("status", ""),
                "paper_auto_gate_allow": result.get("paper_auto_approval_gate", {}).get("allow_auto_approve"),
                "paper_auto_gate_reasons": result.get("paper_auto_approval_gate", {}).get("reasons", []),
                "mock_ready": result.get("mock_runtime", {}).get("mock_ready"),
                "mock_running": result.get("mock_runtime", {}).get("mock_running"),
                "live_readiness_status": result.get("live_readiness", {}).get("status", ""),
                "live_ready": result.get("live_readiness", {}).get("live_ready"),
                "data_health_status": result.get("data_health", {}).get("status", ""),
                "data_health_issue_count": result.get("data_health", {}).get("summary", {}).get("issue_count", 0),
                "data_health_degenerate_count": result.get("data_health", {}).get("summary", {}).get("degenerate_snapshot_count", 0),
                "data_health_misaligned_count": result.get("data_health", {}).get("summary", {}).get("misaligned_count", 0),
                "data_health_gap_count": result.get("data_health", {}).get("summary", {}).get("gap_count", 0),
                "data_health_conflict_count": result.get("data_health", {}).get("summary", {}).get("provider_conflict_count", 0),
                "data_health_latest_age_minutes": result.get("data_health", {}).get("summary", {}).get("latest_age_minutes"),
                "pending_count": result.get("pending_count", 0),
                "execution_error": result.get("execution_error", ""),
                "report": result.get("report", ""),
                "review_notes": result.get("review_notes", ""),
            }
        )
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).replace(microsecond=0)
        status.update(
            {
                "state": "error",
                "finished_at": finished_at.isoformat(),
                "next_run_at": (finished_at + timedelta(seconds=interval_seconds)).isoformat(),
                "error": str(exc),
            }
        )
    recorded = store.record(run_date, status)
    if recorded.get("state") == "ok":
        mock_runtime = MockTradingRuntime(output_root).run(run_date)
        recorded = store.record(
            run_date,
            {
                **recorded,
                "mock_runtime_status": mock_runtime.get("status", ""),
                "mock_ready": mock_runtime.get("mock_ready"),
                "mock_running": mock_runtime.get("mock_running"),
            },
        )
        health_result = HealthCheck(output_root).run(run_date)
        alert_summary = AlertNotifier(output_root).run(run_date, health_result)
        recorded = store.record(
            run_date,
            {
                **recorded,
                "health_status": health_result.get("status", ""),
                "alert_count": alert_summary.get("alert_count", 0),
                "alert_delivered": alert_summary.get("delivered", 0),
                "alert_channel": alert_summary.get("channel", ""),
            },
        )
    # Append a compact, queryable run-history record (one line per cycle).
    RunHistory(output_root).append({
        "run_date": run_date,
        "state": recorded.get("state", ""),
        "health_status": recorded.get("health_status", ""),
        "data_source_status": recorded.get("data_source_status", ""),
        "alert_count": recorded.get("alert_count", 0),
        "risk_monitor_status": recorded.get("risk_monitor_status", ""),
        "next_run_at": recorded.get("next_run_at", ""),
    })
    return recorded


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the gold Trading Bot continuously on a local cadence.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Loop interval. Defaults to 5 minutes.")
    parser.add_argument("--iterations", type=int, default=1, help="Number of runner cycles. Use 0 to run forever.")
    parser.add_argument("--paper-auto-approve", action="store_true", help="Auto-approve the first pending ticket if risk allows it.")
    args = parser.parse_args()

    iteration = 0
    while args.iterations == 0 or iteration < args.iterations:
        status = run_runner_once(args.date, args.paper_auto_approve, args.interval_seconds)
        print(f"runner {status['state']} {status['run_date']} next={status.get('next_run_at', 'n/a')}")
        iteration += 1
        if args.iterations != 0 and iteration >= args.iterations:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
