"""Deterministic Paper-continuity recovery candidates.

The helpers in this module never submit orders.  They rebuild one Grid
candidate from a previously selected strategy intent, current trusted market
facts, and the authoritative Paper account.  The normal control-plane preview,
outer-policy comparison, prepare, and start gates remain responsible for
authorization and execution.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from services.dca_plan import (
    build_dca_preview,
    build_deterministic_dca_candidate_payload_v1,
    dca_preview_id,
)
from services.dualtrack_clock import cycle_window, cycle_window_from_id
from services.grid_sizing import (
    build_grid_preview,
    preview_id as grid_preview_id,
)


PAPER_CONTINUITY_PROPOSAL_SOURCE = "paper_continuity_recovery"
_AI_PLAN_FIELDS = frozenset(
    {
        "direction",
        "style",
        "range",
        "key_levels",
        "grid",
        "signal",
        "tp_sl",
        "risk_budget",
        "intraday_rules",
    }
)


class PaperContinuityRecoveryError(ValueError):
    """Typed, safe machine code for a rejected Paper recovery lineage."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def authoritative_paper_equity(snapshot: Mapping[str, Any]) -> float:
    """Return the first positive finite authoritative Paper account value."""

    account = snapshot.get("account")
    account = account if isinstance(account, Mapping) else {}
    for field in ("equity", "ending_cash", "starting_cash"):
        try:
            value = float(account.get(field))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    raise ValueError("authoritative_execution_account_missing")


def load_immediate_previous_verified_plan(
    output_root: Path,
    cycle_id: str,
) -> dict[str, Any]:
    """Load only the exact prior cycle's fully verified terminal package."""

    # Local import avoids a module cycle: the package builder itself reads the
    # StrategyControlPlane when sealing a terminal cycle.
    from services.strategy_cycle_package import (
        load_latest_verified_cycle_package,
    )

    current = cycle_window_from_id(cycle_id)
    previous_cycle_id = cycle_window(
        current.start - timedelta(seconds=1)
    ).cycle_id
    path = (
        Path(output_root)
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{previous_cycle_id}.json"
    )
    try:
        package = load_latest_verified_cycle_package(path)
    except ValueError as exc:
        raise ValueError("verified_prior_cycle_plan_missing") from exc
    plan = package.get("strategy_plan")
    if (
        package.get("cycle_id") != previous_cycle_id
        or not isinstance(plan, Mapping)
        or str(plan.get("cycle_id") or "") != previous_cycle_id
        or not str(plan.get("strategy_plan_id") or "")
    ):
        raise ValueError("verified_prior_cycle_plan_missing")
    proposals = [
        dict(row)
        for row in package.get("proposals") or []
        if isinstance(row, Mapping)
    ]
    source_proposal = verified_ai_source_proposal(
        plan,
        proposals,
        output_root=Path(output_root),
        package_cycle_id=previous_cycle_id,
        paper_continuity_allow_confirmed_fields=True,
    )
    return {
        "plan": dict(plan),
        "source_proposal": dict(source_proposal),
        "provenance": {
            "source_kind": "verified_immediate_previous_cycle_package",
            "source_cycle_id": previous_cycle_id,
            "source_strategy_plan_id": str(plan["strategy_plan_id"]),
            "source_strategy_plan_version": int(plan.get("version") or 0),
            "source_package_hash": str(package.get("package_hash") or ""),
        },
    }


