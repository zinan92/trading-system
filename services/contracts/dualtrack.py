"""Dual-track cycle/plan/order response builders for the dashboard API."""

from __future__ import annotations

from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.contracts.common import _CYCLE_ID_PATTERN, _truthy


def build_dualtrack_plan_response(cycle_id: str, *, output_root: Path | None = None, as_of: str | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackPlanStore(output_root).plan_response(cycle_id, as_of=as_of)


def build_dualtrack_plan_post_response(payload: dict, *, output_root: Path | None = None) -> dict:
    lock = _truthy(payload.get("lock", True))
    now = payload.get("as_of") or payload.get("now")
    plan = DualTrackPlanStore(output_root).save_human_plan(payload, now=now, lock=lock)
    return {"status": "locked" if lock else "draft", "plan": plan}


def build_dualtrack_machine_response(cycle_id: str, *, output_root: Path | None = None, as_of: str | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackMachineRunner(output_root).machine_payload(cycle_id, as_of=as_of)


def build_dualtrack_human_response(
    cycle_id: str,
    *,
    output_root: Path | None = None,
    mark_price: float | str | None = None,
    mark_source: str | None = None,
) -> dict:
    del mark_price, mark_source
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    output = _dualtrack_output_root(output_root)
    return DualTrackHumanEngine(output).human_payload(cycle_id)


def build_dualtrack_attribution_response(cycle_id: str, *, output_root: Path | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackScorer(output_root).attribution_payload(cycle_id)


def build_dualtrack_ledger_response(*, output_root: Path | None = None, week: str | None = None) -> dict:
    scorer = DualTrackScorer(output_root)
    refreshed = scorer.rebuild_ledgers()
    return scorer.ledger_payload(week=week) if week else refreshed


def _dualtrack_output_root(output_root: Path | None = None) -> Path:
    return Path(output_root) if output_root else ROOT / load_pipeline_config().get("output_root", "outputs")


def build_dualtrack_verdict_post_response(payload: dict, *, output_root: Path | None = None) -> dict:
    cycle_id = str(payload.get("cycle_id") or "")
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    verdict = DualTrackScorer(output_root).record_verdict(cycle_id, str(payload.get("note") or ""))
    return {"status": "recorded", "verdict": verdict}
