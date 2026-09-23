"""Hyperliquid default-perps metadata to canonical instrument mapping."""

from collections.abc import Mapping
from decimal import Decimal

from ...instruments import (
    ContractType,
    InstrumentCatalog,
    InstrumentSpec,
    MarginMode,
    PriceRule,
)
from ...orders import OrderType
from .errors import UnsupportedProductError


class HyperliquidInstrumentAdapter:
    """Maps the default Hyperliquid perp universe into canonical instruments."""

    def __init__(self, catalog: InstrumentCatalog) -> None:
        self._catalog = catalog

    @classmethod
    def from_meta(
        cls,
        meta: Mapping[str, object],
        *,
        revision: str,
    ) -> "HyperliquidInstrumentAdapter":
        universe = meta.get("universe")
        if not isinstance(universe, list) or not universe:
            raise ValueError("Hyperliquid perp metadata must contain a non-empty universe")

        instruments: dict[str, InstrumentSpec] = {}
        for position, raw in enumerate(universe):
            if not isinstance(raw, Mapping):
                raise TypeError("Hyperliquid instrument metadata must be mappings")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError("Hyperliquid instrument name is required")
            if ":" in name:
                raise UnsupportedProductError(
                    f"deferred Hyperliquid product scope in instrument {name}"
                )
            if "szDecimals" not in raw or "maxLeverage" not in raw:
                raise ValueError(f"incomplete Hyperliquid metadata for {name}")

            sz_decimals = int(raw["szDecimals"])
            if sz_decimals < 0 or sz_decimals > 6:
                raise ValueError(f"invalid size precision for {name}")
            asset_index = int(raw.get("index", position))
            margin_mode = (
                MarginMode.ISOLATED
                if bool(raw.get("onlyIsolated"))
                or str(raw.get("marginMode") or "") in {"strictIsolated", "noCross"}
                else MarginMode.CROSS
            )
            instrument = InstrumentSpec(
                broker_id="hyperliquid",
                canonical_symbol=f"{name}-USD-PERP",
                broker_symbol=name,
                asset_index=asset_index,
                contract_type=ContractType.LINEAR_PERPETUAL,
                base_currency=name,
                quote_currency="USD",
                collateral_currency="USDC",
                quantity_step=Decimal(1).scaleb(-sz_decimals),
                minimum_notional=Decimal(10),
                price_rule=PriceRule(max_decimal_places=6 - sz_decimals),
                max_leverage=Decimal(str(raw["maxLeverage"])),
                margin_mode=margin_mode,
                metadata_revision=revision,
                minimum_quantity=Decimal(1).scaleb(-sz_decimals),
                supported_order_types=(OrderType.LIMIT, OrderType.MARKET),
            )
            if instrument.canonical_symbol in instruments:
                raise ValueError(f"duplicate canonical instrument {instrument.canonical_symbol}")
            instruments[instrument.canonical_symbol] = instrument
        return cls(InstrumentCatalog(instruments))

    @property
    def instruments(self) -> Mapping[str, InstrumentSpec]:
        return self._catalog.instruments

    def get(self, canonical_symbol: str) -> InstrumentSpec:
        return self._catalog.get(canonical_symbol)

    def get_by_broker_symbol(self, broker_symbol: str) -> InstrumentSpec:
        return self._catalog.get_by_broker_symbol(broker_symbol)
