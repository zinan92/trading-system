"""Approved local-fixture Hyperliquid Testnet execution boundary."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from schemas.market_data import PaperOrder
from services.broker_port import (
    BrokerCapabilities,
    BrokerCancelRequest,
    BrokerCapability,
    BrokerOrderRequest,
    BrokerPortDescriptor,
    UnsupportedBrokerCapability,
    broker_port_descriptor,
)
from services.standard_broker_host import (
    StandardBrokerEnvironmentIdentity,
    validate_standard_broker_credential_source,
)


STANDARD_BROKER_TESTNET_CAPABILITIES = BrokerCapabilities(
    frozenset(
        {
            BrokerCapability.PREFLIGHT,
            BrokerCapability.SUBMIT_ORDER,
            BrokerCapability.CANCEL_ORDER,
            BrokerCapability.REPLACE_ORDER,
            BrokerCapability.QUERY_ORDER,
            BrokerCapability.OPEN_ORDERS,
            BrokerCapability.ORDER_FILL,
            BrokerCapability.ORDER_RECONCILIATION,
        }
    )
)


class StandardBrokerTestnetHostError(RuntimeError):
    """Stable host-level blocker for an unready Testnet fixture boundary."""


class _FixtureSignerProvider:
    def sign(self, signer: object, payload: bytes) -> bytes:
        del signer
        return payload


class StandardBrokerTestnetExecutionAdapter:
    """Canonical order facade over an approved local Testnet runtime fixture."""

    name = "standard_broker_testnet"
    provider = "standard_broker"

    def __init__(
        self,
        *,
        broker_id: str,
        account_id: str,
        credential_source: str,
        runtime_id: str,
        ledger_namespace: str,
        environment_fingerprint: str,
        release_sha: str,
        execution_scope: str,
        backend: object,
        approval: object,
        instrument_meta: Mapping[str, object],
        expected_version: str,
        expected_commit: str,
    ) -> None:
        try:
            from standard_broker import (
                AccountReference,
                AccountScope,
                BrokerEnvironment,
                BrokerRuntimeSession,
                ExternalEnvironmentApproval,
                RuntimeActivationPolicy,
                RuntimeFactLedger,
                SignerKind,
                SignerReference,
                ProtectionGroup,
            )
            from standard_broker.adapters.hyperliquid import (
                HyperliquidInstrumentAdapter,
                HyperliquidRuntimeOrderAdapter,
                HyperliquidRuntimeProtectionAdapter,
                NautilusHyperliquidRuntime,
                NautilusRuntimeConfig,
            )
        except ModuleNotFoundError as exc:
            raise StandardBrokerTestnetHostError(
                "standard-broker Testnet dependency is unavailable"
            ) from exc

        if broker_id != "hyperliquid":
            raise StandardBrokerTestnetHostError(
                f"unsupported Testnet broker: {broker_id!r}"
            )
        if not isinstance(approval, ExternalEnvironmentApproval):
            raise StandardBrokerTestnetHostError(
                "Testnet requires a canonical ExternalEnvironmentApproval"
            )
        metadata = getattr(backend, "metadata", None)
        capabilities = getattr(metadata, "capabilities", None)
        if capabilities is None or capabilities.environment is not BrokerEnvironment.TESTNET:
            raise StandardBrokerTestnetHostError(
                "Testnet backend capability profile must be canonical TESTNET"
            )
        if getattr(backend, "local_only", False) is not True:
            raise StandardBrokerTestnetHostError(
                "Testnet host requires a local-only fixture backend"
            )
        if not isinstance(instrument_meta, Mapping):
            raise StandardBrokerTestnetHostError("Testnet instrument metadata is required")
        self._protection_group_type = ProtectionGroup

        credential_source = validate_standard_broker_credential_source(
            credential_source,
            environment="testnet",
        )
        self.identity = StandardBrokerEnvironmentIdentity(
            broker_id=broker_id,
            environment="testnet",
            environment_fingerprint=environment_fingerprint,
            execution_scope=execution_scope,
            account_id=account_id,
            credential_source=credential_source,
            runtime_id=runtime_id,
            ledger_namespace=ledger_namespace,
            release_sha=release_sha,
        )
        self.broker_config = {
            "provider": self.provider,
            "broker_id": broker_id,
            "environment": "testnet",
            "account_id": account_id,
            "credential_source": self.identity.credential_source,
            "runtime_id": runtime_id,
            "ledger_namespace": ledger_namespace,
            "environment_fingerprint": environment_fingerprint,
            "release_sha": release_sha,
            "execution_scope": execution_scope,
            "dry_run": True,
            "live_trading_enabled": False,
            "network_io": False,
            "transport_state": "local_fixture",
        }
        signer = SignerReference(
            SignerKind.API_AGENT,
            "environment-reference",
            f"env://{credential_source}",
        )
        session = BrokerRuntimeSession(
            broker_id=broker_id,
            environment=BrokerEnvironment.TESTNET,
            account=AccountReference(AccountScope.MASTER, account_id),
            signer=signer,
            signer_provider=_FixtureSignerProvider(),
            capabilities=capabilities,
            execution_scope=execution_scope,
            lifecycle_id=runtime_id,
        )
        runtime = NautilusHyperliquidRuntime(
            session=session,
            backend=backend,
            config=NautilusRuntimeConfig(
                expected_version=expected_version,
                expected_commit=expected_commit,
                policy=RuntimeActivationPolicy(testnet_approval=approval),
                expected_release_sha=release_sha,
            ),
        )
        try:
            runtime.start()
            instruments = HyperliquidInstrumentAdapter.from_meta(
                instrument_meta,
                revision=release_sha,
            )
            ledger = RuntimeFactLedger()
            self._runtime = runtime
            self._orders = HyperliquidRuntimeOrderAdapter(
                runtime=runtime,
                instruments=instruments,
                ledger=ledger,
            )
            self._protection = (
                HyperliquidRuntimeProtectionAdapter(runtime=runtime)
                if capabilities.supports("protection_order", "submit")
                else None
            )
            if ledger.session_key is None:
                raise StandardBrokerTestnetHostError(
                    "Testnet runtime fact ledger did not bind a session"
                )
            ledger.session_key = f"{self.identity.ledger_namespace}:{ledger.session_key}"
            self._instruments = instruments
            self._ledger = ledger
        except Exception as exc:  # noqa: BLE001 - convert runtime blockers at host seam.
            raise StandardBrokerTestnetHostError(
                f"Testnet runtime blocked: {type(exc).__name__}: {exc}"
            ) from exc

    @property
    def capabilities(self) -> BrokerCapabilities:
        return STANDARD_BROKER_TESTNET_CAPABILITIES

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    @property
    def canonical_order_adapter(self) -> Any:
        return self._orders

    @property
    def protection_adapter(self) -> Any:
        return self._protection

    @property
    def fills(self) -> Mapping[str, object]:
        return self._orders.fills

    @property
    def fact_ledger(self) -> Any:
        return self._ledger

    def preflight(self) -> dict[str, Any]:
        result = self._runtime.preflight(
            required_operations={
                "order_execution": {
                    "submit",
                    "cancel",
                    "replace",
                    "query",
                    "open_orders",
                }
            }
        )
        return {
            "provider": self.provider,
            "broker_id": self.broker_config["broker_id"],
            "mode": "testnet",
            "environment": "testnet",
            "ready": True,
            "network_io": False,
            "external_network": True,
            "real_money_eligible": False,
            "credential_required": result.credential_required,
            "control_plane": "telegram",
            "transport_state": "local_fixture",
            "account_id": self.broker_config["account_id"],
            "runtime_id": self.broker_config["runtime_id"],
            "release_sha": self.broker_config["release_sha"],
            "environment_fingerprint": self.broker_config["environment_fingerprint"],
            "ledger_namespace": self.broker_config["ledger_namespace"],
            "capability_revision": result.capability_revision,
            "protection_ready": self._protection is not None,
        }

    def submit_order(self, request: BrokerOrderRequest) -> Any:
        return self._orders.submit(self._intent_from_request(request))

    def cancel_order(self, request: BrokerCancelRequest) -> Any:
        if request.client_order_id and request.broker_order_id:
            try:
                client_owner = self._orders.resolve_order_id(request.client_order_id)
                broker_owner = self._orders.resolve_order_id(request.broker_order_id)
            except KeyError as exc:
                raise StandardBrokerTestnetHostError(
                    "contradictory or unknown client and broker order identities"
                ) from exc
            if client_owner != broker_owner:
                raise StandardBrokerTestnetHostError(
                    "contradictory client and broker order identities"
                )
            order_id = client_owner
        else:
            order_id = request.client_order_id or request.broker_order_id
        return self.request("order_execution", "cancel", order_id)

    def request(self, port: str, operation: str, payload: object | None = None) -> Any:
        if port != "order_execution":
            if port != "protection_order":
                raise UnsupportedBrokerCapability(
                    f"Testnet adapter does not expose {port}"
                )
            if self._protection is None:
                raise UnsupportedBrokerCapability(
                    "Testnet protection capability is not declared"
                )
            if not isinstance(payload, self._protection_group_type):
                raise StandardBrokerTestnetHostError(
                    "protection_order requests require canonical ProtectionGroup"
                )
            if operation == "submit":
                return self._protection.submit(payload)
            if operation == "replace":
                return self._protection.replace(payload)
            if operation == "cancel":
                return self._protection.cancel(payload)
            if operation in {"query", "reconcile"}:
                return self._protection.reconcile(payload)
            raise UnsupportedBrokerCapability(
                f"unsupported Testnet protection operation: {operation}"
            )
        if operation == "submit":
            if not self._is_order_intent(payload):
                raise StandardBrokerTestnetHostError(
                    "order_execution.submit requires canonical OrderIntent"
                )
            return self._orders.submit(payload)
        if operation == "cancel":
            return self._orders.cancel(self._order_id(payload))
        if operation == "replace":
            if not self._is_order_intent(payload):
                raise StandardBrokerTestnetHostError(
                    "order_execution.replace requires canonical OrderIntent"
                )
            return self._orders.modify(payload.order_id, payload)
        if operation == "query":
            return self._orders.query(self._order_id(payload))
        if operation == "open_orders":
            instrument_id = str(payload) if payload is not None else None
            return self._orders.open_orders(instrument_id)
        if operation == "reconcile":
            if not isinstance(payload, Mapping):
                raise StandardBrokerTestnetHostError(
                    "order_execution.reconcile requires a mapping event"
                )
            return self._orders.reconcile(payload)
        if operation == "apply_fill":
            if not isinstance(payload, Mapping):
                raise StandardBrokerTestnetHostError(
                    "order_execution.apply_fill requires a mapping event"
                )
            return self._orders.apply_fill(payload)
        raise UnsupportedBrokerCapability(
            f"unsupported Testnet order operation: {operation}"
        )

    def _intent_from_request(self, request: BrokerOrderRequest) -> Any:
        try:
            from standard_broker import OrderIntent, OrderSide, OrderType, TimeInForce
        except ModuleNotFoundError as exc:
            raise StandardBrokerTestnetHostError(
                "standard-broker Testnet dependency is unavailable"
            ) from exc
        ticket = request.ticket
        candidate = ticket.get("order_intent")
        if self._is_order_intent(candidate):
            return candidate
        side = str(ticket.get("side") or ticket.get("direction") or "").strip().lower()
        if side in {"long", "buy", "b"}:
            normalized_side = OrderSide.BUY
        elif side in {"short", "sell", "s", "a"}:
            normalized_side = OrderSide.SELL
        else:
            raise StandardBrokerTestnetHostError("Testnet order side is required")
        order_type_value = str(ticket.get("order_type") or "limit").strip().lower()
        try:
            order_type = OrderType(order_type_value)
            time_in_force = TimeInForce(
                str(ticket.get("time_in_force") or "gtc").strip().lower()
            )
        except ValueError as exc:
            raise StandardBrokerTestnetHostError(
                f"unsupported Testnet order enum: {exc}"
            ) from exc
        instrument_id = str(
            ticket.get("instrument_id") or ticket.get("symbol") or ""
        ).strip()
        quantity = request.actual_size or ticket.get("quantity") or ticket.get("size")
        price = ticket.get("limit_price") or ticket.get("price") or request.latest_price
        if not instrument_id or quantity is None:
            raise StandardBrokerTestnetHostError(
                "Testnet order instrument_id and quantity are required"
            )
        return OrderIntent(
            order_id=str(ticket.get("ticket_id") or "").strip(),
            instrument_id=instrument_id,
            side=normalized_side,
            order_type=order_type,
            quantity=Decimal(str(quantity)),
            limit_price=Decimal(str(price)) if price is not None else None,
            time_in_force=time_in_force,
            idempotency_key=str(
                ticket.get("idempotency_key") or ticket.get("ticket_id") or ""
            ).strip(),
            reduce_only=bool(ticket.get("reduce_only", False)),
            close_position=bool(ticket.get("close_position", False)),
            trigger_price=(
                Decimal(str(ticket["trigger_price"]))
                if ticket.get("trigger_price") is not None
                else None
            ),
        )

    @staticmethod
    def _is_order_intent(value: object) -> bool:
        return hasattr(value, "order_id") and hasattr(value, "instrument_id") and hasattr(value, "idempotency_key")

    @staticmethod
    def _order_id(value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if hasattr(value, "order_id"):
            return str(value.order_id)
        if isinstance(value, Mapping) and str(value.get("order_id") or "").strip():
            return str(value["order_id"]).strip()
        raise StandardBrokerTestnetHostError("canonical order_id is required")
