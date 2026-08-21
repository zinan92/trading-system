"""Fixture-backed Hyperliquid default-perps canonical mappings."""

from .account import HyperliquidAccountAdapter, HyperliquidRuntimeAccountAdapter
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
from .fees import HyperliquidFeeAdapter, HyperliquidRuntimeFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .market_data import HyperliquidMarketDataAdapter, HyperliquidRuntimeReadAdapter
from .orders import HyperliquidOrderAdapter, HyperliquidRuntimeOrderAdapter
from .protection import HyperliquidProtectionAdapter

__all__ = [
    "HyperliquidAccountAdapter",
    "HyperliquidRuntimeAccountAdapter",
    "HyperliquidFeeAdapter",
    "HyperliquidRuntimeFeeAdapter",
    "HyperliquidInstrumentAdapter",
    "HyperliquidMarketDataAdapter",
    "HyperliquidRuntimeReadAdapter",
    "HyperliquidOrderAdapter",
    "HyperliquidRuntimeOrderAdapter",
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
