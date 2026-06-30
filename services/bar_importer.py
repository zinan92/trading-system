from __future__ import annotations

from csv import DictReader, Sniffer
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.market_store import MarketStore


class BarCsvImporter:
    def __init__(self, store: MarketStore | None) -> None:
        self.store = store

    def import_csv(
        self,
        path: Path,
        symbol: str = "GOLD",
        timeframe: str = "5m",
        provider: str = "csv_import",
    ) -> dict:
        rows = []
        with path.open(encoding="utf-8") as handle:
            for raw in self._reader(handle.read()):
                rows.append(self._row_to_bar(raw, symbol, timeframe, provider))
        if self.store is None:
            raise ValueError("MarketStore is required to import CSV rows")
        self.store.upsert_bars(rows)
        return {
            "path": str(path),
            "symbol": symbol,
            "timeframe": timeframe,
            "provider": provider,
            "imported_rows": len(rows),
            "coverage": [item for item in self.store.coverage() if item["symbol"] == symbol and item["timeframe"] == timeframe],
        }

    def _row_to_bar(self, row: dict, symbol: str, timeframe: str, provider: str) -> Bar:
        normalized = self._normalize_row(row)
        timestamp = normalized.get("timestamp") or normalized.get("datetime")
        if not timestamp:
            date_value = normalized.get("date")
            time_value = normalized.get("time")
            timestamp = f"{date_value} {time_value}" if date_value and time_value else (date_value or time_value)
        if not timestamp:
            raise ValueError("CSV row must include timestamp/datetime/time/date")
        timestamp = self._normalize_timestamp(timestamp)
        return Bar(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            open=float(normalized["open"]),
            high=float(normalized["high"]),
            low=float(normalized["low"]),
            close=float(normalized["close"]),
            volume=float(normalized.get("volume") or 0),
            provider=provider,
            quality_flags=self._quality_flags(provider),
        )

    def _quality_flags(self, provider: str) -> list[str]:
        flags = ["csv_import"]
        if provider in {"broker_csv", "mt5_csv", "ibkr", "oanda"}:
            flags.append("official_broker_feed")
        return flags

    def _reader(self, text: str) -> DictReader:
        sample = text[:4096]
        try:
            dialect = Sniffer().sniff(sample, delimiters=",;\t")
        except Exception:
            dialect = "excel"
        return DictReader(text.splitlines(), dialect=dialect)

    def _normalize_row(self, row: dict) -> dict:
        normalized = {}
        for key, value in row.items():
            canonical = self._canonical_column(str(key or ""))
            normalized[canonical] = value
        return normalized

    def _canonical_column(self, column: str) -> str:
        value = column.strip().lower().strip("<>").replace(" ", "_")
        aliases = {
            "time": "time",
            "date": "date",
            "datetime": "datetime",
            "timestamp": "timestamp",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "vol": "volume",
            "tickvol": "volume",
            "tick_volume": "volume",
            "tick_volume_": "volume",
            "tick_volume__": "volume",
        }
        return aliases.get(value, value)

    def _normalize_timestamp(self, timestamp: str) -> str:
        value = timestamp.strip()
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        value = self._normalize_mt5_datetime(value)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _normalize_mt5_datetime(self, value: str) -> str:
        parts = value.split()
        if parts and len(parts[0]) == 10 and parts[0][4] == "." and parts[0][7] == ".":
            parts[0] = parts[0].replace(".", "-")
            return " ".join(parts)
        return value
