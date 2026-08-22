"""Hyperliquid TP/SL grouping and approved local runtime protection mapping."""

from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock

from ...errors import BrokerCapabilityError, RuntimeBoundaryError
from ...models import BrokerEnvironment
from ...orders import OrderSide
from ...protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionLifecycleState,
    ProtectionLifecycleStatus,
    ProtectionQuantityPolicy,
    ProtectionReceipt,
    ProtectionType,
    TriggerReference,
)
from .bridge import NautilusHyperliquidRuntime, NautilusRuntimeState


@dataclass(frozen=True)
class HyperliquidProtectionLeg:
    side: OrderSide
    tpsl: str
    execution: str
    trigger_price: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    trigger_reference: TriggerReference
    sibling_id: str


@dataclass(frozen=True)
class HyperliquidProtectionRequest:
    grouping: str
    quantity: Decimal
    quantity_policy: ProtectionQuantityPolicy
    legs: tuple[HyperliquidProtectionLeg, ...]


class HyperliquidProtectionAdapter:
    """Builds explicit Hyperliquid protection semantics without network I/O."""

    name = "protection_order"

    def build_group(self, group: ProtectionGroup) -> HyperliquidProtectionRequest:
        legs = tuple(
            self._build_leg(group, leg)
            for leg in (group.take_profit, group.stop_loss)
            if leg is not None
        )
        return HyperliquidProtectionRequest(
            grouping=(
                "positionTpsl"
                if group.quantity_policy is ProtectionQuantityPolicy.POSITION_FOLLOWING
                else "normalTpsl"
            ),
            quantity=group.quantity,
            quantity_policy=group.quantity_policy,
            legs=legs,
        )

    def repair_after_partial_fill(
        self,
        group: ProtectionGroup,
        *,
        filled_quantity: Decimal,
    ) -> HyperliquidProtectionRequest:
        if filled_quantity <= 0 or filled_quantity > group.quantity:
            raise ValueError("filled_quantity must be within the protection group quantity")
        if group.quantity_policy is ProtectionQuantityPolicy.FIXED_SIZE:
            raise BrokerCapabilityError(
                "protection_order",
                "partial_fill_repair",
                "partial_fill_protection_gap: fixed parent protection is not automatically repaired",
            )
        return self.build_group(replace(group, quantity=filled_quantity))

    @staticmethod
    def _build_leg(group: ProtectionGroup, leg: ProtectionLeg) -> HyperliquidProtectionLeg:
        if leg.trigger_reference is not TriggerReference.MARK:
            raise ValueError("Hyperliquid protection triggers must use mark price")
        is_long = group.entry_side is OrderSide.BUY
        if leg.protection_type is ProtectionType.TAKE_PROFIT:
            valid_direction = leg.trigger_price > group.entry_price if is_long else leg.trigger_price < group.entry_price
            tpsl = "tp"
        else:
            valid_direction = leg.trigger_price < group.entry_price if is_long else leg.trigger_price > group.entry_price
            tpsl = "sl"
        if not valid_direction:
            raise ValueError("protection trigger direction would activate immediately")
        if leg.execution is ProtectionExecution.LIMIT and leg.limit_price is not None:
            if is_long and leg.limit_price > leg.trigger_price:
                raise ValueError("long exit limit protection cannot be more aggressive than its trigger")
            if not is_long and leg.limit_price < leg.trigger_price:
                raise ValueError("short exit limit protection cannot be more aggressive than its trigger")
        return HyperliquidProtectionLeg(
            side=OrderSide.SELL if is_long else OrderSide.BUY,
            tpsl=tpsl,
            execution=leg.execution.value,
            trigger_price=leg.trigger_price,
            limit_price=leg.limit_price,
            reduce_only=True,
            trigger_reference=leg.trigger_reference,
            sibling_id=group.protection_id,
        )


