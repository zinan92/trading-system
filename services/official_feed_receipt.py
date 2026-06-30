from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.broker_feed_doctor import BrokerFeedDoctor
from services.config_loader import ROOT, load_pipeline_config
from services.data_source_lineage import DataSourceLineage
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.oanda_feed_client import OandaFeedClient


class OfficialFeedReceipt:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def refresh(self, run_date: str) -> dict:
        oanda_feed = self._latest_oanda_feed()
        feed_doctor = BrokerFeedDoctor(self.output_root).run(run_date)
        feed_import = self._latest_feed_import(run_date, feed_doctor)
        preflight = DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        lineage = DataSourceLineage(self.output_root, self.market_db).run(run_date)
        return self.build(
            run_date,
            oanda_feed=oanda_feed,
            feed_doctor=feed_doctor,
            feed_import=feed_import,
            preflight=preflight,
            lineage=lineage,
        )

    def build(
        self,
        run_date: str,
        *,
        oanda_feed: dict,
        feed_doctor: dict,
        feed_import: dict,
        preflight: dict,
        lineage: dict,
    ) -> dict:
        provider_groups = lineage.get("provider_groups", {})
        official_group = provider_groups.get("official", {})
        official_rows = int(official_group.get("rows") or preflight.get("official_rows") or 0)
        latest_official_bar = lineage.get("latest_official_bar") or {}
        preflight_ready_for_live = bool(preflight.get("ready_for_live"))
        lineage_ready_for_live = bool(lineage.get("ready_for_live"))
        truth_level = lineage.get("truth_level", "unknown")
        ready_for_live = (
            preflight_ready_for_live
            and lineage_ready_for_live
            and truth_level == "official_broker"
            and official_rows > 0
            and bool(latest_official_bar)
        )
        status = "pass" if ready_for_live else "warn"
        next_actions = self._next_actions(status, oanda_feed, feed_doctor, feed_import, preflight)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "ready_for_live": ready_for_live,
            "preflight_ready_for_live": preflight_ready_for_live,
            "lineage_ready_for_live": lineage_ready_for_live,
            "truth_level": truth_level,
            "official_rows": official_rows,
            "official_providers": official_group.get("providers", []),
            "latest_official_bar": latest_official_bar,
            "latest_provider": preflight.get("latest_provider", ""),
            "latest_price": preflight.get("latest_price"),
            "latest_timestamp": preflight.get("latest_timestamp", ""),
            "oanda_feed": {
                "status": oanda_feed.get("status"),
                "ready": oanda_feed.get("ready"),
                "imported_rows": oanda_feed.get("imported_rows", 0),
                "missing_env": oanda_feed.get("missing_env", []),
                "instrument": oanda_feed.get("instrument", "XAU_USD"),
            },
            "broker_csv": {
                "doctor_status": feed_doctor.get("status"),
                "valid_file_count": feed_doctor.get("valid_file_count", 0),
                "file_count": feed_doctor.get("file_count", 0),
                "row_count": feed_doctor.get("row_count", 0),
                "latest_timestamp": feed_doctor.get("latest_timestamp", ""),
                "quality_summary": feed_doctor.get("quality_summary", {}),
                "price_sanity": feed_doctor.get("price_sanity", {}),
                "input_dir": feed_doctor.get("input_dir") or feed_import.get("input_dir"),
                "new_files": feed_import.get("new_files", 0),
                "imported_rows": feed_import.get("imported_rows", 0),
                "imported_files": feed_import.get("imported_files", []),
                "errors": feed_import.get("errors", []),
            },
            "preflight": {
                "status": preflight.get("status"),
                "message": preflight.get("message"),
                "ready_for_paper": preflight.get("ready_for_paper"),
                "ready_for_live": preflight.get("ready_for_live"),
                "official_rows": preflight.get("official_rows", 0),
                "public_rows": preflight.get("public_rows", 0),
            },
            "lineage": {
                "status": lineage.get("status"),
                "message": lineage.get("lineage_message"),
                "ready_for_live": lineage.get("ready_for_live"),
                "market_db": lineage.get("market_db"),
            },
            "next_actions": next_actions,
        }
        write_json(self.output_root / "official_feed_receipts" / "current.json", [payload])
        write_json(self.output_root / "official_feed_receipts" / f"{run_date}.json", [payload])
        self._write_markdown(payload)
        return payload

    def _next_actions(self, status: str, oanda_feed: dict, feed_doctor: dict, feed_import: dict, preflight: dict) -> list[str]:
        if status == "pass":
            return ["Official GOLD/XAUUSD 5m feed is live-ready; keep paper/live gates controlled by live_readiness and live_activation."]
        actions: list[str] = []
        missing_env = oanda_feed.get("missing_env") or []
        if missing_env:
            actions.append("Copy configs/live.env.template to configs/live.env, fill OANDA_API_TOKEN and OANDA_ACCOUNT_ID, then rerun python3 -m pipelines.import_official_feed --date <date>.")
        if feed_doctor.get("status") != "pass":
            actions.append("Put a valid XAUUSD 5m MT5/broker CSV into the broker feed directory and rerun import_official_feed.")
        elif int(feed_import.get("imported_rows", 0) or 0) == 0 and int(preflight.get("official_rows", 0) or 0) == 0:
            actions.append("Add or update the XAUUSD 5m CSV so broker_feed_bridge imports at least one official row.")
        if not preflight.get("ready_for_live"):
            actions.append("Confirm the latest clean GOLD_5m bar comes from an official provider and is fresh enough for live readiness.")
        return actions

    def _write_markdown(self, payload: dict) -> None:
        run_date = payload["run_date"]
        lines = [
            f"# Official Feed Receipt - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Ready for live: {payload['ready_for_live']}",
            f"- Truth level: {payload['truth_level']}",
            f"- Official rows: {payload['official_rows']}",
            f"- Latest provider: {payload['latest_provider']}",
            f"- Latest price: {payload['latest_price']}",
            f"- Latest timestamp: {payload['latest_timestamp']}",
            "",
            "## Sources",
            f"- OANDA: {payload['oanda_feed']['status']} rows={payload['oanda_feed']['imported_rows']}",
            f"- Broker CSV doctor: {payload['broker_csv']['doctor_status']} files={payload['broker_csv']['valid_file_count']}/{payload['broker_csv']['file_count']}",
            f"- Broker CSV import: rows={payload['broker_csv']['imported_rows']} new_files={payload['broker_csv']['new_files']}",
            f"- Broker CSV quality: {payload['broker_csv']['quality_summary']}",
            f"- Broker CSV price sanity: {payload['broker_csv'].get('price_sanity', {})}",
            "",
            "## Next Actions",
        ]
        lines.extend(f"- {item}" for item in payload["next_actions"])
        path = self.output_root / "official_feed_receipts" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _latest_oanda_feed(self) -> dict:
        rows = load_json(self.output_root / "oanda_feed" / "current.json")
        if rows:
            return rows[-1]
        preflight = OandaFeedClient(MarketStore(self.market_db)).preflight()
        return {
            **preflight,
            "status": "skipped",
            "message": "OANDA import has not run; credentials/preflight only.",
            "imported_rows": 0,
        }

    def _latest_feed_import(self, run_date: str, feed_doctor: dict) -> dict:
        rows = load_json(self.output_root / "broker_feed_imports" / "current.json")
        if rows:
            latest = rows[-1]
            if latest.get("run_date") in {None, "", run_date}:
                return latest
        return {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "input_dir": feed_doctor.get("input_dir", ""),
            "pattern": feed_doctor.get("pattern", "*.csv"),
            "provider": feed_doctor.get("provider", "mt5_csv"),
            "symbol": feed_doctor.get("symbol", "GOLD"),
            "timeframe": feed_doctor.get("timeframe", "5m"),
            "new_files": 0,
            "imported_rows": 0,
            "errors": [],
            "status": "not_run",
        }
