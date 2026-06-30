from __future__ import annotations

import os
from csv import DictReader
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.bar_importer import BarCsvImporter


class BrokerFeedDoctor:
    required_price_columns = {"open", "high", "low", "close"}
    time_columns = ("timestamp", "datetime", "time", "date")

    def __init__(self, output_root: Path | None = None, config: dict | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.config = config or pipeline_config.get("broker_feed", {})
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
        self.input_dir = self._resolve_path(os.getenv("TRADING_ORCHESTRATOR_BROKER_FEED_INPUT_DIR") or self.config.get("input_dir", "data/broker_feeds/gold_5m"))
        self.pattern = str(self.config.get("pattern", "*.csv"))
        self.provider = str(self.config.get("provider", "mt5_csv"))
        self.symbol = str(self.config.get("symbol", "GOLD"))
        self.timeframe = str(self.config.get("timeframe", "5m"))
        self.csv_mapper = BarCsvImporter(store=None)
        source_config = pipeline_config.get("market_data_sources", {}).get("gold_5m", {})
        sanity = source_config.get("price_sanity", {})
        self.price_sanity_enabled = bool(sanity.get("enabled", True))
        self.min_price = float(sanity.get("min_price", 0))
        self.max_price = float(sanity.get("max_price", 999999))

    def run(self, run_date: str) -> dict:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        docs = self._ensure_feed_docs(run_date)
        files = [self.inspect_file(path) for path in sorted(self.input_dir.glob(self.pattern)) if path.is_file() and not self._is_helper_file(path)]
        valid_files = [item for item in files if item["status"] == "pass"]
        errors = [error for item in files for error in item.get("errors", [])]
        warnings = [warning for item in files for warning in item.get("warnings", [])]
        latest_timestamps = [item["latest_timestamp"] for item in valid_files if item.get("latest_timestamp")]
        if not files:
            status = "warn"
            message = "no broker/MT5 XAUUSD 5m CSV files found"
        elif errors:
            status = "fail"
            message = "one or more broker/MT5 CSV files failed validation"
        elif warnings:
            status = "warn"
            message = "broker/MT5 CSV feed files are importable but need review before live"
        else:
            status = "pass"
            message = "broker/MT5 CSV feed files are valid for import"
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "message": message,
            "input_dir": str(self.input_dir),
            "pattern": self.pattern,
            "provider": self.provider,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "file_count": len(files),
            "valid_file_count": len(valid_files),
            "row_count": sum(int(item.get("row_count", 0)) for item in valid_files),
            "latest_timestamp": max(latest_timestamps) if latest_timestamps else "",
            "quality_summary": self._quality_summary(files),
            "price_sanity": {
                "enabled": self.price_sanity_enabled,
                "min_price": self.min_price,
                "max_price": self.max_price,
            },
            "files": files,
            "expected_headers": ["timestamp", "open", "high", "low", "close", "volume"],
            "accepted_formats": [
                "timestamp,open,high,low,close,volume",
                "<DATE>\\t<TIME>\\t<OPEN>\\t<HIGH>\\t<LOW>\\t<CLOSE>\\t<TICKVOL>",
            ],
            "readme": str(docs["readme"]),
            "template": str(docs["template"]),
        }
        write_json(self.output_root / "broker_feed_doctor" / "current.json", [payload])
        write_json(self.output_root / "broker_feed_doctor" / f"{run_date}.json", [payload])
        return payload

    def _is_helper_file(self, path: Path) -> bool:
        return path.name.startswith("NEEDS_") or path.name.endswith(".template") or path.name.upper().startswith("README")

    def _ensure_feed_docs(self, run_date: str) -> dict[str, Path]:
        readme = self.input_dir / "README_XAUUSD_5m.md"
        template = self.input_dir / "XAUUSD_5m.csv.template"
        if not readme.exists():
            readme.write_text(
                "\n".join(
                    [
                        "# XAUUSD 5m Broker Feed",
                        "",
                        "Put real MT5/broker XAUUSD 5m CSV files in this directory.",
                        "",
                        "Accepted standard CSV:",
                        "",
                        "```csv",
                        "timestamp,open,high,low,close,volume",
                        "2026-05-26T09:00:00+00:00,4570,4572,4569,4571,10",
                        "```",
                        "",
                        "Accepted MT5 tab export:",
                        "",
                        "```tsv",
                        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>",
                        "2026.05.26\t09:00:00\t4570\t4572\t4569\t4571\t10",
                        "```",
                        "",
                        "Use a real `.csv` filename such as `XAUUSD_5m.csv`. Files ending in `.template` are ignored.",
                        "",
                        "After adding or replacing the CSV, run:",
                        "",
                        "```bash",
                        f"python3 -m pipelines.import_official_feed --date {run_date}",
                        "```",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        if not template.exists():
            template.write_text(
                "\n".join(
                    [
                        "timestamp,open,high,low,close,volume",
                        "2026-05-26T09:00:00+00:00,4570,4572,4569,4571,10",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        return {"readme": readme, "template": template}

    def inspect_file(self, path: Path) -> dict:
        errors: list[str] = []
        warnings: list[str] = []
        timestamps: list[str] = []
        parsed_timestamps: list[datetime] = []
        closes: list[float] = []
        duplicate_timestamps = 0
        invalid_ohlc = 0
        seen_timestamps: set[str] = set()
        row_count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                reader = self.csv_mapper._reader(handle.read())
                headers = [self.csv_mapper._canonical_column(item) for item in (reader.fieldnames or []) if item]
                time_column = next((item for item in self.time_columns if item in headers), "")
                missing = sorted(self.required_price_columns - set(headers))
                if not time_column:
                    errors.append("missing timestamp/datetime/time/date column")
                if missing:
                    errors.append(f"missing price columns: {', '.join(missing)}")
                for row_index, row in enumerate(reader, start=2):
                    if errors and row_index == 2:
                        continue
                    row_count += 1
                    try:
                        normalized = self.csv_mapper._normalize_row(row)
                        timestamp_value = normalized.get("timestamp") or normalized.get("datetime")
                        if not timestamp_value:
                            date_value = normalized.get("date")
                            time_value = normalized.get("time")
                            timestamp_value = f"{date_value} {time_value}" if date_value and time_value else (date_value or time_value or "")
                        timestamp = self._normalize_timestamp(str(timestamp_value))
                        if timestamp in seen_timestamps:
                            duplicate_timestamps += 1
                        seen_timestamps.add(timestamp)
                        timestamps.append(timestamp)
                        parsed_timestamps.append(datetime.fromisoformat(timestamp))
                        open_price = float(normalized["open"])
                        high_price = float(normalized["high"])
                        low_price = float(normalized["low"])
                        close_price = float(normalized["close"])
                        for column in self.required_price_columns:
                            float(normalized[column])
                        closes.append(close_price)
                        if high_price < max(open_price, close_price, low_price) or low_price > min(open_price, close_price, high_price):
                            invalid_ohlc += 1
                        if self.price_sanity_enabled and (close_price < self.min_price or close_price > self.max_price):
                            errors.append(
                                f"row {row_index}: close {close_price:.2f} outside sanity range {self.min_price:.2f}-{self.max_price:.2f}"
                            )
                        if normalized.get("volume") not in (None, ""):
                            float(normalized["volume"])
                    except (KeyError, TypeError, ValueError) as exc:
                        errors.append(f"row {row_index}: {exc}")
                        if len(errors) >= 5:
                            break
                if row_count == 0 and not errors:
                    errors.append("no data rows")
        except OSError as exc:
            errors.append(str(exc))
        interval_audit = self._interval_audit(parsed_timestamps)
        if duplicate_timestamps:
            warnings.append(f"{duplicate_timestamps} duplicate timestamp(s)")
        if invalid_ohlc:
            errors.append(f"{invalid_ohlc} row(s) have invalid OHLC bounds")
        if interval_audit["non_5m_intervals"]:
            warnings.append(f"{interval_audit['non_5m_intervals']} non-5m interval(s)")
        if interval_audit["gap_intervals"]:
            warnings.append(f"{interval_audit['gap_intervals']} gap interval(s) greater than 5m")
        return {
            "path": str(path),
            "status": "fail" if errors else ("warn" if warnings else "pass"),
            "row_count": row_count,
            "first_timestamp": min(timestamps) if timestamps else "",
            "latest_timestamp": max(timestamps) if timestamps else "",
            "duplicate_timestamps": duplicate_timestamps,
            "invalid_ohlc_rows": invalid_ohlc,
            "min_close": min(closes) if closes else None,
            "max_close": max(closes) if closes else None,
            "interval_audit": interval_audit,
            "warnings": warnings,
            "errors": errors,
        }

    def _interval_audit(self, timestamps: list[datetime]) -> dict:
        sorted_times = sorted(set(timestamps))
        deltas = [
            int((right - left).total_seconds())
            for left, right in zip(sorted_times, sorted_times[1:])
        ]
        non_5m = [delta for delta in deltas if delta != 300]
        gaps = [delta for delta in deltas if delta > 300]
        return {
            "expected_seconds": 300,
            "checked_intervals": len(deltas),
            "non_5m_intervals": len(non_5m),
            "gap_intervals": len(gaps),
            "max_gap_seconds": max(gaps) if gaps else 0,
            "unique_timestamps": len(sorted_times),
        }

    def _quality_summary(self, files: list[dict]) -> dict:
        return {
            "files": len(files),
            "pass": sum(1 for item in files if item.get("status") == "pass"),
            "warn": sum(1 for item in files if item.get("status") == "warn"),
            "fail": sum(1 for item in files if item.get("status") == "fail"),
            "duplicate_timestamps": sum(int(item.get("duplicate_timestamps", 0)) for item in files),
            "invalid_ohlc_rows": sum(int(item.get("invalid_ohlc_rows", 0)) for item in files),
            "non_5m_intervals": sum(int((item.get("interval_audit") or {}).get("non_5m_intervals", 0)) for item in files),
            "gap_intervals": sum(int((item.get("interval_audit") or {}).get("gap_intervals", 0)) for item in files),
            "min_close": min((item.get("min_close") for item in files if item.get("min_close") is not None), default=None),
            "max_close": max((item.get("max_close") for item in files if item.get("max_close") is not None), default=None),
        }

    def _normalize_timestamp(self, timestamp: str) -> str:
        value = timestamp.strip()
        if not value:
            raise ValueError("empty timestamp")
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(self.csv_mapper._normalize_mt5_datetime(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _resolve_path(self, path_value: str) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        return ROOT / path