def verified_ai_source_proposal(
    plan: Mapping[str, Any],
    proposals: list[Mapping[str, Any]],
    *,
    output_root: Path | None = None,
    package_cycle_id: str | None = None,
    paper_continuity_allow_confirmed_fields: bool = False,
) -> dict[str, Any]:
    """Prove one exact AI intent or its append-only recovery lineage.

    A recovery proposal must never be labelled as AI merely because the
    current provider is unavailable.  The intent is inherited only from one
    exact persisted AI proposal, or from one exact Paper-continuity proposal
    whose immutable analysis proves the preceding AI lineage and explicitly
    records that no new AI judgment occurred.
    """

    result = _selected_source_proposal(plan, proposals)
    strategy_type = str(plan.get("strategy_type") or "grid").lower()
    field_sources = plan.get("field_sources")
    source = str(result.get("source") or "")
    if strategy_type == "grid":
        accepted_field_sources = {source}
        if paper_continuity_allow_confirmed_fields:
            # ``confirmed`` is the control plane's immutable marker for fields
            # rebuilt from a trusted preview (direction/range/grid/tp-sl/risk)
            # after the selected proposal was locked.  It does not replace the
            # exact proposal/package/root-AI lineage checks below.  This opt-in
            # is used only by the Paper-continuity inheritance path; callers
            # remain fail closed by default.
            accepted_field_sources.add("confirmed")
        if (
            not isinstance(field_sources, Mapping)
            or set(field_sources) != _AI_PLAN_FIELDS
            or any(
                str(value) not in accepted_field_sources
                for value in field_sources.values()
            )
        ):
            raise PaperContinuityRecoveryError(
                "verified_ai_strategy_intent_missing"
            )
    root = _verify_proposal_lineage(
        result,
        proposals=[dict(row) for row in proposals],
        output_root=Path(output_root) if output_root is not None else None,
        package_cycle_id=(
            str(package_cycle_id)
            if package_cycle_id is not None
            else str(result.get("cycle_id") or "")
        ),
        visited=set(),
    )
    verified = dict(result)
    verified["source_proposal_digest"] = _digest(result)
    verified["root_ai_source_proposal_id"] = root["proposal_id"]
    verified["root_ai_source_proposal_digest"] = root["proposal_digest"]
    return verified


def paper_continuity_proposal_digest(
    proposal: Mapping[str, Any],
) -> str:
    """Return the immutable digest written beside one recovery proposal."""

    payload = dict(proposal)
    payload.pop("recovery_proposal_digest", None)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (
        "paper-continuity-proposal-"
        f"{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
    )


def _selected_source_proposal(
    plan: Mapping[str, Any],
    proposals: list[Mapping[str, Any]],
) -> dict[str, Any]:
    source_ids = [
        str(value)
        for value in plan.get("source_proposal_ids") or []
        if str(value)
    ]
    strategy_type = str(plan.get("strategy_type") or "grid").lower()
    if len(source_ids) > 1:
        raise ValueError("verified_ai_strategy_intent_missing")
    if source_ids:
        matches = [
            dict(row)
            for row in proposals
            if str(row.get("proposal_id") or "") == source_ids[0]
        ]
    elif strategy_type == "dca":
        # Historical DCA plans predate explicit source_proposal_ids.  Their
        # immutable preview identity still proves the one proposal used to
        # build the plan; ambiguity remains fail-closed.
        matches = [
            dict(row)
            for row in proposals
            if str(row.get("strategy_type") or "").lower() == "dca"
            and str(row.get("preview_id") or "")
            == str(plan.get("preview_id") or "")
        ]
    else:
        matches = []
    if len(matches) != 1:
        raise ValueError("verified_ai_strategy_intent_missing")
    return matches[0]


