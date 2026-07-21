from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import (
    cycle_window,
    cycle_window_from_id,
    filter_bars_for_market_session,
    market_session_enabled,
    market_session_status,
    parse_utc,
)
from services.dualtrack_config import dualtrack_config
from services.execution_plugin_composition import build_configured_execution_engine_adapter
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_machine_plan import DualTrackMachinePlanner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.strategy_control_plane import StrategyControlPlane, production_mutation_lock
from services.strategy_cycle_package import StrategyCyclePackager
from services.strategy_market_context import build_strategy_timeframes
from services.dualtrack_market_feed import DualTrackMarketFeed
from services.dualtrack_tiger_human_sync import DualTrackTigerHumanSync
from services.journal_store import load_json, write_json
from services.datafeed_market_repository import DatafeedMarketRepository
from services.market_store import MarketStore
from services.strategy_proposal_composition import compose_strategy_proposal
from services.strategy_proposal_registry import StrategyProposalPluginRegistry
from services.tiger_openapi_order_sync import TigerOpenApiOrderSync


def _parse_execution_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.replace(".", "", 1).isdigit():
            number = float(text)
            magnitude = abs(number)
            if magnitude >= 1e17:
                number /= 1_000_000_000
            elif magnitude >= 1e14:
                number /= 1_000_000
            elif magnitude >= 1e11:
                number /= 1_000
            return datetime.fromtimestamp(number, tz=timezone.utc).replace(microsecond=0)
        return parse_utc(text)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


