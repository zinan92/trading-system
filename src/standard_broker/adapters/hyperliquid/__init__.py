"""Fixture-backed Hyperliquid default-perps canonical mappings."""

from .account import HyperliquidAccountAdapter
from .bridge import (
    NautilusAdapterMetadata,
    NautilusBridgeConfig,
    NautilusBridgeReceipt,
    NautilusCompatibilityError,
    NautilusHyperliquidBridge,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    NautilusRuntimeError,
    NautilusRuntimeHealth,
    NautilusRuntimeReceipt,
    NautilusRuntimeState,
)
from .errors import UnknownInstrumentError, UnsupportedProductError
from .fees import HyperliquidFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .market_data import HyperliquidMarketDataAdapter, HyperliquidRuntimeReadAdapter
from .orders import HyperliquidOrderAdapter
from .protection import HyperliquidProtectionAdapter

__all__ = [
    "HyperliquidAccountAdapter",
    "HyperliquidFeeAdapter",
    "HyperliquidInstrumentAdapter",
    "HyperliquidMarketDataAdapter",
    "HyperliquidRuntimeReadAdapter",
    "HyperliquidOrderAdapter",
    "HyperliquidProtectionAdapter",
    "NautilusAdapterMetadata",
    "NautilusBridgeConfig",
    "NautilusBridgeReceipt",
    "NautilusCompatibilityError",
    "NautilusHyperliquidBridge",
    "NautilusHyperliquidRuntime",
    "NautilusRuntimeConfig",
    "NautilusRuntimeError",
    "NautilusRuntimeHealth",
    "NautilusRuntimeReceipt",
    "NautilusRuntimeState",
    "UnknownInstrumentError",
    "UnsupportedProductError",
]
