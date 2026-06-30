from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class OfficialFeedOnboarding:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        feed_config = config.get("broker_feed", {})
        raw_feed_dir = Path(os.getenv("TRADING_ORCHESTRATOR_BROKER_FEED_INPUT_DIR") or feed_config.get("input_dir", "data/broker_feeds/gold_5m"))
        self.feed_dir = raw_feed_dir if raw_feed_dir.is_absolute() else ROOT / raw_feed_dir

    def build(self, run_date: str) -> dict:
        self.feed_dir.mkdir(parents=True, exist_ok=True)
        doctor = self._latest("broker_feed_doctor", run_date)
        receipt = self._latest("official_feed_receipts", run_date)
        preflight = self._latest("data_source_preflight", run_date)
        gaps = self._latest("data_gaps", run_date)
        repair = self._latest("data_gap_repair_requests", run_date)
        oanda = self._latest("oanda_feed", run_date)
        template = self.feed_dir / "XAUUSD_5m.csv.template"
        if not template.exists():
            template.write_text(
                "timestamp,open,high,low,close,volume\n2026-05-26T09:00:00+00:00,4570,4572,4569,4571,10\n",
                encoding="utf-8",
            )
        commands = [
            f"python3 -m pipelines.broker_feed_doctor --date {run_date}",
            f"python3 -m pipelines.broker_feed --date {run_date}",
            f"python3 -m pipelines.import_official_feed --date {run_date}",
            f"python3 -m pipelines.live_readiness --date {run_date}",
            f"python3 -m pipelines.official_feed_receipt --date {run_date}",
        ]
        acceptance = [
            "broker_feed_doctor.status == pass",
            "data_source_preflight.ready_for_live == true",
            "official_feed_receipt.official_rows > 0",
            "official_feed_receipt.truth_level == official_broker",
            "live_readiness no longer fails official_market_data",
        ]
        broker_feed_config = self.config.get("broker_feed", {})
        source_config = self.config.get("market_data_sources", {}).get("gold_5m", {})
        price_sanity = source_config.get("price_sanity", {})
        handoff = {
            "target_symbol": broker_feed_config.get("symbol", "GOLD"),
            "broker_symbol": "XAUUSD",
            "target_timeframe": broker_feed_config.get("timeframe", "5m"),
            "target_timeframe_seconds": 300,
            "feed_dir": str(self.feed_dir),
            "file_pattern": broker_feed_config.get("pattern", "*.csv"),
            "sample_filename": f"XAUUSD_5m_{run_date}.csv",
            "required_columns": ["timestamp", "open", "high", "low", "close", "volume"],
            "accepted_timezones": ["UTC", "timezone-aware ISO-8601"],
            "provider_after_import": broker_feed_config.get("provider", "mt5_csv"),
            "price_sanity_range": {
                "enabled": bool(price_sanity.get("enabled", True)),
                "min_price": price_sanity.get("min_price", 3000),
                "max_price": price_sanity.get("max_price", 6000),
            },
            "current_blockers": self._current_blockers(receipt, doctor, oanda, preflight),
            "verification_commands": commands,
            "ready_gates": acceptance,
        }
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ready" if self._official_feed_ready(receipt) else "open",
            "feed_dir": str(self.feed_dir),
            "template": str(template),
            "handoff": handoff,
            "target_timeframe_seconds": handoff["target_timeframe_seconds"],
            "required_columns": handoff["required_columns"],
            "price_sanity_range": handoff["price_sanity_range"],
            "current_blockers": handoff["current_blockers"],
            "gap_request": repair.get("csv_template", ""),
            "latest_gap": gaps.get("latest_gap") or gaps.get("latest_paper_snapshot_gap") or {},
            "current_truth_level": receipt.get("truth_level") or "unknown",
            "current_official_rows": receipt.get("official_rows", 0),
            "latest_public_price": preflight.get("latest_price"),
            "latest_public_provider": preflight.get("latest_provider", ""),
            "oanda_missing_env": oanda.get("missing_env", []),
            "mt5_steps": self._mt5_steps(run_date),
            "oanda_steps": self._oanda_steps(run_date, oanda),
            "commands": commands,
            "acceptance": acceptance,
            "next_action": self._next_action(receipt, doctor, oanda, repair),
            "artifacts": {
                "readme": str(self.feed_dir / "README_XAUUSD_5m.md"),
                "template": str(template),
                "gap_request": repair.get("request_markdown", ""),
                "official_feed_receipt": str(self.output_root / "official_feed_receipts" / f"{run_date}.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "official_feed_onboarding" / "current.json", [payload])
        write_json(self.output_root / "official_feed_onboarding" / f"{run_date}.json", [payload])
        self._write_markdown(payload)
        return payload

    def _mt5_steps(self, run_date: str) -> list[str]:
        return [
            "Open MT5, select XAUUSD/GOLD symbol, timeframe M5.",
            "Export history covering the requested UTC window as CSV or tab-separated MT5 export.",
            f"Save the file into {self.feed_dir} with a real .csv name such as XAUUSD_5m_{run_date}.csv.",
            "Accepted headers: timestamp,open,high,low,close,volume or MT5 <DATE>/<TIME>/<OPEN>/<HIGH>/<LOW>/<CLOSE>/<TICKVOL>.",
            f"Run python3 -m pipelines.import_official_feed --date {run_date}.",
        ]

    def _oanda_steps(self, run_date: str, oanda: dict) -> list[str]:
        missing = ", ".join(oanda.get("missing_env", [])) or "none"
        return [
            "Copy configs/live.env.template to configs/live.env if it does not already exist.",
            f"Fill missing OANDA env keys locally: {missing}.",
            "Use OANDA practice first; do not enable real-money execution from data import alone.",
            f"Run python3 -m pipelines.import_official_feed --date {run_date}.",
        ]

    def _next_action(self, receipt: dict, doctor: dict, oanda: dict, repair: dict) -> str:
        if self._official_feed_ready(receipt):
            return "Official XAUUSD 5m data is live-ready; continue through live_readiness and live_activation gates."
        if repair.get("csv_template"):
            return f"Fill the generated gap CSV template with official broker bars: {repair['csv_template']}"
        if doctor.get("status") != "pass":
            return f"Place a real XAUUSD 5m CSV into {self.feed_dir}, then run broker_feed_doctor."
        if oanda.get("missing_env"):
            return f"Fill OANDA credentials in configs/live.env: {', '.join(oanda['missing_env'])}"
        return "Run import_official_feed and confirm official_feed_receipt passes."

    def _official_feed_ready(self, receipt: dict) -> bool:
        return (
            receipt.get("status") == "pass"
            and bool(receipt.get("ready_for_live"))
            and int(receipt.get("official_rows", 0) or 0) > 0
            and receipt.get("truth_level") == "official_broker"
        )

    def _current_blockers(self, receipt: dict, doctor: dict, oanda: dict, preflight: dict) -> list[dict]:
        blockers: list[dict] = []
        if int(receipt.get("official_rows", 0) or 0) <= 0:
            blockers.append(
                {
                    "name": "official_rows",
                    "status": "blocked",
                    "detail": "No official broker XAUUSD 5m rows are stored in the local market database.",
                    "fix": f"Place a real CSV in {self.feed_dir} or configure OANDA practice credentials, then run import_official_feed.",
                }
            )
        if receipt.get("truth_level") != "official_broker":
            blockers.append(
                {
                    "name": "truth_level",
                    "status": "blocked",
                    "detail": f"Current truth level is {receipt.get('truth_level') or 'unknown'}, not official_broker.",
                    "fix": "Import rows from mt5_csv, broker_csv, oanda, or another configured official broker provider.",
                }
            )
        if doctor.get("status") not in {"pass", ""}:
            blockers.append(
                {
                    "name": "broker_feed_doctor",
                    "status": "blocked",
                    "detail": doctor.get("message", "Broker feed doctor has not passed."),
                    "fix": "Run broker_feed_doctor after adding a valid XAUUSD 5m CSV with UTC timestamps.",
                }
            )
        if oanda.get("missing_env"):
            blockers.append(
                {
                    "name": "oanda_env",
                    "status": "blocked",
                    "detail": f"Missing OANDA env keys: {', '.join(oanda['missing_env'])}.",
                    "fix": "Fill configs/live.env for OANDA practice import before using OANDA as the official feed.",
                }
            )
        if preflight and not preflight.get("ready_for_live", False):
            blockers.append(
                {
                    "name": "data_source_preflight",
                    "status": "blocked",
                    "detail": preflight.get("message", "Data source preflight is not live-ready."),
                    "fix": "Re-run data_source_preflight after official rows are imported and freshness passes.",
                }
            )
        return blockers

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        current = load_json(self.output_root / folder / "current.json")
        candidates = []
        if dated:
            candidates.append(dated[-1])
        if current:
            latest = current[-1]
            if latest.get("run_date") in {None, "", run_date}:
                candidates.append(latest)
        if not candidates:
            return {}
        return max(candidates, key=lambda item: str(item.get("checked_at") or item.get("generated_at") or ""))

    def _write_markdown(self, payload: dict) -> None:
        lines = [
            f"# Official XAUUSD 5m Feed Onboarding - {payload['run_date']}",
            "",
            f"- Status: {payload['status']}",
            f"- Feed directory: `{payload['feed_dir']}`",
            f"- Template: `{payload['template']}`",
            f"- Current truth level: `{payload['current_truth_level']}`",
            f"- Current official rows: `{payload['current_official_rows']}`",
            f"- Latest public price: `{payload['latest_public_price']}` from `{payload['latest_public_provider']}`",
            f"- Next action: {payload['next_action']}",
            "",
            "## Broker Feed Handoff",
            "",
            f"- Target: `{payload['handoff']['broker_symbol']}` as `{payload['handoff']['target_symbol']}`",
            f"- Timeframe: `{payload['handoff']['target_timeframe']}` / `{payload['handoff']['target_timeframe_seconds']}` seconds",
            f"- Sample filename: `{payload['handoff']['sample_filename']}`",
            f"- Required columns: `{', '.join(payload['required_columns'])}`",
            f"- Price sanity range: `{payload['price_sanity_range']['min_price']}` to `{payload['price_sanity_range']['max_price']}`",
            "",
            "## Current Blockers",
            "",
        ]
        if payload["current_blockers"]:
            lines.extend(f"- `{item['name']}`: {item['detail']} Fix: {item['fix']}" for item in payload["current_blockers"])
        else:
            lines.append("- None")
        lines.extend(
            [
                "",
            "## MT5 / Broker CSV Steps",
            "",
            ]
        )
        lines.extend(f"{index}. {item}" for index, item in enumerate(payload["mt5_steps"], start=1))
        lines.extend(["", "## OANDA Steps", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(payload["oanda_steps"], start=1))
        lines.extend(["", "## Verification Commands", "", "```bash"])
        lines.extend(payload["commands"])
        lines.extend(["```", "", "## Acceptance Criteria", ""])
        lines.extend(f"- {item}" for item in payload["acceptance"])
        path = self.output_root / "official_feed_onboarding" / f"{payload['run_date']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