class DualTrackCycleRunner:
    """Schedule-facing driver for the dual-track paper engine.

    The runner is intentionally read-only toward venues. It reads local market
    bars and market-view artifacts, then writes only dualtrack paper artifacts.
    """

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        market_db: Path | None = None,
        config: dict[str, Any] | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        proposal_registry: StrategyProposalPluginRegistry | None = None,
    ) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        env_market_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
        self.market_db = Path(market_db or env_market_db or ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))
        self.config = config or dualtrack_config()
        proposal_runtime = compose_strategy_proposal(
            self.config,
            registry=proposal_registry,
        )
        self.proposal_plugin = proposal_runtime.audit_dict()
        market_data = self.config.get("market_data") if isinstance(self.config.get("market_data"), dict) else {}
        self.symbol = symbol or str(market_data.get("symbol") or "GOLD")
        self.timeframe = timeframe or str(market_data.get("timeframe") or "1m")
        self.store = DualTrackPlanStore(self.output_root, config=self.config)
        self.execution = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        self.machine = DualTrackMachineRunner(self.output_root, config=self.config)
        self.machine_planner = DualTrackMachinePlanner(
            self.output_root,
            config=self.config,
            proposal_runtime=proposal_runtime,
        )
        self.scorer = DualTrackScorer(self.output_root, config=self.config)
        # Explicit market_db remains a test/replay compatibility seam. Normal
        # production construction consumes the independent datafeed only.
        self.market = (
            MarketStore(self.market_db)
            if market_db is not None
            else DatafeedMarketRepository(config=pipeline_config)
        )

    def pre_cycle(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        bars = self._cycle_bars(cycle_id, as_of=as_of)
        if not bars:
            self.store.audit(cycle_id, "cycle_runner_pre_cycle_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "pre_cycle", "cycle_id": cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        rejected = self._market_bar_rejection_reason(bars)
        if rejected:
            self.store.audit(cycle_id, "cycle_runner_pre_cycle_skipped", {"reason": rejected})
            return {"event": "pre_cycle", "cycle_id": cycle_id, "status": "skipped", "reason": rejected}
        try:
            prev_range = self.previous_cycle_range(cycle_id)
        except ValueError as exc:
            reason = str(exc)
            self.store.audit(cycle_id, "cycle_runner_pre_cycle_skipped", {"reason": reason})
            return {"event": "pre_cycle", "cycle_id": cycle_id, "status": "skipped", "reason": reason}
        human_plan = self.store.ensure_human_plan_from_market_view(
            cycle_id,
            cycle_open=float(bars[0].open),
            prev_cycle_range=prev_range,
            now=as_of or cycle_window_from_id(cycle_id).start,
        )
        plan = self.machine_planner.ensure_plan(
            cycle_id,
            bars=bars,
            prev_cycle_range=prev_range,
            volatility_context=self.planning_volatility_context(cycle_id),
            as_of=as_of or cycle_window_from_id(cycle_id).start,
        )
        trend_gate_armed = self._freeze_trend_gate(cycle_id, as_of=as_of or cycle_window_from_id(cycle_id).start)
        return {
            "event": "pre_cycle",
            "cycle_id": cycle_id,
            "status": "ai_plan_error_neutral" if plan.get("degraded") else "ai_plan_ready",
            "human_plan_present": human_plan is not None,
            "ai_plan_present": plan is not None,
            "prev_range": prev_range,
            "bar_count": len(bars),
            "trend_gate_armed": trend_gate_armed,
            "proposal_plugin": self.proposal_plugin,
        }

    def intraday_tick(self, cycle_id: str | None = None, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        window = cycle_window(as_of) if cycle_id is None else cycle_window_from_id(cycle_id)
        production_control = StrategyControlPlane(self.output_root)
        if production_control.runtime_configured() and production_control.runtime_state(window.cycle_id)["desired_state"] != "running":
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": "production_strategy_stopped"})
            return {"event": "intraday", "cycle_id": window.cycle_id, "status": "skipped", "reason": "production_strategy_stopped"}
        session_status = self._market_session_status(as_of)
        if self._market_session_enabled() and not session_status["is_open"]:
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": "market_closed", "market_session": session_status})
            return {
                "event": "intraday",
                "cycle_id": window.cycle_id,
                "status": "skipped",
                "reason": "market_closed",
                "market_session": session_status,
            }
        bars = self._cycle_bars(window.cycle_id, as_of=as_of)
        if not bars:
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "intraday", "cycle_id": window.cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        rejected = self._market_bar_rejection_reason(bars)
        if rejected:
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": rejected})
            return {"event": "intraday", "cycle_id": window.cycle_id, "status": "skipped", "reason": rejected}
        try:
            prev_range = self.previous_cycle_range(window.cycle_id)
        except ValueError as exc:
            reason = str(exc)
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": reason})
            return {"event": "intraday", "cycle_id": window.cycle_id, "status": "skipped", "reason": reason}
        trend_gate_armed = self._frozen_or_freeze_trend_gate(window.cycle_id, as_of=window.start)
        state = self.machine.run_effective_plan(
            window.cycle_id,
            bars,
            prev_range=prev_range,
            as_of=as_of,
            trend_gate_armed=trend_gate_armed,
            finalize=False,
            execution_provenance={
                "origin": "live_observed",
                "classified_at": parse_utc(as_of).isoformat(),
                "live_observed_until": parse_utc(as_of).isoformat(),
                "reason": "intraday_tick_observed",
            },
        )
        reassessment = self._advance_range_reassessment(
            window.cycle_id,
            bars,
            state=state,
            prev_range=prev_range,
            as_of=as_of,
        )
        if reassessment is not None:
            state = self._attach_range_reassessment(window.cycle_id, state, reassessment)
        self._write_runner_state(
            window.cycle_id,
            "intraday",
            {"bar_count": len(bars), "prev_range": prev_range, "range_reassessment": reassessment or {}},
            observed_at=as_of,
        )
        trade_notifications = self._notify_machine_trade_records(window.cycle_id)
        return {
            "event": "intraday",
            "cycle_id": window.cycle_id,
            "status": "ran",
            "bar_count": len(bars),
            "state": state,
            "range_reassessment": reassessment,
            "trade_notifications": trade_notifications,
        }

    def _advance_range_reassessment(
        self,
        cycle_id: str,
        bars: Sequence[Bar],
        *,
        state: dict[str, Any],
        prev_range: float,
        as_of: str | datetime | None,
    ) -> dict[str, Any] | None:
        rules = self._range_reassessment_config()
        if not rules["enabled"]:
            return None
        plan = self.store.machine_plan(cycle_id)
        if not plan or plan.get("degraded"):
            return None
        now = parse_utc(as_of)
        latest = self._latest_range_reassessment(cycle_id)
        plan_locked_at = str(plan.get("locked_at") or "")
        same_plan_pending = bool(
            latest
            and latest.get("plan_locked_at") == plan_locked_at
            and latest.get("status") != "replanned"
        )
        trigger = _confirmed_range_breach(plan, bars, confirm_closes=rules["confirm_closes"])
        if not trigger and same_plan_pending and isinstance(latest.get("trigger"), dict):
            trigger = dict(latest["trigger"])
        if not trigger:
            touch = _first_range_touch(plan, bars)
            if not touch:
                return latest if latest and latest.get("status") == "replanned" else None
            return self._record_range_reassessment(cycle_id, {
                "status": "awaiting_confirmation",
                "plan_locked_at": plan_locked_at,
                "previous_range": dict(plan.get("range") or {}),
                "touch": touch,
                "confirmation_rule": {"timeframe": "1m", "consecutive_closes": rules["confirm_closes"]},
                "entry_mode": "paused",
            }, now=now)

        if same_plan_pending and latest.get("status") in {"max_replans_reached", "window_too_short"}:
            return latest
        if same_plan_pending and latest.get("status") == "failed":
            failed_at = parse_utc(latest.get("recorded_at"))
            retry_at = parse_utc(latest.get("retry_at")) if latest.get("retry_at") else (
                failed_at + timedelta(minutes=rules["failure_retry_minutes"])
            )
            if now < retry_at:
                return latest
        if same_plan_pending and latest.get("status") == "cooldown":
            retry_at = parse_utc(latest.get("retry_at")) if latest.get("retry_at") else None
            if retry_at and now < retry_at:
                return latest

        open_positions = _open_machine_positions(
            load_json(self.output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
        )
        if open_positions:
            return self._record_range_reassessment(cycle_id, {
                "status": "waiting_for_flat",
                "plan_locked_at": plan_locked_at,
                "previous_range": dict(plan.get("range") or {}),
                "trigger": trigger,
                "open_position_ids": [str(row.get("position_id") or row.get("trade_id") or "") for row in open_positions],
                "entry_mode": "exit_only",
            }, now=now)

        revision_rows = load_json(self.output_root / "dualtrack" / "plan_revisions" / f"{cycle_id}_ai.json")
        range_revisions = [row for row in revision_rows if str(row.get("reason") or "").startswith("confirmed_range_breach_")]
        if len(range_revisions) >= rules["max_replans_per_cycle"]:
            return self._record_range_reassessment(cycle_id, {
                "status": "max_replans_reached",
                "plan_locked_at": plan_locked_at,
                "previous_range": dict(plan.get("range") or {}),
                "trigger": trigger,
                "revision_count": len(range_revisions),
                "entry_mode": "paused",
            }, now=now)

        if range_revisions:
            last_revision_at = parse_utc(range_revisions[-1]["revised_at"])
            retry_at = last_revision_at + timedelta(minutes=rules["cooldown_minutes"])
            if now < retry_at:
                return self._record_range_reassessment(cycle_id, {
                    "status": "cooldown",
                    "plan_locked_at": plan_locked_at,
                    "previous_range": dict(plan.get("range") or {}),
                    "trigger": trigger,
                    "retry_at": retry_at.isoformat(),
                    "entry_mode": "exit_only",
                }, now=now)

        window = cycle_window_from_id(cycle_id)
        remaining_minutes = (window.end - now).total_seconds() / 60.0
        if remaining_minutes < rules["minimum_remaining_minutes"]:
            return self._record_range_reassessment(cycle_id, {
                "status": "window_too_short",
                "plan_locked_at": plan_locked_at,
                "previous_range": dict(plan.get("range") or {}),
                "trigger": trigger,
                "remaining_minutes": round(remaining_minutes, 2),
                "entry_mode": "paused",
            }, now=now)

        replan_context = {
            "status": "confirmed",
            "trigger": trigger,
            "previous_plan_locked_at": plan_locked_at,
            "previous_range": dict(plan.get("range") or {}),
            "detected_at": now.isoformat(),
            "confirmation_rule": {"timeframe": "1m", "consecutive_closes": rules["confirm_closes"]},
        }
        revision_reason = f"confirmed_range_breach_{trigger['side']}"
        execution_start = max(now, parse_utc(bars[-1].timestamp) + timedelta(minutes=1))
        revised = self.machine_planner.ensure_plan(
            cycle_id,
            bars=bars,
            prev_cycle_range=prev_range,
            volatility_context=self.planning_volatility_context(cycle_id),
            as_of=now,
            force=True,
            execution_start=execution_start,
            revision_reason=revision_reason,
            replan_context=replan_context,
        )
        success = bool(
            revised.get("locked_at")
            and revised.get("locked_at") != plan_locked_at
            and revised.get("revision_reason") == revision_reason
        )
        if not success:
            planning_rows = load_json(self.output_root / "dualtrack" / "planning" / f"{cycle_id}_machine.json")
            planning_error = str((planning_rows[-1] if planning_rows else {}).get("planning_error") or "range_reassessment_failed")
            return self._record_range_reassessment(cycle_id, {
                "status": "failed",
                "plan_locked_at": plan_locked_at,
                "previous_range": dict(plan.get("range") or {}),
                "trigger": trigger,
                "planning_error": planning_error,
                "retry_at": (now + timedelta(minutes=rules["failure_retry_minutes"])).isoformat(),
                "entry_mode": "paused",
            }, now=now)
        return self._record_range_reassessment(cycle_id, {
            "status": "replanned",
            "plan_locked_at": plan_locked_at,
            "previous_range": dict(plan.get("range") or {}),
            "trigger": trigger,
            "replacement_plan": {
                "locked_at": revised.get("locked_at"),
                "execution_start": revised.get("execution_start"),
                "direction": revised.get("direction"),
                "range": dict(revised.get("range") or {}),
                "grid_order_count": len(revised.get("grid_orders") or []),
            },
            "entry_mode": "new_plan",
        }, now=now)

    def _range_reassessment_config(self) -> dict[str, Any]:
        planner = self.config.get("machine_planner") if isinstance(self.config.get("machine_planner"), dict) else {}
        raw = planner.get("range_reassessment") if isinstance(planner.get("range_reassessment"), dict) else {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "confirm_closes": max(2, int(raw.get("confirm_closes", 3))),
            "cooldown_minutes": max(0, int(raw.get("cooldown_minutes", 60))),
            "failure_retry_minutes": max(1, int(raw.get("failure_retry_minutes", 5))),
            "max_replans_per_cycle": max(1, int(raw.get("max_replans_per_cycle", 2))),
            "minimum_remaining_minutes": max(0, int(raw.get("minimum_remaining_minutes", 30))),
        }

    def _latest_range_reassessment(self, cycle_id: str) -> dict[str, Any] | None:
        rows = load_json(self.output_root / "dualtrack" / "reassessment" / f"{cycle_id}.json")
        return rows[-1] if rows else None

    def _record_range_reassessment(
        self,
        cycle_id: str,
        payload: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        path = self.output_root / "dualtrack" / "reassessment" / f"{cycle_id}.json"
        rows = load_json(path)
        row = {
            "schema_version": "dualtrack-range-reassessment-v1",
            "cycle_id": cycle_id,
            "recorded_at": now.isoformat(),
            **payload,
        }
        latest = rows[-1] if rows else {}
        def signature(item: dict[str, Any]) -> tuple[Any, ...]:
            return (
                item.get("status"),
                item.get("plan_locked_at"),
                (item.get("trigger") or {}).get("confirmed_at"),
                (item.get("touch") or {}).get("touched_at"),
                item.get("retry_at"),
                ((item.get("replacement_plan") or {}).get("locked_at")),
            )
        if signature(latest) == signature(row):
            return latest
        rows.append(row)
        write_json(path, rows)
        self.store.audit(cycle_id, f"machine_range_reassessment_{row['status']}", row)
        return row

    def _attach_range_reassessment(
        self,
        cycle_id: str,
        state: dict[str, Any],
        reassessment: dict[str, Any],
    ) -> dict[str, Any]:
        updated = dict(state)
        status = str(reassessment.get("status") or "")
        layers = [item for item in updated.get("layers") or [] if not str(item).startswith("range_reassessment:")]
        layers.append(f"range_reassessment:{status}")
        updated["layers"] = layers
        updated["range_reassessment"] = dict(reassessment)
        if status in {"failed", "max_replans_reached", "window_too_short"}:
            updated["machine_stood_down"] = True
        write_json(self.output_root / "dualtrack" / "cycles" / f"{cycle_id}.json", [updated])
        return updated

    def close_cycle(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        existing = load_json(self.output_root / "dualtrack" / "attribution" / f"{cycle_id}.json")
        if existing:
            return {
                "event": "close",
                "cycle_id": cycle_id,
                "status": "already_closed",
                "attribution": existing[-1],
                "shadow_finalization": self._finalize_execution_shadow(cycle_id),
            }
        bars = self._cycle_bars(cycle_id, as_of=cycle_window_from_id(cycle_id).end)
        if not bars:
            self.store.audit(cycle_id, "cycle_runner_close_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "close", "cycle_id": cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        rejected = self._market_bar_rejection_reason(bars)
        if rejected:
            self.store.audit(cycle_id, "cycle_runner_close_skipped", {"reason": rejected})
            return {"event": "close", "cycle_id": cycle_id, "status": "skipped", "reason": rejected}
        human_fill_sync = self._sync_human_fills_before_close(cycle_id, as_of=as_of or cycle_window_from_id(cycle_id).end)
        if self._human_fill_sync_blocks_close(human_fill_sync):
            self.store.audit(cycle_id, "cycle_runner_close_skipped", {"reason": "human_fill_sync_blocked", "human_fill_sync": human_fill_sync})
            return {
                "event": "close",
                "cycle_id": cycle_id,
                "status": "skipped",
                "reason": "human_fill_sync_blocked",
                "human_fill_sync": human_fill_sync,
            }
        try:
            prev_range = self.previous_cycle_range(cycle_id)
        except ValueError as exc:
            reason = str(exc)
            self.store.audit(cycle_id, "cycle_runner_close_skipped", {"reason": reason})
            return {"event": "close", "cycle_id": cycle_id, "status": "skipped", "reason": reason}
        trend_gate_armed = self._frozen_or_freeze_trend_gate(cycle_id, as_of=cycle_window_from_id(cycle_id).start)
        execution_provenance = self._close_execution_provenance(cycle_id, classified_at=parse_utc(as_of))
        self.machine.run_effective_plan(
            cycle_id,
            bars,
            prev_range=prev_range,
            as_of=as_of or cycle_window_from_id(cycle_id).end,
            trend_gate_armed=trend_gate_armed,
            finalize=True,
            execution_provenance=execution_provenance,
        )
        attribution = self.scorer.close_cycle(cycle_id, bars)
        detail = {"bar_count": len(bars), "prev_range": prev_range}
        if human_fill_sync is not None:
            detail["human_fill_sync"] = self._human_fill_sync_summary(human_fill_sync)
        self._write_runner_state(cycle_id, "close", detail)
        payload = {
            "event": "close",
            "cycle_id": cycle_id,
            "status": "closed",
            "execution_provenance": execution_provenance,
            "attribution": attribution,
        }
        if human_fill_sync is not None:
            payload["human_fill_sync"] = human_fill_sync
        payload["trade_notifications"] = self._notify_machine_trade_records(cycle_id)
        payload["shadow_finalization"] = self._finalize_execution_shadow(cycle_id)
        return payload

    def _finalize_execution_shadow(self, cycle_id: str) -> dict[str, Any] | None:
        flush_shadow = getattr(self.execution, "flush_shadow", None)
        if not callable(flush_shadow):
            return None
        return flush_shadow(cycle_id, cycle_complete=True)

    def fast_forward_day(self, date: str) -> dict[str, Any]:
        results = []
        for kind in ("DAY", "NIGHT"):
            cycle_id = f"{date}_{kind}"
            window = cycle_window_from_id(cycle_id)
            results.append(self.pre_cycle(cycle_id, as_of=window.start))
            results.append(self.intraday_tick(cycle_id, as_of=window.end))
            results.append(self.close_cycle(cycle_id, as_of=window.end))
        return {"event": "fast_forward_day", "date": date, "results": results}

    def reclassify_closed_cycle(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        provenance = self._close_execution_provenance(cycle_id, classified_at=now)
        fills = self.machine.reclassify_execution_provenance(cycle_id, provenance)
        bars = self._cycle_bars(cycle_id, as_of=cycle_window_from_id(cycle_id).end)
        if not bars:
            raise ValueError("cycle_bars_missing")
        attribution = self.scorer.close_cycle(cycle_id, bars, force=True)
        return {
            "event": "reclassify_execution_provenance",
            "cycle_id": cycle_id,
            "execution_provenance": provenance,
            "classified_fill_count": len(fills),
            "attribution": attribution,
        }

    def auto(self, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        session_status = self._market_session_status(now)
        if self._market_session_enabled() and not session_status["is_open"]:
            current = cycle_window(now)
            self._write_runner_state(current.cycle_id, "auto_skipped", {"reason": "market_closed", "market_session": session_status})
            return {
                "event": "auto",
                "as_of": now.isoformat(),
                "status": "skipped",
                "reason": "market_closed",
                "market_session": session_status,
                "results": [],
            }
        current = cycle_window(now)
        results = self._lifecycle_results(now)
        results.append(self.intraday_tick(current.cycle_id, as_of=now))
        return {"event": "auto", "as_of": now.isoformat(), "results": results}

    def sync_obsidian_human_plans(
        self,
        cycle_id: str | None = None,
        *,
        as_of: str | datetime | None = None,
        include_next: bool = False,
    ) -> dict[str, Any]:
        now = parse_utc(as_of)
        cycle_ids = [cycle_id] if cycle_id else [cycle_window(now).cycle_id]
        if include_next and not cycle_id:
            current = cycle_window(now)
            next_cycle = cycle_window(current.end).cycle_id
            if next_cycle not in cycle_ids:
                cycle_ids.append(next_cycle)
        results = [
            self.sync_obsidian_human_plan(item, as_of=now, allow_lock=index == 0)
            for index, item in enumerate(cycle_ids)
        ]
        return {"event": "sync_obsidian_plan", "as_of": now.isoformat(), "results": results}

    def sync_obsidian_human_plan(
        self,
        cycle_id: str,
        *,
        as_of: str | datetime | None = None,
        allow_lock: bool = True,
    ) -> dict[str, Any]:
        now = parse_utc(as_of)
        try:
            reference_open = self._reference_open_for_plan(cycle_id, as_of=now)
            prev_cycle_range = self.previous_cycle_range(cycle_id)
        except ValueError as exc:
            reason = str(exc)
            self.store.audit(cycle_id, "human_plan_import_skipped", {"reason": reason, "source": "obsidian"})
            return {"cycle_id": cycle_id, "status": "skipped", "reason": reason}
        if reference_open is None:
            self.store.audit(cycle_id, "human_plan_import_skipped", {"reason": "reference_price_missing", "source": "obsidian"})
            return {"cycle_id": cycle_id, "status": "skipped", "reason": "reference_price_missing"}
        plan = self.store.ensure_human_plan_from_market_view(
            cycle_id,
            cycle_open=reference_open,
            prev_cycle_range=prev_cycle_range,
            now=now,
            allow_lock=allow_lock,
        )
        if not plan:
            return {"cycle_id": cycle_id, "status": "skipped", "reason": "market_view_missing_or_not_directional"}
        return {
            "cycle_id": cycle_id,
            "status": "synced",
            "plan_status": plan.get("status"),
            "direction": plan.get("direction"),
            "source": plan.get("source"),
        }

    def live_tick(self, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        window = cycle_window(now)
        lifecycle = self._lifecycle_results(now)
        protective_sweep = self._sweep_active_human_protective_exits(window.cycle_id, now=now)
        sync = self.sync_obsidian_human_plans(as_of=now, include_next=False)
        intraday = self.intraday_tick(as_of=now)
        ledger = self.scorer.rebuild_ledgers()
        return {
            "event": "live_tick",
            "as_of": now.isoformat(),
            "lifecycle": lifecycle,
            "protective_sweep": protective_sweep,
            "sync": sync,
            "intraday": intraday,
            "ledger_refreshed": True,
            "ledger_daily_count": len(ledger.get("daily") or []),
        }

    def _lifecycle_results(self, now: datetime) -> list[dict[str, Any]]:
        current = cycle_window(now)
        previous = cycle_window(current.start - timedelta(minutes=1))
        return [
            self.close_cycle(previous.cycle_id, as_of=now),
            self.pre_cycle(current.cycle_id, as_of=now),
            self._rollover_production(previous.cycle_id, current.cycle_id, now=now),
        ]

    def _rollover_production(
        self,
        previous_cycle_id: str,
        current_cycle_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Resume the durable paper rollover state machine at a cycle boundary."""

        with production_mutation_lock(self.output_root):
            return self._rollover_production_locked(
                previous_cycle_id,
                current_cycle_id,
                now=now,
            )

    def _rollover_production_locked(
        self,
        previous_cycle_id: str,
        current_cycle_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:

        control = StrategyControlPlane(self.output_root)
        if not control.runtime_configured():
            return {
                "event": "production_rollover",
                "status": "skipped",
                "reason": "production_runtime_not_configured",
            }
        persisted = control.persisted_runtime_state()
        rows = self._rollover_rows(previous_cycle_id, current_cycle_id)
        latest = rows[-1] if rows else {}
        rollover_owner = f"rollover:{previous_cycle_id}->{current_cycle_id}"
        owned_stop = next(
            (row for row in reversed(rows) if row.get("status") == "previous_cycle_stopped"),
            None,
        )

        if persisted.get("cycle_id") == current_cycle_id:
            desired = str(persisted.get("desired_state") or "stopped")
            actual = str(persisted.get("actual_state") or desired)
            if desired == "running" and actual == "running":
                package_hash = next(
                    (
                        str(row.get("package_hash"))
                        for row in reversed(rows)
                        if row.get("package_hash")
                    ),
                    "",
                )
                if latest.get("status") != "completed" and package_hash:
                    latest = self._record_rollover(
                        previous_cycle_id,
                        current_cycle_id,
                        {
                            "status": "completed",
                            "should_continue": True,
                            "package_hash": package_hash,
                            "strategy_plan_id": persisted.get("strategy_plan_id"),
                            "strategy_plan_version": persisted.get("strategy_plan_version"),
                            "accepted_orders": int(persisted.get("accepted_order_count") or 0),
                            "recovered_after_restart": True,
                        },
                        now=now,
                    )
                    return {"event": "production_rollover", **latest}
                return {
                    "event": "production_rollover",
                    "status": "already_running",
                    "cycle_id": current_cycle_id,
                }
            rollover_owned_incomplete = (
                actual in {"starting", "stopping"}
                or (
                    actual == "error"
                    and str(persisted.get("transition_owner") or "") == rollover_owner
                )
            )
            if rollover_owned_incomplete:
                try:
                    recovered = control.control(
                        current_cycle_id,
                        "stop",
                        {
                            "expected_runtime_updated_at": persisted.get("updated_at"),
                            "transition_owner": rollover_owner,
                        },
                        market=self._paper_safe_action_market_snapshot(now),
                        now=now.isoformat(),
                        actor={"transport": "system", "client": "dualtrack-rollover-recovery"},
                    )
                    reconciliation = dict(recovered.get("reconciliation") or {})
                    if str(reconciliation.get("status") or "") != "ok":
                        raise ValueError("incomplete current cycle recovery reconciliation failed")
                    blocked = self._record_rollover(
                        previous_cycle_id,
                        current_cycle_id,
                        {
                            "status": "blocked",
                            "stage": "current_cycle_recovered_stopped",
                            "should_continue": False,
                            "reason": f"recovered incomplete current cycle runtime from {actual}",
                            "cancelled_orders": int(recovered.get("cancelled_orders") or 0),
                            "flattened_positions": int(recovered.get("flattened_positions") or 0),
                            "reconciliation_status": reconciliation.get("status"),
                            "fail_closed": True,
                        },
                        now=now,
                    )
                except Exception as exc:
                    blocked = self._record_rollover(
                        previous_cycle_id,
                        current_cycle_id,
                        {
                            "status": "blocked",
                            "stage": "current_cycle_recovery_failed",
                            "should_continue": False,
                            "reason": str(exc),
                            "error_type": type(exc).__name__,
                            "fail_closed": True,
                        },
                        now=now,
                    )
                return {"event": "production_rollover", **blocked}
            # Once the new cycle owns the runtime namespace, an automatic
            # retry could override an operator stop or repeat a failed start.
            return {
                "event": "production_rollover",
                "status": "skipped",
                "reason": "current_cycle_runtime_not_running",
                "cycle_id": current_cycle_id,
                "desired_state": desired,
                "actual_state": actual,
            }

        if latest.get("status") == "cancelled" and latest.get("should_continue") is False:
            return {"event": "production_rollover", **latest}

        previous_running = (
            persisted.get("cycle_id") == previous_cycle_id
            and persisted.get("desired_state") == "running"
        )
        rollover_owned_incomplete_stop = (
            persisted.get("cycle_id") == previous_cycle_id
            and str(persisted.get("actual_state") or "") in {"stopping", "error"}
            and str(persisted.get("transition_owner") or "") == rollover_owner
            and bool(rows and any(row.get("should_continue") for row in rows))
        )
        rollover_owned_stop = bool(
            owned_stop
            and persisted.get("cycle_id") == previous_cycle_id
            and persisted.get("desired_state") == "stopped"
            and persisted.get("actual_state") == "stopped"
            and str(persisted.get("updated_at") or "")
            == str(owned_stop.get("runtime_updated_at") or "")
        )
        should_continue = previous_running or rollover_owned_stop or rollover_owned_incomplete_stop
        if not should_continue:
            if rows and any(row.get("should_continue") for row in rows):
                cancelled = self._record_rollover(
                    previous_cycle_id,
                    current_cycle_id,
                    {
                        "status": "cancelled",
                        "stage": "operator_intent_check",
                        "should_continue": False,
                        "reason": "runtime changed after rollover intent",
                        "fail_closed": True,
                    },
                    now=now,
                )
                return {"event": "production_rollover", **cancelled}
            return {
                "event": "production_rollover",
                "status": "skipped",
                "reason": "previous_cycle_not_running",
                "previous_cycle_id": previous_cycle_id,
                "current_cycle_id": current_cycle_id,
            }
        if not latest:
            latest = self._record_rollover(
                previous_cycle_id,
                current_cycle_id,
                {
                    "status": "intent_recorded",
                    "should_continue": True,
                    "runtime_updated_at": persisted.get("updated_at"),
                },
                now=now,
            )

        stage = str(latest.get("status") or "intent_recorded")
        try:
            market = self._paper_safe_action_market_snapshot(now)
            if persisted.get("cycle_id") == previous_cycle_id and persisted.get("actual_state") != "stopped":
                stage = "stopping_previous_cycle"
                intent = next(
                    (row for row in reversed(self._rollover_rows(previous_cycle_id, current_cycle_id)) if row.get("status") == "intent_recorded"),
                    {},
                )
                stopped = control.control(
                    previous_cycle_id,
                    "stop",
                    {
                        "expected_runtime_updated_at": (
                            persisted.get("updated_at")
                            if rollover_owned_incomplete_stop
                            else intent.get("runtime_updated_at")
                        ),
                        "transition_owner": rollover_owner,
                    },
                    market=market,
                    now=now.isoformat(),
                    actor={"transport": "system", "client": "dualtrack-live-tick"},
                )
                reconciliation = dict(stopped.get("reconciliation") or {})
                if str(reconciliation.get("status") or "") != "ok":
                    raise ValueError("previous cycle stop reconciliation failed")
                latest = self._record_rollover(
                    previous_cycle_id,
                    current_cycle_id,
                    {
                        "status": "previous_cycle_stopped",
                        "should_continue": True,
                        "cancelled_orders": int(stopped.get("cancelled_orders") or 0),
                        "flattened_positions": int(stopped.get("flattened_positions") or 0),
                        "reconciliation_status": reconciliation.get("status"),
                        "runtime_updated_at": (stopped.get("runtime") or {}).get("updated_at"),
                    },
                    now=now,
                )

            stage = "packaging_previous_cycle"
            package = StrategyCyclePackager(
                self.output_root,
                config=self.config,
                adapter=self.execution,
            ).package(previous_cycle_id, now=now.isoformat())
            if package.get("status") != "closed":
                raise ValueError("previous production cycle package is not terminal")
            latest = self._record_rollover(
                previous_cycle_id,
                current_cycle_id,
                {
                    "status": "previous_cycle_packaged",
                    "should_continue": True,
                    "package_hash": package.get("package_hash"),
                },
                now=now,
            )

            stage = "starting_current_cycle"
            stopped_receipt = next(
                (
                    row
                    for row in reversed(self._rollover_rows(previous_cycle_id, current_cycle_id))
                    if row.get("status") == "previous_cycle_stopped"
                ),
                None,
            )
            market = self._production_start_market_snapshot(now, market=market)
            plan = control.active_plan(current_cycle_id) or control.ensure_compatible_active_plan(
                current_cycle_id,
                as_of=now.isoformat(),
            )
            if not plan:
                raise ValueError("current cycle has no trusted production plan")
            current_runtime = control.persisted_runtime_state()
            if not (
                stopped_receipt
                and current_runtime.get("cycle_id") == previous_cycle_id
                and current_runtime.get("desired_state") == "stopped"
                and current_runtime.get("actual_state") == "stopped"
                and str(current_runtime.get("updated_at") or "")
                == str(stopped_receipt.get("runtime_updated_at") or "")
            ):
                raise ValueError("paper runtime changed after rollover stop")
            start_payload = self._rollover_start_payload(plan)
            start_payload["rollover_guard"] = {
                "previous_cycle_id": previous_cycle_id,
                "expected_runtime_updated_at": stopped_receipt.get("runtime_updated_at"),
            }
            started = control.control(
                current_cycle_id,
                "start",
                start_payload,
                market=market,
                account=dict((package.get("execution") or {}).get("account") or {}),
                now=now.isoformat(),
                actor={"transport": "system", "client": "dualtrack-live-tick"},
            )
            final = self._record_rollover(
                previous_cycle_id,
                current_cycle_id,
                {
                    "status": "completed",
                    "should_continue": True,
                    "package_hash": package.get("package_hash"),
                    "strategy_plan_id": (started.get("plan") or {}).get("strategy_plan_id"),
                    "strategy_plan_version": (started.get("plan") or {}).get("version"),
                    "accepted_orders": int(started.get("accepted_orders") or 0),
                },
                now=now,
            )
            return {"event": "production_rollover", **final, "package": package}
        except Exception as exc:
            operator_changed_runtime = str(exc) in {
                "paper runtime changed after rollover intent",
                "paper runtime changed after rollover stop",
                "paper runtime changed before rollover start",
            }
            blocked = self._record_rollover(
                previous_cycle_id,
                current_cycle_id,
                {
                    "status": "cancelled" if operator_changed_runtime else "blocked",
                    "stage": stage,
                    "should_continue": not operator_changed_runtime,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    "fail_closed": True,
                },
                now=now,
            )
            return {"event": "production_rollover", **blocked}

    def _production_market_snapshot(self, now: datetime) -> dict[str, Any]:
        pipeline_config = load_pipeline_config()
        feed = DualTrackMarketFeed(config=pipeline_config)
        return feed.snapshot(
            symbol=self.symbol,
            timeframe="1m",
            limit=240,
            as_of=now.isoformat(),
        )

    def _paper_safe_action_market_snapshot(self, now: datetime) -> dict[str, Any]:
        """Never let a quote transport error prevent paper cancel/flatten."""

        try:
            return self._production_market_snapshot(now)
        except Exception as exc:
            return {
                "schema_version": "dualtrack-market-bars-v1",
                "status": "blocked",
                "fresh": False,
                "is_synthetic": False,
                "provider": "",
                "source_mode": "unavailable",
                "symbol": getattr(self, "symbol", "GOLD"),
                "timeframe": "1m",
                "latest_close": None,
                "latest_timestamp": "",
                "bars": [],
                "access_issues": [f"{type(exc).__name__}: {exc}"],
            }

    def _production_start_market_snapshot(
        self,
        now: datetime,
        *,
        market: dict[str, Any],
    ) -> dict[str, Any]:
        """Add planning evidence only after old-cycle safe actions complete."""

        result = dict(market)
        result["strategy_timeframes"] = build_strategy_timeframes(
            symbol=self.symbol,
            as_of=now.isoformat(),
            config=load_pipeline_config(),
            timeframes=("1d", "4h"),
        )
        return result

    @staticmethod
    def _rollover_start_payload(plan: dict[str, Any]) -> dict[str, Any]:
        grid = dict(plan.get("grid") or {})
        risk = dict(plan.get("risk_budget") or {})
        payload: dict[str, Any] = {
            "direction": str(plan.get("direction") or "neutral"),
            "style": str(plan.get("style") or "steady"),
            "out_of_range": str(grid.get("out_of_range") or "wait"),
            "grid": {"mode": str(grid.get("mode") or "arithmetic")},
            "risk_budget": {
                "leverage": float(risk.get("leverage") or risk.get("max_leverage") or 10.0),
            },
        }
        range_spec = dict(plan.get("range") or {})
        if range_spec.get("low") is not None and range_spec.get("high") is not None:
            payload["range"] = {"low": range_spec["low"], "high": range_spec["high"]}
        for key in ("count", "notional_per_grid"):
            if grid.get(key) is not None:
                payload["grid"][key] = grid[key]
        if grid.get("notional_per_grid") is not None:
            payload["grid"]["notional_mode"] = "manual"
        return payload

    def _rollover_rows(self, previous_cycle_id: str, current_cycle_id: str) -> list[dict[str, Any]]:
        return load_json(self._rollover_path(previous_cycle_id, current_cycle_id))

    def _record_rollover(
        self,
        previous_cycle_id: str,
        current_cycle_id: str,
        payload: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        path = self._rollover_path(previous_cycle_id, current_cycle_id)
        rows = load_json(path)
        row = {
            "schema_version": "strategy-cycle-rollover-v1",
            "previous_cycle_id": previous_cycle_id,
            "current_cycle_id": current_cycle_id,
            "recorded_at": now.isoformat(),
            **payload,
        }
        signature = tuple(
            row.get(key)
            for key in ("status", "stage", "reason", "package_hash", "strategy_plan_id")
        )
        if rows:
            latest_signature = tuple(
                rows[-1].get(key)
                for key in ("status", "stage", "reason", "package_hash", "strategy_plan_id")
            )
            if signature == latest_signature:
                return rows[-1]
        rows.append(row)
        write_json(path, rows)
        return row

    def _rollover_path(self, previous_cycle_id: str, current_cycle_id: str) -> Path:
        filename = f"{previous_cycle_id}__{current_cycle_id}.json"
        return self.output_root / "dualtrack" / "strategy_control" / "rollovers" / filename

    def _close_execution_provenance(self, cycle_id: str, *, classified_at: datetime) -> dict[str, Any]:
        window = cycle_window_from_id(cycle_id)
        runner_rows = load_json(self.output_root / "dualtrack" / "runner" / f"{cycle_id}.json")
        observed: list[datetime] = []
        for row in runner_rows:
            if row.get("event") != "intraday" or not row.get("ts"):
                continue
            try:
                timestamp = parse_utc(row["ts"])
            except (TypeError, ValueError):
                continue
            if window.start <= timestamp <= window.end:
                observed.append(timestamp)
        latest = max(observed, default=None)
        complete = latest is not None and (window.end - latest).total_seconds() <= 600
        return {
            "origin": "live_observed" if complete else "recovery_replay",
            "classified_at": classified_at.isoformat(),
            "live_observed_until": latest.isoformat() if latest else None,
            "reason": "live_coverage_reached_cycle_close" if complete else "cycle_closed_without_complete_live_runner_coverage",
        }

    def _sweep_active_human_protective_exits(self, current_cycle_id: str, *, now: datetime) -> dict[str, Any]:
        cycle_ids = self._active_human_cycle_ids(current_cycle_id)
        results = [
            (cycle_id, self._sweep_human_protective_exits(cycle_id, now=now))
            for cycle_id in cycle_ids
        ]
        if len(results) == 1:
            source_cycle_id, result = results[0]
            if source_cycle_id == current_cycle_id:
                return result
            return {**result, "source_cycle_id": source_cycle_id}
        triggered = [row for _, result in results for row in (result.get("triggered") or [])]
        accepted_limit_fills = [
            row for _, result in results for row in (result.get("accepted_limit_fills") or [])
        ]
        return {
            "status": "triggered" if triggered else "ok",
            "triggered": triggered,
            "accepted_limit_fills": accepted_limit_fills,
            "accepted_limit_fill_count": len(accepted_limit_fills),
            "processed_events": sum(int(result.get("processed_events") or 0) for _, result in results),
            "source": next((str(result.get("source") or "") for _, result in reversed(results) if result.get("source")), ""),
            "source_cycle_ids": [cycle_id for cycle_id, _ in results],
            "cycles": [
                {"cycle_id": cycle_id, "status": result.get("status"), "triggered": len(result.get("triggered") or [])}
                for cycle_id, result in results
            ],
        }

    def _active_human_cycle_ids(self, current_cycle_id: str) -> list[str]:
        current_start = cycle_window_from_id(current_cycle_id).start
        candidates = {current_cycle_id}
        for directory in ("fills", "orders"):
            root = self.output_root / "dualtrack" / directory
            if not root.exists():
                continue
            for path in root.glob("*_human.json"):
                candidates.add(path.name.removesuffix("_human.json"))
        active: list[str] = []
        for cycle_id in candidates:
            try:
                if cycle_window_from_id(cycle_id).start > current_start:
                    continue
            except ValueError:
                continue
            snapshot = self.execution.snapshot(cycle_id)
            has_position = any(
                str(position.get("status") or "open") == "open"
                and float(position.get("remaining_units") or 0.0) > 0
                for position in snapshot.get("positions") or []
            )
            has_pending_limit = any(
                str(order.get("state") or "") == "accepted"
                and str(order.get("event") or "entry") == "entry"
                and str(order.get("order_type") or "") == "limit"
                for order in snapshot.get("orders") or []
            )
            if has_position or has_pending_limit:
                active.append(cycle_id)
        return sorted(active, key=lambda value: cycle_window_from_id(value).start) or [current_cycle_id]

    def _sweep_human_protective_exits(self, cycle_id: str, *, now: datetime) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._sweep_human_protective_exits_locked(cycle_id, now=now)

    def _sweep_human_protective_exits_locked(
        self,
        cycle_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        snapshot = self.execution.snapshot(cycle_id)
        open_trades = [
            position
            for position in snapshot.get("positions", [])
            if str(position.get("status") or "open") == "open"
            and float(position.get("remaining_units") or 0.0) > 0
        ]
        pending_limits = [
            order
            for order in snapshot.get("orders", [])
            if str(order.get("state") or "") == "accepted"
            and str(order.get("event") or "entry") == "entry"
            and str(order.get("order_type") or "") == "limit"
        ]
        if not open_trades and not pending_limits:
            return {"status": "ok", "reason": "no_open_positions_or_pending_orders", "triggered": []}

        latest = self._latest_market_record()
        if not latest:
            return {"status": "skipped", "reason": "market_data_missing", "triggered": []}
        provider = str(latest.get("provider") or "")
        flags = {str(item).lower() for item in latest.get("quality_flags") or []}
        if "synthetic" in provider.lower() or "mock" in provider.lower() or any(
            "synthetic" in flag or "mock" in flag for flag in flags
        ):
            return {"status": "skipped", "reason": "synthetic_market_data", "triggered": []}

        expected_provider = str((self.config.get("market_data") or {}).get("provider") or "")
        if expected_provider and provider != expected_provider:
            return {"status": "skipped", "reason": "market_provider_mismatch", "triggered": []}
        try:
            market_ts = parse_utc(latest.get("timestamp"))
        except (TypeError, ValueError):
            return {"status": "skipped", "reason": "market_timestamp_invalid", "triggered": []}
        age_seconds = (now - market_ts).total_seconds()
        if age_seconds < -60:
            return {"status": "skipped", "reason": "market_timestamp_in_future", "triggered": []}
        if age_seconds > self._market_max_age_seconds():
            return {"status": "skipped", "reason": "market_data_stale", "triggered": []}
        if market_ts < cycle_window_from_id(cycle_id).start:
            return {"status": "skipped", "reason": "market_data_outside_cycle", "triggered": []}

        activity_times = [
            parsed
            for value in (
                [position.get("entry_ts") for position in open_trades]
                + [order.get("ts") for order in pending_limits]
            )
            if (parsed := _parse_execution_time(value)) is not None
        ]
        earliest_activity = min(activity_times, default=cycle_window_from_id(cycle_id).start)
        replay_start = max(
            cycle_window_from_id(cycle_id).start,
            earliest_activity.replace(second=0, microsecond=0),
        )
        bars = self._filter_market_session_bars(
            self.market.load_bars_between(self.symbol, self.timeframe, replay_start.isoformat(), now.isoformat())
        )
        rejected = self._market_bar_rejection_reason(bars)
        if rejected:
            return {"status": "skipped", "reason": rejected, "triggered": []}

        # The latest database row may still be the exchange's forming candle.
        # Replaying it before close would freeze an incomplete high/low behind
        # the event id and can create fills which never existed on a final bar.
        completed_bars = [
            bar
            for bar in bars
            if parse_utc(bar.timestamp) + self._timeframe_duration() <= now
        ]
        events = [
            self._protective_bar_event(cycle_id, bar, now=now)
            for bar in completed_bars
        ]
        if not events:
            return {
                "status": "ok",
                "reason": "waiting_for_completed_market_bar",
                "triggered": [],
                "accepted_limit_fills": [],
                "accepted_limit_fill_count": 0,
                "processed_events": 0,
            }

        triggered: list[dict[str, Any]] = []
        accepted_limit_fills: list[dict[str, Any]] = []
        last_sweep: dict[str, Any] = {"status": "ok", "triggered": []}
        for event in events:
            last_sweep = self.execution.process_market_event(event)
            triggered.extend(last_sweep.get("triggered") or [])
            accepted_limit_fills.extend(last_sweep.get("accepted_limit_fills") or [])
        flush_shadow = getattr(self.execution, "flush_shadow", None)
        shadow_flush = flush_shadow(cycle_id) if callable(flush_shadow) else None
        return {
            **last_sweep,
            "status": "triggered" if triggered else "ok",
            "triggered": triggered,
            "accepted_limit_fills": accepted_limit_fills,
            "accepted_limit_fill_count": len(accepted_limit_fills),
            "processed_events": len(events),
            "first_event_ts": events[0].get("event_started_at") or events[0].get("ts_event"),
            "last_event_ts": events[-1].get("event_started_at") or events[-1].get("ts_event"),
            "source": events[-1]["source"],
            "shadow_flush": shadow_flush,
        }

    def _protective_bar_event(self, cycle_id: str, bar: Bar, *, now: datetime) -> dict[str, Any]:
        started_at = parse_utc(bar.timestamp)
        ended_at = started_at + self._timeframe_duration()
        if ended_at > now:
            raise ValueError("market bar is not complete")
        return {
            "cycle_id": cycle_id,
            "event_id": f"{cycle_id}:{self.timeframe}:{started_at.isoformat()}",
            "ts_event": ended_at.isoformat(),
            "event_started_at": started_at.isoformat(),
            "price": float(bar.close),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "fresh": True,
            "is_synthetic": False,
            "source": f"market_db:{bar.provider or 'unknown'}",
            "provider": str(bar.provider or ""),
            "instrument_id": self._execution_instrument_id(),
        }
    def _execution_instrument_id(self) -> str:
        shadow = ((self.config.get("execution_shadow") or {}).get("nautilus") or {})
        return str(shadow.get("execution_instrument_id") or "XAUUSDT")

    def _latest_market_record(self) -> dict[str, Any]:
        records = [
            self.market.load_latest_bar(self.symbol, self.timeframe),
            self.market.load_latest_quote(self.symbol),
        ]
        valid = []
        for record in records:
            if not record or not record.get("timestamp"):
                continue
            try:
                timestamp = parse_utc(record["timestamp"])
            except (TypeError, ValueError):
                continue
            valid.append((timestamp, record))
        return max(valid, key=lambda item: item[0])[1] if valid else {}

    def _market_max_age_seconds(self) -> float:
        market_data = self.config.get("market_data") if isinstance(self.config.get("market_data"), dict) else {}
        configured = market_data.get("max_age_seconds")
        if configured not in (None, ""):
            return max(1.0, float(configured))
        value = str(self.timeframe or "1m").lower()
        if value.endswith("m"):
            try:
                return max(180.0, float(value[:-1]) * 180.0)
            except ValueError:
                pass
        return 180.0

    def _timeframe_duration(self) -> timedelta:
        value = str(self.timeframe or "1m").strip().lower()
        units = {"m": 60, "h": 3600, "d": 86400}
        try:
            seconds = int(value[:-1]) * units[value[-1]]
        except (KeyError, TypeError, ValueError):
            seconds = 60
        return timedelta(seconds=max(1, seconds))

    def previous_cycle_range(self, cycle_id: str) -> float:
        window = cycle_window_from_id(cycle_id)
        start = window.start - (window.end - window.start)
        end = window.start - timedelta(seconds=1)
        bars = self._filter_market_session_bars(
            self.market.load_bars_between(self.symbol, self.timeframe, start.isoformat(), end.isoformat())
        )
        if not bars:
            return 0.0
        rejected = self._market_bar_rejection_reason(bars)
        if rejected:
            raise ValueError(rejected)
        return round(max(float(bar.high) for bar in bars) - min(float(bar.low) for bar in bars), 8)

    def planning_volatility_context(self, cycle_id: str) -> dict[str, Any]:
        """Build a recorded range reference from complete non-weekend cycles."""
        planner = self.config.get("machine_planner") if isinstance(self.config.get("machine_planner"), dict) else {}
        lookback = max(1, int(planner.get("volatility_lookback_cycles") or 10))
        minimum_samples = max(1, int(planner.get("minimum_active_cycle_samples") or 3))
        minimum_coverage = min(1.0, max(0.0, float(planner.get("minimum_sample_coverage_pct") or 0.8)))
        multiplier = max(0.0, float(planner.get("minimum_range_multiplier") or 1.0))
        exclude_weekends = bool(planner.get("exclude_weekends_from_range_reference", True))
        current = cycle_window_from_id(cycle_id)
        duration_seconds = max(1.0, self._timeframe_duration().total_seconds())
        selected: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        cursor = current.start
        attempts = 0
        while len(selected) < lookback and attempts < lookback * 3:
            cursor -= timedelta(seconds=1)
            sample_window = cycle_window(cursor)
            cursor = sample_window.start
            sample_duration_seconds = max(1.0, (sample_window.end - sample_window.start).total_seconds())
            expected_bars = max(1, int(sample_duration_seconds / duration_seconds))
            bars = self._filter_market_session_bars(
                self.market.load_bars_between(
                    self.symbol,
                    self.timeframe,
                    sample_window.start.isoformat(),
                    (sample_window.end - timedelta(seconds=1)).isoformat(),
                )
            )
            attempts += 1
            sample = {
                "cycle_id": sample_window.cycle_id,
                "start": sample_window.start.isoformat(),
                "weekday": sample_window.start.strftime("%a"),
                "bar_count": len(bars),
                "expected_bar_count": expected_bars,
                "coverage_pct": round(min(1.0, len(bars) / expected_bars), 4),
            }
            if bars:
                sample["range"] = round(max(float(bar.high) for bar in bars) - min(float(bar.low) for bar in bars), 8)
            if exclude_weekends and sample_window.start.weekday() >= 5:
                excluded.append({**sample, "reason": "weekend_low_liquidity"})
                continue
            rejected = self._market_bar_rejection_reason(bars) if bars else "cycle_bars_missing"
            if rejected:
                excluded.append({**sample, "reason": rejected})
                continue
            if sample["coverage_pct"] < minimum_coverage:
                excluded.append({**sample, "reason": "sample_coverage_insufficient"})
                continue
            selected.append(sample)
        ranges = [float(sample["range"]) for sample in selected]
        reference = round(float(median(ranges)), 8) if len(ranges) >= minimum_samples else None
        cadence_hours = int((current.end - current.start).total_seconds() // 3600)
        return {
            "schema_version": "dualtrack-planning-volatility-v1",
            "method": f"median_completed_non_weekend_{cadence_hours}h_range",
            "status": "ready" if reference is not None else "insufficient_samples",
            "lookback_cycles": lookback,
            "minimum_active_cycle_samples": minimum_samples,
            "minimum_sample_coverage_pct": minimum_coverage,
            "exclude_weekends": exclude_weekends,
            "selected_samples": selected,
            "excluded_samples": excluded,
            "reference_range": reference,
            "minimum_plan_range": round(reference * multiplier, 8) if reference is not None else None,
            "minimum_range_multiplier": multiplier,
        }

    def _market_bar_rejection_reason(self, bars: Sequence[Bar]) -> str:
        expected_provider = str((self.config.get("market_data") or {}).get("provider") or "")
        for bar in bars:
            provider = str(bar.provider or "")
            flags = {str(item).lower() for item in bar.quality_flags or []}
            provider_key = provider.lower()
            if any(token in provider_key for token in ("synthetic", "mock", "fallback")) or any(
                any(token in flag for token in ("synthetic", "mock", "fallback")) for flag in flags
            ):
                return "synthetic_market_data"
            if expected_provider and provider != expected_provider:
                return "market_provider_mismatch"
        return ""

    def _cycle_bars(self, cycle_id: str, *, as_of: str | datetime | None = None) -> list[Bar]:
        window = cycle_window_from_id(cycle_id)
        end = parse_utc(as_of) if as_of is not None else window.end
        if end >= window.end:
            end = window.end - timedelta(seconds=1)
        if end < window.start:
            return []
        return self._filter_market_session_bars(
            self.market.load_bars_between(self.symbol, self.timeframe, window.start.isoformat(), end.isoformat())
        )

    def _reference_open_for_plan(self, cycle_id: str, *, as_of: str | datetime | None = None) -> float | None:
        bars = self._cycle_bars(cycle_id, as_of=as_of)
        if bars:
            rejected = self._market_bar_rejection_reason(bars)
            if rejected:
                raise ValueError(rejected)
            return float(bars[0].open)
        latest = self.market.load_latest_bar(self.symbol, self.timeframe)
        if latest:
            rejected = self._market_record_rejection_reason(latest)
            if rejected:
                raise ValueError(rejected)
            return float(latest["close"])
        quote = self.market.load_latest_quote(self.symbol)
        if quote:
            rejected = self._market_record_rejection_reason(quote)
            if rejected:
                raise ValueError(rejected)
            return float(quote["close"])
        return None

    def _market_record_rejection_reason(self, record: dict[str, Any]) -> str:
        return self._market_bar_rejection_reason([
            Bar(
                symbol=str(record.get("symbol") or self.symbol),
                timeframe=str(record.get("timeframe") or self.timeframe),
                timestamp=str(record.get("timestamp") or ""),
                open=float(record.get("open") or record.get("close") or 0.0),
                high=float(record.get("high") or record.get("close") or 0.0),
                low=float(record.get("low") or record.get("close") or 0.0),
                close=float(record.get("close") or 0.0),
                volume=float(record.get("volume") or 0.0),
                provider=str(record.get("provider") or ""),
                quality_flags=list(record.get("quality_flags") or []),
            )
        ])

    def _write_runner_state(
        self,
        cycle_id: str,
        event: str,
        detail: dict[str, Any],
        *,
        observed_at: str | datetime | None = None,
    ) -> None:
        path = self.output_root / "dualtrack" / "runner" / f"{cycle_id}.json"
        rows = load_json(path)
        rows.append({"ts": parse_utc(observed_at).isoformat(), "cycle_id": cycle_id, "event": event, "detail": detail})
        write_json(path, rows)

    def _frozen_or_freeze_trend_gate(self, cycle_id: str, *, as_of: str | datetime | None = None) -> bool:
        frozen = self.machine.frozen_trend_gate_armed(cycle_id)
        if frozen is not None:
            return frozen
        return self._freeze_trend_gate(cycle_id, as_of=as_of or cycle_window_from_id(cycle_id).start)

    def _freeze_trend_gate(self, cycle_id: str, *, as_of: str | datetime | None = None) -> bool:
        frozen = self.machine.frozen_trend_gate_armed(cycle_id)
        if frozen is not None:
            return frozen
        armed = self._scoreboard_trend_gate_armed()
        window = cycle_window_from_id(cycle_id)
        path = self.output_root / "dualtrack" / "cycles" / f"{cycle_id}.json"
        rows = load_json(path)
        state = rows[-1] if rows else {
            "cycle_id": cycle_id,
            "kind": cycle_id.rsplit("_", 1)[-1],
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
        }
        state["trend_gate_armed"] = armed
        state["trend_gate_frozen_at"] = parse_utc(as_of or window.start).isoformat()
        state["trend_gate_source"] = "scoreboard_at_cycle_start"
        state.setdefault("layers", ["grid:pending", f"trend:{'armed' if armed else 'standby'}"])
        write_json(path, [state])
        return armed

    def _scoreboard_trend_gate_armed(self) -> bool:
        rows = load_json(self.output_root / "dualtrack" / "scoreboard.json")
        board = rows[-1] if rows else {}
        gate = board.get("trend_leg_gate") if isinstance(board.get("trend_leg_gate"), dict) else {}
        return bool(gate.get("armed", False))

    def _market_session_enabled(self) -> bool:
        return market_session_enabled(self.config)

    def _market_session_status(self, as_of: str | datetime | None) -> dict[str, Any]:
        return market_session_status(as_of, config=self.config)

    def _filter_market_session_bars(self, bars: list[Bar]) -> list[Bar]:
        return filter_bars_for_market_session(bars, self.config)

    def _sync_human_fills_before_close(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any] | None:
        settings = self._human_fill_sync_settings()
        if not bool(settings.get("enabled", False)) or not bool(settings.get("run_before_close", True)):
            return None
        provider = str(settings.get("provider") or "").lower()
        if provider not in {"tiger_openapi", "tiger"}:
            report = {
                "status": "blocked",
                "reason": "unsupported_human_fill_sync_provider",
                "provider": provider,
                "cycle_id": cycle_id,
                "refreshed_order_sync": False,
            }
            self._write_human_fill_sync_runner_report(cycle_id, report)
            return report
        run_date = cycle_id.split("_", 1)[0]
        refresh_report = None
        if bool(settings.get("refresh_order_sync_before_import", False)):
            refresh_report = TigerOpenApiOrderSync(self.output_root).run(run_date)
        import_report = DualTrackTigerHumanSync(self.output_root, config=self.config).run(run_date)
        report = {
            **import_report,
            "cycle_id": cycle_id,
            "refreshed_order_sync": refresh_report is not None,
            "order_sync_refresh": self._human_fill_sync_summary(refresh_report) if refresh_report else None,
        }
        self._write_human_fill_sync_runner_report(cycle_id, report)
        return report

    def _human_fill_sync_blocks_close(self, report: dict[str, Any] | None) -> bool:
        if report is None:
            return False
        if not bool(self._human_fill_sync_settings().get("require_success_before_close", True)):
            return False
        return str(report.get("status") or "") not in {"synced", "partial"}

    def _human_fill_sync_settings(self) -> dict[str, Any]:
        settings = self.config.get("human_fill_sync")
        return settings if isinstance(settings, dict) else {}

    def _write_human_fill_sync_runner_report(self, cycle_id: str, report: dict[str, Any]) -> None:
        self._write_runner_state(cycle_id, "human_fill_sync", self._human_fill_sync_summary(report))

    def _notify_machine_trade_records(self, cycle_id: str) -> dict[str, Any]:
        try:
            from services.dualtrack_feishu import DualTrackTradeRecordNotifier

            return DualTrackTradeRecordNotifier(self.output_root, config=self.config).notify_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001 - notification failure must not stop the paper runner.
            self.store.audit(cycle_id, "machine_trade_notification_failed", {"reason": f"{exc.__class__.__name__}: {exc}"})
            return {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}", "sent": 0, "failed": 1}

    def _human_fill_sync_summary(self, report: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(report, dict):
            return {}
        keys = (
            "status",
            "reason",
            "sync_status",
            "provider",
            "target",
            "source_sync_status",
            "source_filled_order_count",
            "filled_order_count",
            "open_order_count",
            "imported_count",
            "duplicate_count",
            "skipped_count",
            "refreshed_order_sync",
        )
        return {key: report.get(key) for key in keys if key in report}


def _active_plan_bars(plan: dict[str, Any], bars: Sequence[Bar]) -> tuple[Bar, ...]:
    execution_start = plan.get("execution_start")
    if execution_start in (None, ""):
        return tuple(bars)
    start = parse_utc(execution_start)
    return tuple(bar for bar in bars if parse_utc(bar.timestamp) >= start)


def _first_range_touch(plan: dict[str, Any], bars: Sequence[Bar]) -> dict[str, Any] | None:
    plan_range = plan.get("range") if isinstance(plan.get("range"), dict) else {}
    low = plan_range.get("low")
    high = plan_range.get("high")
    if low is None or high is None:
        return None
    low_price = float(low)
    high_price = float(high)
    for bar in _active_plan_bars(plan, bars):
        touched_low = float(bar.low) <= low_price
        touched_high = float(bar.high) >= high_price
        if not touched_low and not touched_high:
            continue
        side = "both" if touched_low and touched_high else "below" if touched_low else "above"
        boundary: float | dict[str, float] = (
            {"low": low_price, "high": high_price}
            if side == "both"
            else low_price if side == "below" else high_price
        )
        return {
            "side": side,
            "boundary": boundary,
            "touched_at": bar.timestamp,
            "bar_low": float(bar.low),
            "bar_high": float(bar.high),
            "bar_close": float(bar.close),
        }
    return None


def _confirmed_range_breach(
    plan: dict[str, Any],
    bars: Sequence[Bar],
    *,
    confirm_closes: int,
) -> dict[str, Any] | None:
    plan_range = plan.get("range") if isinstance(plan.get("range"), dict) else {}
    low = plan_range.get("low")
    high = plan_range.get("high")
    if low is None or high is None:
        return None
    low_price = float(low)
    high_price = float(high)
    rows = _active_plan_bars(plan, bars)
    required = max(2, int(confirm_closes))
    streaks: dict[str, list[Bar]] = {"below": [], "above": []}
    for bar in rows:
        close = float(bar.close)
        streaks["below"] = [*streaks["below"], bar] if close < low_price else []
        streaks["above"] = [*streaks["above"], bar] if close > high_price else []
        for side, boundary in (("below", low_price), ("above", high_price)):
            streak = streaks[side]
            if len(streak) < required:
                continue
            confirmed = streak[-required:]
            return {
                "side": side,
                "boundary": boundary,
                "confirmation_start": confirmed[0].timestamp,
                "confirmed_at": confirmed[-1].timestamp,
                "consecutive_closes": required,
                "closes": [float(item.close) for item in confirmed],
                "bar_low": float(confirmed[-1].low),
                "bar_high": float(confirmed[-1].high),
                "bar_close": float(confirmed[-1].close),
            }
    return None


def _open_machine_positions(fills: Any) -> list[dict[str, Any]]:
    rows = fills if isinstance(fills, list) else []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("event") == "entry"
        and row.get("position_status") == "open"
        and float(row.get("remaining_units") or 0.0) > 0
    ]


def build_parser() -> argparse.ArgumentParser:  # pragma: no cover - thin CLI wrapper
    parser = argparse.ArgumentParser(description="Run the dual-track cycle orchestrator.")
    parser.add_argument("--event", choices=("auto", "pre-cycle", "intraday", "close", "fast-forward-day", "sync-obsidian-plan", "live-tick"), default="auto")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--market-db", default="")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--include-next", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI wrapper
    args = build_parser().parse_args(argv)
    runner = DualTrackCycleRunner(
        output_root=Path(args.output_root) if args.output_root else None,
        market_db=Path(args.market_db) if args.market_db else None,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )
    as_of = args.as_of or None
    if args.event == "auto":
        payload = runner.auto(as_of=as_of)
    elif args.event == "live-tick":
        payload = runner.live_tick(as_of=as_of)
    elif args.event == "sync-obsidian-plan":
        payload = runner.sync_obsidian_human_plans(
            args.cycle_id or None,
            as_of=as_of,
            include_next=bool(args.include_next),
        )
    elif args.event == "fast-forward-day":
        if not args.date:
            raise SystemExit("--date is required for fast-forward-day")
        payload = runner.fast_forward_day(args.date)
    else:
        cycle_id = args.cycle_id or cycle_window(as_of).cycle_id
        if args.event == "pre-cycle":
            payload = runner.pre_cycle(cycle_id, as_of=as_of)
        elif args.event == "intraday":
            payload = runner.intraday_tick(cycle_id, as_of=as_of)
        else:
            payload = runner.close_cycle(cycle_id, as_of=as_of)
    print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
