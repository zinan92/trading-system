from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import Bar


class MarketStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        # WAL + a generous busy_timeout so the two 300s writers (runner's binance
        # feed and the strategies job) wait on a lock instead of raising
        # "database is locked" on the shared market DB.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def upsert_bars(self, bars: list[Bar]) -> None:
        if not bars:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO bars
                (symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bar.symbol,
                        bar.timeframe,
                        bar.timestamp,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.provider,
                        ",".join(bar.quality_flags),
                    )
                    for bar in bars
                ],
            )

    def upsert_quote(self, quote: Bar) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO quotes
                (symbol, timestamp, price, provider, quality_flags)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    quote.symbol,
                    quote.timestamp,
                    quote.close,
                    quote.provider,
                    ",".join(quote.quality_flags),
                ),
            )

    def load_latest_quote(self, symbol: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT symbol, timestamp, price, provider, quality_flags
                FROM quotes
                WHERE symbol = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if not row:
            return {}
        return {
            "symbol": row[0],
            "timestamp": row[1],
            "close": float(row[2]),
            "provider": row[3],
            "quality_flags": [item for item in str(row[4]).split(",") if item],
            "record_type": "quote",
        }

    def load_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags
                FROM bars
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (symbol, timeframe, limit),
            ).fetchall()
        bars = [
            Bar(
                symbol=row[0],
                timeframe=row[1],
                timestamp=row[2],
                open=float(row[3]),
                high=float(row[4]),
                low=float(row[5]),
                close=float(row[6]),
                volume=float(row[7]),
                provider=row[8],
                quality_flags=[item for item in str(row[9]).split(",") if item],
            )
            for row in rows
        ]
        return list(reversed(bars))

    def load_bars_between(self, symbol: str, timeframe: str, start_timestamp: str, end_timestamp: str) -> list[Bar]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags
                FROM bars
                WHERE symbol = ? AND timeframe = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (symbol, timeframe, start_timestamp, end_timestamp),
            ).fetchall()
        return [
            Bar(
                symbol=row[0],
                timeframe=row[1],
                timestamp=row[2],
                open=float(row[3]),
                high=float(row[4]),
                low=float(row[5]),
                close=float(row[6]),
                volume=float(row[7]),
                provider=row[8],
                quality_flags=[item for item in str(row[9]).split(",") if item],
            )
            for row in rows
        ]

    def load_aggregated_bars_between(
        self,
        symbol: str,
        source_timeframe: str,
        target_timeframe: str,
        start_timestamp: str,
        end_timestamp: str,
        target_seconds: int,
        limit: int,
    ) -> list[dict]:
        """Aggregate lower-timeframe bars in SQLite for replay.

        Replay needs point-in-time higher timeframe candles without pulling
        months of 1m bars into Python. This returns buckets whose source rows
        are all <= end_timestamp; the currently forming bucket is included and
        can be marked partial by the caller.
        """
        if target_seconds <= 0 or limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    (CAST(CAST(strftime('%s', timestamp) AS INTEGER) / ? AS INTEGER) * ?) AS bucket_epoch,
                    MIN(timestamp) AS first_timestamp,
                    MAX(timestamp) AS last_timestamp,
                    MAX(high) AS high,
                    MIN(low) AS low,
                    SUM(volume) AS volume,
                    GROUP_CONCAT(quality_flags, '|') AS quality_flags
                FROM bars
                WHERE symbol = ?
                  AND timeframe = ?
                  AND timestamp >= ?
                  AND timestamp <= ?
                GROUP BY bucket_epoch
                ORDER BY bucket_epoch ASC
                """,
                (target_seconds, target_seconds, symbol, source_timeframe, start_timestamp, end_timestamp),
            ).fetchall()
            rows = rows[-limit:]
            first_rows = {
                row[0]: conn.execute(
                    """
                    SELECT symbol, open, provider
                    FROM bars
                    WHERE symbol = ? AND timeframe = ? AND timestamp = ?
                    LIMIT 1
                    """,
                    (symbol, source_timeframe, row[1]),
                ).fetchone()
                for row in rows
            }
            last_rows = {
                row[0]: conn.execute(
                    """
                    SELECT close, provider
                    FROM bars
                    WHERE symbol = ? AND timeframe = ? AND timestamp = ?
                    LIMIT 1
                    """,
                    (symbol, source_timeframe, row[2]),
                ).fetchone()
                for row in rows
            }
        results = []
        for row in rows:
            bucket_epoch = int(row[0])
            first = first_rows.get(row[0])
            last = last_rows.get(row[0])
            if not first or not last:
                continue
            flags = set()
            for chunk in str(row[6] or "").split("|"):
                flags.update(item for item in chunk.split(",") if item)
            flags.update({"derived_timeframe", f"source_{source_timeframe}"})
            bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).replace(microsecond=0)
            bucket_end = datetime.fromtimestamp(bucket_epoch + target_seconds, tz=timezone.utc).replace(microsecond=0)
            results.append({
                "symbol": first[0],
                "timeframe": target_timeframe,
                "timestamp": bucket_start.isoformat(),
                "open": float(first[1]),
                "high": float(row[3]),
                "low": float(row[4]),
                "close": float(last[0]),
                "volume": float(row[5]),
                "provider": f"derived:{last[1]}",
                "quality_flags": sorted(flags),
                "bucket_end": bucket_end.isoformat(),
            })
        return results

    def load_latest_bar(self, symbol: str, timeframe: str, providers: list[str] | None = None) -> dict:
        query = """
            SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags
            FROM bars
            WHERE symbol = ? AND timeframe = ?
        """
        params: list[object] = [symbol, timeframe]
        if providers:
            placeholders = ",".join("?" for _ in providers)
            query += f" AND provider IN ({placeholders})"
            params.extend(providers)
        query += " ORDER BY timestamp DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            return {}
        return {
            "symbol": row[0],
            "timeframe": row[1],
            "timestamp": row[2],
            "open": float(row[3]),
            "high": float(row[4]),
            "low": float(row[5]),
            "close": float(row[6]),
            "volume": float(row[7]),
            "provider": row[8],
            "quality_flags": [item for item in str(row[9]).split(",") if item],
            "record_type": "bar",
        }

    def load_bar_at_or_before(self, symbol: str, timeframe: str, timestamp: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags
                FROM bars
                WHERE symbol = ? AND timeframe = ? AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (symbol, timeframe, timestamp),
            ).fetchone()
        if not row:
            return {}
        return {
            "symbol": row[0],
            "timeframe": row[1],
            "timestamp": row[2],
            "open": float(row[3]),
            "high": float(row[4]),
            "low": float(row[5]),
            "close": float(row[6]),
            "volume": float(row[7]),
            "provider": row[8],
            "quality_flags": [item for item in str(row[9]).split(",") if item],
            "record_type": "bar",
        }

    def delete_bars(self, symbol: str, timeframe: str, provider: str) -> int:
        """Delete all bars for a symbol/timeframe from a single provider.

        Used when one provider's series is replaced wholesale by another (e.g.
        swapping the COMEX yahoo_chart:GC=F proxy for the Binance XAUUSDT
        perpetual so the chart is one coherent instrument). Returns the count
        of deleted rows.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM bars WHERE symbol=? AND timeframe=? AND provider=?",
                (symbol, timeframe, provider),
            )
            return int(cursor.rowcount)

    def coverage(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, timeframe, provider, COUNT(*), MIN(timestamp), MAX(timestamp)
                FROM bars
                GROUP BY symbol, timeframe, provider
                ORDER BY symbol, timeframe, provider
                """
            ).fetchall()
        return [
            {
                "symbol": row[0],
                "timeframe": row[1],
                "provider": row[2],
                "rows": int(row[3]),
                "first_timestamp": row[4],
                "last_timestamp": row[5],
            }
            for row in rows
        ]

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    provider TEXT NOT NULL,
                    quality_flags TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, timestamp)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    price REAL NOT NULL,
                    provider TEXT NOT NULL,
                    quality_flags TEXT NOT NULL,
                    PRIMARY KEY (symbol, timestamp)
                )
                """
            )
