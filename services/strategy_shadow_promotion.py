"""Read-only promotion evidence gate for comparable Grid Strategy Shadows."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable


SCHEMA = "grid-shadow-promotion-evidence-v1"


def evaluate_grid_shadow_promotion(
    rows: Iterable[dict[str, Any]],
    *,
    minimum_trades: int = 100,
    minimum_periods: int = 2,
) -> dict[str, Any]:
    """Evaluate immutable Shadow evidence without granting trading authority."""

    by_cycle: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if not isinstance(row, dict):
            continue
        cycle_id = str(row.get("cycle_id") or "")
        variant_id = str(row.get("variant_id") or "")
        if cycle_id and variant_id:
            by_cycle[cycle_id][variant_id] = row

    candidates = sorted({variant for values in by_cycle.values() for variant in values if variant != "production"})
    evaluations = [_evaluate_candidate(by_cycle, candidate, minimum_trades, minimum_periods) for candidate in candidates]
    ready = [row for row in evaluations if row["status"] == "proposal_ready"]
    return {
        "schema_version": SCHEMA,
        "status": "proposal_ready" if ready else "collecting_evidence",
        "minimum_trades": minimum_trades,
        "minimum_periods": minimum_periods,
        "auto_promote": False,
        "candidates": evaluations,
        "proposal_candidates": ready,
        "safety": {"read_only": True, "changes_production_plan": False, "submits_orders": False},
    }


def _evaluate_candidate(
    by_cycle: dict[str, dict[str, dict[str, Any]]], candidate: str, minimum_trades: int, minimum_periods: int) -> dict[str, Any]:
    comparable: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    blockers: list[str] = []
    for cycle_id, variants in sorted(by_cycle.items()):
        baseline, challenger = variants.get("production"), variants.get(candidate)
        if challenger is None:
            continue
        if baseline is None:
            blockers.append(f"{cycle_id}:production_baseline_missing")
            continue
        reason = _comparability_failure(baseline, challenger)
        if reason:
            blockers.append(f"{cycle_id}:{reason}")
            continue
        comparable.append((cycle_id, baseline, challenger))
    trade_count = sum(_metric(challenger, "trade_count") for _, _, challenger in comparable)
    periods = len(comparable)
    pnl_delta = sum(_metric(challenger, "realized_pnl") - _metric(baseline, "realized_pnl") for _, baseline, challenger in comparable)
    candidate_drawdown = max((_metric(challenger, "max_drawdown") for _, _, challenger in comparable), default=0.0)
    baseline_drawdown = max((_metric(baseline, "max_drawdown") for _, baseline, _ in comparable), default=0.0)
    candidate_cost = sum(_metric(challenger, "cost") for _, _, challenger in comparable)
    baseline_cost = sum(_metric(baseline, "cost") for _, baseline, _ in comparable)
    if periods < minimum_periods:
        blockers.append("cross_period_persistence_insufficient")
    if trade_count < minimum_trades:
        blockers.append("comparable_trade_sample_insufficient")
    if comparable and pnl_delta <= 0:
        blockers.append("candidate_not_outperforming_baseline")
    if comparable and candidate_drawdown > baseline_drawdown:
        blockers.append("candidate_drawdown_worse_than_baseline")
    if comparable and candidate_cost > baseline_cost:
        blockers.append("candidate_cost_worse_than_baseline")
    evidence = [_evidence_row(cycle_id, baseline, challenger) for cycle_id, baseline, challenger in comparable]
    return {
        "variant_id": candidate,
        "status": "proposal_ready" if not blockers else "not_comparable",
        "blockers": sorted(set(blockers)),
        "comparable_cycle_ids": [cycle for cycle, _, _ in comparable],
        "comparable_period_count": periods,
        "comparable_trade_count": trade_count,
        "metrics": {
            "realized_pnl_delta": round(pnl_delta, 8),
            "candidate_max_drawdown": candidate_drawdown,
            "baseline_max_drawdown": baseline_drawdown,
            "max_drawdown_delta": round(candidate_drawdown - baseline_drawdown, 8),
            "candidate_cost": candidate_cost,
            "baseline_cost": baseline_cost,
            "cost_delta": round(candidate_cost - baseline_cost, 8),
        },
        "evidence_ids": [row["evidence_id"] for row in evidence],
        "evidence": evidence,
        "human_confirmation": {
            "required": True,
            "action": "review_evidence_then_create_separate_plan_change",
            "changes_production_plan": False,
            "submits_orders": False,
        },
    }


def _evidence_row(cycle_id: str, baseline: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    """Project the minimum comparable lineage for an operator proposal."""

    review = challenger.get("review") if isinstance(challenger.get("review"), dict) else {}
    scenario = challenger.get("scenario") if isinstance(challenger.get("scenario"), dict) else {}
    return {
        "cycle_id": cycle_id,
        "evidence_id": str(challenger.get("scenario_id") or challenger.get("input_hash") or ""),
        "evaluation_window": {
            "started_at": review.get("evaluation_started_at"),
            "ended_at": review.get("evaluation_ended_at"),
        },
        "contracts": dict(scenario.get("contracts") or {}),
        "input_lineage": {
            "baseline_market_event_hash": str(
                ((baseline.get("scenario") or {}).get("hashes") or {}).get("market_event_hash")
                or ""
            ),
            "candidate_market_event_hash": str(
                (scenario.get("hashes") or {}).get("market_event_hash") or ""
            ),
        },
        "baseline": {
            "scenario_id": str(baseline.get("scenario_id") or baseline.get("input_hash") or ""),
            "realized_pnl": _metric(baseline, "realized_pnl"),
            "max_drawdown": _metric(baseline, "max_drawdown"),
            "cost": _metric(baseline, "cost"),
        },
        "candidate": {
            "realized_pnl": _metric(challenger, "realized_pnl"),
            "max_drawdown": _metric(challenger, "max_drawdown"),
            "cost": _metric(challenger, "cost"),
        },
    }


def _comparability_failure(baseline: dict[str, Any], challenger: dict[str, Any]) -> str | None:
    if baseline.get("status") != "pass" or challenger.get("status") != "pass":
        return "shadow_execution_not_pass"
    base_review = baseline.get("review") if isinstance(baseline.get("review"), dict) else {}
    candidate_review = challenger.get("review") if isinstance(challenger.get("review"), dict) else {}
    if (base_review.get("evaluation_started_at"), base_review.get("evaluation_ended_at")) != (candidate_review.get("evaluation_started_at"), candidate_review.get("evaluation_ended_at")):
        return "evaluation_window_mismatch"
    base_contracts = (baseline.get("scenario") or {}).get("contracts") or {}
    candidate_contracts = (challenger.get("scenario") or {}).get("contracts") or {}
    if base_contracts != candidate_contracts:
        return "execution_or_fee_contract_mismatch"
    base_hashes = (baseline.get("scenario") or {}).get("hashes") or {}
    candidate_hashes = (challenger.get("scenario") or {}).get("hashes") or {}
    base_market_input = str(base_hashes.get("market_event_hash") or "")
    candidate_market_input = str(candidate_hashes.get("market_event_hash") or "")
    if not base_market_input or not candidate_market_input:
        return "market_input_hash_missing"
    if base_market_input != candidate_market_input:
        return "market_input_hash_mismatch"
    return None


def _metric(row: dict[str, Any], key: str) -> float:
    value = ((row.get("metrics") or {}).get(key))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0
