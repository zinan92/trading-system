"""Canonical instrument and precision vocabulary."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType


class ContractType(str, Enum):
    """Supported canonical contract families."""

    LINEAR_PERPETUAL = "linear_perpetual"


class MarginMode(str, Enum):
    """Canonical margin modes."""

    CROSS = "cross"
    ISOLATED = "isolated"


@dataclass(frozen=True)
class PriceRule:
    """A price precision rule expressed independently of a Broker wire format."""

    max_significant_figures: int = 5
    max_decimal_places: int = 0

    def __post_init__(self) -> None:
        if self.max_significant_figures < 1:
            raise ValueError("max_significant_figures must be positive")
        if self.max_decimal_places < 0:
            raise ValueError("max_decimal_places cannot be negative")

    def is_valid(self, value: Decimal) -> bool:
        """Return whether a positive price satisfies this rule."""

        try:
            price = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return False
        if not price.is_finite() or price <= 0:
            return False

        normalized = price.normalize()
        decimal_places = max(0, -normalized.as_tuple().exponent)
        if decimal_places > self.max_decimal_places:
            return False
        if normalized == normalized.to_integral_value():
            return True

        digits = "".join(str(digit) for digit in normalized.as_tuple().digits).lstrip("0")
        digits = digits.rstrip("0")
        return len(digits) <= self.max_significant_figures


@dataclass(frozen=True)
class InstrumentSpec:
    """Canonical trading rules for one instrument."""

    broker_id: str
    canonical_symbol: str
    broker_symbol: str
    asset_index: int
    contract_type: ContractType
    base_currency: str
    quote_currency: str
    collateral_currency: str
    quantity_step: Decimal
    minimum_notional: Decimal
    price_rule: PriceRule
    max_leverage: Decimal
    margin_mode: MarginMode
    metadata_revision: str

    def __post_init__(self) -> None:
        for name in (
            "broker_id",
            "canonical_symbol",
            "broker_symbol",
            "base_currency",
            "quote_currency",
            "collateral_currency",
            "metadata_revision",
        ):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if self.asset_index < 0:
            raise ValueError("asset_index cannot be negative")
        if self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive")
        if self.minimum_notional <= 0:
            raise ValueError("minimum_notional must be positive")
        if self.max_leverage <= 0:
            raise ValueError("max_leverage must be positive")

    def minimum_quantity_for_price(self, price: Decimal) -> Decimal:
        """Derive the smallest step quantity meeting the minimum notional."""

        if not self.price_rule.is_valid(price):
            raise ValueError("price does not satisfy the instrument price rule")
        units = (self.minimum_notional / price / self.quantity_step).to_integral_value(
            rounding=ROUND_CEILING
        )
        return units * self.quantity_step


class InstrumentCatalog:
    """Immutable lookup of canonical instruments by both public identities."""

    def __init__(self, instruments: Mapping[str, InstrumentSpec]) -> None:
        if not instruments:
            raise ValueError("instrument catalog cannot be empty")
        self._by_canonical = MappingProxyType(dict(instruments))
        self._by_broker = MappingProxyType(
            {instrument.broker_symbol: instrument for instrument in instruments.values()}
        )

    def get(self, canonical_symbol: str) -> InstrumentSpec:
        return self._by_canonical[canonical_symbol]

    def get_by_broker_symbol(self, broker_symbol: str) -> InstrumentSpec:
        return self._by_broker[broker_symbol]

    @property
    def instruments(self) -> Mapping[str, InstrumentSpec]:
        return self._by_canonical
