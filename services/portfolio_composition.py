"""Paper/read-only Portfolio composition root and BTC candidate preparation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas.portfolio import (
    AssetAllocationSlice,
    PortfolioOwnershipAssessment,
    PortfolioPolicy,
    PortfolioRebalanceDecision,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyCandidateSet,
    StrategyPositionPlan,
)
from services.portfolio_gate import PortfolioRiskGate
from services.portfolio_ownership import PortfolioOwnershipRegistry
from services.portfolio_rebalance import PortfolioRebalanceRegistry
from services.trading_system_read_model import project_portfolio_read_model


PORTFOLIO_COMPOSITION_SCHEMA = "portfolio-composition-read-only-v1"


@dataclass(frozen=True)
class PortfolioCompositionResult:
    """Replayable composition evidence with no execution request surface."""

    gate_result: PortfolioSelection | PortfolioRiskHold
    ownership_assessment: PortfolioOwnershipAssessment
    read_model: Mapping[str, Any]
    rebalance_decision: PortfolioRebalanceDecision | None = None
    effective_allocations: tuple[AssetAllocationSlice, ...] = ()
    execution_requests: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = PORTFOLIO_COMPOSITION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "gate_result": self.gate_result.to_dict(),
            "ownership_assessment": self.ownership_assessment.to_dict(),
            "read_model": _thaw(self.read_model),
            "rebalance_decision": None if self.rebalance_decision is None else self.rebalance_decision.to_dict(),
            "effective_allocations": [item.to_dict() for item in self.effective_allocations],
            "execution_requests": _thaw(self.execution_requests),
        }


def compose_portfolio_read_only(
    candidate_set: StrategyCandidateSet,
    snapshot: PortfolioSnapshot,
    policy: PortfolioPolicy,
    *,
    rebalance_decision: PortfolioRebalanceDecision | None = None,
    current_selection: PortfolioSelection | None = None,
    seen_decision_ids: Iterable[str] = (),
) -> PortfolioCompositionResult:
    """Compose Gate, ownership, optional rebalance evidence, and read model."""

    _require_type(candidate_set, StrategyCandidateSet, "candidate_set")
    _require_type(snapshot, PortfolioSnapshot, "snapshot")
    _require_type(policy, PortfolioPolicy, "policy")
    if rebalance_decision is not None:
        if current_selection is None:
            raise ValueError("current selection is required to validate a rebalance decision")
        PortfolioRebalanceRegistry().validate(
            rebalance_decision,
            current_selection=current_selection,
            current_policy=policy,
            seen_decision_ids=seen_decision_ids,
        )
    ownership = PortfolioOwnershipRegistry().evaluate(snapshot, policy)
    gate_result = PortfolioRiskGate().evaluate(candidate_set, snapshot, policy)
    if ownership.portfolio_risk_hold is not None:
        gate_result = ownership.portfolio_risk_hold
    effective_allocations: tuple[AssetAllocationSlice, ...] = ()
    if isinstance(gate_result, PortfolioSelection) and not ownership.blocks_new_allocations:
        effective_allocations = tuple(gate_result.selected_allocations)
    read_model = project_portfolio_read_model(gate_result, snapshot, rebalance_decision) or {}
    return PortfolioCompositionResult(
        gate_result=gate_result,
        ownership_assessment=ownership,
        read_model=read_model,
        rebalance_decision=rebalance_decision,
        effective_allocations=effective_allocations,
        execution_requests=(),
    )


def prepare_btc_candidate(
    instrument_facts: Mapping[str, Any],
    *,
    strategy_notional: Decimal | str | int | float,
    strategy_session_id: str,
    strategy_revision_id: str,
    candidate_id: str,
    candidate_rank: int,
    position_action: str = "add",
    position_management: Mapping[str, Any] | None = None,
    protection_intent: Mapping[str, Any] | None = None,
) -> StrategyPositionPlan:
    """Prepare a BTC plan from canonical local facts; never fetches or submits."""

    facts = _public_facts(instrument_facts)
    if str(facts.get("venue") or "").strip().lower() != "hyperliquid":
        raise ValueError("BTC candidate requires venue=hyperliquid")
    if str(facts.get("environment") or "").strip().lower() != "testnet":
        raise ValueError("BTC candidate preparation is Testnet-only")
    if str(facts.get("base_asset") or "").strip().upper() != "BTC":
        raise ValueError("BTC candidate requires base_asset=BTC")
    if not str(facts.get("instrument_id") or "").strip():
        raise ValueError("BTC candidate requires canonical instrument_id")
    contract_type = str(facts.get("contract_type") or "").strip().lower()
    if contract_type not in {"perpetual", "perp"}:
        raise ValueError("BTC candidate requires a perpetual contract")
    notional = _positive_decimal(strategy_notional, "strategy notional")
    price = _positive_decimal(facts.get("mark_price") or facts.get("oracle_price"), "BTC reference price")
    multiplier = _positive_decimal(facts.get("contract_multiplier", "1"), "contract multiplier")
    increment = _positive_decimal(facts.get("quantity_increment"), "quantity increment")
    raw_quantity = notional / (price * multiplier)
    quantity = (raw_quantity // increment) * increment
    if quantity <= 0:
        raise ValueError("strategy notional produces no executable BTC quantity")
    minimum_quantity = facts.get("min_quantity")
    if minimum_quantity is not None and quantity < _positive_decimal(minimum_quantity, "minimum quantity"):
        raise ValueError("BTC quantity is below canonical minimum quantity")
    minimum_notional = facts.get("min_notional")
    if minimum_notional is not None and notional < _positive_decimal(minimum_notional, "minimum notional"):
        raise ValueError("BTC strategy notional is below canonical minimum notional")
    provenance = {
        "source": "canonical_instrument_facts",
        "instrument_facts_digest": _stable_id("instrument", facts),
        "instrument_id": str(facts["instrument_id"]),
        "venue": "hyperliquid",
        "environment": "testnet",
        "reference_price": _decimal_text(price),
        "contract_multiplier": _decimal_text(multiplier),
        "quantity_increment": _decimal_text(increment),
        "strategy_notional": _decimal_text(notional),
        "prepared_read_only": True,
    }
    return StrategyPositionPlan(
        strategy_session_id=strategy_session_id,
        strategy_revision_id=strategy_revision_id,
        candidate_id=candidate_id,
        candidate_rank=candidate_rank,
        asset="BTC",
        direction="long",
        requested_quantity=quantity,
        requested_notional=notional,
        position_action=position_action,
        position_management=position_management or {},
        protection_intent=protection_intent or {},
        provenance=provenance,
    )


def _public_facts(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("instrument_facts must be an object")
    sensitive = {"api_key", "api_secret", "private_key", "secret", "authorization", "signature"}
    return {
        str(key): _public_facts(item) if isinstance(item, Mapping) else [_public_facts(row) if isinstance(row, Mapping) else row for row in item] if isinstance(item, (list, tuple)) else item
        for key, item in value.items()
        if str(key).strip().lower() not in sensitive
    }


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        rendered = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if not rendered.is_finite() or rendered <= 0:
        raise ValueError(f"{label} must be positive")
    return rendered


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _require_type(value: Any, expected: type[Any], label: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be {expected.__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


__all__ = [
    "PORTFOLIO_COMPOSITION_SCHEMA",
    "PortfolioCompositionResult",
    "compose_portfolio_read_only",
    "prepare_btc_candidate",
]
