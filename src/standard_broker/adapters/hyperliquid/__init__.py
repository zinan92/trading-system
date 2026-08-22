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
from .protection import HyperliquidProtectionAdapter, HyperliquidRuntimeProtectionAdapter
from .resilience import (
    HyperliquidRuntimeReconciliationAdapter,
    RateLimitError,
    ObservationSource,
    OrderLifecyclePort,
    ReconciliationFacts,
    ReconciliationSnapshot,
    RecoveryDecision,
    RetryPlan,
    RetryPolicy,
    RuntimeConnectionState,
    RuntimeRecoveryError,
    TransportDisposition,
    classify_transport_error,
    plan_retry,
)

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
    "HyperliquidRuntimeProtectionAdapter",
    "HyperliquidRuntimeReconciliationAdapter",
    "RateLimitError",
    "ObservationSource",
    "OrderLifecyclePort",
    "ReconciliationFacts",
    "ReconciliationSnapshot",
    "RecoveryDecision",
    "RetryPlan",
    "RetryPolicy",
    "RuntimeConnectionState",
    "RuntimeRecoveryError",
    "TransportDisposition",
    "classify_transport_error",
    "plan_retry",
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
