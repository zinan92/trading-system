"""Hyperliquid account-state mapping for default perpetuals."""

from collections.abc import Mapping, Sequence
from decimal import Decimal

from ...account import AccountSnapshot, LiquidationFact, PositionFact, PositionSide
from ...fees import FillFact
from ...instruments import MarginMode
from ...models import Provenance
from .errors import UnknownInstrumentError
from .instruments import HyperliquidInstrumentAdapter


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "None" or value == "":
        return None
    return Decimal(str(value))


class HyperliquidAccountAdapter:
    """Maps official Hyperliquid clearinghouse facts to canonical account facts."""

    name = "account"

    def __init__(self, instruments: HyperliquidInstrumentAdapter) -> None:
        self._instruments = instruments

    def map_clearinghouse(
        self,
        *,
        account_address: str,
        raw: Mapping[str, object],
        provenance: Provenance,
        realized_pnl: Decimal | None = None,
    ) -> AccountSnapshot:
        summary = raw.get("marginSummary") or raw.get("crossMarginSummary") or {}
        if not isinstance(summary, Mapping):
            raise TypeError("Hyperliquid margin summary must be a mapping")
        raw_positions = raw.get("assetPositions") or []
        if not isinstance(raw_positions, list):
            raise TypeError("Hyperliquid assetPositions must be a list")

        positions = tuple(
            self._map_position(item, provenance) for item in raw_positions
        )
        unrealized_values = [position.unrealized_pnl for position in positions]
        if not positions:
            unrealized_pnl = Decimal(0)
        elif all(value is not None for value in unrealized_values):
            unrealized_pnl = sum(value for value in unrealized_values if value is not None)
        else:
            unrealized_pnl = None

        return AccountSnapshot(
            broker_id="hyperliquid",
            account_address=account_address,
            equity=_optional_decimal(summary.get("accountValue")),
            balance=_optional_decimal(summary.get("totalRawUsd")),
            withdrawable=_optional_decimal(raw.get("withdrawable")),
            margin_used=_optional_decimal(summary.get("totalMarginUsed")),
            exposure=_optional_decimal(summary.get("totalNtlPos")),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            positions=positions,
            provenance=provenance,
        )

    def _map_position(self, raw: object, provenance: Provenance) -> PositionFact:
        if not isinstance(raw, Mapping):
            raise TypeError("Hyperliquid asset position must be a mapping")
        position = raw.get("position") or raw
        if not isinstance(position, Mapping):
            raise TypeError("Hyperliquid position must be a mapping")
        broker_symbol = str(position.get("coin") or "")
        try:
            instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        except KeyError as exc:
            raise UnknownInstrumentError(broker_symbol) from exc
        signed_quantity = Decimal(str(position.get("szi", "0")))
        leverage = position.get("leverage") or {}
        if not isinstance(leverage, Mapping):
            raise TypeError("Hyperliquid leverage must be a mapping")
        leverage_type = str(leverage.get("type") or "")
        margin_mode = {
            "cross": MarginMode.CROSS,
            "isolated": MarginMode.ISOLATED,
        }.get(leverage_type)
        side = (
            PositionSide.LONG
            if signed_quantity > 0
            else PositionSide.SHORT
            if signed_quantity < 0
            else None
        )
        return PositionFact(
            instrument_id=instrument.canonical_symbol,
            signed_quantity=signed_quantity,
            side=side,
            entry_price=_optional_decimal(position.get("entryPx")),
            leverage=_optional_decimal(leverage.get("value")),
            margin_mode=margin_mode,
            liquidation_price=_optional_decimal(position.get("liquidationPx")),
            margin_used=_optional_decimal(position.get("marginUsed")),
            position_value=_optional_decimal(position.get("positionValue")),
            unrealized_pnl=_optional_decimal(position.get("unrealizedPnl")),
            provenance=provenance,
        )

    def map_liquidation(
        self,
        raw: Mapping[str, object],
        provenance: Provenance,
    ) -> LiquidationFact:
        return LiquidationFact(
            liquidation_id=str(raw.get("lid") or raw.get("id") or ""),
            broker_id="hyperliquid",
            account_address=str(raw.get("liquidated_user") or ""),
            liquidator=str(raw["liquidator"]) if raw.get("liquidator") else None,
            notional=_optional_decimal(raw.get("liquidated_ntl_pos")),
            account_value=_optional_decimal(raw.get("liquidated_account_value")),
            method=str(raw["method"]) if raw.get("method") else None,
            liquidation_fee=None,
            occurred_at=provenance.received_at,
            provenance=provenance,
        )

    @staticmethod
    def realized_pnl_from_fills(fills: Sequence[FillFact]) -> Decimal:
        """Aggregate unique fill closed-PnL facts without double counting."""

        seen: set[str] = set()
        total = Decimal(0)
        for fill in fills:
            if fill.fill_id in seen:
                continue
            seen.add(fill.fill_id)
            total += fill.closed_pnl
        return total
