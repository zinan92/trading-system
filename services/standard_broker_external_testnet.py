"""Preflight-only bridge to the public standard-broker external Testnet host."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from services.broker_port import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerOrderRequest,
    UnsupportedBrokerCapability,
)
from services.market_source_binding import MarketSourceIdentity
from services.instrument_binding import InstrumentBinding, InstrumentBindingError


STANDARD_BROKER_EXTERNAL_RELEASE_SHA = "916b0eb241b50d5f46be08150eb3197996530552"
STANDARD_BROKER_EXTERNAL_PROFILE = "hyperliquid-testnet-default"
STANDARD_BROKER_RUNTIME_ADAPTER_ID = "nautilus-hyperliquid"
STANDARD_BROKER_RUNTIME_VERSION = "1.230.0"
STANDARD_BROKER_RUNTIME_COMMIT = "8160730c7c550480b0a439fb11086a4c4de15f0b"
STANDARD_BROKER_CAPABILITY_REVISION = "hyperliquid-testnet-runtime-v1"
STANDARD_BROKER_EXTERNAL_MARKET_SOURCES = frozenset(
    {"hyperliquid.external_testnet", "nautilus-hyperliquid.testnet"}
)
STANDARD_BROKER_EXTERNAL_OPERATIONS = {
    "market_data": frozenset({"ticker"}),
    "instrument": frozenset({"read"}),
    "account": frozenset({"read", "positions"}),
    "order_execution": frozenset(
        {"submit", "cancel", "replace", "query", "open_orders", "fills"}
    ),
    "fee": frozenset({"read", "schedule", "fill"}),
}
STANDARD_BROKER_PROTECTION_PROFILE = "hyperliquid-testnet-protection-v1"
STANDARD_BROKER_EXTERNAL_PROTECTION_VALUES = {
    "submit": False,
    "cancel": False,
    "replace": False,
    "query": False,
    "retry": False,
    "position_coverage": False,
    "partial_fill_repair": False,
    "reduce_only_close": True,
    "reduce_only": False,
    "mark_price_trigger": False,
    "grouped_tp_sl": False,
    "sibling_cancellation": False,
    "bracket": False,
    "parent_child": False,
    "fixed_size": False,
    "position_following": False,
    "position_level_tpsl": False,
    "take_profit_market": False,
    "take_profit_limit": False,
    "stop_loss_market": False,
    "stop_loss_limit": False,
    "cancel_replace": False,
}
STANDARD_BROKER_EXTERNAL_TESTNET_CAPABILITIES = BrokerCapabilities(
    frozenset({BrokerCapability.PREFLIGHT})
)


class StandardBrokerExternalTestnetHostError(RuntimeError):
    """Stable blocker for an invalid public external-host binding."""


@dataclass(frozen=True)
class StandardBrokerExternalPortDescriptor:
    adapter_name: str
    provider: str
    broker_id: str
    environment: str
    transport_profile: str
    transport_state: str
    capabilities: tuple[str, ...]
    instrument_binding: dict[str, object] = field(default_factory=dict)
    market_source: dict[str, object] = field(default_factory=dict)
    credential_env_names: tuple[str, ...] = ()
    schema_version: str = "standard-broker-external-port-descriptor-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adapter_name": self.adapter_name,
            "provider": self.provider,
            "broker_id": self.broker_id,
            "environment": self.environment,
            "transport_profile": self.transport_profile,
            "transport_state": self.transport_state,
            "capabilities": list(self.capabilities),
            "instrument_binding": dict(self.instrument_binding),
            "market_source": dict(self.market_source),
            "credential_env_names": list(self.credential_env_names),
        }


class StandardBrokerExternalTestnetExecutionAdapter:
    """Consume external identity/preflight facts without enabling order writes."""

    name = "standard_broker_external_testnet"
    provider = "standard_broker"

    def __init__(
        self,
        *,
        external_host: object,
        account_id: str,
        runtime_id: str,
        release_sha: str,
        execution_scope: str,
        transport_profile: str,
        standard_broker_release_sha: str,
        instrument_binding: Mapping[str, object],
        market_source: Mapping[str, object],
    ) -> None:
        try:
            from standard_broker import BrokerEnvironment, ExternalBrokerHost
        except ModuleNotFoundError as exc:
            raise StandardBrokerExternalTestnetHostError(
                "standard-broker external host dependency is unavailable"
            ) from exc

        if standard_broker_release_sha != STANDARD_BROKER_EXTERNAL_RELEASE_SHA:
            raise StandardBrokerExternalTestnetHostError(
                "standard-broker external dependency SHA mismatch"
            )
        if transport_profile != STANDARD_BROKER_EXTERNAL_PROFILE:
            raise StandardBrokerExternalTestnetHostError(
                "unsupported standard-broker external transport profile"
            )
        if not isinstance(external_host, ExternalBrokerHost):
            raise StandardBrokerExternalTestnetHostError(
                "external_host must be the public standard_broker.ExternalBrokerHost"
            )
        identity = external_host.identity
        context = external_host.context
        runtime_identity = external_host.runtime_identity
        protection = external_host.protection_capabilities
        try:
            source_identity = MarketSourceIdentity.from_mapping(market_source)
        except ValueError as exc:
            raise StandardBrokerExternalTestnetHostError(str(exc)) from exc
        try:
            selected_instrument = InstrumentBinding.from_mapping(instrument_binding)
        except InstrumentBindingError as exc:
            raise StandardBrokerExternalTestnetHostError(str(exc)) from exc
        if selected_instrument.mapping_revision != STANDARD_BROKER_CAPABILITY_REVISION:
            raise StandardBrokerExternalTestnetHostError(
                "instrument_mapping_revision_mismatch"
            )
        if (
            identity.broker_id != "hyperliquid"
            or identity.environment is not BrokerEnvironment.TESTNET
            or identity.account_address != account_id
            or identity.execution_scope != execution_scope
            or context.session.lifecycle_id != runtime_id
            or context.release_sha != release_sha
            or external_host.external_profile_id != transport_profile
            or runtime_identity.transport_state != "external_testnet"
            or runtime_identity.mapping_revision != external_host.capabilities.revision
        ):
            raise StandardBrokerExternalTestnetHostError(
                "external host identity does not match the trading-system binding"
            )
        if (
            source_identity.broker_id != identity.broker_id
            or source_identity.environment != "testnet"
            or source_identity.instrument_id != selected_instrument.instrument_id
        ):
            raise StandardBrokerExternalTestnetHostError(
                "market_source_binding_mismatch"
            )
        if source_identity.source_id not in STANDARD_BROKER_EXTERNAL_MARKET_SOURCES:
            raise StandardBrokerExternalTestnetHostError(
                "unsupported_market_source"
            )
        if (
            runtime_identity.adapter_id != STANDARD_BROKER_RUNTIME_ADAPTER_ID
            or runtime_identity.version != STANDARD_BROKER_RUNTIME_VERSION
            or runtime_identity.commit != STANDARD_BROKER_RUNTIME_COMMIT
            or runtime_identity.mapping_revision != STANDARD_BROKER_CAPABILITY_REVISION
            or external_host.capabilities.revision != STANDARD_BROKER_CAPABILITY_REVISION
            or dict(external_host.capabilities.operations)
            != STANDARD_BROKER_EXTERNAL_OPERATIONS
        ):
            raise StandardBrokerExternalTestnetHostError(
                "external host runtime or capability identity does not match the accepted handoff"
            )
        if (
            protection is None
            or protection.profile_id != STANDARD_BROKER_PROTECTION_PROFILE
            or dict(protection.values)
            != STANDARD_BROKER_EXTERNAL_PROTECTION_VALUES
        ):
            raise StandardBrokerExternalTestnetHostError(
                "external protection capability identity does not match the accepted gap profile"
            )

        self._host = external_host
        self._instrument_binding = selected_instrument
        self._market_source = source_identity
        self._account_fingerprint = "sha256:" + hashlib.sha256(
            account_id.encode("utf-8")
        ).hexdigest()
        self.broker_config = {
            "provider": self.provider,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": transport_profile,
            "transport_state": "external_testnet",
            "account_fingerprint": self._account_fingerprint,
            "runtime_id": runtime_id,
            "release_sha": release_sha,
            "standard_broker_release_sha": standard_broker_release_sha,
            "execution_scope": execution_scope,
            "instrument_binding": selected_instrument.to_dict(),
            "market_source": source_identity.to_dict(),
            "dry_run": True,
            "live_trading_enabled": False,
        }

    @property
    def capabilities(self) -> BrokerCapabilities:
        return STANDARD_BROKER_EXTERNAL_TESTNET_CAPABILITIES

    @property
    def descriptor(self) -> StandardBrokerExternalPortDescriptor:
        return StandardBrokerExternalPortDescriptor(
            adapter_name=self.name,
            provider=self.provider,
            broker_id="hyperliquid",
            environment="testnet",
            transport_profile=self.broker_config["transport_profile"],
            transport_state=self.broker_config["transport_state"],
            capabilities=self.capabilities.names,
            instrument_binding=self._instrument_binding.to_dict(),
            market_source=self._market_source.to_dict(),
        )

    def preflight(self) -> dict[str, Any]:
        receipt = self._host.preflight(
            request_id=(
                "trading-system-external-host:"
                f"{self.broker_config['runtime_id']}:{self.broker_config['release_sha']}"
            ),
            required_operations={},
        )
        protection = self._host.protection_capabilities
        protection_values = dict(getattr(protection, "values", {}) or {})
        protection_gaps = sorted(
            f"protection_order.{name}"
            for name, supported in protection_values.items()
            if supported is not True
        )
        upstream = self._host.capabilities
        order_operations = {
            "submit",
            "cancel",
            "replace",
            "query",
            "open_orders",
            "fills",
        }
        return {
            "provider": self.provider,
            "broker_id": "hyperliquid",
            "mode": "testnet",
            "environment": "testnet",
            "transport_profile": self.broker_config["transport_profile"],
            "transport_state": "external_testnet",
            "instrument_id": self._instrument_binding.instrument_id,
            "instrument_binding": self._instrument_binding.to_dict(),
            "market_source": self._market_source.to_dict(),
            "market_source_ready": True,
            "host_ready": receipt.accepted is True,
            "ready": False,
            "strategy_ready": False,
            "protection_ready": False,
            "account_read_ready": False,
            "instrument_read_ready": upstream.supports("instrument", "read"),
            "order_execution_ready": False,
            "upstream_account_read_ready": upstream.supports("account", "read"),
            "upstream_instrument_read_ready": upstream.supports("instrument", "read"),
            "upstream_order_execution_ready": all(
                upstream.supports("order_execution", operation)
                for operation in order_operations
            ),
            "broker_operation_invoked": False,
            "network_io": receipt.network_io,
            "external_network": True,
            "real_money_eligible": receipt.real_money_eligible,
            "live_trading_enabled": False,
            "control_plane": "telegram",
            "account_fingerprint": self._account_fingerprint,
            "runtime_id": self.broker_config["runtime_id"],
            "release_sha": self.broker_config["release_sha"],
            "standard_broker_release_sha": self.broker_config[
                "standard_broker_release_sha"
            ],
            "execution_scope": self.broker_config["execution_scope"],
            "runtime_adapter_id": receipt.runtime_identity.adapter_id,
            "runtime_version": receipt.runtime_identity.version,
            "runtime_commit": receipt.runtime_identity.commit,
            "capability_revision": receipt.capability_revision,
            "mapping_revision": receipt.runtime_identity.mapping_revision,
            "upstream_receipt_digest": receipt.receipt_digest,
            "capability_gaps": protection_gaps,
            "blocker": "capability_gap:protection_order",
            "next_action": "await_external_protection_capability",
        }

    def submit_order(self, request: BrokerOrderRequest) -> Any:
        del request
        raise UnsupportedBrokerCapability(
            "standard-broker external Testnet bridge is preflight-only; "
            "strategy execution remains blocked until external ProtectionOrder is available"
        )