class HyperliquidRuntimeProtectionAdapter:
    """Submit canonical protection groups through an approved local runtime."""

    name = "hyperliquid_runtime_protection_order"

    def __init__(self, *, runtime: object) -> None:
        if not isinstance(runtime, NautilusHyperliquidRuntime):
            raise RuntimeBoundaryError(
                "runtime_type_invalid",
                "protection adapter requires the canonical Hyperliquid runtime",
            )
        if runtime.state is not NautilusRuntimeState.READY:
            raise RuntimeBoundaryError(
                "runtime_not_ready",
                "protection adapter requires an approved ready runtime",
            )
        session = getattr(runtime, "session", None)
        if session is None or getattr(session, "broker_id", "") != "hyperliquid":
            raise RuntimeBoundaryError(
                "broker_mismatch",
                "Hyperliquid protection adapter requires the Hyperliquid Broker",
            )
        if session.environment not in {BrokerEnvironment.PAPER, BrokerEnvironment.TESTNET}:
            raise RuntimeBoundaryError(
                "protection_runtime_environment_unsupported",
                "protection lifecycle supports Paper and approved Testnet only",
            )
        if session.environment is BrokerEnvironment.TESTNET:
            runtime.preflight(
                required_operations={
                    "order_execution": {
                        "submit",
                        "cancel",
                        "replace",
                        "query",
                        "open_orders",
                    },
                    "protection_order": {"submit"},
                }
            )
        self._runtime = runtime
        self._mapper = HyperliquidProtectionAdapter()
        self._statuses: dict[str, ProtectionLifecycleStatus] = {}
        self._groups: dict[str, ProtectionGroup] = {}
        self._lock = RLock()

    def submit(self, group: ProtectionGroup) -> ProtectionReceipt:
        return self._run(group, operation="submit")

    def replace(self, group: ProtectionGroup) -> ProtectionReceipt:
        return self._run(group, operation="replace")

    def cancel(self, group: ProtectionGroup) -> ProtectionReceipt:
        return self._run(group, operation="cancel")

    def reconcile_position_coverage(
        self,
        group: ProtectionGroup,
        *,
        owned_quantity: Decimal,
    ) -> ProtectionReceipt | ProtectionLifecycleStatus:
        if not owned_quantity.is_finite() or owned_quantity < 0:
            raise ValueError("owned_quantity must be finite and non-negative")
        if owned_quantity == 0:
            return self.cancel(group)
        if owned_quantity != group.quantity:
            if group.quantity_policy is ProtectionQuantityPolicy.FIXED_SIZE:
                raise BrokerCapabilityError(
                    "protection_order",
                    "position_coverage",
                    "partial_fill_protection_gap: fixed-size protection cannot cover the residual position",
                )
            return self.replace(replace(group, quantity=owned_quantity))
        status = self._statuses.get(group.protection_id)
        if status is None or status.state not in {
            ProtectionLifecycleState.SUBMITTED,
            ProtectionLifecycleState.ACTIVE,
        }:
            raise BrokerCapabilityError(
                "protection_order",
                "position_coverage",
                "protection coverage is not active",
            )
        return status

    def status(self, protection_id: str) -> ProtectionLifecycleStatus:
        return self._statuses.get(
            protection_id,
            ProtectionLifecycleStatus(
                protection_id=protection_id,
                state=ProtectionLifecycleState.UNKNOWN,
                reason=None,
                attempts=0,
            ),
        )

    def _run(self, group: ProtectionGroup, *, operation: str) -> ProtectionReceipt:
        with self._lock:
            if operation == "cancel":
                self._require("cancel")
                request = {"protectionId": group.protection_id}
            else:
                self._require_group(group, operation)
                request = self._serialize(group)
            runtime_receipt = self._runtime.invoke(
                "protection_order",
                operation,
                request,
            )
            if runtime_receipt.accepted is not True:
                self._statuses[group.protection_id] = ProtectionLifecycleStatus(
                    protection_id=group.protection_id,
                    state=ProtectionLifecycleState.FROZEN,
                    reason="protection_receipt_not_accepted",
                    attempts=1,
                )
                raise BrokerCapabilityError(
                    "protection_order",
                    operation,
                    "protection_receipt_not_accepted",
                )
            state = (
                ProtectionLifecycleState.CANCELED
                if operation == "cancel"
                else ProtectionLifecycleState.SUBMITTED
            )
            self._statuses[group.protection_id] = ProtectionLifecycleStatus(
                protection_id=group.protection_id,
                state=state,
                reason=None,
                attempts=0,
            )
            self._groups[group.protection_id] = group
            return ProtectionReceipt(
                protection_id=group.protection_id,
                parent_order_id=group.parent_order_id,
                operation=operation,
                accepted=runtime_receipt.accepted,
                broker_id=runtime_receipt.broker_id,
                environment=runtime_receipt.environment,
                provenance=runtime_receipt.provenance,
                account_address=self._runtime.session.account.address,
                lifecycle_id=self._runtime.session.lifecycle_id,
                release_sha=self._runtime._config.expected_release_sha,
            )

    def _require_group(self, group: ProtectionGroup, operation: str) -> None:
        self._require(operation)
        for required in ("reduce_only", "mark_price_trigger"):
            self._require(required)
        if group.take_profit is not None and group.stop_loss is not None:
            for required in ("grouped_tp_sl", "sibling_cancellation"):
                self._require(required)
        if group.quantity_policy is ProtectionQuantityPolicy.POSITION_FOLLOWING:
            for required in ("position_following", "position_level_tpsl"):
                self._require(required)
        else:
            self._require("fixed_size")
        for leg in (group.take_profit, group.stop_loss):
            if leg is not None:
                self._require(f"{leg.protection_type.value}_{leg.execution.value}")

    def _require(self, operation: str) -> None:
        self._runtime.session.capabilities.require("protection_order", operation)

    def _serialize(self, group: ProtectionGroup) -> dict[str, object]:
        request = self._mapper.build_group(group)
        return {
            "protectionId": group.protection_id,
            "parentOrderId": group.parent_order_id,
            "instrumentId": group.instrument_id,
            "grouping": request.grouping,
            "quantity": str(request.quantity),
            "quantityPolicy": request.quantity_policy.value,
            "legs": [
                {
                    "side": "B" if leg.side is OrderSide.BUY else "A",
                    "tpsl": leg.tpsl,
                    "execution": leg.execution,
                    "triggerPx": str(leg.trigger_price),
                    "limitPx": str(leg.limit_price) if leg.limit_price is not None else None,
                    "reduceOnly": leg.reduce_only,
                    "triggerReference": leg.trigger_reference.value,
                    "siblingId": leg.sibling_id,
                }
                for leg in request.legs
            ],
        }
