"""Broker adapter registry and composition roots.

This module performs assembly only. Venue adapters retain network, signing,
order lifecycle, risk, protection, and reconciliation behavior. Imports of
venue modules stay inside builders so the legacy ``broker_adapter`` facade can
delegate here without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from services.broker_port import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerExecutionPort,
    BrokerReconciliationPort,
    execution_capabilities_for,
)


BrokerExecutionFactory = Callable[["BrokerBuildContext"], BrokerExecutionPort]
BrokerReconciliationFactory = Callable[["BrokerBuildContext"], BrokerReconciliationPort]


@dataclass(frozen=True)
class BrokerBuildContext:
    output_root: Path
    execution_mode: str
    live_trading_enabled: bool
    broker_config: dict
    auxiliary_config: dict = field(default_factory=dict)
    opener: Any = None
    selection_environment: str = ""

    def __post_init__(self) -> None:
        mode = str(self.execution_mode or "").strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError(f"unknown execution_mode: {self.execution_mode}")
        if not isinstance(self.broker_config, dict):
            raise ValueError("broker_config must be an object")
        if not isinstance(self.auxiliary_config, dict):
            raise ValueError("auxiliary_config must be an object")
        object.__setattr__(self, "output_root", Path(self.output_root))
        object.__setattr__(self, "execution_mode", mode)
        object.__setattr__(self, "broker_config", dict(self.broker_config))
        object.__setattr__(self, "auxiliary_config", dict(self.auxiliary_config))
        object.__setattr__(
            self,
            "selection_environment",
            str(self.selection_environment or "").strip().lower(),
        )

    @property
    def provider(self) -> str:
        return str(self.broker_config.get("provider") or "manual_gateway").strip().lower()

    @property
    def environment(self) -> str:
        if self.selection_environment:
            return self.selection_environment
        configured = str(self.broker_config.get("environment") or "").strip().lower()
        if configured:
            return configured
        if self.execution_mode == "paper":
            return "paper"
        if self.provider == "tiger_openapi":
            return "paper"
        return "live"

    @property
    def key(self) -> BrokerPluginKey:
        return BrokerPluginKey(self.execution_mode, self.provider, self.environment)

    def with_broker_config(self, broker_config: dict) -> BrokerBuildContext:
        return replace(self, broker_config=dict(broker_config))


@dataclass(frozen=True)
class BrokerPluginKey:
    execution_mode: str
    provider: str
    environment: str

    def __post_init__(self) -> None:
        values = {
            "execution_mode": str(self.execution_mode or "").strip().lower(),
            "provider": str(self.provider or "").strip().lower(),
            "environment": str(self.environment or "").strip().lower(),
        }
        if not all(values.values()):
            raise ValueError("broker plugin key fields are required")
        if values["execution_mode"] not in {"paper", "live"}:
            raise ValueError(f"unknown execution_mode: {values['execution_mode']}")
        for key, value in values.items():
            object.__setattr__(self, key, value)


@dataclass(frozen=True)
class BrokerPlugin:
    key: BrokerPluginKey
    execution_factory: BrokerExecutionFactory
    reconciliation_factory: BrokerReconciliationFactory | None = None
    capabilities: BrokerCapabilities = field(
        default_factory=lambda: BrokerCapabilities(
            frozenset({BrokerCapability.PREFLIGHT, BrokerCapability.SUBMIT_ORDER})
        )
    )

    def __post_init__(self) -> None:
        if not callable(self.execution_factory):
            raise ValueError("broker execution_factory must be callable")
        if self.reconciliation_factory is not None and not callable(self.reconciliation_factory):
            raise ValueError("broker reconciliation_factory must be callable")
        has_reconciliation = self.capabilities.supports(BrokerCapability.RECONCILIATION)
        if has_reconciliation != (self.reconciliation_factory is not None):
            raise ValueError("broker reconciliation capability must match its factory")


class BrokerPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[BrokerPluginKey, BrokerPlugin] = {}

    def register(self, plugin: BrokerPlugin) -> None:
        if plugin.key in self._plugins:
            raise ValueError(f"duplicate broker plugin: {plugin.key}")
        self._plugins[plugin.key] = plugin

    def resolve(self, context: BrokerBuildContext) -> BrokerPlugin:
        key = context.key
        candidates = (
            key,
            BrokerPluginKey(key.execution_mode, key.provider, "*"),
            BrokerPluginKey(key.execution_mode, "*", "*"),
        )
        for candidate in candidates:
            plugin = self._plugins.get(candidate)
            if plugin is not None:
                return plugin
        raise ValueError(
            "unsupported broker plugin: "
            f"mode={key.execution_mode}, provider={key.provider}, environment={key.environment}"
        )

    def build_execution(self, context: BrokerBuildContext) -> BrokerExecutionPort:
        plugin = self.resolve(context)
        execution = plugin.execution_factory(context)
        if not isinstance(execution, BrokerExecutionPort):
            raise TypeError(
                f"broker plugin {plugin.key} did not build a BrokerExecutionPort"
            )
        return execution

    def build_reconciliation(
        self,
        context: BrokerBuildContext,
        *,
        execution_port: BrokerExecutionPort | None = None,
    ) -> BrokerReconciliationPort:
        plugin = self.resolve(context)
        if plugin.reconciliation_factory is None:
            raise RuntimeError(
                f"broker provider {context.provider} does not support reconciliation"
            )
        execution = execution_port or self.build_execution(context)
        effective_config = getattr(execution, "broker_config", context.broker_config)
        effective_context = context.with_broker_config(
            effective_config if isinstance(effective_config, dict) else context.broker_config
        )
        reconciliation = plugin.reconciliation_factory(effective_context)
        if not isinstance(reconciliation, BrokerReconciliationPort):
            raise TypeError(
                f"broker plugin {plugin.key} did not build a BrokerReconciliationPort"
            )
        return reconciliation


def resolve_broker_profile_config(config: dict) -> dict:
    broker_config = dict(config.get("broker", {}) or {})
    profile_name = str(
        config.get("broker_profile")
        or broker_config.get("broker_profile")
        or broker_config.get("profile")
        or ""
    ).strip()
    if not profile_name:
        return broker_config
    profiles = config.get("broker_profiles", {}) or {}
    if profile_name not in profiles:
        raise ValueError(f"unknown broker profile: {profile_name}")
    overrides = {
        key: value
        for key, value in broker_config.items()
        if key not in {"profile", "broker_profile"}
    }
    return {**dict(profiles[profile_name]), **overrides, "profile": profile_name}


def _paper_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.broker_adapter import PaperBrokerAdapter

    return PaperBrokerAdapter(context.output_root)


def _legacy_live_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.broker_adapter import LiveBrokerAdapter

    return LiveBrokerAdapter(
        context.output_root,
        context.live_trading_enabled,
        context.broker_config,
        opener=context.opener,
    )


def _unarmed_unknown_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.broker_adapter import LiveBrokerAdapter

    safe_config = {**context.broker_config, "dry_run": True}
    return LiveBrokerAdapter(
        context.output_root,
        False,
        safe_config,
        opener=context.opener,
    )


def _binance_demo_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.binance_demo_broker_adapter import BinanceDemoBrokerAdapter

    return BinanceDemoBrokerAdapter(
        context.output_root,
        context.broker_config,
        context.auxiliary_config,
        opener=context.opener,
    )


def _binance_testnet_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.binance_usdm_testnet_broker_adapter import BinanceUsdmTestnetBrokerAdapter

    return BinanceUsdmTestnetBrokerAdapter(
        context.output_root,
        context.broker_config,
        context.auxiliary_config,
        opener=context.opener,
    )


def _tiger_paper_execution(context: BrokerBuildContext) -> BrokerExecutionPort:
    from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter

    return TigerOpenApiPaperBrokerAdapter(context.output_root, context.broker_config)


def _binance_reconciliation(context: BrokerBuildContext) -> BrokerReconciliationPort:
    from services.live_reconciliation import LiveBrokerReconciliation

    return LiveBrokerReconciliation(
        context.output_root,
        context.broker_config,
        opener=context.opener,
    )


def _tiger_reconciliation(context: BrokerBuildContext) -> BrokerReconciliationPort:
    from services.tiger_openapi_reconciliation import TigerOpenApiPaperReconciliation

    return TigerOpenApiPaperReconciliation(
        context.output_root,
        context.broker_config,
    )


def _execution_capabilities(provider: str, *, reconciliation: bool = False) -> BrokerCapabilities:
    capabilities = execution_capabilities_for(provider=provider, adapter_name="registered")
    return (
        capabilities.merged(BrokerCapability.RECONCILIATION)
        if reconciliation
        else capabilities
    )


def default_broker_plugin_registry() -> BrokerPluginRegistry:
    registry = BrokerPluginRegistry()
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("paper", "*", "*"),
            execution_factory=_paper_execution,
        )
    )
    for environment, execution_factory in (
        ("demo", _binance_demo_execution),
        ("testnet", _binance_testnet_execution),
    ):
        registry.register(
            BrokerPlugin(
                BrokerPluginKey("live", "binance_usdm", environment),
                execution_factory=execution_factory,
                reconciliation_factory=_binance_reconciliation,
                capabilities=_execution_capabilities("binance_usdm", reconciliation=True),
            )
        )
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("live", "binance_usdm", "*"),
            execution_factory=_legacy_live_execution,
            reconciliation_factory=_binance_reconciliation,
            capabilities=_execution_capabilities("binance_usdm", reconciliation=True),
        )
    )
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("live", "tiger_openapi", "paper"),
            execution_factory=_tiger_paper_execution,
            reconciliation_factory=_tiger_reconciliation,
            capabilities=_execution_capabilities("tiger_openapi", reconciliation=True),
        )
    )
    for provider in ("manual_gateway", "mt5_file_bridge", "oanda_rest"):
        registry.register(
            BrokerPlugin(
                BrokerPluginKey("live", provider, "*"),
                execution_factory=_legacy_live_execution,
                capabilities=_execution_capabilities(provider),
            )
        )
    registry.register(
        BrokerPlugin(
            BrokerPluginKey("live", "*", "*"),
            execution_factory=_unarmed_unknown_execution,
        )
    )
    return registry


def build_broker_execution_port(
    context: BrokerBuildContext,
    *,
    registry: BrokerPluginRegistry | None = None,
) -> BrokerExecutionPort:
    return (registry or default_broker_plugin_registry()).build_execution(context)


def build_configured_live_broker_execution_port(
    output_root: Path,
    live_trading_enabled: bool,
    broker_config: dict,
    *,
    opener: Any = None,
    registry: BrokerPluginRegistry | None = None,
) -> BrokerExecutionPort:
    """Compatibility-preserving composition for the configured live path.

    The historical configured path selects Tiger paper explicitly but keeps a
    Binance config labelled demo/testnet on the activation-gated base adapter.
    Dedicated demo/testnet runners use ``BrokerBuildContext`` directly.
    """

    provider = str(broker_config.get("provider") or "").strip().lower()
    environment = str(broker_config.get("environment") or "").strip().lower()
    selection_environment = (
        "paper"
        if provider == "tiger_openapi" and environment == "paper"
        else "standard"
    )
    return build_broker_execution_port(
        BrokerBuildContext(
            output_root=output_root,
            execution_mode="live",
            live_trading_enabled=live_trading_enabled,
            broker_config=broker_config,
            opener=opener,
            selection_environment=selection_environment,
        ),
        registry=registry,
    )


def build_broker_reconciliation_port(
    context: BrokerBuildContext,
    *,
    execution_port: BrokerExecutionPort | None = None,
    registry: BrokerPluginRegistry | None = None,
) -> BrokerReconciliationPort:
    return (registry or default_broker_plugin_registry()).build_reconciliation(
        context,
        execution_port=execution_port,
    )
