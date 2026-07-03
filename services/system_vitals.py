from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import write_json


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _safe_float(value: Any, default: float) -> float:
    """Never let one malformed external field crash the self-monitoring path."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# A timestamp more than this far in the future is treated as clock skew / a bad
# writer rather than "fresh" — otherwise a far-future stamp masks a dead source.
_FUTURE_SKEW_SECONDS = 120.0
_DEFAULT_HEARTBEAT_CADENCE_SECONDS = 300.0
_MISSED_BEATS_BEFORE_STALE = 3.0
_MIN_STALE_AFTER_SECONDS = 900.0


def _load_any(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _latest_record(path: Path) -> dict:
    data = _load_any(path)
    if isinstance(data, list):
        return data[-1] if data and isinstance(data[-1], dict) else {}
    return data if isinstance(data, dict) else {}


def _stale_after_seconds(interval_seconds: float) -> float:
    return max(interval_seconds * _MISSED_BEATS_BEFORE_STALE, _MIN_STALE_AFTER_SECONDS)


class SystemVitals:
    """Read-only liveness contract for the trading machine.

    These checks intentionally inspect persisted artifacts and the local market
    DB. They never submit, cancel, reconcile, or mutate trade state.
    """

    def __init__(
        self,
        output_root: Path,
        market_db: Path,
        now: datetime | None = None,
        registry: Any = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.market_db = Path(market_db)
        self.now = (now or _utcnow()).astimezone(timezone.utc).replace(microsecond=0)
        self.run_date = ""
        self.registry = registry

    def run(self, run_date: str, persist: bool = True) -> dict:
        # persist=False is for read-only callers (the dashboard GET recomputes
        # vitals live, but must NOT overwrite the runner-owned current.json —
        # especially when viewing a historical date, which would otherwise stamp
        # historical/aliveness over the real current liveness).
        self.run_date = run_date
        data_feed = self._data_feed_vital()
        runner = self._runner_liveness_vital()
        strategy = self._strategy_evaluation_vital(run_date)
        tp_sl = self._tp_sl_coverage_vital()
        execution = self._execution_blocker_vital()
        no_trade = self._no_trade_attribution_vital(
            data_feed=data_feed,
            runner=runner,
            strategy=strategy,
            tp_sl=tp_sl,
            execution=execution,
        )
        vitals = [data_feed, runner, strategy, tp_sl, execution, no_trade]
        always_on = self._always_on_contract(vitals)
        overall = "down" if any(v["status"] == "down" for v in vitals) else "alive"
        payload = {
            "run_date": run_date,
            "checked_at": self.now.isoformat(),
            "overall": overall,
            "alive": overall == "alive",
            "vitals": vitals,
            "always_on": always_on,
        }
        if persist:
            write_json(self.output_root / "system_vitals" / "current.json", [payload])
            write_json(self.output_root / "system_vitals" / f"{run_date}.json", [payload])
        return payload

    def _vital(self, name: str, status: str, message: str, detail: dict | None = None) -> dict:
        return {
            "name": name,
            "status": status,
            "message": message,
            "detail": detail or {},
        }

    def _data_feed_vital(self) -> dict:
        latest = self._latest_bar()
        if latest is None:
            return self._vital("data_feed", "down", "no GOLD bars found", {"market_db": str(self.market_db)})
        age_min = (self.now - latest).total_seconds() / 60
        detail = {"latest_bar_at": latest.isoformat(), "age_minutes": round(age_min, 2)}
        if self.run_date and self.run_date != self.now.date().isoformat():
            return self._vital("data_feed", "up", "GOLD feed exists for historical run_date", detail)
        if age_min * 60 < -_FUTURE_SKEW_SECONDS:
            return self._vital("data_feed", "down", f"GOLD feed timestamp is {-age_min:.1f}m in the FUTURE — clock skew / bad import", detail)
        if age_min > 30:
            return self._vital("data_feed", "down", f"GOLD feed stale: {age_min:.1f}m old", detail)
        return self._vital("data_feed", "up", "GOLD feed is fresh", detail)

    def _latest_bar(self) -> datetime | None:
        if not self.market_db.exists():
            return None
        try:
            with sqlite3.connect(self.market_db) as con:
                row = con.execute(
                    "SELECT timestamp FROM bars WHERE symbol='GOLD' ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return None
        return _parse_ts(row[0]) if row else None

    def _runner_liveness_vital(self) -> dict:
        heartbeat = _latest_record(self.output_root / "runner_status" / "current.json")
        if not heartbeat:
            return self._vital("runner_liveness", "down", "runner heartbeat missing", {})
        interval = _safe_float(heartbeat.get("interval_seconds") or 300, 300.0)
        raw_ts = (
            heartbeat.get("updated_at")
            or heartbeat.get("finished_at")
            or heartbeat.get("generated_at")
            or heartbeat.get("checked_at")
        )
        updated = _parse_ts(raw_ts)
        state = str(heartbeat.get("state", "")).lower()
        detail = {"state": state, "interval_seconds": interval}
        if raw_ts and updated is None:
            return self._vital("runner_liveness", "warn", "runner heartbeat timestamp is unparseable", {**detail, "raw_updated_at": str(raw_ts)})
        if updated:
            age = (self.now - updated).total_seconds()
            detail.update({"updated_at": updated.isoformat(), "age_seconds": round(age, 2)})
            if age < -_FUTURE_SKEW_SECONDS:
                return self._vital("runner_liveness", "down", f"runner heartbeat is {-age / 60:.1f}m in the FUTURE — clock skew", detail)
            if age > _stale_after_seconds(interval):
                return self._vital("runner_liveness", "down", f"runner heartbeat stale: {age / 60:.1f}m old", detail)
        if state in {"error", "failed", "stopped", "dead"}:
            return self._vital("runner_liveness", "down", f"runner state is {state}", detail)
        return self._vital("runner_liveness", "up", "runner heartbeat is fresh", detail)

    def _strategy_evaluation_vital(self, run_date: str) -> dict:
        summary = _latest_record(self.output_root / "strategies" / "summary_current.json")
        expected = self._enabled_strategy_count()
        if not summary:
            status = "down" if expected else "warn"
            return self._vital(status=status, name="strategy_evaluation", message="strategy summary missing", detail={"enabled": expected})
        strategies = summary.get("strategies") or []
        evaluated = len(strategies) if isinstance(strategies, list) else int(summary.get("strategy_count") or 0)
        detail = {
            "run_date": summary.get("run_date"),
            "evaluated": evaluated,
            "enabled": expected,
        }
        if summary.get("run_date") != run_date:
            return self._vital("strategy_evaluation", "down", "strategy summary run_date is stale", detail)
        # run_date stays "today" all day, so it can't catch a strategies job that
        # died mid-session while the (separate) runner keeps the feed fresh. Age
        # the summary's own heartbeat to catch that.
        raw_generated = summary.get("generated_at")
        generated = _parse_ts(raw_generated)
        if raw_generated and generated is None:
            return self._vital("strategy_evaluation", "warn", "strategies summary generated_at is unparseable", {**detail, "raw_generated_at": str(raw_generated)})
        if generated is not None:
            stale_seconds = (self.now - generated).total_seconds()
            detail["age_seconds"] = round(stale_seconds, 2)
            if stale_seconds < -_FUTURE_SKEW_SECONDS:
                return self._vital("strategy_evaluation", "down", f"strategies summary generated_at is {-stale_seconds / 60:.1f}m in the FUTURE — clock skew", detail)
            if stale_seconds > _stale_after_seconds(_DEFAULT_HEARTBEAT_CADENCE_SECONDS):
                return self._vital("strategy_evaluation", "down", f"strategies summary stale: {stale_seconds / 60:.1f}m old — strategies job likely stopped", detail)
        if expected and evaluated < expected:
            return self._vital("strategy_evaluation", "down", f"only {evaluated}/{expected} enabled strategies evaluated", detail)
        return self._vital("strategy_evaluation", "up", "enabled strategies evaluated", detail)

    def _enabled_strategy_count(self) -> int:
        if not self.registry:
            return 0
        try:
            return len(list(self.registry.enabled()))
        except Exception:  # noqa: BLE001 - registry is injected by callers/tests.
            return 0

    def _tp_sl_coverage_vital(self) -> dict:
        open_positions = 0
        protected = 0
        missing = []
        unreadable = []
        for path in sorted((self.output_root / "strategies").glob("*/paper_trades/current.json")):
            data = _load_any(path)
            if data is None:
                # glob only yields existing files, so None here means the file is
                # corrupt/unreadable — it may hold a naked position we cannot see.
                unreadable.append(path.parent.parent.name)
                continue
            rows = data if isinstance(data, list) else []
            for trade in rows:
                if not isinstance(trade, dict) or str(trade.get("status", "")).lower() != "open":
                    continue
                open_positions += 1
                has_stop = trade.get("stop_loss") not in {None, "", 0}
                has_target = trade.get("target") not in {None, "", 0}
                if trade.get("protective_order_missing"):
                    missing.append(trade.get("trade_id") or path.parent.parent.name)
                elif has_stop or has_target or trade.get("exchange_managed"):
                    protected += 1
                else:
                    missing.append(trade.get("trade_id") or path.parent.parent.name)
        detail = {"open_positions": open_positions, "protected": protected, "missing": missing, "unreadable": unreadable}
        if unreadable:
            return self._vital("tp_sl_coverage", "down", f"cannot read {len(unreadable)} paper_trades file(s) — open protection unknown: {', '.join(unreadable)}", detail)
        if open_positions and protected < open_positions:
            return self._vital("tp_sl_coverage", "down", f"{open_positions - protected} open position(s) lack TP/SL protection", detail)
        return self._vital("tp_sl_coverage", "up", "open positions have TP/SL protection", detail)

    def _execution_blocker_vital(self) -> dict:
        reconciliation = _latest_record(self.output_root / "strategies" / "gold_1m_chan" / "live_reconciliation" / "current.json")
        if reconciliation:
            if reconciliation.get("confirmation_status") == "cannot_confirm":
                if reconciliation.get("suspected_naked_position"):
                    return self._vital("execution_blocker", "down", "suspected naked position: active demo venue state cannot be confirmed while an order is submitting", reconciliation)
                return self._vital("execution_blocker", "down", "active demo reconciliation cannot confirm venue state", reconciliation)
            if reconciliation.get("suspected_naked_position"):
                return self._vital("execution_blocker", "down", "suspected naked position blocks execution", reconciliation)
            if reconciliation.get("error"):
                return self._vital("execution_blocker", "down", "active demo reconciliation error blocks execution", reconciliation)
            if int(reconciliation.get("drift_count") or 0) > 0:
                return self._vital("execution_blocker", "down", "active demo reconciliation drift blocks execution", reconciliation)
            if reconciliation.get("reconciled") is False:
                return self._vital("execution_blocker", "down", "active demo reconciliation is not reconciled", reconciliation)
        return self._vital("execution_blocker", "up", "no hard execution blocker detected", {})

    def _always_on_contract(self, vitals: list[dict]) -> dict:
        vital_by_name = {str(item.get("name") or ""): item for item in vitals if isinstance(item, dict)}
        jobs = [
            self._job_from_vital(
                job_name="runner",
                vital=vital_by_name.get("runner_liveness", {}),
                criticality="critical",
                cadence_seconds=self._runner_interval_seconds(),
                source_path=self.output_root / "runner_status" / "current.json",
            ),
            self._job_from_vital(
                job_name="strategies",
                vital=vital_by_name.get("strategy_evaluation", {}),
                criticality="critical",
                cadence_seconds=_DEFAULT_HEARTBEAT_CADENCE_SECONDS,
                source_path=self.output_root / "strategies" / "summary_current.json",
            ),
            self._artifact_heartbeat_job(
                job_name="cycle_audit",
                criticality="non_critical",
                source_path=self.output_root / "cycle_audit" / "current.json",
                cadence_seconds=_DEFAULT_HEARTBEAT_CADENCE_SECONDS,
                timestamp_fields=("finalized_at", "recorded_at", "cycle_timestamp"),
                missing_state="degraded",
            ),
            {
                "name": "dashboard",
                "criticality": "non_critical",
                "state": "read_time_observer",
                "status": "up",
                "blocks_new_orders": False,
                "message": "dashboard liveness is computed by the reader on each request; external deadman detects whole-box loss",
                "source": "dashboard_request",
                "freshness": {
                    "checked_at": self.now.isoformat(),
                    "uses_read_time_utc": True,
                },
            },
            {
                "name": "reconciliation",
                "criticality": "critical",
                "state": "delegated",
                "status": "up",
                "blocks_new_orders": False,
                "message": "reconciliation freshness is enforced by the existing live reconciliation and money guardrail gates",
                "source": "live_reconciliation.current",
                "freshness": {
                    "delegated_to": [
                        "system_vitals.execution_blocker",
                        "live_money_guardrails.reconciliation_daily_loss_invariant",
                    ],
                },
            },
        ]
        blocking = [job for job in jobs if job.get("blocks_new_orders")]
        degraded = [
            job
            for job in jobs
            if job.get("criticality") == "non_critical" and job.get("status") in {"warn", "down"}
        ]
        if blocking:
            status = "BLOCKED_ALWAYS_ON_STALE"
        elif degraded:
            status = "DEGRADED"
        else:
            status = "READY"
        return {
            "schema_version": "always-on-liveness-v1",
            "checked_at": self.now.isoformat(),
            "run_date": self.run_date,
            "status": status,
            "blocks_new_orders": bool(blocking),
            "degraded": bool(degraded),
            "blocking_jobs": blocking,
            "critical_blockers": blocking,
            "degraded_jobs": degraded,
            "jobs": jobs,
            "freshness_policy": {
                "cadence_seconds": _DEFAULT_HEARTBEAT_CADENCE_SECONDS,
                "missed_beats_before_stale": _MISSED_BEATS_BEFORE_STALE,
                "minimum_stale_after_seconds": _MIN_STALE_AFTER_SECONDS,
                "timestamp_timezone": "UTC",
                "computed_at_read_time": True,
            },
        }

    def _runner_interval_seconds(self) -> float:
        heartbeat = _latest_record(self.output_root / "runner_status" / "current.json")
        return _safe_float(heartbeat.get("interval_seconds") or _DEFAULT_HEARTBEAT_CADENCE_SECONDS, _DEFAULT_HEARTBEAT_CADENCE_SECONDS)

    def _job_from_vital(
        self,
        *,
        job_name: str,
        vital: dict,
        criticality: str,
        cadence_seconds: float,
        source_path: Path,
    ) -> dict:
        detail = vital.get("detail", {}) if isinstance(vital.get("detail"), dict) else {}
        status = str(vital.get("status") or "warn")
        message = str(vital.get("message") or "vital missing")
        age = detail.get("age_seconds")
        stale_after = _stale_after_seconds(cadence_seconds)
        state = "fresh"
        if status == "down":
            state = "stale" if self._message_names_staleness(message) else "down"
        elif status == "warn":
            state = "degraded"
        blocks = criticality == "critical" and status == "down"
        return {
            "name": job_name,
            "criticality": criticality,
            "state": state,
            "status": status,
            "blocks_new_orders": blocks,
            "message": message,
            "source": str(source_path),
            "freshness": {
                "age_seconds": age,
                "cadence_seconds": cadence_seconds,
                "stale_after_seconds": stale_after,
                "missed_beats": self._missed_beats(age, cadence_seconds),
                "checked_at": self.now.isoformat(),
                "uses_read_time_utc": True,
            },
            "detail": detail,
        }

    def _artifact_heartbeat_job(
        self,
        *,
        job_name: str,
        criticality: str,
        source_path: Path,
        cadence_seconds: float,
        timestamp_fields: tuple[str, ...],
        missing_state: str,
    ) -> dict:
        record = _latest_record(source_path)
        stale_after = _stale_after_seconds(cadence_seconds)
        if not record:
            status = "warn" if missing_state == "degraded" else "down"
            return {
                "name": job_name,
                "criticality": criticality,
                "state": missing_state,
                "status": status,
                "blocks_new_orders": criticality == "critical",
                "message": f"{job_name} heartbeat missing",
                "source": str(source_path),
                "freshness": {
                    "age_seconds": None,
                    "cadence_seconds": cadence_seconds,
                    "stale_after_seconds": stale_after,
                    "checked_at": self.now.isoformat(),
                    "uses_read_time_utc": True,
                },
            }
        raw_ts = next((record.get(field) for field in timestamp_fields if record.get(field)), None)
        observed = _parse_ts(raw_ts)
        if raw_ts and observed is None:
            status = "warn" if criticality == "non_critical" else "down"
            return {
                "name": job_name,
                "criticality": criticality,
                "state": "invalid_timestamp",
                "status": status,
                "blocks_new_orders": criticality == "critical",
                "message": f"{job_name} heartbeat timestamp is unparseable",
                "source": str(source_path),
                "freshness": {
                    "raw_timestamp": str(raw_ts),
                    "cadence_seconds": cadence_seconds,
                    "stale_after_seconds": stale_after,
                    "checked_at": self.now.isoformat(),
                    "uses_read_time_utc": True,
                },
            }
        age = (self.now - observed).total_seconds() if observed else None
        if age is not None and age < -_FUTURE_SKEW_SECONDS:
            status = "down"
            state = "future_timestamp"
            message = f"{job_name} heartbeat is {-age / 60:.1f}m in the FUTURE - clock skew"
        elif age is None:
            status = "warn" if criticality == "non_critical" else "down"
            state = "missing_timestamp"
            message = f"{job_name} heartbeat timestamp missing"
        elif age > stale_after:
            status = "warn" if criticality == "non_critical" else "down"
            state = "stale"
            message = f"{job_name} heartbeat stale: {age / 60:.1f}m old"
        else:
            status = "up"
            state = "fresh"
            message = f"{job_name} heartbeat is fresh"
        return {
            "name": job_name,
            "criticality": criticality,
            "state": state,
            "status": status,
            "blocks_new_orders": status == "down" and (criticality == "critical" or state == "future_timestamp"),
            "message": message,
            "source": str(source_path),
            "freshness": {
                "timestamp": observed.isoformat() if observed else None,
                "age_seconds": round(age, 2) if age is not None else None,
                "cadence_seconds": cadence_seconds,
                "stale_after_seconds": stale_after,
                "missed_beats": self._missed_beats(age, cadence_seconds),
                "checked_at": self.now.isoformat(),
                "uses_read_time_utc": True,
            },
        }

    def _missed_beats(self, age_seconds: Any, cadence_seconds: float) -> float | None:
        try:
            if age_seconds is None or cadence_seconds <= 0:
                return None
            return round(float(age_seconds) / cadence_seconds, 2)
        except (TypeError, ValueError):
            return None

    def _message_names_staleness(self, message: str) -> bool:
        lowered = message.lower()
        return any(token in lowered for token in ("stale", "missing", "stopped", "heartbeat", "timestamp"))

    def _no_trade_attribution_vital(self, **vitals: dict) -> dict:
        down = [name for name, vital in vitals.items() if vital.get("status") == "down"]
        latest_trade = self._latest_trade_time()
        detail = {"blocking_vitals": down, "latest_trade_at": latest_trade.isoformat() if latest_trade else None}
        if down:
            detail["attribution"] = "system"
            return self._vital("no_trade_attribution", "down", "quiet trading cannot be trusted while system vitals are down", detail)
        if latest_trade is None:
            detail["attribution"] = "market_no_signal"
            return self._vital("no_trade_attribution", "warn", "no recent trade sample, but system is alive", detail)
        age_hours = (self.now - latest_trade).total_seconds() / 3600
        detail["age_hours"] = round(age_hours, 2)
        detail["attribution"] = "trading_normally" if age_hours <= 12 else "market_no_signal"
        return self._vital("no_trade_attribution", "up", "no-trade attribution is explainable", detail)

    def _latest_trade_time(self) -> datetime | None:
        latest = None
        for path in sorted((self.output_root / "strategies").glob("*/paper_trades/current.json")):
            data = _load_any(path)
            rows = data if isinstance(data, list) else []
            for trade in rows:
                if not isinstance(trade, dict):
                    continue
                ts = _parse_ts(trade.get("opened_at") or trade.get("closed_at") or trade.get("timestamp"))
                if ts and (latest is None or ts > latest):
                    latest = ts
        return latest


def vitals_to_health_checks(payload: dict) -> list[dict]:
    checks = []
    for vital in (payload or {}).get("vitals", []) or []:
        name = vital.get("name")
        if not name:
            continue
        vital_status = vital.get("status")
        status = "error" if vital_status == "down" else ("warn" if vital_status == "warn" else "ok")
        checks.append({
            "name": f"vitals_{name}",
            "status": status,
            "message": vital.get("message", ""),
            "details": vital.get("detail", {}),
        })
    return checks
