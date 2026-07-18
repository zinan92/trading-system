from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore, validate_machine_plan
from services.journal_store import load_json, write_json
from services.strategy_proposal_port import (
    MAX_NEWSLETTER_CHARS,
    StrategyProposalRequest,
    StrategyProposalRuntime,
)


DEFAULT_NEWSLETTER_ROOT = Path("/Users/wendy/park-io/007_finance daily newsletter")


class DualTrackMachinePlanner:
    """Build one independent, locked machine decision for each trading cycle."""

    def __init__(
        self,
        output_root: Path | None = None,
        *,
        config: dict[str, Any] | None = None,
        newsletter_root: Path | None = None,
        decision_provider: Callable[[str], dict[str, Any]] | None = None,
        proposal_runtime: StrategyProposalRuntime | None = None,
    ) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.config = config or dualtrack_config()
        planner_config = self.config.get("machine_planner") if isinstance(self.config.get("machine_planner"), dict) else {}
        configured_root = planner_config.get("newsletter_root")
        self.newsletter_root = Path(newsletter_root or configured_root or DEFAULT_NEWSLETTER_ROOT)
        if decision_provider is not None and proposal_runtime is not None:
            raise ValueError("decision_provider cannot be combined with proposal_runtime")
        if proposal_runtime is None:
            # Compatibility-only composition for direct planner construction.
            # The production cycle runner supplies an already frozen runtime.
            from services.strategy_proposal_composition import compose_strategy_proposal

            proposal_runtime = compose_strategy_proposal(
                self.config,
                decision_provider=decision_provider,
            )
        self.proposal = proposal_runtime
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def ensure_plan(
        self,
        cycle_id: str,
        *,
        bars: Iterable[Bar],
        prev_cycle_range: float,
        volatility_context: dict[str, Any] | None = None,
        as_of: str | datetime | None = None,
        force: bool = False,
        execution_start: str | datetime | None = None,
        revision_reason: str = "",
        replan_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.store.machine_plan(cycle_id)
        if existing and not existing.get("degraded") and not force:
            return existing
        rows = tuple(bars)
        if not rows:
            raise ValueError("trusted market bars are required for machine planning")
        now = parse_utc(as_of)
        newsletter_path = self._newsletter_path(cycle_id)
        newsletter_text = ""
        source: dict[str, str] | None = None
        if newsletter_path.exists():
            newsletter_text = newsletter_path.read_text(encoding="utf-8")
            source = {
                "kind": "newsletter",
                "path": str(newsletter_path),
                "title": f"{cycle_id.split('_', 1)[0]} finance daily newsletter 黄金部分",
            }
        previous_review = self._previous_review_context(cycle_id)
        window = cycle_window_from_id(cycle_id)
        request = StrategyProposalRequest(
            cycle_id=cycle_id,
            cycle_hours=int((window.end - window.start).total_seconds() // 3600),
            market=_market_summary(rows),
            prev_cycle_range=float(prev_cycle_range),
            volatility_context=volatility_context or {},
            previous_review=previous_review,
            replan_context=replan_context or {},
            newsletter_text=newsletter_text[:MAX_NEWSLETTER_CHARS],
        )
        requires_newsletter = "newsletter" in self.proposal.descriptor.required_context
        error = ""
        try:
            if requires_newsletter and not newsletter_text:
                raise RuntimeError(f"newsletter_missing:{newsletter_path}")
            decision = self.proposal.port.propose(request)
            if not isinstance(decision, dict):
                raise ValueError("machine decision provider must return a JSON object")
            decision = _apply_range_floor(decision, volatility_context or {})
            candidate = {
                **decision,
                "cycle_id": cycle_id,
                "author": "ai",
                "status": "locked",
                "locked_at": now.isoformat(),
                "source": self.proposal.descriptor.plan_source,
                "decision_mode": str(
                    decision.get("decision_mode") or self.proposal.descriptor.default_decision_mode
                ),
            }
            if volatility_context:
                candidate["planning_context"] = volatility_context
            if previous_review.get("status") == "available":
                if not str(decision.get("review_adjustment") or "").strip():
                    raise ValueError("machine decision must explain how the previous review changes this cycle")
                candidate["previous_review_cycle_id"] = previous_review["cycle_id"]
                expected_change = previous_review.get("next_iteration") or {}
                if expected_change.get("change_id") and expected_change.get("status") in {"proposed", "collecting"}:
                    candidate["review_change"] = _review_change_for_next_cycle(decision, expected_change)
            if execution_start is not None:
                candidate["execution_start"] = parse_utc(execution_start).isoformat()
            if revision_reason:
                candidate["revision_reason"] = revision_reason
            if replan_context:
                candidate["replan_context"] = dict(replan_context)
            if existing and existing.get("locked_at"):
                candidate["replaces_locked_at"] = existing["locked_at"]
            sources = list(decision.get("sources") or [])
            if (
                source
                and requires_newsletter
                and not any(isinstance(item, dict) and item.get("path") == source["path"] for item in sources)
            ):
                sources.insert(0, source)
            candidate["sources"] = sources
            normalized = validate_machine_plan(candidate, now=now)
        except Exception as exc:  # noqa: BLE001 - planning failure is persisted and fails closed.
            error = f"{exc.__class__.__name__}: {exc}"
            if existing and force:
                normalized = existing
            else:
                error_plan = self._decision_error_plan(
                    cycle_id,
                    rows,
                    error=error,
                    source=source if requires_newsletter else None,
                    now=now,
                )
                if volatility_context:
                    error_plan["planning_context"] = volatility_context
                normalized = validate_machine_plan(error_plan, now=now)
        saved = existing if error and existing and force else self.store.save_ai_plan(normalized, now=now)
        if not error and existing and force:
            self.store.record_ai_plan_revision(
                existing,
                saved,
                now=now,
                reason=revision_reason or "forced_machine_plan_revision",
                context=replan_context,
            )
        trace = {
            "cycle_id": cycle_id,
            "planned_at": now.isoformat(),
            "status": "revision_failed_preserved" if error and existing and force else "decision_error" if error else "planned",
            "planning_error": error,
            "newsletter_path": str(newsletter_path),
            "previous_review": previous_review,
            "market": _market_summary(rows),
            "volatility_context": volatility_context or {},
            "replan_context": replan_context or {},
            "proposal_plugin": self.proposal.audit_dict(),
            "plan": saved,
        }
        trace_path = self.output_root / "dualtrack" / "planning" / f"{cycle_id}_machine.json"
        traces = load_json(trace_path)
        traces.append(trace)
        write_json(trace_path, traces)
        if error and existing and force:
            event = "machine_plan_revision_failed_preserved"
        elif error:
            event = "machine_plan_decision_error"
        elif existing and force:
            event = "machine_plan_relocked"
        else:
            event = "machine_plan_locked"
        self.store.audit(cycle_id, event, {
            "source": saved.get("source"),
            "direction": saved.get("direction"),
            "error": error,
            "revision_reason": revision_reason,
            "execution_start": saved.get("execution_start"),
            "previous_review_cycle_id": saved.get("previous_review_cycle_id"),
            "review_adjustment": saved.get("review_adjustment"),
            "review_change": saved.get("review_change"),
            "range_adjustment": saved.get("range_adjustment"),
            "replan_context": saved.get("replan_context"),
            "proposal_plugin": self.proposal.descriptor.name,
            "proposal_registry_fingerprint": self.proposal.registry_fingerprint,
        })
        return {**saved, "effective_author": "ai"}

    def repair_legacy_neutral_plan(
        self,
        cycle_id: str,
        *,
        bars: Iterable[Bar],
        prev_cycle_range: float,
        as_of: str | datetime,
    ) -> dict[str, Any]:
        existing = self.store.machine_plan(cycle_id)
        if not existing or existing.get("direction") != "neutral" or existing.get("grid_orders"):
            raise ValueError("repair requires a legacy neutral plan with no grid orders")
        fills = load_json(self.output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
        if fills:
            raise ValueError("cannot revise a machine plan after fills exist")
        return self.ensure_plan(
            cycle_id,
            bars=bars,
            prev_cycle_range=prev_cycle_range,
            as_of=as_of,
            force=True,
            execution_start=as_of,
            revision_reason="neutral_bilateral_grid_contract_fix",
        )

    def _newsletter_path(self, cycle_id: str) -> Path:
        run_date = cycle_id.split("_", 1)[0]
        return self.newsletter_root / f"{run_date}-finance-daily-newsletter.md"

    def _previous_review_context(self, cycle_id: str) -> dict[str, Any]:
        current = cycle_window_from_id(cycle_id)
        previous_cycle_id = cycle_window(current.start - timedelta(minutes=1)).cycle_id
        reviews = DualTrackScorer(self.output_root, config=self.config).ledger_payload().get("recent_reviews") or []
        snapshot = next((row for row in reviews if row.get("cycle_id") == previous_cycle_id), None)
        if not snapshot:
            return {"status": "missing", "cycle_id": previous_cycle_id}
        review = snapshot.get("machine_review") or {}
        return {
            "status": "available",
            "cycle_id": previous_cycle_id,
            "direction": {
                "decision": review.get("decision"),
                "realized_direction": review.get("realized_regime", review.get("realized_direction")),
                "hit": (snapshot.get("plan_grades") or {}).get("ai", {}).get("hit"),
            },
            "market": review.get("market_review") or {},
            "direction_review": review.get("direction_review") or {},
            "range_review": review.get("range_review") or {},
            "key_levels": (review.get("key_level_review") or {}).get("summary"),
            "signal": (review.get("signal_review") or {}).get("summary"),
            "tpsl": (review.get("tpsl_review") or {}).get("summary"),
            "evidence": review.get("evidence") or {},
            "next_iteration": review.get("next_iteration") or {},
            "recorded_realized_pnl": review.get("recorded_realized_pnl", review.get("realized_pnl")),
            "conclusion": review.get("summary"),
        }

    def _decision_error_plan(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        *,
        error: str,
        source: dict[str, str] | None,
        now: datetime,
    ) -> dict[str, Any]:
        market = _market_summary(bars)
        low = float(market["low"])
        high = float(market["high"])
        if low >= high:
            close = float(market["close"])
            width = max(abs(close) * 0.001, 0.01)
            low, high = close - width, close + width
        return {
            "cycle_id": cycle_id,
            "author": "ai",
            "direction": "neutral",
            "range": {"low": low, "high": high},
            "key_levels": [low, high],
            "grid_orders": [],
            "invalidation": [],
            "confidence": 1,
            "rationale": "机器决策未成功，本周期不下单；错误已公开记录，仍在收盘后复盘行情。",
            "sources": [source] if source else [],
            "decision_mode": "decision_error",
            "planning_error": error,
            "degraded": True,
            "source": "machine_ai_decision_error",
            "status": "locked",
            "locked_at": now.isoformat(),
        }

def _review_change_for_next_cycle(decision: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    change = decision.get("review_change")
    if not isinstance(change, dict):
        raise ValueError("previous review requires one structured paper challenger change")
    for key in ("change_id", "mode", "dimension", "expected_metric"):
        if str(change.get(key) or "").strip() != str(expected.get(key) or "").strip():
            raise ValueError(f"review_change.{key} must match the previous review proposal")
    if not str(change.get("summary") or "").strip():
        raise ValueError("review_change.summary is required")
    return dict(change)


def _market_summary(bars: tuple[Bar, ...]) -> dict[str, Any]:
    return {
        "symbol": bars[-1].symbol,
        "timeframe": bars[-1].timeframe,
        "provider": bars[-1].provider,
        "bar_count": len(bars),
        "start": bars[0].timestamp,
        "end": bars[-1].timestamp,
        "open": float(bars[0].open),
        "high": max(float(bar.high) for bar in bars),
        "low": min(float(bar.low) for bar in bars),
        "close": float(bars[-1].close),
        "recent_closes": [float(bar.close) for bar in bars[-60:]],
    }


def _apply_range_floor(decision: dict[str, Any], volatility_context: dict[str, Any]) -> dict[str, Any]:
    """Widen an undersized AI range transparently without moving its grid entries."""
    adjusted = deepcopy(decision)
    minimum = volatility_context.get("minimum_plan_range")
    plan_range = adjusted.get("range") if isinstance(adjusted.get("range"), dict) else {}
    try:
        low = float(plan_range["low"])
        high = float(plan_range["high"])
        minimum_width = float(minimum)
    except (KeyError, TypeError, ValueError):
        return adjusted
    width = high - low
    if width <= 0 or minimum_width <= 0 or width >= minimum_width:
        return adjusted
    center = (low + high) / 2.0
    new_low = round(center - minimum_width / 2.0, 4)
    new_high = round(center + minimum_width / 2.0, 4)
    adjusted["range"] = {"low": new_low, "high": new_high}
    invalidation = []
    for row in adjusted.get("invalidation") or []:
        item = dict(row)
        if item.get("side") == "below":
            item["price"] = new_low
        elif item.get("side") == "above":
            item["price"] = new_high
        invalidation.append(item)
    adjusted["invalidation"] = invalidation
    levels = []
    for value in adjusted.get("key_levels") or []:
        number = float(value)
        if abs(number - low) <= 1e-8:
            number = new_low
        elif abs(number - high) <= 1e-8:
            number = new_high
        levels.append(number)
    adjusted["key_levels"] = levels
    adjusted["range_adjustment"] = {
        "applied": True,
        "reason": f"below_{str(volatility_context.get('method') or 'cycle_range').removeprefix('median_completed_')}",
        "original_range": {"low": low, "high": high, "width": round(width, 8)},
        "adjusted_range": {"low": new_low, "high": new_high, "width": round(new_high - new_low, 8)},
        "reference_method": volatility_context.get("method"),
        "reference_range": volatility_context.get("reference_range"),
        "excluded_weekend_sample_count": sum(
            1 for sample in volatility_context.get("excluded_samples") or [] if sample.get("reason") == "weekend_low_liquidity"
        ),
    }
    return adjusted
