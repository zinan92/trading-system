"""Provider-neutral Instrument binding facts used at composition boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping


class InstrumentBindingError(ValueError):
    """An Instrument binding is missing or violates canonical rules."""


@dataclass(frozen=True)
class InstrumentBinding:
    instrument_id: str
    broker_symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    contract_multiplier: Decimal
    minimum_quantity: Decimal
    mapping_revision: str
    supported_order_types: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> "InstrumentBinding":
        if not isinstance(value, Mapping):
            raise InstrumentBindingError("instrument_binding_invalid")
        required = (
            "instrument_id",
            "broker_symbol",
            "price_tick",
            "quantity_step",
            "contract_multiplier",
            "minimum_quantity",
            "mapping_revision",
            "supported_order_types",
        )
        missing = [field for field in required if field not in value]
        if missing:
            raise InstrumentBindingError(
                "instrument_binding_fields_missing:" + ",".join(missing)
            )
        order_types = value["supported_order_types"]
        if not isinstance(order_types, (list, tuple, set, frozenset)):
            raise InstrumentBindingError("instrument_order_types_invalid")
        normalized_order_types = tuple(
            sorted(
                {
                    str(item).strip().lower()
                    for item in order_types
                    if str(item).strip()
                }
            )
        )
        if "limit" not in normalized_order_types:
            raise InstrumentBindingError("instrument_limit_order_unsupported")
        return cls(
            instrument_id=_text(value["instrument_id"], "instrument_id"),
            broker_symbol=_text(value["broker_symbol"], "broker_symbol"),
            price_tick=_decimal(value["price_tick"], "price_tick"),
            quantity_step=_decimal(value["quantity_step"], "quantity_step"),
            contract_multiplier=_decimal(value["contract_multiplier"], "contract_multiplier"),
            minimum_quantity=_decimal(value["minimum_quantity"], "minimum_quantity"),
            mapping_revision=_text(value["mapping_revision"], "mapping_revision"),
            supported_order_types=normalized_order_types,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "broker_symbol": self.broker_symbol,
            "price_tick": str(self.price_tick),
            "quantity_step": str(self.quantity_step),
            "contract_multiplier": str(self.contract_multiplier),
            "minimum_quantity": str(self.minimum_quantity),
            "mapping_revision": self.mapping_revision,
            "supported_order_types": list(self.supported_order_types),
        }


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise InstrumentBindingError(f"{field}_missing")
    return result


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InstrumentBindingError(f"{field}_invalid") from exc
    if not result.is_finite() or result <= 0:
        raise InstrumentBindingError(f"{field}_invalid")
    return result
