"""Fixture-backed Hyperliquid default-perps canonical mappings."""

from .account import HyperliquidAccountAdapter
from .bridge import (
    NautilusAdapterMetadata,
    NautilusBridgeConfig,
    NautilusBridgeReceipt,
    NautilusCompatibilityError,
    NautilusHyperliquidBridge,
)
from .errors import UnknownInstrumentError, UnsupportedProductError
from .fees import HyperliquidFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .market_data import HyperliquidMarketDataAdapter
from .orders import HyperliquidOrderAdapter
from .protection import HyperliquidProtectionAdapter

__all__ = [
    "HyperliquidAccountAdapter",
    "HyperliquidFeeAdapter",
    "HyperliquidInstrumentAdapter",
    "HyperliquidMarketDataAdapter",
    "HyperliquidOrderAdapter",
    "HyperliquidProtectionAdapter",
    "NautilusAdapterMetadata",
    "NautilusBridgeConfig",
    "NautilusBridgeReceipt",
    "NautilusCompatibilityError",
    "NautilusHyperliquidBridge",
    "UnknownInstrumentError",
    "UnsupportedProductError",
]
