from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class DataGapDoctor:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def run(self, run_date: str, symbol: str = "GOLD", timeframe: str = "5m") -> dict:
        bars = load_json(self.output_root / "clean_bars" / run_date / f"{symbol}_{timeframe}.json")
        manifest_rows = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        manifest = next((item for item in manifest_rows if item.get("symbol") == symbol and item.get("timeframe") == timeframe), {})
        expected_seconds = self._timeframe_seconds(timeframe)
        gaps, snapshot_gaps = self._gaps(bars, expected_seconds)
        missing_bars = sum(item["estimated_missing_bars"] for item in gaps)
        clean_rows = len(bars)
        missing_ratio = round(missing_bars / clean_rows, 4) if clean_rows else 1.0
        status = "pass" if not gaps and not snapshot_gaps else ("warn" if not gaps or missing_ratio <= 0.05 else "fail")
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "symbol": symbol,
            "timeframe": timeframe,
            "clean_rows": clean_rows,
            "gap_count": len(gaps),
            "paper_snapshot_gap_count": len(snapshot_gaps),
            "estimated_missing_bars": missing_bars,
            "missing_ratio": missing_ratio,
            "manifest_missing_bars": manifest.get("missing_bars", 0),
            "largest_gap_minutes": max((item["gap_minutes"] for item in gaps + snapshot_gaps), default=0),
            "latest_gap": gaps[-1] if gaps else {},
            "latest_paper_snapshot_gap": snapshot_gaps[-1] if snapshot_gaps else {},
            "gaps": gaps[-20:],
            "paper_snapshot_gaps": snapshot_gaps[-20:],
            "repair_actions": self._repair_actions(gaps, snapshot_gaps),
        }
        write_json(self.output_root / "data_gaps" / "current.json", [payload])
        write_json(self.output_root / "data_gaps" / f"{run_date}.json", [payload])
        return payload

    def _gaps(self, bars: list[dict], expected_seconds: int) -> tuple[list[dict], list[dict]]:
        gaps = []
        snapshot_gaps = []
        previous = None
        previous_time = None
        for bar in bars:
            current_time = self._parse_time(str(bar.get("timestamp", "")))
            if previous and previous_time and current_time:
                gap_seconds = (current_time - previous_time).total_seconds()
                if gap_seconds > expected_seconds * 1.5:
                    if "market_closure_before" in (bar.get("quality_flags") or []):
                        previous = bar
                        previous_time = current_time
                        continue
                    is_paper_snapshot_gap = "quote_snapshot_gap_before" in (bar.get("quality_flags") or [])
                    estimated = max(1, round(gap_seconds / expected_seconds) - 1)
                    gap = {
                        "from_timestamp": previous.get("timestamp", ""),
                        "to_timestamp": bar.get("timestamp", ""),
                        "gap_minutes": round(gap_seconds / 60, 2),
                        "estimated_missing_bars": estimated,
                        "from_provider": previous.get("provider", ""),
                        "to_provider": bar.get("provider", ""),
                        "from_close": previous.get("close"),
                        "to_close": bar.get("close"),
                        "to_flags": bar.get("quality_flags", []),
                    }
                    if is_paper_snapshot_gap:
                        snapshot_gaps.append({**gap, "gap_type": "paper_quote_snapshot"})
                    else:
                        gaps.append(gap)
            previous = bar
            previous_time = current_time
        return gaps, snapshot_gaps

    def _repair_actions(self, gaps: list[dict], snapshot_gaps: list[dict] | None = None) -> list[str]:
        snapshot_gaps = snapshot_gaps or []
        if not gaps and not snapshot_gaps:
            return ["No 5m gap repair needed."]
        if not gaps and snapshot_gaps:
            latest = snapshot_gaps[-1]
            return [
                "Paper mode is using the latest public spot quote as a quote-derived snapshot bar.",
                f"For continuous 5m evidence, import official XAUUSD bars covering {latest['from_timestamp']} through {latest['to_timestamp']}.",
            ]
        actions = ["Run broker feed doctor, then import official XAUUSD 5m CSV covering the gap window."]
        latest = gaps[-1]
        actions.append(f"Required window starts after {latest['from_timestamp']} and must cover through {latest['to_timestamp']}.")
        if latest.get("from_provider") == latest.get("to_provider") == "gold-api.com":
            actions.append("Public spot snapshots are not a continuous 5m feed; use MT5/broker bars for continuity.")
        return actions

    def _parse_time(self, value: str) -> datetime | None:
        if not value or value.startswith("mock-"):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _timeframe_seconds(self, timeframe: str) -> int:
        unit = timeframe[-1:]
        multiplier = {"m": 60, "h": 3600, "d": 86400}.get(unit)
        if not multiplier:
            return 300
        try:
            return int(timeframe[:-1]) * multiplier
        except ValueError:
            return 300
