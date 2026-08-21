"""Hyperliquid TP/SL grouping and partial-fill capability mapping."""

from dataclasses import dataclass, replace
from decimal import Decimal

from ...errors import BrokerCapabilityError
from ...orders import OrderSide
from ...protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionQuantityPolicy,
    ProtectionType,
    TriggerReference,
)


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