def _verify_proposal_lineage(
    proposal: Mapping[str, Any],
    *,
    proposals: list[Mapping[str, Any]],
    output_root: Path | None,
    package_cycle_id: str,
    visited: set[tuple[str, str, str]],
) -> dict[str, str]:
    result = dict(proposal)
    proposal_id = str(result.get("proposal_id") or "")
    proposal_digest = _digest(result)
    visit = (str(package_cycle_id), proposal_id, proposal_digest)
    if not proposal_id or visit in visited:
        raise ValueError("verified_ai_strategy_intent_missing")
    visited.add(visit)
    source = str(result.get("source") or "")
    if source == "ai":
        return {
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
        }
    if source != PAPER_CONTINUITY_PROPOSAL_SOURCE:
        raise ValueError("verified_ai_strategy_intent_missing")
    analysis = dict(result.get("analysis") or {})
    recovery = dict(analysis.get("paper_continuity_recovery") or {})
    inherited_id = str(
        analysis.get("inherited_source_proposal_id") or ""
    )
    inherited_digest = str(
        analysis.get("inherited_source_proposal_digest") or ""
    )
    supplied_recovery_digest = str(
        result.get("recovery_proposal_digest") or ""
    )
    if (
        analysis.get("new_ai_judgment") is not False
        or analysis.get("inherited_intent_source") != "ai"
        or not inherited_id
        or not inherited_digest
        or not supplied_recovery_digest
        or supplied_recovery_digest
        != paper_continuity_proposal_digest(result)
    ):
        raise ValueError("verified_ai_strategy_intent_missing")

    source_kind = str(recovery.get("source_kind") or "")
    if source_kind == "current_cycle_active_plan":
        source_cycle_id = str(recovery.get("source_cycle_id") or "")
        if source_cycle_id != str(package_cycle_id):
            raise ValueError("verified_ai_strategy_intent_missing")
        parent_proposals = proposals
        parent_package_cycle_id = source_cycle_id
    elif source_kind == "verified_immediate_previous_cycle_package":
        if output_root is None:
            raise ValueError("verified_ai_strategy_intent_missing")
        source_cycle_id = str(recovery.get("source_cycle_id") or "")
        source_package_hash = str(
            recovery.get("source_package_hash") or ""
        )
        if not source_cycle_id or not source_package_hash:
            raise ValueError("verified_ai_strategy_intent_missing")
        try:
            expected_source_cycle_id = cycle_window(
                cycle_window_from_id(package_cycle_id).start
                - timedelta(seconds=1)
            ).cycle_id
        except ValueError as exc:
            raise ValueError(
                "verified_ai_strategy_intent_missing"
            ) from exc
        if source_cycle_id != expected_source_cycle_id:
            raise ValueError("verified_ai_strategy_intent_missing")
        from services.strategy_cycle_package import (
            load_verified_cycle_package_revision,
        )

        path = (
            output_root
            / "dualtrack"
            / "strategy_cycle_packages"
            / f"{source_cycle_id}.json"
        )
        try:
            parent_package = load_verified_cycle_package_revision(
                path,
                source_package_hash,
            )
        except ValueError as exc:
            raise ValueError(
                "verified_ai_strategy_intent_missing"
            ) from exc
        parent_plan = dict(parent_package.get("strategy_plan") or {})
        if (
            parent_package.get("cycle_id") != source_cycle_id
            or str(parent_plan.get("strategy_plan_id") or "")
            != str(recovery.get("source_strategy_plan_id") or "")
            or int(parent_plan.get("version") or 0)
            != int(recovery.get("source_strategy_plan_version") or 0)
        ):
            raise ValueError("verified_ai_strategy_intent_missing")
        parent_proposals = [
            dict(row)
            for row in parent_package.get("proposals") or []
            if isinstance(row, Mapping)
        ]
        selected_parent = _selected_source_proposal(
            parent_plan,
            parent_proposals,
        )
        if str(selected_parent.get("proposal_id") or "") != inherited_id:
            raise ValueError("verified_ai_strategy_intent_missing")
        parent_package_cycle_id = source_cycle_id
    else:
        raise ValueError("verified_ai_strategy_intent_missing")

    matches = [
        dict(row)
        for row in parent_proposals
        if str(row.get("proposal_id") or "") == inherited_id
        and _digest(dict(row)) == inherited_digest
    ]
    if len(matches) != 1:
        raise ValueError("verified_ai_strategy_intent_missing")
    root = _verify_proposal_lineage(
        matches[0],
        proposals=parent_proposals,
        output_root=output_root,
        package_cycle_id=parent_package_cycle_id,
        visited=visited,
    )
    if (
        str(analysis.get("root_ai_source_proposal_id") or "")
        != root["proposal_id"]
        or str(analysis.get("root_ai_source_proposal_digest") or "")
        != root["proposal_digest"]
    ):
        raise ValueError("verified_ai_strategy_intent_missing")
    return root


def build_recovery_candidate(
    **kwargs: Any,
) -> dict[str, Any]:
    """Dispatch to the prior plan's exact strategy family."""

    source_plan = kwargs.get("source_plan")
    strategy_type = str(
        dict(source_plan or {}).get("strategy_type") or "grid"
    ).lower()
    if strategy_type == "grid":
        return build_grid_recovery_candidate(**kwargs)
    if strategy_type == "dca":
        return build_dca_recovery_candidate(**kwargs)
    raise ValueError("paper_continuity_recovery_strategy_unsupported")


