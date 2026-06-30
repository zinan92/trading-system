from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class DataGapRepairRequest:
    def __init__(self, output_root: Path, feed_dir: Path) -> None:
        self.output_root = output_root
        self.feed_dir = feed_dir

    def build(self, run_date: str) -> dict:
        gap_rows = load_json(self.output_root / "data_gaps" / "current.json")
        gaps = gap_rows[-1] if gap_rows else {}
        self.feed_dir.mkdir(parents=True, exist_ok=True)
        request_dir = self.output_root / "data_gap_repair_requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        if not gaps or gaps.get("gap_count", 0) == 0:
            payload = {
                "run_date": run_date,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "status": "pass",
                "message": "No GOLD 5m gap repair request needed.",
                "request_markdown": "",
                "csv_template": "",
                "gap_count": 0,
            }
            write_json(request_dir / "current.json", [payload])
            write_json(request_dir / f"{run_date}.json", [payload])
            return payload

        latest = gaps.get("latest_gap", {})
        start = str(latest.get("from_timestamp", "")).replace(":", "").replace("-", "").replace("+", "").replace("Z", "Z")
        end = str(latest.get("to_timestamp", "")).replace(":", "").replace("-", "").replace("+", "").replace("Z", "Z")
        template = self.feed_dir / f"NEEDS_XAUUSD_5m_{start}_to_{end}.csv"
        template.write_text(
            "\n".join(
                [
                    "timestamp,open,high,low,close,volume",
                    "# Replace this comment with official MT5/broker XAUUSD 5m bars for the requested UTC window.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        markdown_path = request_dir / f"{run_date}.md"
        lines = [
            f"# GOLD 5m Data Gap Repair Request - {run_date}",
            "",
            "## Required Action",
            "",
            "Export official MT5/broker XAUUSD 5m OHLCV bars and save the CSV into:",
            "",
            f"`{self.feed_dir}`",
            "",
            "Then run:",
            "",
            "```bash",
            f"python3 -m pipelines.broker_feed_doctor --date {run_date}",
            f"python3 -m pipelines.broker_feed --date {run_date}",
            f"python3 -m pipelines.daily --date {run_date}",
            "```",
            "",
            "## Latest Gap",
            "",
            f"- From: `{latest.get('from_timestamp', 'n/a')}`",
            f"- To: `{latest.get('to_timestamp', 'n/a')}`",
            f"- Gap minutes: `{latest.get('gap_minutes', 'n/a')}`",
            f"- Estimated missing 5m bars: `{latest.get('estimated_missing_bars', 'n/a')}`",
            f"- Provider boundary: `{latest.get('from_provider', 'n/a')} -> {latest.get('to_provider', 'n/a')}`",
            "",
            "## All Open Gaps",
            "",
        ]
        for item in gaps.get("gaps", []):
            lines.append(f"- `{item.get('from_timestamp')}` -> `{item.get('to_timestamp')}` | missing `{item.get('estimated_missing_bars')}` | `{item.get('from_provider')} -> {item.get('to_provider')}`")
        lines.extend(
            [
                "",
                "## Expected CSV Format",
                "",
                "```text",
                "timestamp,open,high,low,close,volume",
                "2026-05-26T01:00:00Z,4570,4572,4569,4571,10",
                "```",
                "",
                f"Template created: `{template}`",
            ]
        )
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "open",
            "message": "GOLD 5m official broker data is required to repair current gaps.",
            "request_markdown": str(markdown_path),
            "csv_template": str(template),
            "gap_count": gaps.get("gap_count", 0),
            "estimated_missing_bars": gaps.get("estimated_missing_bars", 0),
            "latest_gap": latest,
        }
        write_json(request_dir / "current.json", [payload])
        write_json(request_dir / f"{run_date}.json", [payload])
        return payload
