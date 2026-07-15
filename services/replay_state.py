from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.journal_store import load_json
from services.market_data_access import market_data_repository
from services.market_view import MarketViewStore, infer_market_view_reference_price, market_view_target_expiry_bounds
from services.trade_record_card import TradeRecordCardBuilder
from schemas.market_data import Bar


class ReplayState:
    """Build a point-in-time, multi-timeframe replay payload.

    The important invariant is that every returned candle has closed at or
    before the requested replay cursor. Replay is a closed-candle tool: the UI
    should never render a bar that was still forming at the decision timestamp.
    """

    SCHEMA_VERSION = "replay-v1"
    DEFAULT_TIMEFRAMES = ("1d", "4h", "15m", "5m", "1m")
    DEFAULT_LIMITS = {
        "1m": 360,
        "5m": 360,
        "15m": 240,
        "1h": 240,
        "4h": 180,
        "1d": 160,
        "3d": 90,
    }
    MAX_LIMIT = 1200

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        env_output_root = os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        env_market_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
        self.output_root = output_root or Path(env_output_root or str(ROOT / config.get("output_root", "outputs")))
        self.market_db = market_db or Path(env_market_db or str(ROOT / config.get("local_market_db", "data/market_data.db")))

    def snapshot(
        self,
        run_date: str,
        *,
        cursor: str | None = None,
        symbol: str = "GOLD",
        timeframes: Iterable[str] | None = None,
        strategy_id: str = "",
        limit: int | None = None,
        trade_id: str = "",
    ) -> dict:
        requested_timeframes = [self._normalize_timeframe(item) for item in (timeframes or self.DEFAULT_TIMEFRAMES)]
        requested_timeframes = [item for item in requested_timeframes if item]
        if not requested_timeframes:
            requested_timeframes = list(self.DEFAULT_TIMEFRAMES)
        cursor_dt = self._resolve_cursor(run_date, cursor, symbol)
        limit_value = self._bounded_limit(limit) if limit is not None else None
        charts: dict[str, dict] = {}
        for timeframe in requested_timeframes:
            charts[timeframe] = self._chart(symbol, timeframe, cursor_dt, limit_value)
        snapshots = self._decision_snapshots(run_date, strategy_id, cursor_dt)
        active_snapshot = self._active_decision_snapshot(snapshots, cursor_dt, strategy_id, symbol, charts, run_date)
        strategy_config = self._configured_strategy(strategy_id)
        strategy_timeframe = self._strategy_timeframe(strategy_id, strategy_config, active_snapshot)
        latest_price = self._latest_chart_price(charts)
        market_view = self._market_view_as_of(run_date, cursor_dt)
        artifact_dates = self._artifact_dates(run_date, cursor_dt)
        trade_markers = self._trade_markers(run_date, strategy_id, cursor_dt, strategy_config, latest_price)
        requested_trade_id = str(trade_id or "").strip()
        focused_trade_marker = self._focused_trade_marker(trade_markers, cursor_dt, requested_trade_id)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "run_date": run_date,
            "cursor_date": cursor_dt.date().isoformat(),
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "symbol": symbol,
            "strategy_id": strategy_id,
            "strategy_timeframe": strategy_timeframe,
            "cursor": cursor_dt.isoformat(),
            "requested_trade_id": requested_trade_id,
            "timeframes": charts,
            "decision_snapshot": active_snapshot,
            "latest_explicit_decision_snapshot": self._latest_explicit_decision_snapshot(snapshots, cursor_dt),
            "latest_go_decision_snapshot": self._latest_go_decision_snapshot(snapshots, cursor_dt),
            "nearby_decision_snapshots": self._nearby_decision_snapshots(snapshots, cursor_dt),
            "market_view": market_view,
            "market_view_expiry": self._market_view_expiry(market_view, cursor_dt, latest_price),
            "direction_bias_decisions": self._rows_at_or_before(
                self._artifact_rows_for_dates(
                    "direction_bias_decisions",
                    artifact_dates,
                    strategy_id=strategy_id,
                    include_current=True,
                ),
                cursor_dt,
                timestamp_keys=("bar_timestamp", "generated_at", "timestamp"),
                limit=12,
            ),
            "trade_markers": trade_markers,
            "focused_trade_marker": focused_trade_marker,
            "focused_trade_entry_decision_snapshot": self._focused_trade_entry_decision_snapshot(
                snapshots,
                focused_trade_marker,
                cursor_dt,
                run_date,
                strategy_id,
            ),
            "pm_evidence": self._pm_evidence_summary(strategy_id),
            "as_of_contract": {
                "uses_bars_through": cursor_dt.isoformat(),
                "no_future_bars": True,
                "higher_timeframes_may_include_partial_bar": False,
                "chart_bar_contract": "visible bars must have bucket_end <= cursor",
                "artifact_dates_considered": artifact_dates,
                "cursor_date_priority": True,
                "decision_contract": "one_go_or_no_go_decision_per_closed_candle",
                "implicit_no_go_when_no_trigger": True,
                "decision_clock_timeframe": "1m",
                "strategy_timeframe": strategy_timeframe,
                "strategy_decision_note": (
                    "Replay advances on the 1m master clock; non-1m strategies only "
                    "produce explicit candidates on their own candle close."
                ),
            },
        }

    def _pm_evidence_summary(self, strategy_id: str) -> dict:
        backend = self._latest_row(self.output_root / "backend_maturity" / "current.json")
        checks = backend.get("checks") if isinstance(backend.get("checks"), list) else []
        check_map = {
            str(item.get("name") or ""): {
                "status": item.get("status", ""),
                "summary": item.get("summary", ""),
                "evidence": item.get("evidence", {}),
            }
            for item in checks
            if isinstance(item, dict)
        }
        frequency = self._latest_row(self.output_root / "strategy_frequency" / "current.json")
        strategy_frequency = {}
        for row in frequency.get("strategies", []) if isinstance(frequency.get("strategies"), list) else []:
            if str(row.get("strategy_id") or "") == str(strategy_id or ""):
                strategy_frequency = {
                    "strategy_id": row.get("strategy_id", ""),
                    "stage": row.get("stage", ""),
                    "stage_label": row.get("stage_label", ""),
                    "reason": row.get("reason", ""),
                    "primary_reason": row.get("primary_reason", ""),
                    "limiting_reason": row.get("limiting_reason", ""),
                    "executed_trade_count": row.get("executed_trade_count", 0),
                    "min_daily_executed_trades": row.get("min_daily_executed_trades", 0),
                    "signal_count": row.get("signal_count", 0),
                    "candidate_count": row.get("candidate_count", 0),
                    "ticket_count": row.get("ticket_count", 0),
                    "recommendation": row.get("recommendation", {}),
                }
                break
        m3 = check_map.get("M3_edge_judgment", {})
        labels = ((m3.get("evidence") or {}).get("labels") or {}) if isinstance(m3.get("evidence"), dict) else {}
        return {
            "schema_version": "replay-pm-evidence-v1",
            "status": backend.get("status", ""),
            "run_date": backend.get("run_date", ""),
            "generated_at": backend.get("generated_at", ""),
            "platform_status": (backend.get("summary") or {}).get("platform_status", ""),
            "pm_allocation_status": (backend.get("summary") or {}).get("pm_allocation_status", ""),
            "strategy_id": strategy_id,
            "maturity_checks": {
                name: check_map.get(name, {})
                for name in (
                    "M1_trade_record_cards",
                    "M2_strategy_books",
                    "M3_edge_judgment",
                    "M4_frequency_attribution",
                    "M5_execution_safety",
                )
            },
            "edge_label": labels.get(strategy_id, ""),
            "strategy_frequency": strategy_frequency,
        }

    def _chart(self, symbol: str, timeframe: str, cursor_dt: datetime, limit: int | None) -> dict:
        limit_value = limit or self.DEFAULT_LIMITS.get(timeframe, 240)
        if timeframe != "1m":
            for source_timeframe in ("1m", "5m"):
                if source_timeframe == timeframe:
                    continue
                derived = self._derived_bars(symbol, timeframe, cursor_dt, limit_value + 1, source_timeframe)[-limit_value:]
                if derived:
                    return self._chart_payload(
                        timeframe=timeframe,
                        bars=derived,
                        source="derived_from_market_data_db",
                        source_timeframe=source_timeframe,
                        derived=True,
                        cursor_dt=cursor_dt,
                    )

        native = self._native_bars(symbol, timeframe, cursor_dt, limit_value + 1)
        if native:
            bars = [bar.to_dict() for bar in native]
            return self._chart_payload(
                timeframe=timeframe,
                bars=self._closed_rows(
                    [self._bar_payload(item, cursor_dt, source_timeframe=timeframe, derived=False) for item in bars],
                    cursor_dt,
                    timeframe,
                )[-limit_value:],
                source="market_data_db",
                source_timeframe=timeframe,
                derived=False,
                cursor_dt=cursor_dt,
            )

        return self._chart_payload(
            timeframe=timeframe,
            bars=[],
            source="unavailable",
            source_timeframe="",
            derived=True,
            cursor_dt=cursor_dt,
        )

    def _native_bars(self, symbol: str, timeframe: str, cursor_dt: datetime, limit: int | None) -> list[Bar]:
        if not self.market_db.exists():
            return []
        limit_value = limit or self.DEFAULT_LIMITS.get(timeframe, 240)
        seconds = self._timeframe_seconds(timeframe)
        start = cursor_dt - timedelta(seconds=seconds * (limit_value + 4))
        rows = market_data_repository(self.market_db).load_bars_between(
            symbol,
            timeframe,
            start.isoformat(),
            cursor_dt.isoformat(),
        )
        return rows[-limit_value:]

    def _source_rows_for_derived(
        self,
        symbol: str,
        target_timeframe: str,
        cursor_dt: datetime,
        limit: int,
        source_timeframe: str,
    ) -> list[Bar]:
        if not self.market_db.exists():
            return []
        seconds = self._timeframe_seconds(target_timeframe)
        source_seconds = self._timeframe_seconds(source_timeframe)
        start = cursor_dt - timedelta(seconds=seconds * (limit + 4) + source_seconds * 4)
        return market_data_repository(self.market_db).load_bars_between(
            symbol,
            source_timeframe,
            start.isoformat(),
            cursor_dt.isoformat(),
        )

    def _derived_bars(
        self,
        symbol: str,
        target_timeframe: str,
        cursor_dt: datetime,
        limit: int,
        source_timeframe: str,
    ) -> list[dict]:
        if not self.market_db.exists():
            return []
        seconds = self._timeframe_seconds(target_timeframe)
        source_seconds = self._timeframe_seconds(source_timeframe)
        start = cursor_dt - timedelta(seconds=seconds * (limit + 4) + source_seconds * 4)
        rows = market_data_repository(self.market_db).load_aggregated_bars_between(
            symbol,
            source_timeframe,
            target_timeframe,
            start.isoformat(),
            cursor_dt.isoformat(),
            seconds,
            limit,
        )
        closed_rows = []
        for row in rows:
            bucket_end = self._parse_ts(row.get("bucket_end"))
            row["is_partial"] = bool(bucket_end and bucket_end > cursor_dt)
            if not row["is_partial"]:
                closed_rows.append(row)
        return closed_rows

    def _aggregate(self, bars: list[Bar], timeframe: str, cursor_dt: datetime, limit: int) -> list[dict]:
        seconds = self._timeframe_seconds(timeframe)
        buckets: dict[int, dict] = {}
        for bar in sorted(bars, key=lambda item: item.timestamp):
            ts = self._parse_ts(bar.timestamp)
            if ts is None or ts > cursor_dt:
                continue
            bucket_start = int(ts.timestamp()) // seconds * seconds
            row = buckets.get(bucket_start)
            if row is None:
                row = {
                    "symbol": bar.symbol,
                    "timeframe": timeframe,
                    "timestamp": datetime.fromtimestamp(bucket_start, tz=timezone.utc).replace(microsecond=0).isoformat(),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "provider": f"derived:{bar.provider}",
                    "quality_flags": sorted(set(bar.quality_flags + ["derived_timeframe", f"source_{bar.timeframe}"])),
                    "_bucket_end_epoch": bucket_start + seconds,
                }
                buckets[bucket_start] = row
            else:
                row["high"] = max(float(row["high"]), float(bar.high))
                row["low"] = min(float(row["low"]), float(bar.low))
                row["close"] = float(bar.close)
                row["volume"] = float(row["volume"]) + float(bar.volume)
                flags = set(row.get("quality_flags", []))
                flags.update(bar.quality_flags)
                flags.add("derived_timeframe")
                flags.add(f"source_{bar.timeframe}")
                row["quality_flags"] = sorted(flags)
        results = []
        for item in sorted(buckets.values(), key=lambda row: row["timestamp"])[-limit:]:
            bucket_end = datetime.fromtimestamp(int(item.pop("_bucket_end_epoch")), tz=timezone.utc).replace(microsecond=0)
            if bucket_end > cursor_dt:
                continue
            item["bucket_end"] = bucket_end.isoformat()
            item["is_partial"] = False
            results.append(item)
        return results

    def _chart_payload(
        self,
        *,
        timeframe: str,
        bars: list[dict],
        source: str,
        source_timeframe: str,
        derived: bool,
        cursor_dt: datetime,
    ) -> dict:
        first = bars[0]["timestamp"] if bars else ""
        last = bars[-1]["timestamp"] if bars else ""
        future_rows = [
            item["timestamp"]
            for item in bars
            if (self._bar_end_dt(item, source_timeframe or timeframe) or datetime.max.replace(tzinfo=timezone.utc)) > cursor_dt
        ]
        return {
            "timeframe": timeframe,
            "source": source,
            "source_timeframe": source_timeframe,
            "derived": derived,
            "bar_count": len(bars),
            "first_timestamp": first,
            "last_timestamp": last,
            "bars": bars,
            "no_future_bars": not future_rows,
            "future_bar_count": len(future_rows),
        }

    def _bar_payload(self, bar: dict, cursor_dt: datetime, *, source_timeframe: str, derived: bool) -> dict:
        ts = self._parse_ts(bar.get("timestamp", ""))
        bucket_end = None
        if ts is not None:
            bucket_end = ts + timedelta(seconds=self._timeframe_seconds(source_timeframe))
        result = {
            "timestamp": bar.get("timestamp", ""),
            "open": float(bar.get("open", 0)),
            "high": float(bar.get("high", 0)),
            "low": float(bar.get("low", 0)),
            "close": float(bar.get("close", 0)),
            "volume": float(bar.get("volume", 0)),
            "provider": bar.get("provider", ""),
            "quality_flags": list(bar.get("quality_flags") or []),
            "source_timeframe": source_timeframe,
            "derived": derived,
        }
        if bucket_end is not None:
            result["bucket_end"] = bucket_end.replace(microsecond=0).isoformat()
        result["is_partial"] = bool(bucket_end and bucket_end > cursor_dt)
        return result

    def _bar_end_dt(self, row: dict, timeframe: str) -> datetime | None:
        bucket_end = self._parse_ts(row.get("bucket_end"))
        if bucket_end is not None:
            return bucket_end
        ts = self._parse_ts(row.get("timestamp", ""))
        if ts is None:
            return None
        return ts + timedelta(seconds=self._timeframe_seconds(timeframe))

    def _closed_rows(self, rows: list[dict], cursor_dt: datetime, timeframe: str) -> list[dict]:
        return [
            item
            for item in rows
            if (self._bar_end_dt(item, timeframe) or datetime.max.replace(tzinfo=timezone.utc)) <= cursor_dt
        ]

    def _resolve_cursor(self, run_date: str, cursor: str | None, symbol: str) -> datetime:
        parsed = self._parse_ts(cursor) if cursor else None
        if parsed is not None:
            return parsed
        end = datetime.combine(date.fromisoformat(run_date), time.max, tzinfo=timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=1)
        if self.market_db.exists():
            store = market_data_repository(self.market_db)
            rows = store.load_bars_between(symbol, "1m", start.isoformat(), end.isoformat())
            if rows:
                last = self._parse_ts(rows[-1].timestamp)
                if last is not None:
                    return last
            latest = store.load_latest_bar(symbol, "1m") or store.load_latest_bar(symbol, "5m")
            latest_ts = self._parse_ts(latest.get("timestamp")) if latest else None
            if latest_ts is not None:
                return latest_ts
        return end

    def _decision_snapshots(self, run_date: str, strategy_id: str, cursor_dt: datetime | None = None) -> list[dict]:
        dates = self._artifact_dates(run_date, cursor_dt)
        scoped = self._artifact_rows_for_dates("decision_snapshots", dates, strategy_id=strategy_id, include_current=True) if strategy_id else []
        global_rows = self._artifact_rows_for_dates("decision_snapshots", dates, include_current=True)
        rows = scoped or global_rows
        return sorted(rows, key=lambda item: self._parse_ts(item.get("bar_timestamp") or item.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc))

    def _active_decision_snapshot(
        self,
        snapshots: list[dict],
        cursor_dt: datetime,
        strategy_id: str,
        symbol: str,
        charts: dict[str, dict],
        run_date: str,
    ) -> dict:
        exact = []
        for row in snapshots or []:
            timestamps = [
                self._parse_ts(row.get("bar_timestamp")),
                self._parse_ts(row.get("generated_at")),
                self._parse_ts(row.get("timestamp")),
            ]
            if any(ts == cursor_dt for ts in timestamps if ts is not None):
                exact.append(row)
        if exact:
            return self._enrich_decision_snapshot(
                {**max(exact, key=self._decision_rank), "decision_source": "explicit_snapshot"},
                run_date,
                strategy_id,
                cursor_dt,
            )
        return self._implicit_no_go_snapshot(cursor_dt, strategy_id, symbol, charts)

    def _enrich_decision_snapshot(self, row: dict, run_date: str, strategy_id: str, cursor_dt: datetime) -> dict:
        """Add read-time diagnostics for older snapshots that predate gate payloads."""
        if row.get("risk_block"):
            return row
        signal = row.get("signal") or {}
        plan = row.get("execution_plan") or {}
        final_decision = str(row.get("final_decision") or "").lower()
        if final_decision != "no_go" or signal.get("direction") not in {"long", "short"}:
            return row
        if plan.get("ticket_id") or plan.get("entry_zone"):
            return row
        dates = self._artifact_dates(run_date, cursor_dt)
        backtest = self._backtest_for_signal(dates, strategy_id, str(row.get("signal_id") or ""))
        risk = load_risk_rules().get("default", {}) or {}
        signal_gate = self._signal_gate_payload(signal, risk)
        if backtest.get("verdict") == "thin":
            row = {
                **row,
                "backtest_context": {
                    "verdict": backtest.get("verdict"),
                    "sample_size": backtest.get("sample_size"),
                    "setup_count": backtest.get("setup_count"),
                    "blocking": False,
                    "summary": "Thin backtest evidence is context only; it does not block paper/shadow sampling.",
                    "diagnostic_source": "replay_read_time_enrichment",
                },
            }
        if not signal_gate.get("passes") and signal_gate.get("reasons"):
            return {
                **row,
                "risk_block": {
                    "asset": row.get("asset", ""),
                    "ticket_id": "",
                    "signal_id": row.get("signal_id", ""),
                    "reason": "signal threshold gate blocked trading",
                    "signal_gate": signal_gate,
                    "diagnostic_source": "replay_read_time_enrichment",
                },
            }
        return row

    def _backtest_for_signal(self, dates: list[str], strategy_id: str, signal_id: str) -> dict:
        if not signal_id:
            return {}
        roots = [self._strategy_output_root(strategy_id)] if strategy_id else []
        roots.append(self.output_root)
        rows = []
        for root in roots:
            for artifact_date in dates:
                rows.extend(self._load_rows(root / "backtests" / f"{artifact_date}.json"))
            rows.extend(self._load_rows(root / "backtests" / "current.json"))
        rows = [row for row in rows if row.get("signal_id") == signal_id]
        return rows[-1] if rows else {}

    def _signal_gate_payload(self, signal: dict, default: dict) -> dict:
        min_strength = int(default.get("min_signal_strength", 0) or 0)
        min_confidence = int(default.get("min_confidence", 0) or 0)
        strength = int(signal.get("strength") or 0)
        confidence = int(signal.get("confidence") or 0)
        direction = str(signal.get("direction") or "")
        direction_passes = direction in {"long", "short"}
        strength_passes = strength >= min_strength
        confidence_passes = confidence >= min_confidence
        reasons = []
        if not direction_passes:
            reasons.append("signal is not directional")
        if not strength_passes:
            reasons.append(f"signal strength {strength} below minimum {min_strength}")
        if not confidence_passes:
            reasons.append(f"signal confidence {confidence} below minimum {min_confidence}")
        return {
            "passes": bool(direction_passes and strength_passes and confidence_passes),
            "direction": direction,
            "strength": strength,
            "confidence": confidence,
            "min_signal_strength": min_strength,
            "min_confidence": min_confidence,
            "direction_passes": direction_passes,
            "strength_passes": strength_passes,
            "confidence_passes": confidence_passes,
            "reasons": reasons,
        }

    def _latest_explicit_decision_snapshot(self, snapshots: list[dict], cursor_dt: datetime) -> dict:
        candidates = self._rows_at_or_before(snapshots, cursor_dt, timestamp_keys=("bar_timestamp", "generated_at"), limit=max(1, len(snapshots or [])))
        return max(candidates, key=self._decision_rank) if candidates else {}

    def _latest_go_decision_snapshot(self, snapshots: list[dict], cursor_dt: datetime) -> dict:
        candidates = [
            row
            for row in self._rows_at_or_before(snapshots, cursor_dt, timestamp_keys=("bar_timestamp", "generated_at"), limit=max(1, len(snapshots or [])))
            if str(row.get("final_decision") or "").lower() == "go"
        ]
        return max(candidates, key=self._decision_rank) if candidates else {}

    def _decision_rank(self, row: dict) -> tuple[datetime, int, datetime]:
        bar_ts = self._parse_ts(row.get("bar_timestamp") or row.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc)
        generated_at = self._parse_ts(row.get("generated_at")) or bar_ts
        final_decision = str(row.get("final_decision") or "").lower()
        has_plan = bool((row.get("execution_plan") or {}).get("ticket_id") or (row.get("execution_plan") or {}).get("entry_zone"))
        priority = 2 if final_decision == "go" else (1 if has_plan else 0)
        return (bar_ts, priority, generated_at)

    def _implicit_no_go_snapshot(
        self,
        cursor_dt: datetime,
        strategy_id: str,
        symbol: str,
        charts: dict[str, dict],
    ) -> dict:
        bars = []
        for preferred in ("1m", "5m", "15m"):
            rows = ((charts.get(preferred) or {}).get("bars") or [])
            if rows:
                bars = rows
                break
        latest = bars[-1] if bars else {}
        price = latest.get("close")
        return {
            "strategy_id": strategy_id,
            "asset": symbol,
            "timeframe": latest.get("timeframe") or "1m",
            "bar_timestamp": cursor_dt.isoformat(),
            "price": price,
            "decision_source": "implicit_per_bar_no_go",
            "signal": {
                "direction": "watch",
                "strength": 0,
                "confidence": 0,
                "regime": "implicit_no_go",
                "thesis": "This candle closed without a qualifying strategy trigger.",
            },
            "direction_bias": {},
            "position_gate": {},
            "indicators": self._indicator_snapshot(bars),
            "execution_plan": {
                "ticket_id": "",
                "action": "",
                "entry_zone": "",
                "take_profit": None,
                "stop_loss": None,
                "risk_reward": None,
                "target_equity_return_pct": None,
            },
            "final_decision": "no_go",
            "no_go_reason": "no qualifying strategy trigger at this candle close",
            "risk_block": {},
            "as_of_contract": {
                "uses_bars_through": cursor_dt.isoformat(),
                "no_future_bars": True,
                "decision_contract": "one_go_or_no_go_decision_per_closed_candle",
                "implicit_no_go_when_no_trigger": True,
                "decision_clock_timeframe": "1m",
            },
        }

    def _indicator_snapshot(self, bars: list[dict]) -> dict:
        closes = [float(row.get("close")) for row in bars or [] if row.get("close") is not None]
        if not closes:
            return {"bar_count": 0}
        macd_line, macd_signal, macd_histogram = self._macd(closes)
        return {
            "ema20": self._ema_last(closes, 20),
            "ema50": self._ema_last(closes, 50),
            "ema100": self._ema_last(closes, 100),
            "ema200": self._ema_last(closes, 200),
            "rsi14": self._rsi_last(closes, 14),
            "macd": {
                "line": macd_line,
                "signal": macd_signal,
                "histogram": macd_histogram,
            },
            "bar_count": len(closes),
        }

    def _nearby_decision_snapshots(self, snapshots: list[dict], cursor_dt: datetime) -> list[dict]:
        return self._rows_at_or_before(snapshots, cursor_dt, timestamp_keys=("bar_timestamp", "generated_at"), limit=10)

    def _focused_trade_entry_decision_snapshot(
        self,
        snapshots: list[dict],
        focused_trade: dict,
        cursor_dt: datetime,
        run_date: str,
        strategy_id: str,
    ) -> dict:
        if not focused_trade:
            return {}
        signal_id = str(focused_trade.get("signal_id") or ((focused_trade.get("record_card") or {}).get("entry") or {}).get("signal_id") or "")
        ticket_id = str(focused_trade.get("ticket_id") or ((focused_trade.get("record_card") or {}).get("entry") or {}).get("ticket_id") or "")
        lookup_rows = list(snapshots or [])
        for artifact_date in self._focused_trade_decision_lookup_dates(run_date, cursor_dt, focused_trade):
            lookup_rows.extend(
                self._artifact_rows_for_dates(
                    "decision_snapshots",
                    [artifact_date],
                    strategy_id=strategy_id,
                    include_current=False,
                )
            )
        candidates = []
        for row in lookup_rows:
            row_signal_id = str(row.get("signal_id") or "")
            plan = row.get("execution_plan") or {}
            row_ticket_id = str(row.get("ticket_id") or plan.get("ticket_id") or "")
            generated_at = self._parse_ts(row.get("generated_at") or row.get("bar_timestamp") or row.get("timestamp"))
            bar_ts = self._parse_ts(row.get("bar_timestamp") or row.get("generated_at") or row.get("timestamp"))
            visible_as_of_cursor = bool(
                (generated_at and generated_at <= cursor_dt)
                or (bar_ts and self._same_decision_minute(bar_ts, cursor_dt))
            )
            if not visible_as_of_cursor:
                continue
            if signal_id and row_signal_id == signal_id:
                candidates.append(row)
                continue
            if ticket_id and row_ticket_id == ticket_id:
                candidates.append(row)
        if not candidates:
            return {}
        selected = max(candidates, key=self._decision_rank)
        return self._enrich_decision_snapshot(
            {**selected, "decision_source": "focused_trade_entry_decision"},
            run_date,
            strategy_id,
            cursor_dt,
        )

    def _focused_trade_decision_lookup_dates(self, run_date: str, cursor_dt: datetime, focused_trade: dict) -> list[str]:
        dates: list[str] = []

        def add(value: str) -> None:
            if value and value not in dates:
                dates.append(value)

        for value in self._artifact_dates(run_date, cursor_dt):
            add(value)
        for key in ("opened_at", "closed_at"):
            parsed = self._parse_ts(focused_trade.get(key))
            if parsed is None:
                continue
            for offset in (-1, 0, 1):
                add((parsed.date() + timedelta(days=offset)).isoformat())
        add((cursor_dt.date() + timedelta(days=1)).isoformat())
        return dates

    def _trade_markers(
        self,
        run_date: str,
        strategy_id: str,
        cursor_dt: datetime,
        strategy_config: dict | None = None,
        latest_price: float | None = None,
    ) -> list[dict]:
        root = self._strategy_output_root(strategy_id) if strategy_id else self.output_root
        rows = []
        for artifact_date in self._artifact_dates(run_date, cursor_dt):
            for row in self._load_rows(root / "paper_trades" / "closed" / f"{artifact_date}.json"):
                rows.append(self._trade_marker(row, cursor_dt, artifact_date, strategy_id, strategy_config, latest_price))
        for row in self._load_rows(root / "paper_trades" / "current.json"):
            rows.append(self._trade_marker(row, cursor_dt, run_date, strategy_id, strategy_config, latest_price))
        return self._dedupe_rows([row for row in rows if row], keys=("trade_id", "ticket_id", "opened_at", "closed_at"))

    def _focused_trade_marker(self, markers: list[dict], cursor_dt: datetime, trade_id: str = "") -> dict:
        if not markers:
            return {}
        requested = str(trade_id or "").strip()
        if requested:
            for marker in markers:
                if self._matches_trade_identifier(marker, requested):
                    return marker
        same_minute = []
        active = []
        nearby = []
        for marker in markers:
            opened_at = self._parse_ts(marker.get("opened_at"))
            closed_at = self._parse_ts(marker.get("closed_at")) if marker.get("closed_at") else None
            if opened_at is None:
                continue
            if opened_at.replace(second=0, microsecond=0) == cursor_dt.replace(second=0, microsecond=0):
                same_minute.append(marker)
            if opened_at <= cursor_dt and (closed_at is None or cursor_dt <= closed_at):
                active.append(marker)
            delta = abs((opened_at - cursor_dt).total_seconds())
            if delta <= 90 * 60:
                nearby.append({**marker, "_distance_seconds": delta})
        if same_minute:
            return max(same_minute, key=lambda item: self._parse_ts(item.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc))
        if active:
            return max(active, key=lambda item: self._parse_ts(item.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc))
        if nearby:
            selected = min(nearby, key=lambda item: float(item.get("_distance_seconds") or 0))
            selected.pop("_distance_seconds", None)
            return selected
        return {}

    @staticmethod
    def _matches_trade_identifier(marker: dict, trade_id: str) -> bool:
        requested = str(trade_id or "").strip()
        if not requested:
            return False
        candidates = (
            marker.get("trade_id"),
            marker.get("ticket_id"),
            marker.get("order_id"),
            marker.get("signal_id"),
        )
        return any(str(value or "").strip() == requested for value in candidates)

    def _trade_marker(
        self,
        row: dict,
        cursor_dt: datetime,
        run_date: str,
        strategy_id: str,
        strategy_config: dict | None,
        latest_price: float | None,
    ) -> dict:
        opened_at = self._parse_ts(row.get("opened_at"))
        if opened_at is None or opened_at > cursor_dt:
            if opened_at is None or not self._same_decision_minute(opened_at, cursor_dt):
                return {}
        closed_at = self._parse_ts(row.get("closed_at")) if row.get("closed_at") else None
        exit_visible = bool(closed_at and (closed_at <= cursor_dt or self._same_decision_minute(closed_at, cursor_dt)))
        visible_trade = {**row}
        if not exit_visible:
            visible_trade.update({
                "status": "open",
                "closed_at": "",
                "exit_price": None,
                "exit_reason": "",
                "realized_pnl": None,
                "gross_realized_pnl": None,
                "unrealized_pnl": None,
                "gross_unrealized_pnl": None,
            })
        card = TradeRecordCardBuilder(
            run_date=run_date,
            strategy_id=strategy_id or str(row.get("strategy_id") or ""),
            strategy_config=strategy_config or {},
            latest_price=latest_price,
        ).build(visible_trade)
        return {
            "trade_id": row.get("trade_id", ""),
            "ticket_id": row.get("ticket_id", ""),
            "order_id": row.get("order_id", ""),
            "signal_id": row.get("signal_id", ""),
            "strategy_id": row.get("strategy_id", ""),
            "side": row.get("side", ""),
            "status": visible_trade.get("status", ""),
            "opened_at": opened_at.isoformat(),
            "closed_at": closed_at.isoformat() if exit_visible else "",
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price") if exit_visible else None,
            "take_profit": row.get("target") or row.get("take_profit"),
            "stop_loss": row.get("stop_loss"),
            "realized_pnl": row.get("realized_pnl") if exit_visible else None,
            "record_card": card,
        }

    def _same_decision_minute(self, left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return False
        return left.replace(second=0, microsecond=0) == right.replace(second=0, microsecond=0)

    def _configured_strategy(self, strategy_id: str) -> dict:
        if not strategy_id:
            return {}
        config = load_strategy_config()
        return config.get(strategy_id, {}) if isinstance(config, dict) else {}

    def _strategy_timeframe(self, strategy_id: str, strategy_config: dict, active_snapshot: dict) -> str:
        configured = str(strategy_config.get("timeframe") or "").strip().lower()
        if configured:
            return configured
        text = f"_{str(strategy_id or '').lower()}_"
        for timeframe in ("1m", "5m", "15m", "1h", "4h", "1d", "3d"):
            if f"_{timeframe}_" in text:
                return timeframe
        snapshot_timeframe = str(active_snapshot.get("timeframe") or "").strip().lower()
        return snapshot_timeframe or "1m"

    def _latest_chart_price(self, charts: dict[str, dict]) -> float | None:
        for timeframe in ("1m", "5m", "15m", "4h", "1d"):
            rows = (charts.get(timeframe) or {}).get("bars") or []
            if rows and rows[-1].get("close") is not None:
                try:
                    return float(rows[-1]["close"])
                except (TypeError, ValueError):
                    return None
        return None

    def _market_view_as_of(self, run_date: str, cursor_dt: datetime) -> dict:
        rows = self._artifact_rows_for_dates("market_views", self._artifact_dates(run_date, cursor_dt), include_current=True)
        candidates = self._rows_at_or_before(rows, cursor_dt, timestamp_keys=("generated_at", "timestamp"), limit=1)
        return candidates[-1] if candidates else {}

    def _artifact_dates(self, run_date: str, cursor_dt: datetime | None = None) -> list[str]:
        dates: list[str] = []
        if cursor_dt is not None:
            dates.append(cursor_dt.date().isoformat())
        if run_date:
            dates.append(str(run_date))
        result: list[str] = []
        for item in dates:
            if item and item not in result:
                result.append(item)
        return result

    def _artifact_rows_for_dates(
        self,
        section: str,
        dates: list[str],
        *,
        strategy_id: str = "",
        include_current: bool = False,
    ) -> list[dict]:
        roots = [self._strategy_output_root(strategy_id)] if strategy_id else []
        roots.append(self.output_root)
        rows: list[dict] = []
        for root in roots:
            for artifact_date in dates:
                rows.extend(self._load_rows(root / section / f"{artifact_date}.json"))
            if include_current:
                rows.extend(self._load_rows(root / section / "current.json"))
        return self._dedupe_rows(rows, keys=("strategy_id", "bar_timestamp", "generated_at", "final_decision", "trade_id", "ticket_id"))

    def _dedupe_rows(self, rows: list[dict], *, keys: tuple[str, ...]) -> list[dict]:
        seen = set()
        result = []
        for row in rows:
            marker = tuple(row.get(key, "") for key in keys)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(row)
        return result

    def _market_view_expiry(self, market_view: dict, cursor_dt: datetime, current_price: float | None) -> dict:
        if not market_view:
            return {
                "status": "missing",
                "expired": True,
                "reason": "no market view available",
                "filter_effect": "neutral_no_filter",
                "operator_message": "没有可用口述方向；系统不会按人工方向过滤多空信号。",
            }
        expiry = market_view.get("expiry") or {}
        generated_at = self._parse_ts(market_view.get("generated_at")) or cursor_dt
        valid_hours = self._safe_float(expiry.get("valid_for_hours")) or MarketViewStore.DEFAULT_VALID_FOR_HOURS
        expires_at = self._parse_ts(expiry.get("expires_at"))
        if expires_at is None:
            expires_at = generated_at.replace(microsecond=0) + timedelta(hours=valid_hours)
        checked_at = cursor_dt.replace(microsecond=0)
        reference_price = infer_market_view_reference_price(market_view, self.market_db)
        move_pct = self._safe_float(expiry.get("expires_if_price_moves_pct"))
        if move_pct is None:
            move_pct = MarketViewStore.DEFAULT_PRICE_MOVE_EXPIRY_PCT
        expire_above, expire_below, target_rule = market_view_target_expiry_bounds(market_view)
        base = {
            "status": "active",
            "expired": False,
            "reason": "market view active",
            "filter_effect": "active_direction_filter_enabled",
            "operator_message": "口述方向仍有效；强多/强空会过滤反向信号，偏多/偏空会降权反向信号。",
            "checked_at": checked_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else "",
            "valid_for_hours": valid_hours,
            "current_price": current_price,
            "reference_price": reference_price,
            "expires_if_price_moves_pct": move_pct,
            "target_price": self._safe_float(expiry.get("target_price")),
            "target_rule": target_rule,
            "expire_above": expire_above,
            "expire_below": expire_below,
            "price_expiry_ready": bool(reference_price and move_pct),
        }
        if expires_at and checked_at > expires_at:
            return {**base, **self._expired_market_view_fields(f"time expired at {expires_at.isoformat()}")}
        if current_price is not None:
            expire_above = base["expire_above"]
            expire_below = base["expire_below"]
            if expire_above is not None and current_price >= expire_above:
                return {**base, **self._expired_market_view_fields(f"price reached expire_above {expire_above}")}
            if expire_below is not None and current_price <= expire_below:
                return {**base, **self._expired_market_view_fields(f"price reached expire_below {expire_below}")}
            if reference_price and move_pct:
                actual_move = abs((current_price - reference_price) / reference_price) * 100
                if actual_move >= move_pct:
                    return {
                        **base,
                        **self._expired_market_view_fields(f"price moved {actual_move:.2f}% from reference {reference_price}"),
                    }
        return base

    @staticmethod
    def _expired_market_view_fields(reason: str) -> dict:
        return {
            "status": "expired",
            "expired": True,
            "reason": reason,
            "filter_effect": "expired_direction_filter_disabled",
            "operator_message": "口述方向已失效；系统不再按这条观点过滤多空信号，只把它保留为历史判断。",
        }

    def _rows_at_or_before(
        self,
        rows: list[dict],
        cursor_dt: datetime,
        *,
        timestamp_keys: tuple[str, ...],
        limit: int,
    ) -> list[dict]:
        filtered = []
        for row in rows or []:
            ts = None
            for key in timestamp_keys:
                ts = self._parse_ts(row.get(key))
                if ts is not None:
                    break
            if ts is None or ts <= cursor_dt:
                filtered.append(row)
        return filtered[-limit:]

    def _strategy_output_root(self, strategy_id: str) -> Path:
        if strategy_id:
            return self.output_root / "strategies" / strategy_id
        return self.output_root

    def _load_rows(self, path: Path) -> list[dict]:
        try:
            rows = load_json(path)
        except (OSError, ValueError):
            return []
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
        if isinstance(rows, dict):
            return [rows]
        return []

    def _latest_row(self, path: Path) -> dict:
        rows = self._load_rows(path)
        return rows[-1] if rows else {}

    def _bounded_limit(self, limit: int | None) -> int:
        if limit is None:
            return 0
        return max(1, min(self.MAX_LIMIT, int(limit)))

    def _ema_last(self, values: list[float], period: int) -> float | None:
        series = self._ema_series(values, period)
        return round(series[-1], 4) if series else None

    def _ema_series(self, values: list[float], period: int) -> list[float]:
        if not values:
            return []
        alpha = 2 / (period + 1)
        ema = values[0]
        results = []
        for value in values:
            ema = (float(value) * alpha) + (ema * (1 - alpha))
            results.append(ema)
        return results

    def _rsi_last(self, values: list[float], period: int = 14) -> float | None:
        if len(values) <= period:
            return None
        gains = []
        losses = []
        for previous, current in zip(values, values[1:]):
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(abs(min(change, 0.0)))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for gain, loss in zip(gains[period:], losses[period:]):
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 4)

    def _macd(self, values: list[float]) -> tuple[float | None, float | None, float | None]:
        if not values:
            return None, None, None
        ema12 = self._ema_series(values, 12)
        ema26 = self._ema_series(values, 26)
        macd_series = [fast - slow for fast, slow in zip(ema12, ema26)]
        signal_series = self._ema_series(macd_series, 9)
        if not macd_series or not signal_series:
            return None, None, None
        line = macd_series[-1]
        signal = signal_series[-1]
        return round(line, 4), round(signal, 4), round(line - signal, 4)

    @classmethod
    def _normalize_timeframe(cls, timeframe: str) -> str:
        return str(timeframe or "").strip().lower()

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        text = str(timeframe or "1m").strip().lower()
        units = {"m": 60, "h": 3600, "d": 86400}
        number = ""
        unit = ""
        for char in text:
            if char.isdigit():
                number += char
            else:
                unit += char
        if not number or unit not in units:
            return 60
        return max(1, int(number)) * units[unit]

    @staticmethod
    def _safe_float(value: object) -> float | None:
        try:
            if value in ("", None):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_ts(cls, value: object) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0)
        except ValueError:
            return None
