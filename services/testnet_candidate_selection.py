"""Canonical all-pair Testnet candidate selection.

The selector consumes already-read Broker/market facts.  It keeps structural
inventory separate from execution eligibility and delegates exposure policy to
the existing subtractive Portfolio Risk Gate.  No method in this module can
open, cancel, protect, or otherwise mutate a Broker order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from schemas.portfolio import (
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyCandidateSet,
    StrategyPositionPlan,
)
from services.portfolio_gate import PortfolioRiskGate
from services.trading_system_read_model import project_portfolio_read_model


CANDIDATE_SELECTION_SCHEMA = "testnet-candidate-selection-v1"
MARKET_MAX_AGE_SECONDS = Decimal("120")
_DIGEST_PREFIX = "sha256:"
_STRATEGY_FAMILIES = frozenset({"dca", "grid"})
_REQUIRED_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "asset",
        "instrument_id",
        "rank",
        "strategy",
        "market",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "private_key",
        "secret",
        "signature",
        "signed_payload",
        "authorization",
    }
)


class CandidateSelectionError(ValueError):
    """Stable blocker for malformed candidate facts."""

    __test__ = False

    def __init__(self, code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported_candidate_value:{type(value).__name__}")


def _digest(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CandidateSelectionError(f"{field}_required")
    return text


def _decimal(value: Any, field: str, *, positive: bool = True) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CandidateSelectionError(f"{field}_invalid") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise CandidateSelectionError(f"{field}_invalid")
    return number


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateSelectionError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise CandidateSelectionError(f"{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _now(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CandidateSelectionError("selection_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise CandidateSelectionError("selection_timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _plan_from_candidate(
    candidate: Mapping[str, Any],
    *,
    strategy_session_id: str,
    strategy_revision_id: str,
) -> StrategyPositionPlan:
    strategy = candidate.get("strategy")
    if not isinstance(strategy, Mapping):
        raise CandidateSelectionError("strategy_facts_invalid")
    asset = _text(candidate.get("asset"), "asset")
    instrument_id = _text(candidate.get("instrument_id"), "instrument_id")
    return StrategyPositionPlan(
        strategy_session_id=strategy_session_id,
        strategy_revision_id=strategy_revision_id,
        candidate_id=_text(candidate.get("candidate_id"), "candidate_id"),
        candidate_rank=int(candidate.get("rank") or 0),
        asset=asset,
        direction=_text(strategy.get("direction"), "direction").lower(),
        requested_quantity=_decimal(strategy.get("requested_quantity"), "requested_quantity", positive=False),
        requested_notional=(
            None
            if strategy.get("requested_notional") is None
            else _decimal(strategy.get("requested_notional"), "requested_notional", positive=False)
        ),
        position_action=str(strategy.get("position_action") or "open").lower(),
        position_management={
            **dict(strategy.get("position_management") or {}),
            "instrument_id": instrument_id,
        },
        protection_intent=dict(strategy.get("protection_intent") or {}),
        provenance={
            "source": "testnet_candidate_selector",
            "candidate_facts_digest": _digest(_public(candidate)),
            "instrument_id": instrument_id,
            "venue": "hyperliquid",
            "environment": "testnet",
            "strategy_family": str(strategy.get("strategy_family") or "").lower() or None,
        },
    )


def _market_blockers(
    candidate: Mapping[str, Any],
    *,
    plan: StrategyPositionPlan,
    now: datetime,
) -> list[str]:
    market = candidate.get("market")
    if not isinstance(market, Mapping):
        return ["market_facts_invalid"]
    blockers: list[str] = []
    try:
        observed = _timestamp(market.get("observed_at"), "market_observed_at")
    except CandidateSelectionError:
        return ["market_timestamp_invalid"]
    age = (now - observed).total_seconds()
    if age < 0 or Decimal(str(age)) > MARKET_MAX_AGE_SECONDS:
        blockers.append("market_stale")
    numeric: dict[str, Decimal] = {}
    for field in ("bid", "ask", "mid", "mark", "oracle", "impact", "depth_notional"):
        try:
            numeric[field] = _decimal(market.get(field), f"market_{field}")
        except CandidateSelectionError:
            blockers.append(f"market_{field}_invalid")
    if blockers and any(item.endswith("_invalid") for item in blockers):
        return blockers
    if not (numeric["bid"] < numeric["ask"]):
        blockers.append("bbo_not_two_sided")
    if not numeric["bid"] <= numeric["mid"] <= numeric["ask"]:
        blockers.append("mid_outside_bbo")
    requested_notional = plan.requested_notional
    if requested_notional is not None and numeric["depth_notional"] < requested_notional:
        blockers.append("insufficient_depth")
    try:
        max_slippage = _decimal(
            market.get("max_slippage"), "market_max_slippage", positive=False
        )
    except CandidateSelectionError:
        blockers.append("slippage_bound_missing")
    else:
        if abs(numeric["impact"] - numeric["mid"]) > max_slippage:
            blockers.append("impact_slippage_exceeded")
    try:
        max_oracle_bps = _decimal(
            market.get("max_oracle_deviation_bps"),
            "market_max_oracle_deviation_bps",
            positive=False,
        )
    except CandidateSelectionError:
        blockers.append("oracle_bound_missing")
    else:
        deviation_bps = abs(numeric["mark"] - numeric["oracle"]) / numeric["oracle"] * Decimal("10000")
        if deviation_bps > max_oracle_bps:
            blockers.append("oracle_dislocation")
    for field in ("source", "cursor", "broker_id", "environment", "instrument_id"):
        if not str(market.get(field) or "").strip():
            blockers.append(f"market_{field}_missing")
    if str(market.get("broker_id") or "").lower() != "hyperliquid":
        blockers.append("market_broker_mismatch")
    if str(market.get("environment") or "").lower() != "testnet":
        blockers.append("market_environment_mismatch")
    if str(market.get("instrument_id") or "") != str(candidate.get("instrument_id") or ""):
        blockers.append("market_instrument_mismatch")
    return sorted(set(blockers))


def _instrument_blockers(candidate: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if candidate.get("delisted") is True:
        blockers.append("delisted")
    if str(candidate.get("status") or "active").lower() not in {"active", "trading"}:
        blockers.append("inactive")
    if candidate.get("mapping_valid") is not True:
        blockers.append("instrument_mapping_invalid")
    for field in ("price_tick", "quantity_step", "minimum_quantity", "minimum_notional"):
        try:
            _decimal(candidate.get(field), field)
        except CandidateSelectionError:
            blockers.append(f"instrument_{field}_invalid")
    return sorted(set(blockers))


@dataclass(frozen=True)
class CandidateSelectionResult:
    """Immutable selection evidence returned by the candidate selector."""

    schema_version: str
    status: str
    strategy_family: str
    strategy_session_id: str
    strategy_revision_id: str
    candidate_set: StrategyCandidateSet
    inventory: tuple[Mapping[str, Any], ...]
    selected_asset: str | None
    selected_instrument_id: str | None
    portfolio_result: PortfolioSelection | PortfolioRiskHold
    read_model: Mapping[str, Any]
    global_hold: bool
    blocker: str | None = None

    __test__ = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "strategy_family": self.strategy_family,
            "strategy_session_id": self.strategy_session_id,
            "strategy_revision_id": self.strategy_revision_id,
            "candidate_set": self.candidate_set.to_dict(),
            "inventory": [_public(row) for row in self.inventory],
            "selected_asset": self.selected_asset,
            "selected_instrument_id": self.selected_instrument_id,
            "portfolio_result": self.portfolio_result.to_dict(),
            "read_model": _public(self.read_model),
            "global_hold": self.global_hold,
            "blocker": self.blocker,
        }


class TestnetCandidateSelector:
    """Select one eligible asset without invoking execution."""

    __test__ = False

    def __init__(self, gate: PortfolioRiskGate | None = None) -> None:
        self.gate = gate or PortfolioRiskGate()

    def select(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        strategy_family: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        now: str | datetime,
    ) -> CandidateSelectionResult:
        family = _text(strategy_family, "strategy_family").lower()
        if family not in _STRATEGY_FAMILIES:
            raise CandidateSelectionError("strategy_family_invalid")
        session_id = _text(strategy_session_id, "strategy_session_id")
        revision_id = _text(strategy_revision_id, "strategy_revision_id")
        if not isinstance(snapshot, PortfolioSnapshot):
            raise CandidateSelectionError("portfolio_snapshot_required")
        if not isinstance(policy, PortfolioPolicy):
            raise CandidateSelectionError("portfolio_policy_required")
        observed_at = _now(now)
        raw_rows = list(candidates)
        seen_ids: set[str] = set()
        inventory: list[dict[str, Any]] = []
        eligible_plans: list[StrategyPositionPlan] = []
        for index, raw in enumerate(raw_rows, start=1):
            if not isinstance(raw, Mapping):
                raise CandidateSelectionError("candidate_shape_invalid", {"index": index})
            missing = sorted(_REQUIRED_CANDIDATE_FIELDS.difference(raw))
            if missing:
                raise CandidateSelectionError("candidate_fields_missing", {"fields": missing})
            candidate_id = _text(raw.get("candidate_id"), "candidate_id")
            if candidate_id in seen_ids:
                raise CandidateSelectionError("candidate_id_duplicate", {"candidate_id": candidate_id})
            seen_ids.add(candidate_id)
            try:
                rank = int(raw.get("rank"))
            except (TypeError, ValueError) as exc:
                raise CandidateSelectionError("candidate_rank_invalid") from exc
            if rank <= 0:
                raise CandidateSelectionError("candidate_rank_invalid")
            row = {
                "candidate_id": candidate_id,
                "asset": _text(raw.get("asset"), "asset"),
                "instrument_id": _text(raw.get("instrument_id"), "instrument_id"),
                "rank": rank,
                "status": "observed",
                "blockers": [],
                "facts_digest": _digest(_public(raw)),
            }
            blockers = _instrument_blockers(raw)
            try:
                plan = _plan_from_candidate(
                    raw,
                    strategy_session_id=session_id,
                    strategy_revision_id=revision_id,
                )
            except (CandidateSelectionError, TypeError, ValueError) as exc:
                blockers.append(str(getattr(exc, "code", "strategy_plan_invalid")))
                plan = None
            if plan is not None:
                blockers.extend(_market_blockers(raw, plan=plan, now=observed_at))
            if blockers:
                row["status"] = "blocked"
                row["blockers"] = sorted(set(blockers))
            else:
                row["status"] = "eligible"
                eligible_plans.append(plan)
                row["plan_digest"] = plan.digest
            inventory.append(row)

        ordered_plans = tuple(sorted(eligible_plans, key=lambda item: (item.candidate_rank, item.candidate_id)))
        candidate_set = StrategyCandidateSet(
            candidate_set_id=_digest(
                {
                    "strategy_family": family,
                    "strategy_session_id": session_id,
                    "strategy_revision_id": revision_id,
                    "inventory": inventory,
                }
            ),
            strategy_session_id=session_id,
            strategy_revision_id=revision_id,
            created_at=observed_at.isoformat(),
            candidates=ordered_plans,
            provenance={
                "source": "testnet_candidate_selector",
                "strategy_family": family,
                "inventory_count": len(inventory),
                "eligible_count": len(ordered_plans),
                "selection_environment": "testnet",
            },
        )

        rejected: list[Mapping[str, Any]] = []
        selected_result: PortfolioSelection | PortfolioRiskHold | None = None
        selected_asset: str | None = None
        selected_instrument: str | None = None
        for plan in ordered_plans:
            gate_result = self.gate.evaluate(plan, snapshot, policy)
            if isinstance(gate_result, PortfolioRiskHold):
                selected_result = gate_result
                break
            if gate_result.selected_allocations:
                selected_result = gate_result
                selected_asset = plan.asset
                selected_instrument = str(plan.position_management.get("instrument_id") or "") or None
                break
            rejected.extend(gate_result.rejected_candidates)
        if selected_result is None:
            # Build an immutable empty selection for the no-eligible case.  A
            # global Portfolio Risk Hold is reserved for account-level facts.
            provenance = {
                "gate_schema": self.gate.schema_version,
                "candidate_set_id": candidate_set.candidate_set_id,
                "candidate_set_digest": candidate_set.digest,
                "snapshot_id": snapshot.snapshot_id,
                "policy_id": policy.policy_id,
                "policy_revision": policy.policy_revision,
                "outcome": "NO_ELIGIBLE_CANDIDATE",
            }
            selected_result = PortfolioSelection(
                selection_id=_digest(provenance),
                portfolio_session_id=snapshot.portfolio_session_id,
                candidate_set_id=candidate_set.candidate_set_id,
                policy_id=policy.policy_id,
                policy_revision=policy.policy_revision,
                snapshot_id=snapshot.snapshot_id,
                created_at=observed_at.isoformat(),
                selected_allocations=(),
                rejected_candidates=tuple(rejected)
                + tuple(
                    {
                        "candidate_id": row["candidate_id"],
                        "reason": "candidate_ineligible",
                    }
                    for row in inventory
                    if row["status"] == "blocked"
                ),
                decision_provenance=provenance,
            )
        if isinstance(selected_result, PortfolioRiskHold):
            status = "held"
            blocker = selected_result.reason_code
            global_hold = True
        else:
            status = "selected" if selected_result.selected_allocations else "blocked"
            blocker = None if selected_result.selected_allocations else "no_eligible_candidate"
            global_hold = False
        read_model = project_portfolio_read_model(selected_result, snapshot) or {
            "status": status,
            "read_only": True,
        }
        return CandidateSelectionResult(
            schema_version=CANDIDATE_SELECTION_SCHEMA,
            status=status,
            strategy_family=family,
            strategy_session_id=session_id,
            strategy_revision_id=revision_id,
            candidate_set=candidate_set,
            inventory=tuple(inventory),
            selected_asset=selected_asset,
            selected_instrument_id=selected_instrument,
            portfolio_result=selected_result,
            read_model=read_model,
            global_hold=global_hold,
            blocker=blocker,
        )


__all__ = [
    "CANDIDATE_SELECTION_SCHEMA",
    "MARKET_MAX_AGE_SECONDS",
    "CandidateSelectionError",
    "CandidateSelectionResult",
    "TestnetCandidateSelector",
]
