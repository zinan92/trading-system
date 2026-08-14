"""Park Grid fixed-range lifecycle and proposal-only reassessment."""

from __future__ import annotations

from typing import Any, Mapping

from services.park_strategy_lifecycle import ParkStrategyLifecycleLedger
from services.park_telegram_control import ParkTelegramLedger


class ParkGridLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ParkGridLifecycle:
    def __init__(
        self,
        plan: Mapping[str, Any],
        *,
        confirmation_receipt: Mapping[str, Any],
        output_root,
        park_user_id: str,
        chat_id: str,
    ) -> None:
        normalized = plan.get("normalized_input") if isinstance(plan.get("normalized_input"), Mapping) else {}
        if normalized.get("strategy_type") != "grid":
            raise ParkGridLifecycleError("wrong_strategy_type", "Park Grid lifecycle requires a Grid plan")
        self.plan = dict(plan)
        self.normalized = dict(normalized)
        self.session_id = str(self.normalized.get("strategy_session_id") or plan.get("strategy_session_id") or "").strip()
        self.revision_id = str(self.normalized.get("strategy_revision_id") or plan.get("strategy_revision_id") or "").strip()
        self.plan_digest = str(plan.get("plan_digest") or "").strip()
        if not self.session_id or not self.revision_id or not self.plan_digest:
            raise ParkGridLifecycleError("identity_missing", "Grid plan identity is incomplete")
        receipt = dict(confirmation_receipt or {})
        if (
            receipt.get("execution_authorized") is not True
            or receipt.get("plan_digest") != self.plan_digest
            or receipt.get("strategy_session_id") != self.session_id
            or receipt.get("strategy_revision_id") != self.revision_id
            or receipt.get("start_or_order_submitted") is not False
        ):
            raise ParkGridLifecycleError("confirmation_required", "Grid lifecycle requires an exact Park confirmation receipt")
        self.lifecycle = ParkStrategyLifecycleLedger(output_root)
        self.lifecycle.activate({
            **self.normalized,
            "strategy_session_id": self.session_id,
            "strategy_revision_id": self.revision_id,
            "plan_digest": self.plan_digest,
            "maximum_leverage": (plan.get("risk") or {}).get("effective_leverage"),
            "maximum_acceptable_loss": (plan.get("risk") or {}).get("theoretical_max_loss"),
        })
        self.telegram = ParkTelegramLedger(output_root, park_user_id=park_user_id, chat_id=chat_id)
        self._levels: list[dict[str, Any]] | None = None

    def levels(self) -> list[dict[str, Any]]:
        if self._levels is not None:
            return [dict(row) for row in self._levels]
        count = int((self.plan.get("risk") or {}).get("order_count") or self.normalized.get("order_count") or 1)
        if count <= 0:
            raise ParkGridLifecycleError("invalid_grid_count", "Grid count must be positive")
        lower = float(self.normalized["lower_price_boundary"])
        upper = float(self.normalized["upper_price_boundary"])
        step = (upper - lower) / (count + 1)
        direction = str(self.normalized["direction"])
        quantity = float((self.plan.get("risk") or {}).get("per_order_quantity") or 0)
        if quantity <= 0:
            raise ParkGridLifecycleError("risk_incomplete", "Grid risk plan has no executable quantity")
        self._levels = [
            {
                "command_type": "grid_level",
                "level_id": f"{self.revision_id}:grid:{index + 1}",
                "strategy_session_id": self.session_id,
                "strategy_revision_id": self.revision_id,
                "plan_digest": self.plan_digest,
                "direction": direction,
                "price": round(lower + step * (index + 1), 12),
                "quantity": quantity,
                "geometry_locked": True,
            }
            for index in range(count)
        ]
        return [dict(row) for row in self._levels]

    def observe(self, *, price: float, trusted: bool, fresh: bool) -> dict[str, Any]:
        if not trusted or not fresh:
            raise ParkGridLifecycleError("market_not_authoritative", "Grid observation requires trusted fresh market")
        existing = next(
            (row for row in self.lifecycle.rows() if row.get("event") == "terminal_action_plan" and row.get("strategy_session_id") == self.session_id and row.get("strategy_revision_id") == self.revision_id),
            None,
        )
        if existing:
            return {"status": "terminal", "levels": self.levels(), "action_plan": dict(existing), "notification": self._notification(existing)}
        upper = float(self.normalized["upper_price_boundary"])
        lower = float(self.normalized["lower_price_boundary"])
        if lower < float(price) < upper:
            return {"status": "active", "levels": self.levels(), "reassessment": "none"}
        boundary = "upper" if float(price) >= upper else "lower"
        action = self.lifecycle.boundary_action_plan(
            strategy_session_id=self.session_id,
            strategy_revision_id=self.revision_id,
            boundary=boundary,
            observed_price=float(price),
            trusted_market=trusted,
            fresh_tick=fresh,
        )
        return {"status": "terminal", "levels": self.levels(), "action_plan": action, "notification": self._notification(action)}

    def propose_geometry_change(self, *, direction: str, upper: float, lower: float) -> dict[str, Any]:
        """Return a proposal only; it cannot mutate the active Grid."""

        return {
            "status": "proposal_only",
            "requires_clean_slate": True,
            "requires_new_strategy_revision": True,
            "requires_new_confirmation": True,
            "current_strategy_unchanged": True,
            "proposed": {"direction": direction, "upper_price_boundary": float(upper), "lower_price_boundary": float(lower)},
        }

    def _notification(self, action: Mapping[str, Any]) -> dict[str, Any]:
        return self.telegram.queue_outbound(
            idempotency_key=f"park-grid-terminal:{self.session_id}:{self.revision_id}",
            message_type="grid_terminal",
            text=(
                f"Grid terminal: {action['boundary']} boundary at {action['observed_price']}; "
                "geometry frozen, entries canceled, positions reconcile, paused; await Park."
            ),
            binding={"strategy_session_id": self.session_id, "strategy_revision_id": self.revision_id},
        )
