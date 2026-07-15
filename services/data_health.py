"""Data health auditor for the local market_data.db bars table.

Surfaces the *real* state of the data the chart and signal pipeline depend
on — gaps, degenerate snapshot rows, timestamp misalignment, provider
conflicts where two sources occupy the same 5min window, suspicious price
jumps, and overall staleness. Used both as a CLI (``pipelines.data_health``)
and as a JSON source for the dashboard's data-health panel.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from services.config_loader import ROOT, load_pipeline_config
from services.market_data_access import market_data_repository, uses_independent_datafeed


_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


@dataclass(frozen=True)
class _BarRow:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str
    quality_flags: list[str] = field(default_factory=list)

    def parsed_time(self) -> datetime | None:
        try:
            # Tolerate both "...+00:00" and "...Z" plus sub-second precision.
            iso = re.sub(r"(\.\d{6})\d+", r"\1", self.timestamp.replace("Z", "+00:00"))
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def is_degenerate_snapshot(self) -> bool:
        """True when this row carries no real OHLC range (a point quote stored
        as a degenerate candle). These are the spot-price snapshots that
        gold-api.com produces."""
        return (
            self.volume == 0.0
            and self.open == self.high == self.low == self.close
        )

    def is_aligned_to(self, timeframe_seconds: int) -> bool:
        t = self.parsed_time()
        if not t:
            return False
        return int(t.timestamp()) % timeframe_seconds == 0


class DataHealthAuditor:
    """Audit the local bars table for a single symbol/timeframe combination."""

    def __init__(
        self,
        db_path: Path | None = None,
        symbol: str = "GOLD",
        timeframe: str = "5m",
    ) -> None:
        config = load_pipeline_config()
        self.db_path = db_path or ROOT / config.get("local_market_db", "data/market_data.db")
        self.symbol = symbol
        self.timeframe = timeframe
        self.timeframe_seconds = _TIMEFRAME_SECONDS.get(timeframe, 300)

    def run(self, run_date: str | None = None) -> dict:
        rows = self._load_rows()
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if not rows:
            return {
                "run_date": run_date or "",
                "checked_at": checked_at,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "status": "error",
                "message": "no bars available from market data port",
                "summary": {},
                "issues": [],
                "providers": [],
                "recommendations": ["run pipelines.binance_usdm_feed or import a broker CSV to seed the bars table"],
            }
        per_provider = self._per_provider_breakdown(rows)
        gaps = self._gap_analysis(rows)
        degenerate = self._degenerate_count(rows)
        misaligned = self._misalignment_count(rows)
        conflicts = self._provider_conflicts(rows)
        price_jumps = self._suspicious_price_jumps(rows)
        latest = rows[-1]
        latest_ts = latest.parsed_time()
        age_min = ((datetime.now(timezone.utc) - latest_ts).total_seconds() / 60) if latest_ts else None

        issues = self._build_issues(per_provider, gaps, degenerate, misaligned, conflicts, price_jumps, age_min)
        status = self._rollup_status(issues)
        recommendations = self._recommendations(issues, per_provider, degenerate, conflicts)

        return {
            "run_date": run_date or "",
            "checked_at": checked_at,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "market_data_backend": "datafeed" if uses_independent_datafeed(self.db_path) else "legacy_test_store",
            "status": status,
            "summary": {
                "total_bars": len(rows),
                "first_timestamp": rows[0].timestamp,
                "last_timestamp": rows[-1].timestamp,
                "latest_age_minutes": round(age_min, 2) if age_min is not None else None,
                "latest_provider": latest.provider,
                "latest_close": latest.close,
                "degenerate_snapshot_count": degenerate["total"],
                "misaligned_count": misaligned["total"],
                "gap_count": len(gaps),
                "provider_conflict_count": len(conflicts),
                "issue_count": len(issues),
            },
            "providers": per_provider,
            "gaps": gaps,
            "degenerate": degenerate,
            "misaligned": misaligned,
            "provider_conflicts": conflicts,
            "suspicious_price_jumps": price_jumps,
            "issues": issues,
            "recommendations": recommendations,
        }

    def clean_degenerate_snapshots(self, dry_run: bool = True) -> dict:
        """Remove degenerate snapshot rows (V=0, H=L=O=C) from the bars table.

        These rows are gold-api.com point quotes stored as degenerate candles.
        They were a useful fallback when binance feed wasn't running but now
        just inject zigzag noise into the chart. Returns the count that
        would be / was removed.
        """
        if uses_independent_datafeed(self.db_path):
            count = self._degenerate_count(self._load_rows())["total"]
            if not dry_run:
                raise RuntimeError("datafeed owns market-data maintenance; trading is read-only")
            return {"would_delete": count, "dry_run": True, "owner": "datafeed"}
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM bars
                WHERE symbol=? AND timeframe=?
                  AND volume=0 AND high=low AND low=open AND open=close
                """,
                (self.symbol, self.timeframe),
            ).fetchone()[0]
            if not dry_run and count:
                conn.execute(
                    """
                    DELETE FROM bars
                    WHERE symbol=? AND timeframe=?
                      AND volume=0 AND high=low AND low=open AND open=close
                    """,
                    (self.symbol, self.timeframe),
                )
        return {"would_delete" if dry_run else "deleted": int(count), "dry_run": dry_run}

    def clean_synthetic_seed(self, dry_run: bool = True) -> dict:
        """Remove cold-start synthetic seed rows (provider=local_synthetic_seed).

        These are the fabricated bars kline_client injects at a cold start to
        reach the 20-bar signal minimum before any real OHLC arrives. They carry
        misaligned timestamps and overlap real provider windows, so once the
        binance_usdm / yahoo feeds are populated they only add chart zigzag and
        misalignment/conflict warnings. Returns the count that would be / was
        removed.
        """
        if uses_independent_datafeed(self.db_path):
            count = sum(1 for row in self._load_rows() if row.provider == "local_synthetic_seed")
            if not dry_run:
                raise RuntimeError("datafeed owns market-data maintenance; trading is read-only")
            return {"would_delete": count, "dry_run": True, "owner": "datafeed"}
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM bars
                WHERE symbol=? AND timeframe=? AND provider='local_synthetic_seed'
                """,
                (self.symbol, self.timeframe),
            ).fetchone()[0]
            if not dry_run and count:
                conn.execute(
                    """
                    DELETE FROM bars
                    WHERE symbol=? AND timeframe=? AND provider='local_synthetic_seed'
                    """,
                    (self.symbol, self.timeframe),
                )
        return {"would_delete" if dry_run else "deleted": int(count), "dry_run": dry_run}

    # ------------------------------------------------------------------ internals

    def _load_rows(self) -> list[_BarRow]:
        if uses_independent_datafeed(self.db_path):
            bars = market_data_repository(self.db_path).load_bars(
                self.symbol, self.timeframe, 60_000
            )
            return [
                _BarRow(
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    provider=bar.provider,
                    quality_flags=bar.quality_flags,
                )
                for bar in bars
            ]
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT timestamp, open, high, low, close, volume, provider, quality_flags
                FROM bars WHERE symbol=? AND timeframe=?
                ORDER BY timestamp ASC
                """,
                (self.symbol, self.timeframe),
            )
            return [
                _BarRow(
                    timestamp=r[0],
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5]),
                    provider=r[6],
                    quality_flags=[s for s in str(r[7]).split(",") if s],
                )
                for r in cursor.fetchall()
            ]

    def _per_provider_breakdown(self, rows: list[_BarRow]) -> list[dict]:
        per: dict[str, dict] = {}
        for r in rows:
            entry = per.setdefault(
                r.provider,
                {"provider": r.provider, "rows": 0, "first_timestamp": r.timestamp, "last_timestamp": r.timestamp, "degenerate": 0, "misaligned": 0},
            )
            entry["rows"] += 1
            if r.timestamp < entry["first_timestamp"]:
                entry["first_timestamp"] = r.timestamp
            if r.timestamp > entry["last_timestamp"]:
                entry["last_timestamp"] = r.timestamp
            if r.is_degenerate_snapshot():
                entry["degenerate"] += 1
            if not r.is_aligned_to(self.timeframe_seconds):
                entry["misaligned"] += 1
        return sorted(per.values(), key=lambda x: -x["rows"])

    def _gap_analysis(self, rows: list[_BarRow]) -> list[dict]:
        gaps: list[dict] = []
        threshold = self.timeframe_seconds * 2  # tolerate 1 missed bar
        for i in range(1, len(rows)):
            t1 = rows[i - 1].parsed_time()
            t2 = rows[i].parsed_time()
            if not t1 or not t2:
                continue
            delta = (t2 - t1).total_seconds()
            if delta > threshold:
                gaps.append(
                    {
                        "from_timestamp": rows[i - 1].timestamp,
                        "to_timestamp": rows[i].timestamp,
                        "duration_minutes": round(delta / 60, 1),
                        "from_provider": rows[i - 1].provider,
                        "to_provider": rows[i].provider,
                        "from_close": rows[i - 1].close,
                        "to_open": rows[i].open,
                        "price_jump": round(rows[i].open - rows[i - 1].close, 2),
                    }
                )
        return gaps

    def _degenerate_count(self, rows: list[_BarRow]) -> dict:
        by_provider: dict[str, int] = {}
        total = 0
        for r in rows:
            if r.is_degenerate_snapshot():
                total += 1
                by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
        return {"total": total, "by_provider": by_provider}

    def _misalignment_count(self, rows: list[_BarRow]) -> dict:
        by_provider: dict[str, int] = {}
        total = 0
        for r in rows:
            if not r.is_aligned_to(self.timeframe_seconds):
                total += 1
                by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
        return {"total": total, "by_provider": by_provider, "timeframe_seconds": self.timeframe_seconds}

    def _provider_conflicts(self, rows: list[_BarRow]) -> list[dict]:
        """Two different providers within the same timeframe window."""
        conflicts: list[dict] = []
        last_per_window: dict[int, _BarRow] = {}
        for r in rows:
            t = r.parsed_time()
            if not t:
                continue
            window = int(t.timestamp()) // self.timeframe_seconds
            existing = last_per_window.get(window)
            if existing and existing.provider != r.provider:
                conflicts.append(
                    {
                        "window_start": datetime.fromtimestamp(window * self.timeframe_seconds, tz=timezone.utc).isoformat(),
                        "a": {"timestamp": existing.timestamp, "provider": existing.provider, "close": existing.close, "volume": existing.volume},
                        "b": {"timestamp": r.timestamp, "provider": r.provider, "close": r.close, "volume": r.volume},
                    }
                )
            else:
                last_per_window[window] = r
        return conflicts[:50]  # cap so the JSON stays small

    def _suspicious_price_jumps(self, rows: list[_BarRow]) -> list[dict]:
        """Adjacent bars where the close-to-open jump > 1% of price."""
        out: list[dict] = []
        for i in range(1, len(rows)):
            prev_close = rows[i - 1].close
            cur_open = rows[i].open
            if prev_close <= 0:
                continue
            pct = (cur_open - prev_close) / prev_close * 100
            if abs(pct) >= 1.0:
                out.append(
                    {
                        "from_timestamp": rows[i - 1].timestamp,
                        "to_timestamp": rows[i].timestamp,
                        "from_close": prev_close,
                        "to_open": cur_open,
                        "pct_jump": round(pct, 3),
                        "from_provider": rows[i - 1].provider,
                        "to_provider": rows[i].provider,
                    }
                )
        return out[:30]

    def _build_issues(
        self,
        per_provider: list[dict],
        gaps: list[dict],
        degenerate: dict,
        misaligned: dict,
        conflicts: list[dict],
        price_jumps: list[dict],
        age_min: float | None,
    ) -> list[dict]:
        issues: list[dict] = []
        if degenerate["total"]:
            issues.append({
                "level": "warn",
                "code": "degenerate_snapshots_in_bars_table",
                "message": (
                    f"{degenerate['total']} rows in bars table are point quotes (V=0, H=L=O=C) "
                    f"stored as candles — these are spot snapshots, not real OHLC bars."
                ),
                "by_provider": degenerate["by_provider"],
                "action": "run `pipelines.data_health --clean` to drop them",
            })
        if misaligned["total"]:
            issues.append({
                "level": "warn",
                "code": "misaligned_timestamps",
                "message": (
                    f"{misaligned['total']} bars do not align to the {misaligned['timeframe_seconds']}s boundary; "
                    f"they interleave with the aligned bars and create chart zigzag."
                ),
                "by_provider": misaligned["by_provider"],
                "action": "drop the misaligned provider rows or fix the collector to bucket-align",
            })
        if conflicts:
            issues.append({
                "level": "warn",
                "code": "provider_conflicts",
                "message": (
                    f"{len(conflicts)} timeframe windows have rows from MORE THAN ONE provider. "
                    f"The chart shows them as separate candles instead of one window."
                ),
                "sample": conflicts[:3],
                "action": "prefer official broker rows; drop the lower-tier ones",
            })
        if gaps:
            biggest = max(gaps, key=lambda g: g["duration_minutes"])
            issues.append({
                "level": "warn" if biggest["duration_minutes"] < 60 else "error",
                "code": "data_gaps",
                "message": (
                    f"{len(gaps)} gaps detected; largest is {biggest['duration_minutes']} min "
                    f"({biggest['from_timestamp']} → {biggest['to_timestamp']}, price jumped {biggest['price_jump']})."
                ),
                "biggest": biggest,
                "action": "run pipelines.binance_usdm_feed (limit=1500 for deeper backfill) or import a broker CSV",
            })
        if age_min is not None and age_min > 15:
            issues.append({
                "level": "error",
                "code": "stale_latest_bar",
                "message": f"latest bar is {age_min:.1f} min old — > 15 min means trading is stale-blocked.",
                "action": "verify the runner is up and `binance_feed_status: pass` in runner_status",
            })
        if price_jumps:
            issues.append({
                "level": "info",
                "code": "suspicious_price_jumps",
                "message": f"{len(price_jumps)} adjacent-bar jumps > 1%. May be real events or data corruption.",
                "sample": price_jumps[:3],
                "action": "verify the largest ones against the broker/exchange chart",
            })
        # Hint about provider mix that's currently producing all the noise:
        official_like = [p for p in per_provider if p["provider"] in {"broker_csv", "mt5_csv", "ibkr", "oanda", "binance_usdm"}]
        public_only = [p for p in per_provider if p["provider"] in {"gold-api.com", "env_gold_price", "local_synthetic_seed"}]
        if public_only and not official_like:
            issues.append({
                "level": "error",
                "code": "no_official_provider",
                "message": "bars table contains only public-tier providers; no real OHLC source is active.",
                "action": "start pipelines.binance_usdm_feed or import broker CSV",
            })
        return issues

    def _rollup_status(self, issues: list[dict]) -> str:
        levels = {i["level"] for i in issues}
        if "error" in levels:
            return "error"
        if "warn" in levels:
            return "warn"
        if "info" in levels:
            return "info"
        return "ok"

    def _recommendations(
        self,
        issues: list[dict],
        per_provider: list[dict],
        degenerate: dict,
        conflicts: list[dict],
    ) -> list[str]:
        recs: list[str] = []
        if degenerate["total"]:
            recs.append(
                f"`python3 -m pipelines.data_health --clean` will drop {degenerate['total']} degenerate snapshot rows."
            )
        if conflicts:
            conflicting = sorted(
                {p for c in conflicts for p in (c["a"]["provider"], c["b"]["provider"])}
            )
            pair = " and ".join(conflicting) if conflicting else "multiple providers"
            preferred = "binance_usdm" if "binance_usdm" in conflicting else conflicting[0]
            recs.append(
                f"Consider preferring {preferred} rows when a window has rows from {pair} — "
                "the chart currently shows both, which causes zigzag."
            )
        if not any(p["provider"] == "binance_usdm" for p in per_provider):
            recs.append(
                "binance_usdm provider is missing from the bars table. "
                "Run `python3 -m pipelines.binance_usdm_feed --date <date>` once to seed it; "
                "the bot cycle now keeps it fresh."
            )
        return recs


def run_data_health(run_date: str, output_root: Path | None = None) -> dict:
    """CLI/pipeline entrypoint. Writes results to outputs/data_health/."""

    from services.journal_store import write_json  # local import to avoid cycle at module load

    config = load_pipeline_config()
    output_root = output_root or ROOT / config.get("output_root", "outputs")
    result = DataHealthAuditor().run(run_date)
    write_json(output_root / "data_health" / "current.json", [result])
    write_json(output_root / "data_health" / f"{run_date}.json", [result])
    return result
