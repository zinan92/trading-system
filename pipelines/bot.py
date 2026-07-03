from __future__ import annotations

import argparse
from datetime import date
from services.run_date import utc_run_date

from services.binance_futures_feed import run_binance_usdm_feed_import
from services.broker_adapter import broker_preflight
from services.broker_feed_bridge import BrokerFeedBridge
from services.broker_receipts import BrokerReceiptImporter
from services.completion_audit import CompletionAudit
from services.data_health import run_data_health
from services.data_source_lineage import DataSourceLineage
from services.data_source_preflight import DataSourcePreflight
from services.live_submission_safety import LiveSubmissionSafetySmoke
from services.oanda_feed_client import run_oanda_feed_import
from pipelines.collect import collect_once
from pipelines.daily import run_daily_pipeline
from services.journal_store import JournalStore, load_json
from services.live_readiness import LiveReadiness
from services.mock_runtime import MockTradingRuntime
from services.paper_auto_approval_gate import PaperAutoApprovalGate
from services.pending_auto_resolver import resolve_pending_cycle
from services.reporting import ReportBuilder
from services.risk_monitor import RiskMonitor
from services.strategy_guardrails import StrategyGuardrails


def run_bot_cycle(run_date: str, paper_auto_approve: bool = False) -> dict:
    # Pull a fresh batch of real 5m OHLC bars from Binance USDM XAUUSDT.
    # This populates the bars table with proper open/high/low/close/volume
    # candles. Without this step the only source of "current" market data
    # is gold-api.com — and that only returns single-point spot quotes
    # (H=L=O=C, V=0) which render as disconnected ticks on the chart.
    binance_feed = run_binance_usdm_feed_import(run_date)
    oanda_feed = run_oanda_feed_import(run_date)
    broker_feed = BrokerFeedBridge().import_pending(run_date)
    broker_receipts = BrokerReceiptImporter().import_pending(run_date)
    collector_records = collect_once(run_date)
    paths = run_daily_pipeline(run_date)
    pending = load_json(paths["journal_pending"])
    decision = None
    execution_error = ""
    auto_gate = PaperAutoApprovalGate().evaluate(run_date, auto_requested=paper_auto_approve)
    pre_execution_risk_monitor = auto_gate.get("risk_monitor", {})
    resolution: dict = {"executed": [], "rejected": [], "skipped": [], "errors": [], "decisions": []}
    if paper_auto_approve and pending:
        # Autonomous: terminate EVERY pending ticket this cycle (execute the gate-approved
        # primary; auto-reject the rest with a reason). No ticket is left in the manual
        # middle-state or silently dropped by the next journal overwrite.
        resolution = resolve_pending_cycle(
            run_date,
            pending,
            auto_approve=paper_auto_approve,
            gate_allows=auto_gate.get("allow_auto_approve", False),
            gate_reasons=auto_gate.get("reasons", []),
            store=JournalStore(),
        )
        if resolution["decisions"]:
            decision = resolution["decisions"][0]
        if resolution["errors"]:
            execution_error = resolution["errors"][0]["error"]
        elif not resolution["executed"] and not auto_gate.get("allow_auto_approve", False):
            execution_error = f"paper auto-approval gate blocks execution: {'; '.join(auto_gate.get('reasons', [])) or 'manual review required'}"
    report_path = ReportBuilder().build_daily_report(run_date)
    review_path = report_path.parents[1] / "review_notes" / f"{run_date}.md"
    journal_path = report_path.parents[1] / "journals" / f"{run_date}.md"
    broker_status = broker_preflight()
    data_source_status = DataSourcePreflight().run(run_date)
    data_source_lineage = DataSourceLineage().run(run_date)
    live_submission_safety = LiveSubmissionSafetySmoke().run(run_date)
    audit = CompletionAudit().run(run_date)
    mock_runtime = MockTradingRuntime().run(run_date)
    risk_monitor = RiskMonitor().run(run_date)
    live_readiness = LiveReadiness().run(run_date)
    data_health = run_data_health(run_date)
    return {
        "binance_feed": binance_feed,
        "oanda_feed": oanda_feed,
        "broker_feed": broker_feed,
        "broker_receipts": broker_receipts,
        "collector_records": collector_records,
        "paths": {name: str(path) for name, path in paths.items()},
        "pending_count": len(load_json(paths["journal_pending"])),
        "decision": decision,
        "auto_resolution": resolution,
        "execution_error": execution_error,
        "report": str(report_path),
        "review_notes": str(review_path),
        "journal": str(journal_path),
        "broker_preflight": broker_status,
        "data_source_preflight": data_source_status,
        "data_source_lineage": data_source_lineage,
        "live_submission_safety": live_submission_safety,
        "audit": audit,
        "mock_runtime": mock_runtime,
        "paper_auto_approval_gate": auto_gate,
        "pre_execution_risk_monitor": pre_execution_risk_monitor,
        "risk_monitor": risk_monitor,
        "live_readiness": live_readiness,
        "data_health": data_health,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one complete gold Trading Bot cycle.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    parser.add_argument("--paper-auto-approve", action="store_true", help="Auto-execute the first pending ticket in the local paper account.")
    args = parser.parse_args()

    result = run_bot_cycle(args.date, paper_auto_approve=args.paper_auto_approve)
    print("Trading Bot cycle completed.")
    print(f"binance feed: {result['binance_feed']['status']} rows={result['binance_feed']['imported_rows']} latest={result['binance_feed'].get('latest_timestamp', '')} px={result['binance_feed'].get('latest_price', '')}")
    print(f"oanda feed: {result['oanda_feed']['status']} rows={result['oanda_feed']['imported_rows']}")
    print(f"broker feed: files={result['broker_feed']['new_files']} rows={result['broker_feed']['imported_rows']}")
    print(f"broker receipts: new={result['broker_receipts']['new_receipts']} total={result['broker_receipts']['total_receipts']}")
    for item in result["collector_records"]:
        print(f"collected: {item['symbol']} {item['timeframe']} {item['close']} {item['provider']} {item['timestamp']}")
    if result["decision"]:
        print(f"paper decision: {result['decision']['ticket_id']} {result['decision']['decision_status']}")
    if result["execution_error"]:
        print(f"paper decision blocked: {result['execution_error']}")
    print(f"pending_count: {result['pending_count']}")
    print(f"data_source: {result['data_source_preflight']['status']} {result['data_source_preflight']['message']}")
    print(f"data_lineage: {result['data_source_lineage']['status']} truth={result['data_source_lineage']['truth_level']} live={result['data_source_lineage']['ready_for_live']}")
    print(f"live_submission_safety: {result['live_submission_safety']['status']} blocked={result['live_submission_safety']['blocked_by_activation_gate']}")
    print(f"audit: {result['audit']['status']}")
    print(f"mock_runtime: {result['mock_runtime']['status']} mock_ready={result['mock_runtime']['mock_ready']} mock_running={result['mock_runtime']['mock_running']}")
    print(f"risk_monitor: {result['risk_monitor']['status']} kill_switch={result['risk_monitor']['kill_switch_active']}")
    print(f"live_readiness: {result['live_readiness']['status']} live_ready={result['live_readiness']['live_ready']}")
    print(f"report: {result['report']}")
    print(f"review_notes: {result['review_notes']}")
    print(f"journal: {result['journal']}")


if __name__ == "__main__":
    main()
