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
from .credentials import LocalFileSecretProvider
from .external import (
    HyperliquidTestnetBackendConfig,
    NautilusHyperliquidTestnetBackend,
    default_testnet_capabilities,
)
from .testnet_proof import TestnetProofPlan, TestnetProofResult, run_testnet_lifecycle
from .fees import HyperliquidFeeAdapter, HyperliquidRuntimeFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .market_data import HyperliquidMarketDataAdapter, HyperliquidRuntimeReadAdapter
from .orders import (
    ExternalOrderLifecyclePort,
    HyperliquidExternalOrderAdapter,
    HyperliquidOrderAdapter,
    HyperliquidRuntimeOrderAdapter,
)
from .read_facts import HyperliquidExternalFactAdapter
from .protection import (
    default_external_testnet_protection_capabilities,
    HyperliquidProtectionAdapter,
    HyperliquidRuntimeProtectionAdapter,
    ProtectionLifecycleState,
    ProtectionLifecycleStatus,
    ProtectionRetryPlan,
)
from .profile import (
    HYPERLIQUID_TESTNET_PROFILE,
    build_hyperliquid_testnet_host,
    build_hyperliquid_testnet_order_adapter,
    resolve_external_profile,
)
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
    "HyperliquidExternalOrderAdapter",
    "ExternalOrderLifecyclePort",
    "HyperliquidExternalFactAdapter",
    "HyperliquidProtectionAdapter",
    "default_external_testnet_protection_capabilities",
    "HyperliquidRuntimeProtectionAdapter",
    "ProtectionLifecycleState",
    "ProtectionLifecycleStatus",
    "ProtectionRetryPlan",
    "HYPERLIQUID_TESTNET_PROFILE",
    "build_hyperliquid_testnet_host",
    "build_hyperliquid_testnet_order_adapter",
    "resolve_external_profile",
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
    "LocalFileSecretProvider",
    "HyperliquidTestnetBackendConfig",
    "NautilusHyperliquidTestnetBackend",
    "default_testnet_capabilities",
    "TestnetProofPlan",
    "TestnetProofResult",
    "run_testnet_lifecycle",
]