def build_grid_recovery_candidate(
    *,
    cycle_id: str,
    source_plan: Mapping[str, Any],
    source_proposal: Mapping[str, Any] | None,
    provenance: Mapping[str, Any],
    market: Mapping[str, Any],
    authoritative_equity: float,
    config: Mapping[str, Any],
    outer_policy: Mapping[str, Any],
    supervisor_attempt_id: str,
    provider_readiness: Mapping[str, Any] | None,
    degradation_event_refs: list[Mapping[str, Any]] | None = None,
    start_facts_digest: str | None = None,
) -> dict[str, Any]:
    """Recenter and resize a Grid candidate inside the exact Park boundary."""

    if str(source_plan.get("strategy_type") or "grid").lower() != "grid":
        raise ValueError("paper_continuity_recovery_strategy_unsupported")
    direction = str(source_plan.get("direction") or "neutral").lower()
    style = str(source_plan.get("style") or "steady").lower()
    source_grid = (
        dict(source_plan.get("grid") or {})
        if isinstance(source_plan.get("grid"), Mapping)
        else {}
    )
    limits = (
        dict(outer_policy.get("limits") or {})
        if isinstance(outer_policy.get("limits"), Mapping)
        else {}
    )
    count = _bounded_grid_count(source_grid.get("count"), limits)
    request: dict[str, Any] = {
        "direction": direction,
        "style": style,
        "strategy_type": "grid",
        "grid": {
            "count": count,
            "mode": str(source_grid.get("mode") or "arithmetic"),
            "out_of_range": str(
                source_grid.get("out_of_range") or "exit_only"
            ),
            "notional_mode": "auto",
        },
    }
    account = {
        "equity": authoritative_equity,
        "ending_cash": authoritative_equity,
        "starting_cash": authoritative_equity,
        "execution_account_source": "authoritative_execution_snapshot",
    }
    preview = build_grid_preview(
        cycle_id,
        request,
        market=dict(market),
        account=account,
        config=dict(config),
    )

    cap = _notional_cap(
        preview,
        limits=limits,
        equity=authoritative_equity,
    )
    current_notional = float(
        dict(preview.get("grid") or {}).get("notional_per_grid") or 0
    )
    repriced = cap + 1e-9 < current_notional
    if repriced:
        request["grid"] = {
            **dict(request["grid"]),
            "notional_mode": "manual",
            "notional_per_grid": cap,
        }
        preview = build_grid_preview(
            cycle_id,
            request,
            market=dict(market),
            account=account,
            config=dict(config),
            # Paper continuity may sacrifice the per-grid profit objective, but
            # the venue precision, capital, leverage, market and Park boundary
            # checks still run below and again in the control plane.
            allow_unsafe_manual_preview=True,
        )
    # Each convergence attempt owns a distinct candidate identity even when
    # market and equity facts are byte-for-byte unchanged.
    if start_facts_digest is not None:
        preview["start_facts_digest"] = str(start_facts_digest)
    preview["supervisor_preview_nonce"] = str(supervisor_attempt_id)
    preview["preview_id"] = grid_preview_id(preview)

    source = dict(source_proposal or {})
    recovery = {
        **dict(provenance),
        "supervisor_attempt_id": str(supervisor_attempt_id),
        "market_recentered": True,
        "authoritative_equity": authoritative_equity,
        "equity_source": "authoritative_execution_snapshot",
        "risk_repriced": repriced,
        "original_notional_per_grid": current_notional,
        "selected_notional_per_grid": float(
            dict(preview.get("grid") or {}).get("notional_per_grid") or 0
        ),
        "degradation_event_refs": [
            dict(row) for row in degradation_event_refs or []
        ],
    }
    proposal_seed = {
        "cycle_id": cycle_id,
        "attempt_id": supervisor_attempt_id,
        "preview_id": preview.get("preview_id"),
        "source_plan_id": source_plan.get("strategy_plan_id"),
    }
    proposal_id = "proposal-paper-continuity-" + hashlib.sha256(
        json.dumps(
            proposal_seed,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    proposal = {
        "proposal_id": proposal_id,
        "cycle_id": cycle_id,
        # The strategy intent is inherited from an AI-sourced plan; the
        # analysis below makes explicit that no new AI judgment occurred.
        "source": PAPER_CONTINUITY_PROPOSAL_SOURCE,
        "direction": direction,
        "style": style,
        "strategy_type": "grid",
        "range": dict(preview.get("range") or {}),
        "key_levels": [
            dict(preview.get("range") or {}).get("low"),
            dict(preview.get("range") or {}).get("high"),
        ],
        "grid": {
            **dict(preview.get("grid") or {}),
            "orders": [
                dict(row)
                for row in preview.get("orders") or []
                if isinstance(row, Mapping)
            ],
        },
        "signal": dict(source.get("signal") or source_plan.get("signal") or {}),
        "tp_sl": dict(source.get("tp_sl") or source_plan.get("tp_sl") or {}),
        "risk_budget": dict(preview.get("risk") or {}),
        "intraday_rules": list(
            source.get("intraday_rules")
            or source_plan.get("intraday_rules")
            or []
        ),
        "rationale": (
            "Paper continuity fallback: preserve the previously selected "
            "strategy intent while rebuilding executable geometry from current "
            "trusted market and authoritative Paper equity."
        ),
        "evidence_used": list(source.get("evidence_used") or []),
        "analysis": {
            **dict(source.get("analysis") or {}),
            "paper_continuity_recovery": recovery,
            "new_ai_judgment": False,
            "inherited_intent_source": "ai",
            "inherited_source_proposal_id": source.get("proposal_id"),
            "inherited_source_proposal_digest": source.get(
                "source_proposal_digest"
            ),
            "root_ai_source_proposal_id": source.get(
                "root_ai_source_proposal_id"
            ),
            "root_ai_source_proposal_digest": source.get(
                "root_ai_source_proposal_digest"
            ),
        },
        "prompt_contract": dict(source.get("prompt_contract") or {}),
        "evaluation_receipt": dict(source.get("evaluation_receipt") or {}),
        "preview_id": preview.get("preview_id"),
        "start_facts_digest": start_facts_digest,
    }
    if provider_readiness is not None:
        proposal["provider_readiness"] = dict(provider_readiness)
    return {
        "proposal": proposal,
        "preview": preview,
        "request": request,
        "recovery": recovery,
    }


def build_dca_recovery_candidate(
    *,
    cycle_id: str,
    source_plan: Mapping[str, Any],
    source_proposal: Mapping[str, Any] | None,
    provenance: Mapping[str, Any],
    market: Mapping[str, Any],
    authoritative_equity: float,
    config: Mapping[str, Any],
    outer_policy: Mapping[str, Any],
    supervisor_attempt_id: str,
    provider_readiness: Mapping[str, Any] | None,
    degradation_event_refs: list[Mapping[str, Any]] | None = None,
    start_facts_digest: str | None = None,
) -> dict[str, Any]:
    """Rebuild one inherited DCA intent around the current trusted mark."""

    if str(source_plan.get("strategy_type") or "").lower() != "dca":
        raise ValueError("paper_continuity_recovery_strategy_unsupported")
    if str(outer_policy.get("strategy_type") or "").lower() != "dca":
        raise ValueError("outer_strategy_policy_envelope_out_of_bounds")
    direction = str(source_plan.get("direction") or "").lower()
    market_price = _bounded_float(market.get("latest_close"))
    if market_price is None:
        raise ValueError("trusted_market_temporarily_unavailable")
    payload = build_deterministic_dca_candidate_payload_v1(
        direction=direction,
        market_price=market_price,
    )
    limits = dict(outer_policy.get("limits") or {})
    source_dca = dict(source_plan.get("dca") or {})
    source_risk = dict(source_plan.get("risk_budget") or {})
    dca = dict(payload["dca"])
    levels = list(dca["entry_levels"])
    source_additions = source_dca.get("max_additions") or len(
        source_dca.get("entries") or []
    )
    additions = _bounded_additions(
        source_additions,
        limits=limits,
        available=len(levels),
    )
    dca["entry_levels"] = levels[:additions]
    dca["max_additions"] = additions
    source_notional = _bounded_float(
        source_dca.get("notional_per_addition")
    )
    if source_notional is not None:
        dca["notional_per_addition"] = source_notional
    payload["dca"] = dca
    source_leverage = _bounded_float(
        source_risk.get("leverage")
        or source_risk.get("selected_leverage")
    )
    if source_leverage is not None:
        payload["risk_budget"] = {"leverage": source_leverage}
    account = {
        "equity": authoritative_equity,
        "ending_cash": authoritative_equity,
        "starting_cash": authoritative_equity,
        "execution_account_source": "authoritative_execution_snapshot",
    }
    preview = build_dca_preview(
        cycle_id,
        payload,
        market=dict(market),
        account=account,
        config=dict(config),
    )
    current_notional = float(preview["dca"]["notional_per_addition"])
    cap = _dca_notional_cap(
        preview,
        limits=limits,
        equity=authoritative_equity,
    )
    repriced = cap + 1e-9 < current_notional
    if repriced:
        payload["dca"] = {
            **dict(payload["dca"]),
            "notional_per_addition": cap,
        }
        preview = build_dca_preview(
            cycle_id,
            payload,
            market=dict(market),
            account=account,
            config=dict(config),
        )
    if start_facts_digest is not None:
        preview["start_facts_digest"] = str(start_facts_digest)
    preview["supervisor_preview_nonce"] = str(supervisor_attempt_id)
    preview["preview_id"] = dca_preview_id(preview)
    source = dict(source_proposal or {})
    recovery = {
        **dict(provenance),
        "supervisor_attempt_id": str(supervisor_attempt_id),
        "market_recentered": True,
        "authoritative_equity": authoritative_equity,
        "equity_source": "authoritative_execution_snapshot",
        "risk_repriced": repriced,
        "original_notional_per_addition": current_notional,
        "selected_notional_per_addition": float(
            preview["dca"]["notional_per_addition"]
        ),
        "degradation_event_refs": [
            dict(row) for row in degradation_event_refs or []
        ],
    }
    proposal_id = _digest_id(
        "proposal-paper-continuity",
        {
            "cycle_id": cycle_id,
            "attempt_id": supervisor_attempt_id,
            "preview_id": preview["preview_id"],
            "source_plan_id": source_plan.get("strategy_plan_id"),
        },
    )
    proposal = {
        "proposal_id": proposal_id,
        "cycle_id": cycle_id,
        "source": PAPER_CONTINUITY_PROPOSAL_SOURCE,
        "direction": direction,
        "style": str(source_plan.get("style") or "steady"),
        "strategy_type": "dca",
        "range": {},
        "key_levels": list(preview["dca"]["entry_levels"]),
        "grid": {},
        "dca": dict(preview["dca"]),
        "signal": dict(source.get("signal") or source_plan.get("signal") or {}),
        "tp_sl": {
            "target_price": preview["dca"]["target_price"],
            "stop_price": preview["dca"]["stop_price"],
        },
        "risk_budget": dict(preview["risk"]),
        "intraday_rules": list(
            source.get("intraday_rules")
            or source_plan.get("intraday_rules")
            or []
        ),
        "rationale": (
            "Paper continuity fallback: inherit the verified DCA direction "
            "and rebuild levels from the current trusted market within the "
            "exact Paper account and Park policy boundary."
        ),
        "evidence_used": list(source.get("evidence_used") or []),
        "analysis": {
            **dict(source.get("analysis") or {}),
            "paper_continuity_recovery": recovery,
            "new_ai_judgment": False,
            "inherited_intent_source": "ai",
            "inherited_source_proposal_id": source.get("proposal_id"),
            "inherited_source_proposal_digest": source.get(
                "source_proposal_digest"
            ),
            "root_ai_source_proposal_id": source.get(
                "root_ai_source_proposal_id"
            ),
            "root_ai_source_proposal_digest": source.get(
                "root_ai_source_proposal_digest"
            ),
        },
        "prompt_contract": {},
        "evaluation_receipt": {},
        "preview_id": preview["preview_id"],
        "start_facts_digest": start_facts_digest,
    }
    if provider_readiness is not None:
        proposal["provider_readiness"] = dict(provider_readiness)
    return {
        "proposal": proposal,
        "preview": preview,
        "request": payload,
        "recovery": recovery,
    }


def _bounded_grid_count(value: Any, limits: Mapping[str, Any]) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 0
    minimum = _bounded_int(
        limits.get("min_grid_count"),
        default=2,
        zero_is_default=True,
    )
    maximum = _bounded_int(limits.get("max_grid_count"), default=None)
    count = max(minimum, count or minimum)
    if maximum is not None:
        count = min(count, maximum)
    return count


def _bounded_int(
    value: Any,
    *,
    default: int | None,
    zero_is_default: bool = False,
) -> int | None:
    if value in {None, "", "unbounded"}:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("outer_strategy_policy_invalid") from exc
    if parsed == 0 and zero_is_default:
        return default
    if parsed <= 0:
        raise ValueError("outer_strategy_policy_invalid")
    return parsed


def _notional_cap(
    preview: Mapping[str, Any],
    *,
    limits: Mapping[str, Any],
    equity: float,
) -> float:
    grid = dict(preview.get("grid") or {})
    risk = dict(preview.get("risk") or {})
    current = float(grid.get("notional_per_grid") or 0)
    caps = [current, float(risk.get("safe_notional_cap_per_grid") or current)]
    max_notional = _bounded_float(limits.get("max_notional_per_grid"))
    if max_notional is not None:
        caps.append(max_notional)
    max_leverage = _bounded_float(limits.get("max_actual_leverage"))
    simultaneous = int(risk.get("max_simultaneous_same_side_levels") or 0)
    if max_leverage is not None and simultaneous > 0:
        caps.append(equity * max_leverage / simultaneous)
    max_loss = _bounded_float(limits.get("max_full_depth_loss"))
    preview_loss = float(risk.get("max_loss") or 0)
    if max_loss is not None and preview_loss > 0 and current > 0:
        caps.append(current * max_loss / preview_loss)
    cap = math.floor(min(caps) * 100.0) / 100.0
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("outer_strategy_policy_envelope_out_of_bounds")
    return cap


def _bounded_float(value: Any) -> float | None:
    if value in {None, "", "unbounded"}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("outer_strategy_policy_invalid") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("outer_strategy_policy_invalid")
    return parsed


def _bounded_additions(
    value: Any,
    *,
    limits: Mapping[str, Any],
    available: int,
) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = available
    minimum = _bounded_int(limits.get("min_additions"), default=1)
    maximum = _bounded_int(
        limits.get("max_additions"),
        default=available,
    )
    if minimum is None or maximum is None or minimum > maximum:
        raise ValueError("outer_strategy_policy_invalid")
    selected = max(minimum, requested or minimum)
    selected = min(selected, maximum, available)
    if selected < minimum:
        raise ValueError("outer_strategy_policy_envelope_out_of_bounds")
    return selected


def _dca_notional_cap(
    preview: Mapping[str, Any],
    *,
    limits: Mapping[str, Any],
    equity: float,
) -> float:
    dca = dict(preview.get("dca") or {})
    risk = dict(preview.get("risk") or {})
    current = float(dca.get("notional_per_addition") or 0)
    additions = int(dca.get("max_additions") or 0)
    caps = [current]
    max_per_addition = _bounded_float(
        limits.get("max_notional_per_addition")
    )
    if max_per_addition is not None:
        caps.append(max_per_addition)
    max_total = _bounded_float(
        limits.get("max_total_possible_notional")
    )
    if max_total is not None and additions > 0:
        caps.append(max_total / additions)
    max_leverage = _bounded_float(limits.get("max_actual_leverage"))
    if max_leverage is not None and additions > 0:
        caps.append(equity * max_leverage / additions)
    max_loss = _bounded_float(limits.get("max_full_depth_loss"))
    preview_loss = float(risk.get("maximum_loss_at_full_depth") or 0)
    if max_loss is not None and preview_loss > 0 and current > 0:
        caps.append(current * max_loss / preview_loss)
    cap = math.floor(min(caps) * 100.0) / 100.0
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("outer_strategy_policy_envelope_out_of_bounds")
    return cap


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _digest_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}-{_digest(value)[:20]}"
