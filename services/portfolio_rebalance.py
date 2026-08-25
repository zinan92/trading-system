"""Pure validation for explicit Portfolio Rebalance Decisions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from schemas.portfolio import PortfolioPolicy, PortfolioRebalanceDecision, PortfolioSelection


PORTFOLIO_REBALANCE_REGISTRY_SCHEMA = "portfolio-rebalance-registry-v1"


class PortfolioRebalanceRegistry:
    """Validate stale/duplicate boundaries without persisting or executing."""

    schema_version = PORTFOLIO_REBALANCE_REGISTRY_SCHEMA

    def validate(
        self,
        decision: PortfolioRebalanceDecision,
        *,
        current_selection: PortfolioSelection,
        current_policy: PortfolioPolicy,
        seen_decision_ids: Iterable[str] = (),
    ) -> PortfolioRebalanceDecision:
        if not isinstance(decision, PortfolioRebalanceDecision):
            raise TypeError("rebalance decision must be PortfolioRebalanceDecision")
        if not isinstance(current_selection, PortfolioSelection):
            raise TypeError("current selection must be PortfolioSelection")
        if not isinstance(current_policy, PortfolioPolicy):
            raise TypeError("current policy must be PortfolioPolicy")
        if decision.old_selection.selection_id != current_selection.selection_id:
            raise ValueError("stale rebalance decision: old selection is not current")
        if decision.policy_id != current_policy.policy_id or decision.policy_revision != current_policy.policy_revision:
            raise ValueError("stale rebalance decision: policy revision is not current")
        if decision.decision_id in {str(item) for item in seen_decision_ids}:
            raise ValueError("duplicate rebalance decision replay")
        return decision

    def replay_key(self, decision: PortfolioRebalanceDecision) -> str:
        if not isinstance(decision, PortfolioRebalanceDecision):
            raise TypeError("rebalance decision must be PortfolioRebalanceDecision")
        return decision.digest


__all__ = ["PORTFOLIO_REBALANCE_REGISTRY_SCHEMA", "PortfolioRebalanceRegistry"]
