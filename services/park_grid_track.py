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
        geometry = (self.plan.get("risk") or {}).get("grid_rungs")
        if isinstance(geometry, list) and geometry:
            quantity = float((self.plan.get("risk") or {}).get("per_order_quantity") or 0)
            if quantity <= 0:
                raise ParkGridLifecycleError("risk_incomplete", "Grid risk plan has no executable quantity")
            levels = []
            direction = str(self.normalized.get("direction") or "")
            for rung in geometry:
                if not isinstance(rung, Mapping):
                    raise ParkGridLifecycleError("grid_geometry_invalid", "Grid rung evidence is malformed")
                price = float(rung.get("price") or 0)
                take_profit = float(rung.get("take_profit") or 0)
                hard_stop = float(rung.get("hard_stop") or 0)
                if price <= 0 or take_profit <= 0 or hard_stop <= 0:
                    raise ParkGridLifecycleError("grid_geometry_invalid", "Grid rung prices must be positive")
                side = str(rung.get("side") or "")
                if side not in {"buy", "sell"}:
                    raise ParkGridLifecycleError("grid_geometry_invalid", "Grid rung side is invalid")
                levels.append({
                    "command_type": "grid_level",
                    "level_id": f"{self.revision_id}:grid:{int(rung.get('rung') or len(levels) + 1)}",
                    "strategy_session_id": self.session_id,
                    "strategy_revision_id": self.revision_id,
                    "plan_digest": self.plan_digest,
                    "direction": direction,
                    "side": side,
                    "price": price,
                    "quantity": quantity,
                    "tp": take_profit,
                    "sl": hard_stop,
                    "geometry_locked": True,
                })
            if direction == "neutral" and {row["side"] for row in levels} != {"buy", "sell"}:
                raise ParkGridLifecycleError("neutral_grid_requires_two_legs", "neutral Grid must contain both buy and sell entries")
            self._levels = levels
            return [dict(row) for row in self._levels]
        count = int((self.plan.get("risk") or {}).get("order_count") or self.normalized.get("order_count") or 1)
        if count <= 0:
            raise ParkGridLifecycleError("invalid_grid_count", "Grid count must be positive")
        lower = float(self.normalized["lower_price_boundary"])
        upper = float(self.normalized["upper_price_boundary"])
        current = float((self.plan.get("market") or {}).get("price") or 0.0)
        step = (upper - lower) / (count + 1)
        direction = str(self.normalized["direction"])
        if direction == "neutral" and not lower < current < upper:
            raise ParkGridLifecycleError(
                "neutral_grid_requires_interior_price",
                "neutral Grid requires the current price to be strictly inside its range",
            )
        quantity = float((self.plan.get("risk") or {}).get("per_order_quantity") or 0)
        if quantity <= 0:
            raise ParkGridLifecycleError("risk_incomplete", "Grid risk plan has no executable quantity")
        prices = [round(lower + step * (index + 1), 12) for index in range(count)]
        levels: list[dict[str, Any]] = []
        for index, price in enumerate(prices):
            side = None
            protections: dict[str, float] = {}
            if direction == "neutral":
                side = "buy" if price < current else "sell"
                if side == "buy":
                    # A filled buy exits at the next higher grid line; the
                    # lower authorized boundary is its hard stop.
                    next_level = prices[index + 1] if index + 1 < len(prices) else upper
                    protections = {"tp": next_level, "sl": lower}
                else:
                    # A filled sell exits at the next lower grid line; the
                    # upper authorized boundary is its hard stop.
                    next_level = prices[index - 1] if index > 0 else lower
                    protections = {"tp": next_level, "sl": upper}
            levels.append(
                {
                    "command_type": "grid_level",
                    "level_id": f"{self.revision_id}:grid:{index + 1}",
                    "strategy_session_id": self.session_id,
                    "strategy_revision_id": self.revision_id,
                    "plan_digest": self.plan_digest,
                    "direction": direction,
                    "side": side,
                    "price": price,
                    "quantity": quantity,
                    "geometry_locked": True,
                    **protections,
                }
            )
        if direction == "neutral":
            sides = {str(row.get("side") or "") for row in levels}
            if sides != {"buy", "sell"}:
                raise ParkGridLifecycleError(
                    "neutral_grid_requires_two_legs",
                    "neutral Grid must create at least one buy and one sell entry",
                )
        self._levels = levels
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
        position_text = (
            "owned positions preserved for reconciliation"
            if action.get("position_authority") == "preserve_strategy_owned_positions"
            else "owned positions closed only for this exact strategy"
        )
        return self.telegram.queue_outbound(
            idempotency_key=f"park-grid-terminal:{self.session_id}:{self.revision_id}",
            message_type="grid_terminal",
            text=(
                f"Grid terminal: {action['boundary']} boundary at {action['observed_price']}; "
                f"geometry frozen, entries canceled, {position_text}, paused; await Park."
            ),
            binding={"strategy_session_id": self.session_id, "strategy_revision_id": self.revision_id},
        )
