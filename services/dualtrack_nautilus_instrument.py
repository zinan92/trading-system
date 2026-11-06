from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


# Fields a venue may genuinely not have (OKX publishes no minimum notional and
# no maximum price). Only these, and only when the definition names a reason
# under ``not_applicable_fields``; every other field stays required.
NOT_APPLICABLE_ALLOWED = frozenset({"min_notional", "max_price"})


def not_applicable_fields(definition: dict[str, Any]) -> frozenset[str]:
    declared = definition.get("not_applicable_fields") or {}
    if not isinstance(declared, dict):
        raise ValueError("not_applicable_fields must be a mapping of field to reason")
    unknown = sorted(set(declared) - NOT_APPLICABLE_ALLOWED)
    if unknown:
        raise ValueError(f"fields cannot be declared not applicable: {', '.join(unknown)}")
    for field, reason in declared.items():
        if not str(reason or "").strip():
            raise ValueError(f"not_applicable field {field} needs a reason")
        if definition.get(field) not in (None, ""):
            raise ValueError(f"not_applicable field {field} must be null, got {definition.get(field)!r}")
    return frozenset(declared)


PAPER_ASSET_CLASSES = ("commodity",)
# The backtest entry (#1337) also replays a crypto perpetual. Paper and every other caller keep the
# default, commodity only; only the backtest replay passes this wider set.
BACKTEST_ASSET_CLASSES = ("commodity", "crypto")


def validate_instrument_definition(definition: dict[str, Any], *, asset_classes: tuple[str, ...] = PAPER_ASSET_CLASSES) -> dict[str, Any]:
    absent = not_applicable_fields(definition)
    required = {
        "instrument_id",
        "venue",
        "symbol",
        "asset_class",
        "market_type",
        "contract_type",
        "status",
        "base_currency",
        "quote_currency",
        "settlement_currency",
        "is_inverse",
        "price_precision",
        "price_increment",
        "min_price",
        "max_price",
        "size_precision",
        "size_increment",
        "min_quantity",
        "max_quantity",
        "min_notional",
        "margin_init_rate",
        "margin_maint_rate",
        "contract_multiplier",
        "provider",
        "source_mode",
        "execution_venue",
        "is_synthetic",
        "upstream_server_time",
    }
    missing = sorted(key for key in required - absent if definition.get(key) in (None, ""))
    if missing:
        raise ValueError(f"instrument definition missing required fields: {', '.join(missing)}")
    if definition.get("schema_version") != "instrument-definition-v1":
        raise ValueError("unsupported instrument definition schema")
    if definition.get("execution_venue") is not True:
        raise ValueError("instrument definition is not from an execution venue")
    if definition.get("is_synthetic") is not False:
        raise ValueError("synthetic instrument definitions are forbidden")
    if definition.get("served_from") != "upstream":
        raise ValueError("instrument definition must be served from upstream")
    if str(definition.get("status")).upper() != "TRADING":
        raise ValueError("instrument is not in TRADING status")
    if str(definition.get("asset_class")).lower() not in asset_classes:
        raise ValueError(f"instrument asset class must be one of {', '.join(asset_classes)}")
    if bool(definition.get("is_inverse")):
        raise ValueError("inverse contracts are not supported by the GOLD spike")
    derived_fields = definition.get("derived_fields") or {}
    if derived_fields.get("contract_multiplier") != "usd_m_notional_equals_price_times_quantity":
        raise ValueError("contract multiplier provenance is missing or unsupported")
    for field in (
        "price_increment",
        "size_increment",
        "min_quantity",
        "max_quantity",
        "min_notional",
        "margin_init_rate",
        "margin_maint_rate",
        "contract_multiplier",
    ):
        if field in absent:
            continue
        try:
            value = Decimal(str(definition[field]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"instrument field {field} is not numeric") from exc
        if value <= 0:
            raise ValueError(f"instrument field {field} must be positive")
    return dict(definition)


def build_nautilus_instrument(
    definition: dict[str, Any],
    *,
    maker_fee_rate: str | Decimal | None = None,
    taker_fee_rate: str | Decimal | None = None,
    asset_classes: tuple[str, ...] = PAPER_ASSET_CLASSES,
):
    definition = validate_instrument_definition(definition, asset_classes=asset_classes)
    maker = _resolve_fee_rate(definition.get("maker_fee_rate"), maker_fee_rate, "maker")
    taker = _resolve_fee_rate(definition.get("taker_fee_rate"), taker_fee_rate, "taker")

    try:
        from nautilus_trader.model.currencies import Currency
        from nautilus_trader.model.enums import AssetClass
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from nautilus_trader.model.instruments import PerpetualContract
        from nautilus_trader.model.objects import Money, Price, Quantity
    except ImportError as exc:
        raise RuntimeError("NautilusTrader is not installed in this environment") from exc

    symbol = str(definition["symbol"])
    venue = str(definition["venue"])
    timestamp_ns = int(definition["upstream_server_time"]) * 1_000_000
    quote_currency = Currency.from_str(str(definition["quote_currency"]))
    return PerpetualContract(
        instrument_id=InstrumentId.from_str(f"{symbol}-PERP.{venue}"),
        raw_symbol=Symbol(symbol),
        underlying=str(definition["base_currency"]),
        asset_class=AssetClass.CRYPTOCURRENCY if str(definition["asset_class"]).lower() == "crypto" else AssetClass.COMMODITY,
        base_currency=Currency.from_str(str(definition["base_currency"])),
        quote_currency=quote_currency,
        settlement_currency=Currency.from_str(str(definition["settlement_currency"])),
        is_inverse=False,
        price_precision=int(definition["price_precision"]),
        size_precision=int(definition["size_precision"]),
        price_increment=Price.from_str(str(definition["price_increment"])),
        size_increment=Quantity.from_str(str(definition["size_increment"])),
        multiplier=Quantity.from_str(str(definition["contract_multiplier"])),
        min_quantity=Quantity.from_str(str(definition["min_quantity"])),
        max_quantity=Quantity.from_str(str(definition["max_quantity"])),
        min_notional=(
            None
            if definition.get("min_notional") in (None, "")
            else Money.from_str(f"{definition['min_notional']} {definition['quote_currency']}")
        ),
        min_price=Price.from_str(str(definition["min_price"])),
        max_price=(
            None
            if definition.get("max_price") in (None, "")
            else Price.from_str(str(definition["max_price"]))
        ),
        margin_init=Decimal(str(definition["margin_init_rate"])),
        margin_maint=Decimal(str(definition["margin_maint_rate"])),
        maker_fee=maker,
        taker_fee=taker,
        ts_event=timestamp_ns,
        ts_init=timestamp_ns,
        info={"instrument_definition": definition},
    )


def _resolve_fee_rate(
    source_value: Any,
    explicit_value: str | Decimal | None,
    label: str,
) -> Decimal:
    value = source_value if source_value not in (None, "") else explicit_value
    if value in (None, ""):
        raise ValueError(f"{label} fee rate is required explicitly")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} fee rate is not numeric") from exc
    if rate < 0:
        raise ValueError(f"{label} fee rate must not be negative")
    return rate
