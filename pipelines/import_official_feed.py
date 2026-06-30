from __future__ import annotations

import argparse
import json
from datetime import date

from pipelines.daily import run_daily_pipeline
from services.broker_feed_bridge import BrokerFeedBridge
from services.broker_feed_doctor import BrokerFeedDoctor
from services.completion_audit import CompletionAudit
from services.data_archive_manifest import DataArchiveManifest
from services.data_source_lineage import DataSourceLineage
from services.data_source_preflight import DataSourcePreflight
from services.daily_review_runner import DailyReviewRunner
from services.health_check import HealthCheck
from services.journal_store import load_json, write_json
from services.live_submission_safety import LiveSubmissionSafetySmoke
from services.live_readiness import LiveReadiness
from services.mock_runtime import MockTradingRuntime
from services.oanda_feed_client import run_oanda_feed_import
from services.official_feed_receipt import OfficialFeedReceipt
from services.system_doctor import SystemDoctor


def run_import_official_feed(run_date: str) -> dict:
    oanda_feed = run_oanda_feed_import(run_date)
    feed_doctor = BrokerFeedDoctor().run(run_date)
    feed_import = BrokerFeedBridge().import_pending(run_date)
    daily_paths = run_daily_pipeline(run_date)
    preflight_service = DataSourcePreflight()
    preflight = preflight_service.run(run_date)
    _write_official_quote_snapshot(preflight_service.output_root, run_date, preflight)
    lineage = DataSourceLineage().run(run_date)
    official_feed_receipt = OfficialFeedReceipt(preflight_service.output_root).build(
        run_date,
        oanda_feed=oanda_feed,
        feed_doctor=feed_doctor,
        feed_import=feed_import,
        preflight=preflight,
        lineage=lineage,
    )
    live_submission_safety = LiveSubmissionSafetySmoke().run(run_date)
    daily_review = DailyReviewRunner().run(run_date)
    health = HealthCheck().run(run_date)
    audit = CompletionAudit().run(run_date)
    mock_runtime = MockTradingRuntime().run(run_date)
    live_readiness = LiveReadiness().run(run_date)
    doctor = SystemDoctor().run(run_date)
    data_archive = DataArchiveManifest().run(run_date)
    return {
        "run_date": run_date,
        "oanda_feed": oanda_feed,
        "feed_doctor": feed_doctor,
        "feed_import": feed_import,
        "daily_paths": {name: str(path) for name, path in daily_paths.items()},
        "data_source_preflight": preflight,
        "data_source_lineage": lineage,
        "official_feed_receipt": official_feed_receipt,
        "live_submission_safety": live_submission_safety,
        "daily_review": daily_review,
        "health": health,
        "audit": audit,
        "mock_runtime": mock_runtime,
        "live_readiness": live_readiness,
        "doctor": doctor,
        "data_archive": data_archive,
    }


def _write_official_quote_snapshot(output_root, run_date: str, preflight: dict) -> None:
    latest = preflight.get("latest_bar") or {}
    if not latest:
        return
    path = output_root / "raw_snapshots" / run_date / "quote_snapshots.json"
    rows = load_json(path)
    record = {
        "collected_at": preflight.get("checked_at", ""),
        "symbol": latest.get("symbol", "GOLD"),
        "timeframe": latest.get("timeframe", "5m"),
        "timestamp": latest.get("timestamp", ""),
        "close": latest.get("close"),
        "provider": latest.get("provider", ""),
        "quality_flags": latest.get("quality_flags", []),
        "record_type": "bar_as_quote",
        "local_db": preflight.get("market_db", ""),
        "stored_rows_seen": preflight.get("official_rows", 0) or preflight.get("public_rows", 0),
    }
    key = (record["symbol"], record["timeframe"], record["timestamp"], record["record_type"])
    existing_keys = {(item.get("symbol"), item.get("timeframe"), item.get("timestamp"), item.get("record_type")) for item in rows}
    if key not in existing_keys:
        write_json(path, rows + [record])


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official XAUUSD 5m feed, refresh the pipeline, and rerun readiness gates.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = run_import_official_feed(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    preflight = result["data_source_preflight"]
    doctor = result["doctor"]
    print(f"official feed import completed: date={args.date}")
    print(f"oanda feed: {result['oanda_feed']['status']} rows={result['oanda_feed']['imported_rows']}")
    print(f"feed doctor: {result['feed_doctor']['status']} files={result['feed_doctor']['valid_file_count']}/{result['feed_doctor']['file_count']}")
    print(f"feed import: new_files={result['feed_import']['new_files']} rows={result['feed_import']['imported_rows']}")
    print(f"data source: {preflight['status']} paper_ready={preflight['ready_for_paper']} live_ready={preflight['ready_for_live']}")
    print(f"data lineage: {result['data_source_lineage']['status']} truth={result['data_source_lineage']['truth_level']} official_rows={result['data_source_lineage']['provider_groups']['official']['rows']}")
    print(f"official feed receipt: {result['official_feed_receipt']['status']} official_rows={result['official_feed_receipt']['official_rows']} live={result['official_feed_receipt']['ready_for_live']}")
    print(f"latest bar: {preflight['latest_price']} provider={preflight['latest_provider']} timestamp={preflight['latest_timestamp']}")
    print(f"live submission safety: {result['live_submission_safety']['status']} blocked={result['live_submission_safety']['blocked_by_activation_gate']}")
    print(f"daily review: {result['daily_review']['status']} mock={result['daily_review']['summary'].get('mock_runtime')}")
    print(f"mock runtime: {result['mock_runtime']['status']} mock_ready={result['mock_runtime']['mock_ready']} mock_running={result['mock_runtime']['mock_running']}")
    print(f"live readiness: {result['live_readiness']['status']} live_ready={result['live_readiness']['live_ready']}")
    print(f"data archive: {result['data_archive']['status']} files={result['data_archive']['present_file_count']}/{result['data_archive']['file_count']}")
    print(f"doctor: {doctor['status']} health={doctor['summary']['health']} audit={doctor['summary']['audit']}")
    for action in doctor["next_actions"]:
        print(f"- {action}")


if __name__ == "__main__":
    main()
