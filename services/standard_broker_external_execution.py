"""Provider-neutral execution facade for the opt-in Hyperliquid Testnet profile.

The default ``hyperliquid-testnet-default`` profile remains preflight-only.  A
caller must explicitly select the reviewed position-protection profile before
this adapter can be constructed.  All order, account, fee, fill, and
ProtectionOrder operations are delegated through the public standard-broker
binding; no Hyperliquid-native payload or private runtime method crosses this
module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from services.broker_port import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerCancelRequest,
    BrokerOrderRequest,
    BrokerPortDescriptor,
    broker_port_descriptor,
)


STANDARD_BROKER_RELEASE_SHA = "2d3a5cc26538b24fb31ab81201facd5ee46476ba"
PROTECTED_EXTERNAL_PROFILE = "hyperliquid-testnet-position-protection"
PROTECTED_CAPABILITY_REVISION = "hyperliquid-testnet-position-protection-runtime-v1"
PROTECTED_PROTECTION_MATRIX_ID = "hyperliquid-testnet-position-protection-v1"

_EXECUTION_CAPABILITIES = BrokerCapabilities(
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
PROTECTED_EXTERNAL_EXECUTION_CAPABILITIES = _EXECUTION_CAPABILITIES
_REQUIRED_PROTECTION = frozenset(
    {
        "submit",
        "cancel",
        "replace",
        "query",
        "reduce_only",
        "mark_price_trigger",
        "grouped_tp_sl",
        "sibling_cancellation",
        "position_following",
        "position_level_tpsl",
        "take_profit_limit",
        "stop_loss_market",
        "position_coverage",
        "cancel_replace",
    }
)
_PROTECTION_OPERATIONS = frozenset({"submit", "cancel", "replace", "query", "reconcile"})


class StandardBrokerExternalExecutionError(RuntimeError):
    """Redacted blocker for an unavailable or mismatched public binding."""


_RECOVERY_STATE_ALIASES = {
    "accepted": "resting",
    "partial": "partially_filled",
    "cancelled": "canceled",
}


class StandardBrokerExternalExecutionAdapter:
    """Adapt the public protected standard-broker binding to BrokerExecutionPort."""

    name = "standard_broker_external_testnet_protected"
    provider = "standard_broker"

    def __init__(
        self,
        binding: object,
        *,
        runtime: object | None = None,
        broker_config: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._binding = binding
        self._runtime = runtime
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(getattr(binding, "preflight", None)):
            raise StandardBrokerExternalExecutionError("public_binding_preflight_missing")
        for method in (
            "submit",
            "query",
            "query_by_idempotency_key",
            "cancel",
            "replace",
            "read_facts",
            "market_fact",
        ):
            if not callable(getattr(binding, method, None)):
                raise StandardBrokerExternalExecutionError(f"public_binding_{method}_missing")
        protection = getattr(binding, "protection", None)
        matrix = getattr(binding, "protection_capabilities", None)
        if protection is None or matrix is None:
            raise StandardBrokerExternalExecutionError("external_protection_binding_missing")
        if str(getattr(binding, "profile_id", "") or "") != PROTECTED_EXTERNAL_PROFILE:
            raise StandardBrokerExternalExecutionError("external_protection_profile_required")
        supports = getattr(matrix, "supports", None)
        if not callable(supports) or any(supports(name) is not True for name in _REQUIRED_PROTECTION):
            raise StandardBrokerExternalExecutionError("external_protection_capability_gap")
        session = getattr(binding, "runtime_session", None)
        account = getattr(session, "account", None)
        account_id = str(getattr(account, "address", "") or "").strip()
        runtime_id = str(getattr(session, "lifecycle_id", "") or "").strip()
        broker_id = str(getattr(session, "broker_id", "") or "").strip().lower()
        environment = str(
            getattr(getattr(session, "environment", None), "value", getattr(session, "environment", ""))
            or ""
        ).strip().lower()
        capability_revision = str(
            getattr(session, "capability_revision", "") or PROTECTED_CAPABILITY_REVISION
        ).strip()
        transport_state = str(getattr(binding, "transport_state", "") or "").strip().lower()
        if (
            not account_id
            or not runtime_id
            or broker_id != "hyperliquid"
            or environment != "testnet"
            or transport_state != "external_testnet"
            or capability_revision != PROTECTED_CAPABILITY_REVISION
        ):
            raise StandardBrokerExternalExecutionError("external_binding_identity_incomplete")
        config = dict(broker_config or {})
        self.broker_config = {
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "transport_state": "external_testnet",
            "account_id": account_id,
            "account_fingerprint": "sha256:" + hashlib.sha256(account_id.encode("utf-8")).hexdigest(),
            "runtime_id": runtime_id,
            "release_sha": str(config.get("release_sha") or ""),
            "standard_broker_release_sha": str(
                config.get("standard_broker_release_sha") or STANDARD_BROKER_RELEASE_SHA
            ),
            "capability_revision": capability_revision,
            "execution_scope": str(config.get("execution_scope") or "hypercore:default"),
            "ledger_namespace": str(
                config.get("ledger_namespace") or "ledger.standard-broker.testnet.external"
            ),
            "instrument_id": str(config.get("instrument_id") or ""),
            "dry_run": False,
            "live_trading_enabled": False,
            "real_money_eligible": False,
            **{
                key: value
                for key, value in config.items()
                if key
                in {
                    "instrument_binding",
                    "market_source",
                    "ledger_namespace",
                    "approval_id",
                    "approved_by",
                }
            },
        }
        self.protection_adapter = protection
        self.account_adapter = self
        self.canonical_order_adapter = _CanonicalOrderAdapter(self)

    @classmethod
    def from_build_context(cls, context: object) -> "StandardBrokerExternalExecutionAdapter":
        config = getattr(context, "broker_config", None)
        if not isinstance(config, Mapping):
            raise StandardBrokerExternalExecutionError("external_context_invalid")
        if (
            str(getattr(context, "execution_mode", "") or "").lower() != "live"
            or getattr(context, "live_trading_enabled", False) is True
        ):
            raise StandardBrokerExternalExecutionError("external_testnet_execution_mode_invalid")
        injected_binding = config.get("external_binding")
        if injected_binding is not None:
            if config.get("external_binding_test_only") is not True:
                raise StandardBrokerExternalExecutionError("injected_external_binding_forbidden")
            configured_sha = str(config.get("standard_broker_release_sha") or STANDARD_BROKER_RELEASE_SHA)
            if configured_sha != STANDARD_BROKER_RELEASE_SHA:
                raise StandardBrokerExternalExecutionError("standard_broker_dependency_sha_mismatch")
            return cls(
                injected_binding,
                runtime=config.get("external_runtime"),
                broker_config=config,
            )
        required = (
            "account_id",
            "runtime_id",
            "release_sha",
            "standard_broker_release_sha",
            "approval_id",
            "approved_by",
            "secret_file",
        )
        missing = [key for key in required if not str(config.get(key) or "").strip()]
        if missing:
            raise StandardBrokerExternalExecutionError(
                "external_context_missing:" + ",".join(missing)
            )
        if str(config.get("standard_broker_release_sha")) != STANDARD_BROKER_RELEASE_SHA:
            raise StandardBrokerExternalExecutionError("standard_broker_dependency_sha_mismatch")
        from services.standard_broker_external_protection import (
            ExternalProtectionBuildConfig,
            build_external_protected_canary_binding,
        )

        capability_revision = str(
            config.get("capability_revision") or PROTECTED_CAPABILITY_REVISION
        )
        build_config = ExternalProtectionBuildConfig(
            account_address=str(config["account_id"]),
            runtime_id=str(config["runtime_id"]),
            release_sha=str(config["release_sha"]),
            approval_id=str(config["approval_id"]),
            approved_by=str(config["approved_by"]),
            secret_file=Path(str(config["secret_file"])),
            credential_reference=str(
                config.get("credential_reference") or "file-secret://hyperliquid-testnet"
            ),
            capability_revision=capability_revision,
        )
        runtime, binding = build_external_protected_canary_binding(build_config)
        return cls(
            binding,
            runtime=runtime,
            broker_config={
                **dict(config),
                "release_sha": str(config["release_sha"]),
                "standard_broker_release_sha": STANDARD_BROKER_RELEASE_SHA,
                "capability_revision": capability_revision,
            },
        )

    @classmethod
    def preflight_build_context(cls, context: object) -> dict[str, Any]:
        """Prove the opt-in profile without reading a signer or instruments."""

        config = getattr(context, "broker_config", None)
        if not isinstance(config, Mapping):
            raise StandardBrokerExternalExecutionError("external_context_invalid")
        required = (
            "account_id",
            "runtime_id",
            "release_sha",
            "standard_broker_release_sha",
            "approval_id",
            "approved_by",
            "secret_file",
        )
        missing = [key for key in required if not str(config.get(key) or "").strip()]
        if missing:
            raise StandardBrokerExternalExecutionError(
                "external_context_missing:" + ",".join(missing)
            )
        if str(config.get("standard_broker_release_sha")) != STANDARD_BROKER_RELEASE_SHA:
            raise StandardBrokerExternalExecutionError("standard_broker_dependency_sha_mismatch")
        from services.standard_broker_external_protection import (
            ExternalProtectionBuildConfig,
            build_external_position_protection_binding,
        )

        build_config = ExternalProtectionBuildConfig(
            account_address=str(config["account_id"]),
            runtime_id=str(config["runtime_id"]),
            release_sha=str(config["release_sha"]),
            approval_id=str(config["approval_id"]),
            approved_by=str(config["approved_by"]),
            secret_file=Path(str(config["secret_file"])),
            credential_reference=str(
                config.get("credential_reference") or "file-secret://hyperliquid-testnet"
            ),
            capability_revision=str(
                config.get("capability_revision") or PROTECTED_CAPABILITY_REVISION
            ),
        )
        runtime, binding = build_external_position_protection_binding(
            build_config,
            canary=False,
        )
        try:
            matrix = getattr(binding, "protection_capabilities", None)
            supports = getattr(matrix, "supports", None)
            gaps = (
                ["protection_order.profile"]
                if not callable(supports)
                else [
                    f"protection_order.{name}"
                    for name in sorted(_REQUIRED_PROTECTION)
                    if supports(name) is not True
                ]
            )
            receipt = runtime.preflight(
                required_operations={"protection_order": {"submit", "query"}}
            )
            return {
                "status": "PREFLIGHT_READY" if not gaps and receipt.accepted is True else "BLOCKED",
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "transport_profile": PROTECTED_EXTERNAL_PROFILE,
                "transport_state": "external_testnet",
                "account_fingerprint": "sha256:" + hashlib.sha256(
                    str(config["account_id"]).encode("utf-8")
                ).hexdigest(),
                "runtime_id": str(config["runtime_id"]),
                "release_sha": str(config["release_sha"]),
                "standard_broker_release_sha": STANDARD_BROKER_RELEASE_SHA,
                "capability_revision": PROTECTED_CAPABILITY_REVISION,
                "protection_matrix_id": PROTECTED_PROTECTION_MATRIX_ID,
                "protection_ready": not gaps,
                "capability_gaps": gaps,
                "external_network": receipt.external_network is True,
                "invocation_performed": getattr(runtime.health, "invocation_performed", None),
                "real_money_eligible": receipt.real_money_eligible,
                "secret_resolved": False,
            }
        finally:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()

    @property
    def capabilities(self) -> BrokerCapabilities:
        return _EXECUTION_CAPABILITIES

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    @property
    def local_only(self) -> bool:
        return False

    @property
    def transport_state(self) -> str:
        return "external_testnet"

    @property
    def runtime_session(self) -> object:
        return self._binding.runtime_session

    def preflight(self, *, strategy_family: str | None = None) -> dict[str, Any]:
        raw = self._binding.preflight()
        if not isinstance(raw, Mapping):
            raise StandardBrokerExternalExecutionError("external_preflight_invalid")
        matrix = getattr(self.protection_adapter, "protection_capabilities", None)
        supports = getattr(matrix, "supports", None)
        protection_gaps = (
            []
            if callable(supports)
            else ["protection_order.profile"]
        )
        if callable(supports):
            protection_gaps.extend(
                f"protection_order.{name}"
                for name in sorted(_REQUIRED_PROTECTION)
                if supports(name) is not True
            )
        session = self.runtime_session
        account_id = str(getattr(getattr(session, "account", None), "address", "") or "")
        context = getattr(self._binding, "context", None)
        declared = getattr(getattr(context, "capabilities", None), "supports", None)
        account_ready = bool(raw.get("account_read_ready", True))
        order_ready = bool(raw.get("order_execution_ready", True))
        if callable(declared):
            account_ready = declared("account", "read")
            order_ready = all(
                declared("order_execution", operation)
                for operation in ("submit", "cancel", "replace", "query", "open_orders", "fills")
            )
        required_protection_ready = not protection_gaps
        ready = (
            raw.get("host_ready") is True
            and raw.get("canary_ready") is True
            and required_protection_ready
            and account_ready
            and order_ready
            and raw.get("real_money_eligible") is False
        )
        return {
            **dict(raw),
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "mode": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "transport_state": "external_testnet",
            "account_id": account_id,
            "account_fingerprint": raw.get("account_fingerprint") or self.broker_config["account_fingerprint"],
            "runtime_id": self.broker_config["runtime_id"],
            "release_sha": self.broker_config["release_sha"],
            "standard_broker_release_sha": self.broker_config["standard_broker_release_sha"],
            "capability_revision": PROTECTED_CAPABILITY_REVISION,
            "strategy_family": str(strategy_family or "").lower() or None,
            "ready": ready and not protection_gaps,
            "strategy_ready": ready and not protection_gaps,
            "protection_ready": required_protection_ready,
            "account_read_ready": account_ready,
            "order_execution_ready": order_ready,
            "capability_gaps": protection_gaps,
            "network_io": raw.get("network_io", True),
            "external_network": True,
            "real_money_eligible": False,
            "live_trading_enabled": False,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
        }

    def submit_order(self, request: BrokerOrderRequest) -> object:
        return self._binding.submit(self._intent(request))

    def cancel_order(self, request: BrokerCancelRequest) -> object:
        reference = str(request.broker_order_id or request.client_order_id or "").strip()
        if not reference:
            raise StandardBrokerExternalExecutionError("cancel_order_identity_required")
        try:
            return self._binding.cancel(reference)
        except KeyError:
            # A restarted public binding has no in-memory identity index. The
            # submission ledger is the durable source of the broker OID/cloid
            # pair, so restore that identity before retrying the same cancel.
            intent = self._intent(
                BrokerOrderRequest(
                    run_date=request.run_date,
                    ticket={
                        "ticket_id": request.broker_order_id or request.client_order_id,
                        "instrument_id": request.asset,
                        "side": "buy",
                        "quantity": "0.00000001",
                        "order_type": "limit",
                        "limit_price": "0.00000001",
                        "client_order_id": request.client_order_id,
                    },
                )
            )
            recover = getattr(self._binding, "recover", None)
            if callable(recover) and request.broker_order_id:
                recover(intent, broker_order_id=request.broker_order_id, state="resting")
                return self._binding.cancel(request.broker_order_id)
            recover_client = getattr(self._binding, "recover_client_order", None)
            if callable(recover_client) and request.client_order_id:
                recover_client(intent, client_order_id=request.client_order_id, state="resting")
                return self._binding.cancel(request.client_order_id)
            raise

    def query_by_idempotency_key(self, idempotency_key: str) -> object:
        query = getattr(self._binding, "query_by_idempotency_key", None)
        if not callable(query):
            raise StandardBrokerExternalExecutionError(
                "external_idempotency_query_unavailable"
            )
        key = str(idempotency_key or "").strip()
        if not key:
            raise StandardBrokerExternalExecutionError("external_idempotency_key_required")
        return query(key)

    def recover(
        self,
        request: BrokerOrderRequest,
        *,
        broker_order_id: str,
        state: str,
    ) -> None:
        recover = getattr(self._binding, "recover", None)
        if not callable(recover):
            raise StandardBrokerExternalExecutionError(
                "external_order_recovery_unavailable"
            )
        canonical_state = _RECOVERY_STATE_ALIASES.get(
            str(state or "").strip().lower(),
            str(state or "").strip().lower(),
        )
        if not canonical_state:
            raise StandardBrokerExternalExecutionError("external_recovery_state_required")
        if not str(broker_order_id or "").strip():
            raise StandardBrokerExternalExecutionError("external_broker_order_id_required")
        recover(
            self._intent(request),
            broker_order_id=str(broker_order_id or "").strip(),
            state=canonical_state,
        )

    def recover_client_order(
        self,
        request: BrokerOrderRequest,
        *,
        client_order_id: str,
        state: str,
    ) -> object:
        recover = getattr(self._binding, "recover_client_order", None)
        if not callable(recover):
            raise StandardBrokerExternalExecutionError(
                "external_client_order_recovery_unavailable"
            )
        canonical_state = _RECOVERY_STATE_ALIASES.get(
            str(state or "").strip().lower(),
            str(state or "").strip().lower(),
        )
        if not canonical_state:
            raise StandardBrokerExternalExecutionError("external_recovery_state_required")
        if not str(client_order_id or "").strip():
            raise StandardBrokerExternalExecutionError("external_client_order_id_required")
        return recover(
            self._intent(request),
            client_order_id=str(client_order_id or "").strip(),
            state=canonical_state,
        )

    def request(self, port: str, operation: str, payload: object | None = None) -> object:
        normalized_port = str(port or "").strip()
        normalized_operation = str(operation or "").strip()
        if normalized_port == "protection_order":
            if normalized_operation not in _PROTECTION_OPERATIONS:
                raise StandardBrokerExternalExecutionError(
                    f"protection_operation_unsupported:{normalized_operation}"
                )
            if normalized_operation == "query":
                normalized_operation = "reconcile"
            method = getattr(self.protection_adapter, normalized_operation, None)
            if not callable(method):
                raise StandardBrokerExternalExecutionError(
                    f"protection_operation_unsupported:{normalized_operation}"
                )
            return method(payload)
        if normalized_port == "order_execution":
            if normalized_operation == "query":
                return self._binding.query(self._reference(payload))
            if normalized_operation == "open_orders":
                return self._read_bundle(
                    str(payload or self._instrument_id()).strip()
                ).open_orders
            if normalized_operation == "fills":
                if isinstance(payload, Mapping):
                    return self._read_bundle(
                        self._instrument_scope(payload),
                        order_id=str(
                            payload.get("order_id")
                            or payload.get("broker_order_id")
                            or ""
                        ),
                        client_order_id=str(payload.get("client_order_id") or "") or None,
                    ).fills
                return self._read_bundle(self._instrument_scope(payload)).fills
            if normalized_operation == "reconcile":
                return self._read_bundle(self._instrument_scope(payload)).reconciliation
            if normalized_operation == "apply_fill":
                return self.canonical_order_adapter.apply_fill(payload)
        if normalized_port == "account":
            account_id = str(payload or self.broker_config["account_id"])
            if account_id != self.broker_config["account_id"]:
                raise StandardBrokerExternalExecutionError("account_identity_mismatch")
            bundle = self._read_bundle(self._instrument_id())
            if normalized_operation == "read":
                return bundle.account
            if normalized_operation == "positions":
                return bundle.positions
        if normalized_port == "fee" and normalized_operation in {"read", "fill", "schedule"}:
            if isinstance(payload, Mapping):
                return self._read_bundle(
                    self._instrument_scope(payload),
                    order_id=str(
                        payload.get("order_id")
                        or payload.get("broker_order_id")
                        or ""
                    ),
                    client_order_id=str(payload.get("client_order_id") or "") or None,
                ).fees
            return self._read_bundle(self._instrument_scope(payload)).fees
        raise StandardBrokerExternalExecutionError(
            f"external_operation_unsupported:{normalized_port}.{normalized_operation}"
        )

    def market_fact(self, *, instrument_id: str, now: datetime) -> Mapping[str, Any]:
        return self._binding.market_fact(instrument_id=instrument_id, now=now)

    def read_facts(
        self,
        *,
        instrument_id: str,
        now: datetime | None = None,
        order_id: str = "",
        client_order_id: str | None = None,
    ) -> object:
        """Read one public, cursor-bound fact bundle for the selected instrument."""

        if not str(instrument_id or "").strip():
            raise StandardBrokerExternalExecutionError("external_facts_instrument_required")
        reader = getattr(self._binding, "read_facts", None)
        if not callable(reader):
            raise StandardBrokerExternalExecutionError("external_facts_reader_missing")
        return reader(
            order_id=str(order_id or ""),
            instrument_id=str(instrument_id),
            now=now or self._clock(),
            client_order_id=client_order_id,
        )

    def close(self) -> None:
        close = getattr(self._runtime, "close", None)
        if callable(close):
            close()

    def _read_bundle(
        self,
        scope: str,
        *,
        order_id: str = "",
        client_order_id: str | None = None,
    ) -> object:
        return self.read_facts(
            order_id=order_id,
            instrument_id=scope or self._instrument_id(),
            now=self._clock(),
            client_order_id=client_order_id,
        )

    def _instrument_scope(self, payload: object) -> str:
        if isinstance(payload, Mapping):
            return str(
                payload.get("instrument_id")
                or payload.get("symbol")
                or self._instrument_id()
            ).strip()
        return self._instrument_id()

    def _instrument_id(self) -> str:
        binding = self.broker_config.get("instrument_binding")
        if isinstance(binding, Mapping):
            return str(binding.get("instrument_id") or "").strip()
        return str(self.broker_config.get("instrument_id") or "").strip()

    @staticmethod
    def _reference(payload: object) -> str:
        if isinstance(payload, Mapping):
            return str(
                payload.get("order_id")
                or payload.get("broker_order_id")
                or payload.get("client_order_id")
                or ""
            ).strip()
        return str(payload or "").strip()

    @staticmethod
    def _intent(request: BrokerOrderRequest) -> object:
        if not isinstance(request, BrokerOrderRequest):
            raise TypeError("external order request must be BrokerOrderRequest")
        try:
            from standard_broker import OrderIntent, OrderSide, OrderType, TimeInForce
        except (ImportError, ModuleNotFoundError) as exc:
            raise StandardBrokerExternalExecutionError("standard_broker_public_types_unavailable") from exc
        ticket = request.ticket
        side = str(ticket.get("side") or ticket.get("direction") or "").lower()
        if side in {"buy", "long", "b"}:
            order_side = OrderSide.BUY
        elif side in {"sell", "short", "a", "s"}:
            order_side = OrderSide.SELL
        else:
            raise StandardBrokerExternalExecutionError("external_order_side_required")
        requested_type = str(ticket.get("order_type") or "limit").lower()
        order_type = OrderType.LIMIT if requested_type == "market" else OrderType(requested_type)
        tif = "ioc" if requested_type == "market" else str(ticket.get("time_in_force") or "gtc").lower()
        price = ticket.get("limit_price") or ticket.get("price") or request.latest_price
        if price is None:
            raise StandardBrokerExternalExecutionError("external_order_price_required")
        quantity = request.actual_size or ticket.get("quantity") or ticket.get("size")
        if quantity is None:
            raise StandardBrokerExternalExecutionError("external_order_quantity_required")
        return OrderIntent(
            order_id=str(ticket.get("ticket_id") or "").strip(),
            instrument_id=str(ticket.get("instrument_id") or ticket.get("symbol") or "").strip(),
            side=order_side,
            order_type=order_type,
            quantity=Decimal(str(quantity)),
            limit_price=Decimal(str(price)),
            time_in_force=TimeInForce(tif),
            idempotency_key=str(ticket.get("idempotency_key") or ticket.get("ticket_id") or "").strip(),
            client_order_id=str(ticket.get("client_order_id") or "").strip() or None,
            reduce_only=bool(ticket.get("reduce_only", False)),
            close_position=bool(ticket.get("close_position", ticket.get("reduce_only", False))),
        )


class _CanonicalOrderAdapter:
    def __init__(self, owner: StandardBrokerExternalExecutionAdapter) -> None:
        self._owner = owner

    def apply_fill(self, raw_fill: object) -> object:
        reference = self._owner._reference(raw_fill)
        if not reference:
            raise StandardBrokerExternalExecutionError("fill_order_identity_required")
        return self._owner._binding.query(reference)


__all__ = [
    "PROTECTED_CAPABILITY_REVISION",
    "PROTECTED_EXTERNAL_EXECUTION_CAPABILITIES",
    "PROTECTED_EXTERNAL_PROFILE",
    "PROTECTED_PROTECTION_MATRIX_ID",
    "STANDARD_BROKER_RELEASE_SHA",
    "StandardBrokerExternalExecutionAdapter",
    "StandardBrokerExternalExecutionError",
]
