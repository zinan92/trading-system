"""Park DCA lifecycle projection and terminal notification adapter."""

from __future__ import annotations

from typing import Any, Mapping

from services.park_strategy_lifecycle import ParkStrategyLifecycleError, ParkStrategyLifecycleLedger
from services.park_telegram_control import ParkTelegramLedger


class ParkDcaLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ParkDcaLifecycle:
    """Project finite DCA entries and one confirmed terminal lifecycle.

    No method submits or cancels an order.  Consumers receive commands and a
    durable terminal action/notification identity to verify before acting.
    """

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
        if normalized.get("strategy_type") != "dca":
            raise ParkDcaLifecycleError("wrong_strategy_type", "Park DCA lifecycle requires a DCA plan")
        self.plan = dict(plan)
        self.normalized = dict(normalized)
        self.session_id = str(self.normalized.get("strategy_session_id") or plan.get("strategy_session_id") or "").strip()
        self.revision_id = str(self.normalized.get("strategy_revision_id") or plan.get("strategy_revision_id") or "").strip()
        self.plan_digest = str(plan.get("plan_digest") or "").strip()
        if not self.session_id or not self.revision_id or not self.plan_digest:
            raise ParkDcaLifecycleError("identity_missing", "DCA plan identity is incomplete")
        receipt = dict(confirmation_receipt or {})
        if (
            receipt.get("execution_authorized") is not True
            or receipt.get("plan_digest") != self.plan_digest
            or receipt.get("strategy_session_id") != self.session_id
            or receipt.get("strategy_revision_id") != self.revision_id
            or receipt.get("start_or_order_submitted") is not False
        ):
            raise ParkDcaLifecycleError("confirmation_required", "DCA lifecycle requires an exact Park confirmation receipt")
        self.lifecycle = ParkStrategyLifecycleLedger(output_root)
        lifecycle_plan = {
            **self.normalized,
            "strategy_session_id": self.session_id,
            "strategy_revision_id": self.revision_id,
            "plan_digest": self.plan_digest,
            "maximum_leverage": (plan.get("risk") or {}).get("effective_leverage"),
            "maximum_acceptable_loss": (plan.get("risk") or {}).get("theoretical_max_loss"),
        }
        self.lifecycle.activate(lifecycle_plan)
        self.telegram = ParkTelegramLedger(output_root, park_user_id=park_user_id, chat_id=chat_id)
        self._entries: list[dict[str, Any]] | None = None

    def entry_commands(self) -> list[dict[str, Any]]:
        if self._entries is not None:
            return [dict(row) for row in self._entries]
        risk = self.plan.get("risk") if isinstance(self.plan.get("risk"), Mapping) else {}
        count = int(risk.get("order_count") or self.normalized.get("order_count") or 1)
        quantity = float(risk.get("per_order_quantity") or 0)
        if count <= 0 or quantity <= 0:
            raise ParkDcaLifecycleError("risk_incomplete", "DCA risk plan has no executable quantity")
        current = float((self.plan.get("market") or {}).get("price") or 0)
        upper = float(self.normalized["upper_price_boundary"])
        lower = float(self.normalized["lower_price_boundary"])
        direction = str(self.normalized["direction"])
        step = (upper - current) / count if direction == "short" else (current - lower) / count
        rows: list[dict[str, Any]] = []
        for index in range(count):
            price = current + step * (index + 1) if direction == "short" else current - step * (index + 1)
            rows.append({
                "command_type": "dca_entry",
                "entry_id": f"{self.revision_id}:entry:{index + 1}",
                "strategy_session_id": self.session_id,
                "strategy_revision_id": self.revision_id,
                "plan_digest": self.plan_digest,
                "direction": direction,
                "price": round(price, 12),
                "quantity": quantity,
                "loop_enabled": False,
                "after_terminal": "cancel_remaining_entries",
            })
        self._entries = rows
        return [dict(row) for row in rows]

    def on_market(self, *, price: float, trusted: bool, fresh: bool) -> dict[str, Any]:
        if not trusted or not fresh:
            raise ParkDcaLifecycleError("market_not_authoritative", "DCA boundary requires trusted fresh market")
        existing = next(
            (row for row in self.lifecycle.rows() if row.get("event") == "terminal_action_plan" and row.get("strategy_session_id") == self.session_id and row.get("strategy_revision_id") == self.revision_id),
            None,
        )
        if existing:
            return {"status": "terminal", "action_plan": dict(existing), "notification": self._notification(existing)}
        upper = float(self.normalized["upper_price_boundary"])
        lower = float(self.normalized["lower_price_boundary"])
        if not (float(price) >= upper or float(price) <= lower):
            return {"status": "active", "entries_frozen": False, "next_action": "monitor_trusted_fresh_market"}
        boundary = "upper" if float(price) >= upper else "lower"
        action = self.lifecycle.boundary_action_plan(
            strategy_session_id=self.session_id,
            strategy_revision_id=self.revision_id,
            boundary=boundary,
            observed_price=float(price),
            trusted_market=trusted,
            fresh_tick=fresh,
        )
        return {"status": "terminal", "action_plan": action, "notification": self._notification(action)}

    def _notification(self, action: Mapping[str, Any]) -> dict[str, Any]:
        return self.telegram.queue_outbound(
            idempotency_key=f"park-dca-terminal:{self.session_id}:{self.revision_id}",
            message_type="dca_terminal",
            text=(
                f"DCA terminal: {action['boundary']} boundary at {action['observed_price']}; "
                "entries frozen/canceled, positions reconcile, paused; await Park."
            ),
            binding={"strategy_session_id": self.session_id, "strategy_revision_id": self.revision_id},
        )
