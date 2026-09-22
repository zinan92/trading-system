"""Nautilus-backed Hyperliquid Testnet transport behind the Broker boundary."""

from collections.abc import Callable, Mapping
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import inspect
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import re
from concurrent.futures import ThreadPoolExecutor
from time import sleep, time_ns

from ...capabilities import CapabilityDescriptor
from ...external_host import digest_canonical
from ...models import BrokerEnvironment, Provenance, SignerKind
from ...models import AccountScope
from ...runtime import BrokerRuntimeSession, RuntimeBoundaryError, SignerReference, is_read_only_capabilities
from .bridge import NautilusAdapterMetadata
from .credentials import LocalFileSecretProvider
from .protection import enabled_external_testnet_position_protection_capabilities

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
NAUTILUS_HYPERLIQUID_VERSION = "1.230.0"
NAUTILUS_HYPERLIQUID_COMMIT = "8160730c7c550480b0a439fb11086a4c4de15f0b"


def default_testnet_capabilities(revision: str = "hyperliquid-testnet-runtime-v1") -> CapabilityDescriptor:
    """Return the deliberately narrow first external Testnet capability profile."""

    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations={
            "market_data": frozenset({"ticker"}),
            "instrument": frozenset({"read"}),
            "account": frozenset({"read", "positions"}),
            "order_execution": frozenset(
                {"submit", "cancel", "replace", "query", "open_orders", "fills"}
            ),
            "fee": frozenset({"read", "schedule", "fill"}),
        },
        revision=revision,
    )


def enabled_testnet_position_protection_capabilities(
    revision: str = "hyperliquid-testnet-position-protection-runtime-v1",
) -> CapabilityDescriptor:
    """Opt-in external capability set for position-level TP/SL groups."""

    capabilities = default_testnet_capabilities(revision)
    operations = dict(capabilities.operations)
    protection = enabled_external_testnet_position_protection_capabilities()
    operations["protection_order"] = frozenset(
        capability
        for capability, supported in protection.values.items()
        if supported
    )
    return CapabilityDescriptor(
        broker_id=capabilities.broker_id,
        environment=capabilities.environment,
        operations=operations,
        revision=revision,
    )


MAINNET_BTC_READONLY_REVISION = "hyperliquid-mainnet-btc-readonly-v1"
MAINNET_BTC_INSTRUMENTS = frozenset({"BTC-USD-PERP.HYPERLIQUID"})


def mainnet_btc_readonly_capabilities(revision: str = MAINNET_BTC_READONLY_REVISION) -> CapabilityDescriptor:
    """Mainnet reads for one BTC sub-account; no submit, cancel, replace or protection."""

    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.MAINNET,
        operations={
            "market_data": frozenset({"ticker"}),
            "instrument": frozenset({"read"}),
            "account": frozenset({"read", "positions"}),
            "order_execution": frozenset({"query", "open_orders", "fills"}),
            "fee": frozenset({"read", "schedule", "fill"}),
        },
        revision=revision,
    )


@dataclass(frozen=True)
class HyperliquidMainnetReadOnlyBackendConfig:
    """Public, non-secret identity for the read-only Mainnet BTC sub-account."""

    account_address: str
    expected_version: str = NAUTILUS_HYPERLIQUID_VERSION
    expected_commit: str = NAUTILUS_HYPERLIQUID_COMMIT
    execution_scope: str = "hypercore:default"
    capability_revision: str = MAINNET_BTC_READONLY_REVISION
    capabilities: CapabilityDescriptor = field(default_factory=mainnet_btc_readonly_capabilities)

    def __post_init__(self) -> None:
        if not _ADDRESS.fullmatch(self.account_address):
            raise RuntimeBoundaryError(
                "account_address_invalid",
                "Hyperliquid Mainnet sub-account address must be a 20-byte hex address",
            )
        if (
            self.capabilities.environment is not BrokerEnvironment.MAINNET
            or self.capabilities.broker_id != "hyperliquid"
            or self.capabilities.revision != self.capability_revision
            or self.capabilities != mainnet_btc_readonly_capabilities(self.capability_revision)
            or self.capability_revision != MAINNET_BTC_READONLY_REVISION
            or not is_read_only_capabilities(self.capabilities)
        ):
            raise RuntimeBoundaryError(
                "mainnet_capabilities_invalid",
                "Mainnet backend accepts only the exact read-only BTC capability set",
            )
        if (
            self.expected_version != NAUTILUS_HYPERLIQUID_VERSION
            or self.expected_commit != NAUTILUS_HYPERLIQUID_COMMIT
        ):
            raise RuntimeBoundaryError(
                "nautilus_release_not_pinned",
                "Mainnet backend must use the reviewed Nautilus 1.230.0 release commit",
            )


@dataclass(frozen=True)
class HyperliquidTestnetBackendConfig:
    """Public, non-secret identity for one approved Testnet transport."""

    account_address: str
    expected_version: str = NAUTILUS_HYPERLIQUID_VERSION
    expected_commit: str = NAUTILUS_HYPERLIQUID_COMMIT
    execution_scope: str = "hypercore:default"
    capability_revision: str = "hyperliquid-testnet-runtime-v1"
    capabilities: CapabilityDescriptor = field(default_factory=default_testnet_capabilities)

    def __post_init__(self) -> None:
        if not _ADDRESS.fullmatch(self.account_address):
            raise RuntimeBoundaryError(
                "account_address_invalid",
                "Hyperliquid Testnet account address must be a 20-byte hex address",
            )
        if self.capabilities.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "capability_environment_mismatch",
                "external Hyperliquid backend capabilities must be Testnet",
            )
        if self.capabilities.broker_id != "hyperliquid":
            raise RuntimeBoundaryError(
                "capability_broker_mismatch",
                "external Hyperliquid backend capabilities must belong to Hyperliquid",
            )
        if self.capabilities.revision != self.capability_revision:
            raise RuntimeBoundaryError(
                "capability_revision_mismatch",
                "backend capability revision must match its public configuration",
            )
        if (
            self.expected_version != NAUTILUS_HYPERLIQUID_VERSION
            or self.expected_commit != NAUTILUS_HYPERLIQUID_COMMIT
        ):
            raise RuntimeBoundaryError(
                "nautilus_release_not_pinned",
                "Testnet backend must use the reviewed Nautilus 1.230.0 release commit",
            )


class NautilusHyperliquidTestnetBackend:
    """Synchronous canonical backend over Nautilus' async Hyperliquid client.

    The client owns REST, signing, nonce, and response parsing.  This class
    only translates the already-canonical runtime requests and redacts native
    transport objects before they cross into the standard-broker model.
    """

    local_only = False
    external_network = True
    transport_state = "external_testnet"
    provenance_source = "nautilus-hyperliquid.testnet"
    _session_environment = BrokerEnvironment.TESTNET
    _hyperliquid_environment = "TESTNET"

    def __init__(
        self,
        *,
        session: BrokerRuntimeSession,
        config: HyperliquidTestnetBackendConfig,
        secrets: LocalFileSecretProvider,
        client_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        if session.environment is not self._session_environment:
            raise RuntimeBoundaryError(
                "external_environment_invalid",
                f"this Nautilus Hyperliquid external backend is {self._session_environment.value}-only",
            )
        if session.signer.kind is not SignerKind.API_AGENT:
            raise RuntimeBoundaryError(
                "api_agent_required",
                "the first external proof requires an API-agent signer",
            )
        if session.account.address != config.account_address:
            raise RuntimeBoundaryError(
                "account_identity_mismatch",
                "backend account does not match the runtime session",
            )
        if session.capabilities != config.capabilities:
            raise RuntimeBoundaryError(
                "capability_mismatch",
                "backend capabilities do not match the runtime session",
            )
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version=config.expected_version,
            commit=config.expected_commit,
            capabilities=config.capabilities,
        )
        self._session = session
        self._config = config
        self._secrets = secrets
        self._client_factory = client_factory or self._default_client_factory
        self._client: object | None = None
        self._instruments: dict[str, object] = {}
        self._protection_orders: dict[str, dict[str, object]] = {}
        self._activated = False

    def activate(self, *, release_sha: str | None) -> None:
        """Arm external calls only after the runtime's release-bound preflight."""

        if release_sha is None or not re.fullmatch(r"[0-9a-f]{40}", release_sha):
            raise RuntimeBoundaryError(
                "testnet_release_binding_required",
                "external backend activation requires a full lowercase release SHA",
            )
        self._activated = True

    def invoke(self, port: str, operation: str, request: object) -> object:
        """Invoke one supported canonical operation after runtime preflight."""

        if not self._activated:
            raise RuntimeBoundaryError(
                "external_backend_not_activated",
                "external Testnet backend must be armed by runtime preflight",
            )
        if not isinstance(request, Mapping):
            raise RuntimeBoundaryError(
                "canonical_request_required",
                "external Hyperliquid backend requires a typed mapping request",
            )
        if port == "order_execution":
            return self._invoke_order(operation, request)
        if port == "protection_order":
            return self._invoke_protection(operation, request)
        if port == "market_data" and operation == "ticker":
            return self._read_ticker(request)
        if port == "instrument" and operation == "read":
            return self._read_instruments()
        if port == "account" and operation == "read":
            return self._read_account()
        if port == "account" and operation == "positions":
            return self._read_positions(request)
        if port == "fee" and operation in {"read", "schedule"}:
            return self._read_fees()
        if port == "fee" and operation == "fill":
            return self._read_fill(request)
        raise RuntimeBoundaryError(
            "external_operation_unsupported",
            f"Testnet backend does not implement {port}.{operation}",
        )

    def _invoke_order(self, operation: str, request: Mapping[str, object]) -> object:
        if operation == "submit":
            return self._submit(request)
        if operation == "cancel":
            return self._cancel(request)
        if operation == "replace":
            return self._replace(request)
        if operation == "query":
            return self._query(request)
        if operation == "open_orders":
            return self._open_orders(request)
        if operation == "fills":
            return self._fills(request)
        raise RuntimeBoundaryError(
            "external_operation_unsupported",
            f"Testnet order backend does not implement {operation}",
        )

    def _invoke_protection(self, operation: str, request: Mapping[str, object]) -> object:
        """Map one canonical position-level TP/SL group through Nautilus.

        The default public profile still advertises this port as unavailable
        until a release enables the capability matrix.  Keeping the mapping
        behind the same backend makes the eventual enablement explicit and
        testable without exposing native order objects to consumers.
        """

        protection_id = str(request.get("protectionId") or "").strip()
        if not protection_id:
            raise RuntimeBoundaryError(
                "protection_id_required",
                "external protection requests require a canonical protectionId",
            )
        if operation == "submit":
            return self._submit_protection(request)
        if operation == "query":
            return self._query_protection(protection_id)
        if operation == "cancel":
            return self._cancel_protection(protection_id)
        if operation == "replace":
            return self._replace_protection(request)
        raise RuntimeBoundaryError(
            "external_operation_unsupported",
            f"Testnet protection backend does not implement {operation}",
        )

    def _submit_protection(self, request: Mapping[str, object]) -> Mapping[str, object]:
        protection_id = str(request.get("protectionId") or "").strip()
        grouping = str(request.get("grouping") or "").strip()
        legs = request.get("legs")
        if grouping != "positionTpsl":
            raise RuntimeBoundaryError(
                "protection_grouping_unsupported",
                "external Testnet v1 only submits an existing-position positionTpsl group",
            )
        if str(request.get("quantityPolicy") or "").strip().lower() != "position_following":
            raise RuntimeBoundaryError(
                "protection_quantity_policy_unsupported",
                "external Testnet v1 requires position-following protection coverage",
            )
        if not isinstance(legs, list) or len(legs) != 2:
            raise RuntimeBoundaryError(
                "protection_group_invalid",
                "positionTpsl requires exactly one TP and one SL leg",
            )
        orders = self._build_protection_orders(request)
        reports = self._call("submit_orders", orders)
        if not isinstance(reports, (list, tuple)) or not reports:
            rows, evidence = self._recover_protection_rows(request)
            if len(rows) == 2:
                self._protection_orders[protection_id] = {
                    "request": dict(request),
                    "rows": tuple(rows),
                }
                return self._protection_observation(
                    protection_id=protection_id,
                    operation="submit",
                    state="submitted",
                    covered_quantity=Decimal("0"),
                    rows=rows,
                )
            reason = "protection_submit_partial" if rows else "protection_submit_unconfirmed"
            raise RuntimeBoundaryError(
                reason,
                f"Nautilus returned no protection report; recovery evidence: {evidence}",
            )
        if len(reports) == len(orders):
            rows = tuple(self._protection_report(report) for report in reports)
        elif len(reports) == 1 and len(legs) == 2:
            # Hyperliquid returns one group-level status for positionTpsl.  The
            # child CLOIDs are deterministic and can be queried individually
            # once the user-events stream assigns their venue order IDs.
            group_row = self._protection_report(reports[0])
            if group_row["status"] in {
                "unknown",
                "rejected",
                "canceled",
                "cancelled",
                "filled",
                "partially_filled",
                "partial",
            }:
                raise RuntimeBoundaryError(
                    "protection_group_submit_rejected",
                    "Nautilus returned a non-accepted positionTpsl group report",
                )
            rows = tuple(
                {
                    "status": "waiting_for_trigger",
                    "oid": None,
                    "cloid": self._protection_client_id(
                        protection_id,
                        index,
                        leg,
                        quantity=request.get("quantity"),
                    ),
                }
                for index, leg in enumerate(legs)
                if isinstance(leg, Mapping)
            )
        else:
            raise RuntimeBoundaryError(
                "protection_submit_response_invalid",
                "Nautilus returned an ambiguous protection group response",
            )
        self._protection_orders[protection_id] = {
            "request": dict(request),
            "rows": rows,
        }
        return self._protection_observation(
            protection_id=protection_id,
            operation="submit",
            state=(
                "submitted"
                if all(
                    row["status"]
                    not in {
                        "unknown",
                        "rejected",
                        "canceled",
                        "cancelled",
                        "filled",
                        "partially_filled",
                        "partial",
                    }
                    for row in rows
                )
                else "unknown"
            ),
            covered_quantity=Decimal("0"),
            rows=rows,
        )

    def _recover_protection_rows(
        self, request: Mapping[str, object]
    ) -> tuple[list[dict[str, object]], str]:
        """Recover native legs by deterministic identity, never by quantity."""

        instrument = self._instrument(request)
        legs = request["legs"]
        assert isinstance(legs, list)
        protection_id = str(request.get("protectionId") or "")
        expected = [
            (
                index,
                leg,
                self._protection_client_id(
                    protection_id, index, leg, quantity=request.get("quantity")
                ),
            )
            for index, leg in enumerate(legs)
            if isinstance(leg, Mapping)
        ]
        found: dict[int, dict[str, object]] = {}
        rounds: list[dict[str, object]] = []
        for attempt in range(5):
            observed: list[dict[str, object]] = []
            for index, leg, sbp_id in expected:
                if index in found:
                    continue
                venue_cloid = self._native_protection_cloid(sbp_id)
                for stage, lookup in (("sbp", sbp_id), ("venue", venue_cloid)):
                    try:
                        report = self._call(
                            "request_order_status_report",
                            venue_order_id=None,
                            client_order_id=self._optional_client_order_id({"cloid": lookup}),
                        )
                    except Exception as exc:  # noqa: BLE001 - recovery remains fail-closed.
                        observed.append(
                            {"stage": stage, "leg": index, "error": type(exc).__name__}
                        )
                        continue
                    if report is None:
                        observed.append({"stage": stage, "leg": index, "report": None})
                        continue
                    row = self._protection_report(report)
                    observed.append(
                        {"stage": stage, "leg": index, **self._protection_summary(row)}
                    )
                    if self._protection_row_owned(row, sbp_id):
                        found[index] = self._checked_protection_row(
                            row, leg, sbp_id, venue_cloid
                        )
                        break

            try:
                reports = self._call("request_order_status_reports", str(instrument.id))
            except Exception as exc:  # noqa: BLE001 - recovery remains fail-closed.
                observed.append({"stage": "list", "error": type(exc).__name__})
            else:
                listed = (
                    [self._protection_report(item) for item in reports]
                    if isinstance(reports, (list, tuple))
                    else []
                )
                observed.append(
                    {
                        "stage": "list",
                        "count": len(listed),
                        "reports": [self._protection_summary(row) for row in listed[:20]],
                    }
                )
                for index, leg, sbp_id in expected:
                    owned = [
                        row for row in listed if self._protection_row_owned(row, sbp_id)
                    ]
                    if len(owned) > 1:
                        self._raise_recovery_conflict(
                            sbp_id,
                            ["identity:multiple_list_reports"],
                            owned,
                            rounds + [{"attempt": attempt + 1, "observed": observed}],
                        )
                    if owned and index not in found:
                        found[index] = self._checked_protection_row(
                            owned[0], leg, sbp_id, self._native_protection_cloid(sbp_id)
                        )
            rounds.append({"attempt": attempt + 1, "observed": observed})
            if len(found) == len(expected):
                break
            if attempt < 4:
                sleep(1)
        rows = [found[index] for index, _, _ in expected if index in found]
        return rows, self._protection_recovery_evidence(rows, rounds)

    @classmethod
    def _native_protection_cloid(cls, client_id: str) -> str:
        try:
            from nautilus_trader.core import nautilus_pyo3

            return str(
                nautilus_pyo3.hyperliquid_cloid_from_client_order_id(
                    nautilus_pyo3.ClientOrderId(client_id)
                )
            )
        except Exception as exc:  # noqa: BLE001 - identity translation is a hard boundary.
            raise RuntimeBoundaryError(
                "client_order_identity_normalization_failed",
                "Nautilus could not derive the native Hyperliquid protection identity",
            ) from exc

    @classmethod
    def _protection_row_owned(cls, row: Mapping[str, object], sbp_id: str) -> bool:
        return str(row.get("cloid") or "").strip() in {
            sbp_id,
            cls._native_protection_cloid(sbp_id),
        }

    @classmethod
    def _checked_protection_row(
        cls,
        row: Mapping[str, object],
        leg: Mapping[str, object],
        sbp_id: str,
        venue_cloid: str,
    ) -> dict[str, object]:
        conflicts: list[str] = []
        if str(row.get("status") or "").lower() not in {
            "resting", "waiting_for_trigger", "waiting_for_fill", "open"
        }:
            conflicts.append("status")
        if row.get("reduce_only") is not True:
            conflicts.append("reduce_only")
        if str(row.get("side") or "") != str(leg.get("side") or ""):
            conflicts.append("side")
        try:
            if Decimal(str(row.get("trigger_px"))) != Decimal(str(leg.get("triggerPx"))):
                conflicts.append("trigger_px")
        except (InvalidOperation, TypeError, ValueError):
            conflicts.append("trigger_px")
        if conflicts:
            cls._raise_recovery_conflict(sbp_id, conflicts, [row])
        return {**dict(row), "cloid": venue_cloid, "client_order_id": sbp_id}

    @classmethod
    def _raise_recovery_conflict(
        cls,
        sbp_id: str,
        fields: list[str],
        rows: list[Mapping[str, object]],
        rounds: list[Mapping[str, object]] = (),
    ) -> None:
        evidence = cls._protection_recovery_evidence([], rounds)
        raise RuntimeBoundaryError(
            "protection_recovery_conflict",
            f"protection leg {sbp_id} conflicts on {','.join(fields)}; "
            f"reports={json.dumps([cls._protection_summary(row) for row in rows], sort_keys=True)}; "
            f"recovery evidence: {evidence}",
        )

    @classmethod
    def _protection_summary(cls, row: Mapping[str, object]) -> dict[str, object]:
        return {
            key: row.get(key)
            for key in (
                "status", "oid", "cloid", "side", "trigger_px", "quantity", "reduce_only"
            )
            if row.get(key) is not None
        }

    @classmethod
    def _protection_recovery_evidence(
        cls,
        rows: list[Mapping[str, object]],
        rounds: list[Mapping[str, object]] = (),
    ) -> str:
        evidence = json.dumps(
            {"rows": [cls._protection_summary(row) for row in rows], "rounds": list(rounds)[-2:]},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return evidence if len(evidence) <= 4000 else evidence[:3990] + "...<cut>"

    def _query_protection(self, protection_id: str) -> Mapping[str, object]:
        record = self._protection_orders.get(protection_id)
        if not isinstance(record, Mapping):
            return self._protection_observation(
                protection_id=protection_id,
                operation="query",
                state="unknown",
                covered_quantity=Decimal("0"),
                rows=(),
            )
        request = record.get("request")
        old_rows = record.get("rows")
        if not isinstance(request, Mapping) or not isinstance(old_rows, (list, tuple)):
            return self._protection_observation(
                protection_id=protection_id,
                operation="query",
                state="unknown",
                covered_quantity=Decimal("0"),
                rows=(),
            )
        rows: list[dict[str, object]] = []
        for row in old_rows:
            if not isinstance(row, Mapping):
                continue
            try:
                report = self._call(
                    "request_order_status_report",
                    venue_order_id=row.get("oid") or None,
                    client_order_id=row.get("cloid") or None,
                )
                normalized = self._protection_report(report)
                if not self._protection_identity_matches(row, normalized):
                    raise RuntimeBoundaryError(
                        "protection_identity_conflict",
                        "Hyperliquid protection status report conflicts with its requested identity",
                    )
                rows.append(normalized)
            except Exception:
                rows.append(
                    {
                        "status": "unknown",
                        "oid": row.get("oid"),
                        "cloid": row.get("cloid"),
                    }
                )
        state = self._protection_state(rows)
        covered = self._protection_covered_quantity(rows) if state == "active" else Decimal("0")
        if state == "active" and (covered is None or covered <= 0):
            state = "unknown"
            covered = Decimal("0")
        self._protection_orders[protection_id] = {"request": dict(request), "rows": tuple(rows)}
        return self._protection_observation(
            protection_id=protection_id,
            operation="query",
            state=state,
            covered_quantity=covered,
            rows=rows,
        )

    def _cancel_protection(self, protection_id: str) -> Mapping[str, object]:
        record = self._protection_orders.get(protection_id)
        if not isinstance(record, Mapping):
            return self._protection_observation(
                protection_id=protection_id,
                operation="cancel",
                state="unknown",
                covered_quantity=Decimal("0"),
                rows=(),
            )
        request = record.get("request")
        rows = record.get("rows")
        if not isinstance(request, Mapping) or not isinstance(rows, (list, tuple)):
            return self._protection_observation(
                protection_id=protection_id,
                operation="cancel",
                state="unknown",
                covered_quantity=Decimal("0"),
                rows=(),
            )
        instrument = self._instrument(request)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            self._call(
                "cancel_order",
                instrument.id,
                client_order_id=self._optional_client_order_id(row),
                venue_order_id=self._optional_venue_order_id(row),
            )
        observed = self._query_protection(protection_id)
        state = "canceled" if observed.get("state") == "canceled" else "unknown"
        return self._rewrite_protection_observation(
            observed,
            operation="cancel",
            state=state,
        )

    def _replace_protection(self, request: Mapping[str, object]) -> Mapping[str, object]:
        protection_id = str(request.get("protectionId") or "").strip()
        # Hyperliquid's public order API exposes modify for individual orders,
        # while the canonical group identity is owned here.  Use an explicit,
        # same-venue cancel -> terminal query -> grouped submit decomposition;
        # any unknown cancel or submit outcome stops before the next step.
        canceled = self._cancel_protection(protection_id)
        if canceled.get("state") != "canceled":
            return self._protection_observation(
                protection_id=protection_id,
                operation="replace",
                state="unknown",
                covered_quantity=Decimal("0"),
                rows=(),
            )
        submitted = self._submit_protection(request)
        return self._rewrite_protection_observation(
            submitted,
            operation="replace",
            state=str(submitted.get("state") or "unknown"),
        )

    def _build_protection_orders(self, request: Mapping[str, object]) -> list[object]:
        """Build Nautilus order objects for one positionTpsl group."""

        try:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.model.enums import (
                ContingencyType,
                OrderSide,
                TimeInForce,
                TriggerType,
            )
            from nautilus_trader.model.identifiers import (
                ClientOrderId,
                InstrumentId,
                StrategyId,
                TraderId,
            )
            from nautilus_trader.model.objects import Price, Quantity
            from nautilus_trader.model.orders import (
                LimitIfTouchedOrder,
                StopLimitOrder,
                StopMarketOrder,
            )
            from nautilus_trader.cache.transformers import transform_order_to_pyo3
        except ImportError as exc:
            raise RuntimeBoundaryError(
                "nautilus_protection_order_model_missing",
                "Nautilus Python order models are required for grouped protection",
            ) from exc

        instrument_id = str(request.get("instrumentId") or "").strip()
        if not instrument_id:
            raise RuntimeBoundaryError("instrument_required", "protection instrument is required")
        if not instrument_id.endswith(".HYPERLIQUID"):
            instrument_id = instrument_id + ".HYPERLIQUID"
        instrument = InstrumentId.from_str(instrument_id)
        quantity = Quantity.from_str(str(request.get("quantity") or "0"))
        legs = request.get("legs")
        if not isinstance(legs, list):
            raise RuntimeBoundaryError("protection_group_invalid", "protection legs must be a list")
        client_ids = [
            ClientOrderId(
                self._protection_client_id(
                    str(request.get("protectionId")),
                    index,
                    leg,
                    quantity=request.get("quantity"),
                )
            )
            for index, leg in enumerate(legs)
            if isinstance(leg, Mapping)
        ]
        if len(client_ids) != len(legs):
            raise RuntimeBoundaryError("protection_group_invalid", "protection legs must be mappings")
        trader_id = TraderId("STANDARD-BROKER")
        strategy_id = StrategyId("EXTERNAL-PROTECTION")
        now_ns = time_ns()
        result: list[object] = []
        for index, (leg, client_id) in enumerate(zip(legs, client_ids, strict=True)):
            side = OrderSide.BUY if str(leg.get("side") or "").upper() == "B" else OrderSide.SELL
            execution = str(leg.get("execution") or "").lower()
            tpsl = str(leg.get("tpsl") or "").lower()
            trigger_reference = str(leg.get("triggerReference") or "").strip().lower()
            if trigger_reference != "mark":
                raise RuntimeBoundaryError(
                    "protection_trigger_reference_unsupported",
                    "external Testnet v1 requires mark-price protection triggers",
                )
            trigger = Price.from_str(str(leg.get("triggerPx") or "0"))
            limit_value = leg.get("limitPx")
            linked = [other for position, other in enumerate(client_ids) if position != index]
            common = {
                "trader_id": trader_id,
                "strategy_id": strategy_id,
                "instrument_id": instrument,
                "client_order_id": client_id,
                "order_side": side,
                "quantity": quantity,
                "trigger_price": trigger,
                "trigger_type": TriggerType.MARK_PRICE,
                "init_id": UUID4(),
                "ts_init": now_ns,
                "time_in_force": TimeInForce.GTC,
                "reduce_only": leg.get("reduceOnly") is True,
                "contingency_type": ContingencyType.OCO,
                "linked_order_ids": linked,
            }
            if not common["reduce_only"]:
                raise RuntimeBoundaryError(
                    "protection_reduce_only_required",
                    "every external protection leg must be reduce-only",
                )
            if tpsl == "tp" and execution == "market":
                # Nautilus 1.230.0 cannot convert MARKET_IF_TOUCHED at the
                # public pyo3 transformer boundary. The capability matrix
                # rejects this model before transport; keep the backend
                # fail-closed for direct callers as well.
                raise RuntimeBoundaryError(
                    "protection_leg_unsupported",
                    "take-profit market protection is unsupported by the pinned "
                    "Nautilus conversion boundary; use take-profit limit",
                )
            elif tpsl == "sl" and execution == "market":
                order = StopMarketOrder(**common)
            elif tpsl == "tp" and execution == "limit":
                order = LimitIfTouchedOrder(
                    **common,
                    price=Price.from_str(str(limit_value or "0")),
                )
            elif tpsl == "sl" and execution == "limit":
                order = StopLimitOrder(
                    **common,
                    price=Price.from_str(str(limit_value or "0")),
                )
            else:
                raise RuntimeBoundaryError(
                    "protection_leg_unsupported",
                    "external protection leg type is unsupported",
                )
            try:
                result.append(transform_order_to_pyo3(order))
            except Exception as exc:  # noqa: BLE001 - conversion is a hard boundary.
                raise RuntimeBoundaryError(
                    "nautilus_protection_order_conversion_failed",
                    "Nautilus order could not be converted at the public order seam",
                ) from exc
        return result

    @staticmethod
    def _protection_client_id(
        protection_id: str,
        index: int,
        leg: object,
        *,
        quantity: object,
    ) -> str:
        digest = hashlib.sha256(
            f"{protection_id}|{index}|{str(leg)}|{str(quantity)}".encode("utf-8")
        ).hexdigest()[:28]
        return f"SBP-{digest}"

    def _protection_report(self, report: object) -> dict[str, object]:
        mapping = self._to_mapping(report)
        nested_order = mapping.get("order")
        if isinstance(nested_order, Mapping):
            # Replay/API payloads wrap the native order and put its lifecycle
            # status beside it.  Keep this normalization at the adapter edge.
            mapping = {**dict(nested_order), **mapping}
        event = self._order_event(mapping)
        quantity = self._string_value(report, mapping, "quantity", "sz", "size") or event.get("sz")
        trigger_px = self._string_value(
            report, mapping, "trigger_price", "triggerPx", "trigger_px"
        )
        reduce_only = mapping.get("reduce_only")
        if reduce_only is None:
            reduce_only = mapping.get("reduceOnly")
        return {
            "status": str(event.get("status") or "unknown").lower(),
            "oid": event.get("oid"),
            "cloid": event.get("cloid"),
            "side": event.get("side"),
            "px": event.get("px"),
            "sz": event.get("sz"),
            "quantity": quantity,
            "trigger_px": trigger_px,
            "reduce_only": reduce_only is True,
            "time": event.get("time"),
        }

    @classmethod
    def _protection_identity_matches(
        cls,
        requested: Mapping[str, object],
        observed: Mapping[str, object],
    ) -> bool:
        requested_oid = str(requested.get("oid") or "").strip()
        requested_cloid = str(requested.get("cloid") or "").strip()
        observed_oid = str(observed.get("oid") or "").strip()
        observed_cloid = str(observed.get("cloid") or "").strip()
        if requested_oid and observed_oid and requested_oid != observed_oid:
            return False
        if requested_cloid:
            if observed_cloid:
                return observed_cloid in cls._client_order_id_candidates(requested_cloid)
            return bool(requested_oid and observed_oid == requested_oid)
        return bool(requested_oid and observed_oid == requested_oid)

    @staticmethod
    def _protection_state(rows: list[dict[str, object]]) -> str:
        statuses = {str(row.get("status") or "unknown").lower() for row in rows}
        if not rows or "unknown" in statuses or "rejected" in statuses:
            return "unknown"
        if statuses and statuses <= {"canceled", "cancelled"}:
            return "canceled"
        if statuses & {"filled", "partially_filled", "partial"}:
            return "unknown"
        if statuses <= {"resting", "waiting_for_trigger", "waiting_for_fill"}:
            return "active"
        return "unknown"

    @staticmethod
    def _protection_covered_quantity(
        rows: list[dict[str, object]],
    ) -> Decimal | None:
        """Return the shared coverage of all active protection legs."""

        quantities: list[Decimal] = []
        for row in rows:
            raw_quantity = row.get("quantity")
            if raw_quantity is None:
                return None
            try:
                quantity = Decimal(str(raw_quantity))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if not quantity.is_finite() or quantity < 0:
                return None
            quantities.append(quantity)
        return min(quantities) if quantities else None

    def _protection_observation(
        self,
        *,
        protection_id: str,
        operation: str,
        state: str,
        covered_quantity: Decimal,
        rows: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    ) -> Mapping[str, object]:
        order_ids = [
            str(row.get("oid") or row.get("cloid") or "")
            for row in rows
            if str(row.get("oid") or row.get("cloid") or "")
        ]
        result = self._with_provenance(
            {
                "protection_id": protection_id,
                "operation": operation,
                "state": state,
                "accepted": state not in {"unknown", "rejected"},
                "covered_quantity": str(covered_quantity),
                "order_ids": order_ids,
            }
        )
        result["observation_digest"] = digest_canonical(result)
        return result

    @staticmethod
    def _rewrite_protection_observation(
        observation: Mapping[str, object],
        *,
        operation: str,
        state: str,
    ) -> Mapping[str, object]:
        """Rewrite a canonical observation and re-seal its final contents."""

        result = dict(observation)
        result["operation"] = operation
        result["state"] = state
        result["accepted"] = state not in {"unknown", "rejected"}
        if state == "unknown":
            result["covered_quantity"] = "0"
        result["observation_digest"] = digest_canonical(
            {key: value for key, value in result.items() if key != "observation_digest"}
        )
        return result

    def _submit(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        order_side, order_type, time_in_force, post_only = self._order_enums(request)
        result = self._call(
            "submit_order",
            instrument.id,
            self._client_order_id(request),
            order_side,
            order_type,
            self._quantity(request),
            time_in_force,
            self._price(request),
            None,
            post_only,
            bool(request.get("reduceOnly", False)),
        )
        response = self._submit_response(result, request)
        return self._with_provenance(response)

    def _cancel(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        self._call(
            "cancel_order",
            instrument.id,
            client_order_id=self._optional_client_order_id(request),
            venue_order_id=self._optional_venue_order_id(request),
        )
        return self._with_provenance({"status": "ok"})

    def _replace(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        order_side, order_type, time_in_force, post_only = self._order_enums(request)
        try:
            self._call(
                "modify_order",
                instrument.id,
                self._venue_order_id(request),
                order_side,
                order_type,
                self._price(request),
                self._quantity(request),
                None,
                bool(request.get("reduceOnly", False)),
                post_only,
                time_in_force,
                self._client_order_id(request),
            )
        except Exception as exc:
            error_text = str(exc).lower()
            if not (
                "invalid new order" in error_text
                or "post only order would have immediately matched" in error_text
            ):
                raise
            return self._cancel_then_submit_replacement(request)
        # The canonical lifecycle treats modify as cancel-replace.  The
        # replacement identity is promoted only after a later query/event;
        # do not pretend the modify response is a terminal order receipt.
        return self._with_provenance({"status": "ok"})

    def _cancel_then_submit_replacement(self, request: Mapping[str, object]) -> Mapping[str, object]:
        """Use an explicit, reconciled fallback for a deterministic modify rejection."""

        instrument = self._instrument(request)
        old_order_id = self._venue_order_id(request)
        old_client_id = self._client_order_id(request)
        old_report = self._call(
            "request_order_status_report",
            venue_order_id=str(old_order_id),
            client_order_id=str(old_client_id),
        )
        old_event = self._order_event(old_report)
        if old_event.get("status") not in {"canceled", "rejected"}:
            self._call(
                "cancel_order",
                instrument.id,
                client_order_id=old_client_id,
                venue_order_id=old_order_id,
            )
            old_report = self._call(
                "request_order_status_report",
                venue_order_id=str(old_order_id),
                client_order_id=str(old_client_id),
            )
            old_event = self._order_event(old_report)
        if old_event.get("status") not in {"canceled", "rejected"}:
            raise RuntimeBoundaryError(
                "replace_old_order_unreconciled",
                "native modify failed and the original order was not confirmed terminal",
            )
        replacement_cloid = str(request.get("replacement_cloid") or "").strip()
        if not replacement_cloid:
            raise RuntimeBoundaryError(
                "replacement_client_order_id_required",
                "explicit cancel-replace fallback requires a new client order ID",
            )
        replacement = dict(request)
        replacement.pop("oid", None)
        replacement.pop("replacement_cloid", None)
        replacement["cloid"] = replacement_cloid
        submitted = self._submit(replacement)
        return self._with_provenance(
            {
                "status": "ok",
                "native_cloid": replacement_cloid,
                "replacement": submitted,
            }
        )

    def _query(self, request: Mapping[str, object]) -> Mapping[str, object]:
        result = self._call(
            "request_order_status_report",
            venue_order_id=self._optional_venue_order_id_text(request),
            client_order_id=self._optional_client_order_id_text(request),
        )
        event = self._order_event(result)
        requested_cloid = self._optional_client_order_id_text(request)
        requested_client_candidates = (
            self._client_order_id_candidates(requested_cloid)
            if requested_cloid
            else ()
        )
        event = self._normalize_report_client_identity(
            event,
            requested_client_order_id=requested_cloid,
        )
        reported_cloid = event.get("cloid")
        requested_oid = self._optional_venue_order_id_text(request)
        reported_oid = event.get("oid")
        if (
            requested_cloid
            and reported_cloid == requested_cloid
            and requested_oid
            and reported_oid
            and reported_oid != requested_oid
        ):
            raise RuntimeBoundaryError(
                "order_identity_conflict",
                "Hyperliquid status report venue identity conflicts with the requested identity",
            )
        if (
            requested_cloid
            and not reported_cloid
            and requested_oid
            and reported_oid
            and reported_oid != requested_oid
        ):
            raise RuntimeBoundaryError(
                "order_identity_conflict",
                "Hyperliquid status report venue identity conflicts with the requested identity",
            )
        if requested_cloid and reported_cloid is None:
            event["cloid"] = requested_cloid
        elif requested_cloid and reported_cloid != requested_cloid:
            if reported_cloid in requested_client_candidates:
                if requested_oid and reported_oid and reported_oid != requested_oid:
                    raise RuntimeBoundaryError(
                        "order_identity_conflict",
                        "Hyperliquid status report venue identity conflicts with the requested identity",
                    )
                event["cloid"] = requested_cloid
            elif not reported_cloid and event.get("status") in {"canceled", "rejected", "filled", "partially_filled"}:
                same_venue_order = not requested_oid or not reported_oid or reported_oid == requested_oid
                if same_venue_order:
                    event["cloid"] = requested_cloid
                else:
                    raise RuntimeBoundaryError(
                        "order_identity_conflict",
                        "Hyperliquid status report venue identity conflicts with the requested identity",
                    )
            else:
                raise RuntimeBoundaryError(
                    "order_identity_conflict",
                    "Hyperliquid status report client identity conflicts with the requested identity",
                )
        return self._with_provenance(self._attach_fill(event, request))

    def _open_orders(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self._ensure_instruments()
        instrument_id = request.get("instrument_id")
        result = self._call(
            "request_order_status_reports",
            str(self._instrument_id(instrument_id)) if instrument_id else None,
        )
        rows = [self._open_order_event(item) for item in (result or [])]
        return self._with_provenance({"orders": rows})

    def _open_order_event(self, report: object) -> dict[str, object]:
        """Project Nautilus cumulative quantities into Hyperliquid open-order fields."""

        mapping = self._to_mapping(report)
        event = self._order_event(report)
        original_text = self._string_value(report, mapping, "quantity")
        if original_text is None:
            return event
        filled_text = self._string_value(report, mapping, "filled_qty") or "0"
        try:
            original = Decimal(original_text)
            filled = Decimal(filled_text)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeBoundaryError(
                "open_order_quantity_invalid",
                "Hyperliquid open-order quantities are not valid decimals",
            ) from exc
        remaining = original - filled
        if (
            not original.is_finite()
            or not filled.is_finite()
            or original <= 0
            or filled < 0
            or remaining < 0
        ):
            raise RuntimeBoundaryError(
                "open_order_quantity_invalid",
                "Hyperliquid open-order quantities are inconsistent",
            )
        instrument_id = self._string_value(report, mapping, "instrument_id")
        instrument = self._instruments.get(str(instrument_id)) if instrument_id else None
        precision = int(getattr(instrument, "size_precision", 0))
        quantum = Decimal(1).scaleb(-precision)
        event["sz"] = format(remaining.quantize(quantum), f".{precision}f")
        event["origSz"] = format(original.quantize(quantum), f".{precision}f")
        return event

    def _fills(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument_id = request.get("instrument_id")
        if not instrument_id:
            raise RuntimeBoundaryError(
                "instrument_required",
                "Hyperliquid external fill query requires an instrument scope",
            )
        result = self._call(
            "request_fill_reports",
            str(self._instrument_id(instrument_id)),
        )
        requested_oid = str(request.get("oid") or "")
        requested_cloid = str(request.get("cloid") or request.get("client_order_id") or "")
        requested_client_candidates = (
            self._client_order_id_candidates(requested_cloid)
            if requested_cloid
            else ()
        )
        rows: list[dict[str, object]] = []
        for report in result or []:
            event = self._fill_event(report, include_fee=True)
            reported_oid = str(event.get("oid") or "")
            reported_cloid = str(event.get("cloid") or "")
            if requested_cloid and reported_cloid and reported_cloid not in requested_client_candidates:
                continue
            if requested_oid and reported_oid and reported_oid != requested_oid:
                continue
            if (
                requested_oid
                and reported_oid != requested_oid
                and requested_cloid
                and reported_cloid not in requested_client_candidates
            ):
                continue
            if requested_oid and not requested_cloid and reported_oid != requested_oid:
                continue
            if requested_cloid and not requested_oid and reported_cloid not in requested_client_candidates:
                continue
            if requested_cloid and reported_cloid in requested_client_candidates:
                event["cloid"] = requested_cloid
            rows.append(event)
        return self._with_provenance({"fills": rows})

    def _read_instruments(self) -> Mapping[str, object]:
        self._ensure_instruments()
        meta = self._call("get_perp_meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError as exc:
                raise RuntimeBoundaryError(
                    "instrument_metadata_invalid",
                    "Nautilus perpetual metadata was not valid JSON",
                ) from exc
        return self._with_provenance(
            {
                "meta": meta,
                "instruments": [
                    self._to_mapping(value)
                    for key, value in self._instruments.items()
                    if key.endswith(".HYPERLIQUID")
                ],
            }
        )

    def _read_ticker(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        bid, ask, timestamp = _resolve(self._read_book_quote(instrument))
        return self._with_provenance(
            {
                "mid": str((Decimal(bid) + Decimal(ask)) / Decimal(2)),
                "bbo": {
                    "coin": str(instrument.raw_symbol),
                    "bbo": [{"px": bid}, {"px": ask}],
                    "time": timestamp,
                },
            }
        )

    async def _read_book_quote(self, instrument: object) -> tuple[str, str, int]:
        try:
            from nautilus_trader.core.nautilus_pyo3 import HyperliquidEnvironment
            from nautilus_trader.core.nautilus_pyo3 import HyperliquidWebSocketClient
            from nautilus_trader.model.data import capsule_to_data
        except ImportError as exc:
            raise RuntimeBoundaryError(
                "nautilus_runtime_missing",
                "nautilus_trader 1.230.0 is required for external Testnet BBO",
            ) from exc

        bids: dict[str, Decimal] = {}
        asks: dict[str, Decimal] = {}
        snapshot_ready = asyncio.Event()
        latest_timestamp = 0

        def on_message(message: object) -> None:
            nonlocal latest_timestamp
            try:
                data = capsule_to_data(message)
                deltas = getattr(data, "deltas", None)
                if deltas is None:
                    return
                for delta in deltas:
                    if delta.is_clear:
                        bids.clear()
                        asks.clear()
                        continue
                    side = getattr(getattr(delta, "order", None), "side", None)
                    price = str(getattr(getattr(delta, "order", None), "price", ""))
                    size = Decimal(str(getattr(getattr(delta, "order", None), "size", "0")))
                    side_name = str(getattr(side, "name", side)).split(".")[-1].upper()
                    book = bids if side_name == "BUY" else asks if side_name == "SELL" else None
                    if book is None:
                        continue
                    if delta.is_delete or size == 0:
                        book.pop(price, None)
                    else:
                        book[price] = size
                    latest_timestamp = max(
                        latest_timestamp,
                        int(getattr(data, "ts_event", 0)),
                    )
                if bids and asks:
                    snapshot_ready.set()
            except Exception:
                # A malformed WS payload is not a valid BBO.  The bounded
                # wait below will fail closed rather than using partial data.
                return

        client = HyperliquidWebSocketClient(
            environment=getattr(HyperliquidEnvironment, self._hyperliquid_environment)
        )
        try:
            await client.connect(asyncio.get_running_loop(), [instrument], on_message)
            await client.subscribe_book(instrument.id)
            await asyncio.wait_for(snapshot_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            raise RuntimeBoundaryError(
                "bbo_timeout",
                "Hyperliquid Testnet order book did not produce a complete BBO",
            ) from exc
        except Exception as exc:
            raise RuntimeBoundaryError(
                "market_data_transport",
                "Hyperliquid Testnet WebSocket BBO request failed",
            ) from exc
        finally:
            try:
                await client.close()
            except Exception:
                pass

        bid = max(bids, key=Decimal)
        ask = min(asks, key=Decimal)
        return bid, ask, latest_timestamp // 1_000_000

    def _read_account(self) -> Mapping[str, object]:
        state = self._call("request_account_state")
        return self._with_provenance({"data": self._to_mapping(state)})

    def _read_positions(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        # Nautilus' PyO3 method accepts the canonical InstrumentId string,
        # not the native InstrumentId object.  Keep that conversion inside
        # the adapter boundary so the core never sees provider types.
        reports = self._call("request_position_status_reports", str(instrument.id))
        return self._with_provenance(
            {"positions": [self._to_mapping(report) for report in reports or []]}
        )

    def _read_fees(self) -> Mapping[str, object]:
        value = self._call("info_user_fees")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise RuntimeBoundaryError(
                    "fee_response_invalid",
                    "Nautilus fee response was not valid JSON",
                ) from exc
        return self._with_provenance({"data": value})

    def _read_fill(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument = self._instrument(request)
        reports = self._call("request_fill_reports", str(instrument.id))
        requested_id = str(request.get("fill_id") or request.get("tid") or "")
        for report in reports or []:
            mapping = self._to_mapping(report)
            fill_id = self._string_value(report, mapping, "trade_id", "tid", "fill_id")
            if requested_id and fill_id != requested_id:
                continue
            return self._with_provenance(self._fill_event(report, include_fee=True))
        raise RuntimeBoundaryError("fill_not_found", "Hyperliquid did not return the requested fill")

    def _client_for_use(self) -> object:
        if self._client is None:
            private_key = self._secrets.resolve_private_key(self._session.signer)
            self._client = self._client_factory(private_key, self._config.account_address)
        return self._client

    @staticmethod
    def _default_client_factory(private_key: str, account_address: str) -> object:
        return _nautilus_http_client(private_key, account_address, "TESTNET")

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        client = self._client_for_use()
        function = getattr(client, method, None)
        if not callable(function):
            raise RuntimeBoundaryError(
                "nautilus_operation_missing",
                f"Nautilus Hyperliquid client lacks {method}",
            )

        async def invoke_in_loop() -> object:
            value = function(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value

        return _resolve(invoke_in_loop())

    def _ensure_instruments(self) -> None:
        if self._instruments:
            return
        try:
            result = self._call(
                "load_instrument_definitions",
                # Spot metadata is needed by Nautilus to resolve collateral
                # for the full perp metadata response, even though v1 does
                # not expose spot instruments to the canonical adapter.
                include_spot=True,
                include_perps=True,
                include_perps_hip3=False,
                include_outcomes=False,
            )
        except ValueError as exc:
            if "settlement currency" not in str(exc).lower():
                raise
            # Some Testnet metadata snapshots contain a malformed deferred
            # HIP-3 collateral definition.  v1 explicitly excludes HIP-3;
            # rebuild only the default validator-operated universe from the
            # authoritative perp metadata instead of silently accepting it.
            result = self._build_standard_instruments_from_meta()
        instruments = list(result or [])
        standard_perps = [
            instrument
            for instrument in instruments
            if self._is_standard_perp(instrument)
        ]
        if any(self._asset_index(instrument) is None for instrument in standard_perps):
            # load_instrument_definitions may return a usable instrument whose
            # info omits Hyperliquid's asset index.  Nautilus cancel/replace
            # resolves that index from the cached instrument, so rebuild the
            # default perp set from the authoritative metadata before caching.
            instruments = self._build_standard_instruments_from_meta()
        for instrument in instruments:
            cache = getattr(self._client_for_use(), "cache_instrument", None)
            if callable(cache):
                cache(instrument)
            instrument_id = str(instrument.id)
            if not instrument_id.endswith("-USD-PERP.HYPERLIQUID") or ":" in instrument_id:
                continue
            self._instruments[str(instrument.id)] = instrument
            self._instruments[str(instrument.raw_symbol)] = instrument

    @staticmethod
    def _is_standard_perp(instrument: object) -> bool:
        instrument_id = str(getattr(instrument, "id", ""))
        return instrument_id.endswith("-USD-PERP.HYPERLIQUID") and ":" not in instrument_id

    @staticmethod
    def _asset_index(instrument: object) -> object | None:
        info = getattr(instrument, "info", None)
        if isinstance(info, Mapping):
            return info.get("asset_index")
        return None

    def _build_standard_instruments_from_meta(self) -> list[object]:
        try:
            from nautilus_trader.core import nautilus_pyo3
        except ImportError as exc:
            raise RuntimeBoundaryError(
                "nautilus_runtime_missing",
                "nautilus_trader is required to build the default Testnet instrument universe",
            ) from exc
        raw_meta = self._call("get_perp_meta")
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except json.JSONDecodeError as exc:
                raise RuntimeBoundaryError(
                    "instrument_metadata_invalid",
                    "Hyperliquid perpetual metadata was not valid JSON",
                ) from exc
        universe = raw_meta.get("universe") if isinstance(raw_meta, Mapping) else None
        if not isinstance(universe, list) or not universe:
            raise RuntimeBoundaryError(
                "instrument_metadata_missing",
                "Hyperliquid perpetual metadata did not contain a universe",
            )
        instruments: list[object] = []
        now = time_ns()
        for position, raw in enumerate(universe):
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name or ":" in name:
                continue
            sz_decimals = int(raw.get("szDecimals", 0))
            if sz_decimals < 0 or sz_decimals > 6:
                raise RuntimeBoundaryError("instrument_metadata_invalid", "Hyperliquid size precision is invalid")
            price_decimals = 6 - sz_decimals
            instrument = nautilus_pyo3.CryptoPerpetual(
                nautilus_pyo3.InstrumentId.from_str(f"{name}-USD-PERP.HYPERLIQUID"),
                nautilus_pyo3.Symbol(name),
                nautilus_pyo3.Currency.from_str(name),
                nautilus_pyo3.Currency.from_str("USD"),
                nautilus_pyo3.Currency.from_str("USDC"),
                False,
                price_decimals,
                sz_decimals,
                nautilus_pyo3.Price.from_str(str(Decimal(1).scaleb(-price_decimals))),
                nautilus_pyo3.Quantity.from_str(str(Decimal(1).scaleb(-sz_decimals))),
                now,
                now,
                info={"asset_index": int(raw.get("index", position))},
            )
            instruments.append(instrument)
        if not instruments:
            raise RuntimeBoundaryError(
                "instrument_metadata_missing",
                "Hyperliquid metadata contained no default validator-operated perps",
            )
        return instruments

    def _instrument(self, request: Mapping[str, object]) -> object:
        self._ensure_instruments()
        value = request.get("instrument_id") or request.get("instrumentId") or request.get("coin")
        if not value:
            raise RuntimeBoundaryError("instrument_required", "Hyperliquid order instrument is required")
        key = str(value)
        instrument = self._instruments.get(key)
        if instrument is None and key.endswith("-USD-PERP"):
            instrument = self._instruments.get(key + ".HYPERLIQUID")
        if instrument is None:
            raise RuntimeBoundaryError("instrument_unknown", "Hyperliquid instrument is not in the loaded universe")
        return instrument

    def _instrument_id(self, value: object) -> object:
        self._ensure_instruments()
        instrument = self._instruments.get(str(value))
        if instrument is None and value is not None:
            instrument = self._instruments.get(str(value) + ".HYPERLIQUID")
        if instrument is None:
            raise RuntimeBoundaryError("instrument_unknown", "Hyperliquid instrument is not in the loaded universe")
        return instrument.id

    @staticmethod
    def _client_order_id(request: Mapping[str, object]) -> object:
        from nautilus_trader.core import nautilus_pyo3

        value = str(request.get("cloid") or request.get("client_order_id") or "").strip()
        if not value:
            raise RuntimeBoundaryError("client_order_id_required", "Hyperliquid client order ID is required")
        return nautilus_pyo3.ClientOrderId(value)

    @staticmethod
    def _optional_client_order_id(request: Mapping[str, object]) -> object | None:
        if not request.get("cloid") and not request.get("client_order_id"):
            return None
        return NautilusHyperliquidTestnetBackend._client_order_id(request)

    @staticmethod
    def _client_order_id_candidates(value: object) -> tuple[str, ...]:
        """Return the canonical ID and its deterministic native Hyperliquid CLOID."""

        text = str(value or "").strip()
        if not text:
            return ()
        candidates = [text]
        if text.startswith("SBP-") or (len(text) == 34 and text.startswith("0x")):
            try:
                from nautilus_trader.core import nautilus_pyo3

                native = str(
                    nautilus_pyo3.hyperliquid_cloid_from_client_order_id(
                        nautilus_pyo3.ClientOrderId(text)
                    )
                )
            except Exception as exc:  # noqa: BLE001 - identity translation is a hard boundary.
                raise RuntimeBoundaryError(
                    "client_order_identity_normalization_failed",
                    "Nautilus could not derive the native Hyperliquid client identity",
                ) from exc
            if native not in candidates:
                candidates.append(native)
        return tuple(candidates)

    @classmethod
    def _normalize_report_client_identity(
        cls,
        event: dict[str, object],
        *,
        requested_client_order_id: str | None,
    ) -> dict[str, object]:
        if not requested_client_order_id:
            return event
        reported = str(event.get("cloid") or "")
        if reported and reported in cls._client_order_id_candidates(requested_client_order_id):
            event["cloid"] = requested_client_order_id
        return event

    @staticmethod
    def _venue_order_id(request: Mapping[str, object]) -> object:
        value = request.get("oid") or request.get("venue_order_id")
        if value is None:
            raise RuntimeBoundaryError("venue_order_id_required", "Hyperliquid venue order ID is required")
        from nautilus_trader.core import nautilus_pyo3

        return nautilus_pyo3.VenueOrderId(str(value))

    @staticmethod
    def _optional_venue_order_id(request: Mapping[str, object]) -> object | None:
        if not request.get("oid") and not request.get("venue_order_id"):
            return None
        return NautilusHyperliquidTestnetBackend._venue_order_id(request)

    @staticmethod
    def _optional_client_order_id_text(request: Mapping[str, object]) -> str | None:
        value = request.get("cloid") or request.get("client_order_id")
        return str(value) if value is not None else None

    @staticmethod
    def _optional_venue_order_id_text(request: Mapping[str, object]) -> str | None:
        value = request.get("oid") or request.get("venue_order_id")
        return str(value) if value is not None else None

    @staticmethod
    def _quantity(request: Mapping[str, object]) -> object:
        from nautilus_trader.core import nautilus_pyo3

        return nautilus_pyo3.Quantity.from_str(str(request["sz"]))

    @staticmethod
    def _price(request: Mapping[str, object]) -> object:
        from nautilus_trader.core import nautilus_pyo3

        value = request.get("limitPx") or request.get("price")
        if value is None:
            raise RuntimeBoundaryError("limit_price_required", "Hyperliquid limit price is required")
        return nautilus_pyo3.Price.from_str(str(value))

    @staticmethod
    def _order_enums(request: Mapping[str, object]) -> tuple[object, object, object, bool]:
        from nautilus_trader.core import nautilus_pyo3

        side = str(request.get("side") or "").upper()
        if side not in {"B", "A"}:
            raise RuntimeBoundaryError("order_side_invalid", "Hyperliquid side must be B or A")
        tif = str(request.get("tif") or "GTC").upper()
        if tif not in {"GTC", "IOC", "ALO"}:
            raise RuntimeBoundaryError("time_in_force_unsupported", "Testnet adapter supports GTC, IOC, and ALO")
        return (
            nautilus_pyo3.OrderSide.BUY if side == "B" else nautilus_pyo3.OrderSide.SELL,
            nautilus_pyo3.OrderType.LIMIT,
            nautilus_pyo3.TimeInForce.IOC if tif == "IOC" else nautilus_pyo3.TimeInForce.GTC,
            tif == "ALO",
        )

    def _submit_response(self, report: object, request: Mapping[str, object]) -> Mapping[str, object]:
        mapping = self._to_mapping(report)
        status = self._status_name(report, mapping)
        venue_order_id = self._string_value(report, mapping, "venue_order_id", "oid")
        native_cloid = self._string_value(report, mapping, "client_order_id", "cloid")

        def response(status_value: object) -> dict[str, object]:
            payload: dict[str, object] = {
                "response": {"data": {"statuses": [status_value]}},
            }
            if native_cloid is not None:
                payload["native_cloid"] = native_cloid
            return payload

        if status in {"FILLED", "PARTIALLY_FILLED", "PARTIAL"}:
            event = self._order_event(report)
            event = self._attach_fill(event, request)
            if event.get("tid") is not None:
                filled = dict(event)
                filled["totalSz"] = event.get("sz")
                filled["avgPx"] = event.get("px")
                return response({"filled": filled})
        if status in {"REJECTED", "DENIED", "ERROR"}:
            return response({"error": self._string_value(report, mapping, "cancel_reason") or status})
        if status in {"CANCELED", "CANCELLED"}:
            return response("waitingForFill")
        if not venue_order_id:
            return response("waitingForFill")
        return response({"resting": {"oid": venue_order_id}})

    def _order_event(self, report: object) -> dict[str, object]:
        mapping = self._to_mapping(report)
        status = self._status_name(report, mapping)
        instrument_id = self._string_value(report, mapping, "instrument_id")
        coin = instrument_id.split("-USD-PERP", 1)[0] if instrument_id else None
        return {
            "status": self._hl_status(status),
            "oid": self._string_value(report, mapping, "venue_order_id", "oid"),
            "cloid": self._string_value(report, mapping, "client_order_id", "cloid"),
            "coin": coin,
            "side": self._hl_side(report, mapping),
            "px": self._string_value(report, mapping, "price", "avg_px"),
            "sz": self._string_value(report, mapping, "filled_qty", "quantity"),
            "time": self._timestamp_ms(report, mapping),
        }

    def _attach_fill(self, event: dict[str, object], request: Mapping[str, object]) -> dict[str, object]:
        if event.get("status") not in {"filled", "partially_filled"}:
            return event
        instrument_id = event.get("coin") or request.get("instrument_id")
        if not instrument_id:
            return event
        try:
            reports = self._call("request_fill_reports", str(self._instrument_id(instrument_id)))
        except Exception:
            return event
        order_id = str(event.get("oid") or request.get("oid") or "")
        client_id = str(event.get("cloid") or request.get("cloid") or "")
        client_candidates = self._client_order_id_candidates(client_id) if client_id else ()
        for report in reports or []:
            mapping = self._to_mapping(report)
            report_oid = self._string_value(report, mapping, "venue_order_id", "oid")
            report_cloid = self._string_value(report, mapping, "client_order_id", "cloid")
            if client_id and report_cloid and report_cloid not in client_candidates:
                continue
            if order_id and report_oid and report_oid != order_id:
                continue
            if order_id and report_oid != order_id and client_id and report_cloid not in client_candidates:
                continue
            fill = self._fill_event(report, include_fee=True)
            if client_id and str(fill.get("cloid") or "") in client_candidates:
                fill["cloid"] = client_id
            event.update({key: value for key, value in fill.items() if value is not None})
            break
        return event

    def _fill_event(self, report: object, *, include_fee: bool = False) -> dict[str, object]:
        mapping = self._to_mapping(report)
        instrument_value = self._string_value(report, mapping, "instrument_id", "coin")
        coin = (
            instrument_value.split("-USD-PERP", 1)[0]
            if instrument_value and "-USD-PERP" in instrument_value
            else instrument_value
        )
        event: dict[str, object] = {
            "tid": self._string_value(report, mapping, "trade_id", "tid", "fill_id"),
            "oid": self._string_value(report, mapping, "venue_order_id", "oid"),
            "cloid": self._string_value(report, mapping, "client_order_id", "cloid"),
            "coin": coin,
            "side": self._hl_side(report, mapping),
            "px": self._string_value(report, mapping, "last_px", "price", "avg_px"),
            "sz": self._string_value(report, mapping, "last_qty", "quantity"),
            "time": self._timestamp_ms(report, mapping),
        }
        if include_fee:
            commission = mapping.get("commission") or getattr(report, "commission", None)
            commission_mapping = self._to_mapping(commission) if commission is not None else {}
            fee = self._string_value(commission, commission_mapping, "amount", "value")
            fee_token = self._string_value(commission, commission_mapping, "currency", "currency_code")
            if isinstance(commission, str):
                match = re.fullmatch(r"\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9]+)\s*", commission)
                if match:
                    fee, fee_token = match.groups()
            liquidity = self._enum_name(
                mapping.get("liquidity_side") or getattr(report, "liquidity_side", None)
            )
            if fee is not None and fee_token is not None:
                event.update(
                    {
                        "fee": fee,
                        "feeToken": fee_token,
                        "crossed": liquidity != "MAKER",
                        "closedPnl": self._string_value(report, mapping, "closed_pnl", "closedPnl") or "0",
                    }
                )
        return event

    def _with_provenance(self, value: object) -> object:
        if isinstance(value, Mapping):
            result = dict(value)
        else:
            result = {"data": value}
        result["provenance"] = self._provenance()
        return result

    def _provenance(self) -> Provenance:
        return Provenance(
            source=self.provenance_source,
            execution_scope=self._session.execution_scope,
            transport_state=self.transport_state,
            mapping_revision=self._config.capability_revision,
        )

    @staticmethod
    def _to_mapping(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {"value": value}
            return dict(decoded) if isinstance(decoded, Mapping) else {"value": decoded}
        converter = getattr(value, "to_dict", None)
        if callable(converter):
            decoded = converter()
            return dict(decoded) if isinstance(decoded, Mapping) else {"value": decoded}
        return {"value": value}

    @staticmethod
    def _string_value(value: object, mapping: Mapping[str, object], *names: str) -> str | None:
        for name in names:
            candidate = mapping.get(name)
            if candidate is None:
                candidate = getattr(value, name, None)
            if candidate is not None:
                text = str(candidate)
                if text and text != "None":
                    return text
        return None

    @classmethod
    def _status_name(cls, value: object, mapping: Mapping[str, object]) -> str:
        candidate = mapping.get("order_status") or getattr(value, "order_status", None)
        if candidate is None:
            candidate = mapping.get("status") or getattr(value, "status", "")
        return cls._enum_name(candidate)

    @staticmethod
    def _enum_name(value: object) -> str:
        if value is None:
            return ""
        name = getattr(value, "name", None)
        return str(name or value).split(".")[-1].upper().replace(" ", "_")

    @classmethod
    def _hl_status(cls, status: str) -> str:
        return {
            "ACCEPTED": "resting",
            "NEW": "resting",
            "OPEN": "resting",
            "RESTING": "resting",
            "PARTIALLY_FILLED": "partially_filled",
            "PARTIAL": "partially_filled",
            "FILLED": "filled",
            "CANCELED": "canceled",
            "CANCELLED": "canceled",
            "REJECTED": "rejected",
        }.get(status, status.lower())

    @classmethod
    def _hl_side(cls, value: object, mapping: Mapping[str, object]) -> str | None:
        candidate = mapping.get("order_side") or mapping.get("side") or getattr(value, "order_side", None)
        name = cls._enum_name(candidate)
        return {"BUY": "B", "SELL": "A", "B": "B", "A": "A"}.get(name)

    @classmethod
    def _timestamp_ms(cls, value: object, mapping: Mapping[str, object]) -> int:
        for name in ("ts_last", "ts_event", "ts_init", "time", "timestamp"):
            candidate = mapping.get(name)
            if candidate is None:
                candidate = getattr(value, name, None)
            if candidate is None:
                continue
            try:
                number = int(candidate)
            except (TypeError, ValueError):
                continue
            return number // 1_000_000 if number > 10**12 else number
        return int(datetime.now(UTC).timestamp() * 1000)


def _resolve(value: object) -> object:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, value).result()


def _nautilus_http_client(private_key: str, account_address: str, environment_name: str) -> object:
    try:
        from nautilus_trader.core.nautilus_pyo3 import HyperliquidEnvironment
        from nautilus_trader.core.nautilus_pyo3 import HyperliquidHttpClient
        installed_version = package_version("nautilus-trader")
    except (ImportError, PackageNotFoundError) as exc:
        raise RuntimeBoundaryError(
            "nautilus_runtime_missing",
            "nautilus_trader 1.230.0 is required for external Testnet execution",
        ) from exc
    if installed_version != NAUTILUS_HYPERLIQUID_VERSION:
        raise RuntimeBoundaryError(
            "nautilus_version_mismatch",
            "installed Nautilus version does not match the reviewed Testnet adapter",
        )
    client = HyperliquidHttpClient(
        private_key=private_key,
        account_address=account_address,
        environment=getattr(HyperliquidEnvironment, environment_name),
        include_builder_attribution=False,
    )
    client.set_account_id(f"{account_address}-HYPERLIQUID")
    return client


class NautilusHyperliquidMainnetReadOnlyBackend(NautilusHyperliquidTestnetBackend):
    """Read-only Mainnet reads for one BTC sub-account.

    Reuses the reviewed Testnet read mapping.  Every write path is refused
    here as well as being absent from the capability descriptor, so a direct
    backend call cannot place, cancel or protect a real-money order.
    """

    transport_state = "external_mainnet"
    provenance_source = "nautilus-hyperliquid.mainnet"
    _session_environment = BrokerEnvironment.MAINNET
    _hyperliquid_environment = "MAINNET"
    _WRITE_ORDER_OPERATIONS = frozenset({"submit", "cancel", "replace", "cancel_replace", "modify"})

    def __init__(
        self,
        *,
        session: BrokerRuntimeSession,
        config: HyperliquidMainnetReadOnlyBackendConfig,
        secrets: LocalFileSecretProvider,
        client_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        if not isinstance(config, HyperliquidMainnetReadOnlyBackendConfig):
            raise RuntimeBoundaryError(
                "mainnet_config_required",
                "Mainnet backend requires the read-only Mainnet configuration",
            )
        if session.account.scope is not AccountScope.SUBACCOUNT:
            raise RuntimeBoundaryError(
                "mainnet_subaccount_required",
                "Mainnet access is limited to one sub-account",
            )
        super().__init__(session=session, config=config, secrets=secrets, client_factory=client_factory)

    def invoke(self, port: str, operation: str, request: object) -> object:
        if port == "protection_order" or (port == "order_execution" and operation in self._WRITE_ORDER_OPERATIONS):
            raise RuntimeBoundaryError("mainnet_read_only", "the Mainnet profile cannot write orders")
        return super().invoke(port, operation, request)

    def _submit(self, request: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeBoundaryError("mainnet_read_only", "the Mainnet profile cannot write orders")

    def _cancel(self, request: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeBoundaryError("mainnet_read_only", "the Mainnet profile cannot write orders")

    def _replace(self, request: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeBoundaryError("mainnet_read_only", "the Mainnet profile cannot write orders")

    def _invoke_protection(self, operation: str, request: Mapping[str, object]) -> object:
        raise RuntimeBoundaryError("mainnet_read_only", "the Mainnet profile cannot write orders")

    def _instrument(self, request: Mapping[str, object]) -> object:
        instrument = super()._instrument(request)
        if str(instrument.id) not in MAINNET_BTC_INSTRUMENTS:
            raise RuntimeBoundaryError("mainnet_instrument_not_allowed", "the Mainnet profile reads BTC-USD-PERP only")
        return instrument

    def _instrument_id(self, value: object) -> object:
        instrument_id = super()._instrument_id(value)
        if str(instrument_id) not in MAINNET_BTC_INSTRUMENTS:
            raise RuntimeBoundaryError("mainnet_instrument_not_allowed", "the Mainnet profile reads BTC-USD-PERP only")
        return instrument_id

    @staticmethod
    def _default_client_factory(private_key: str, account_address: str) -> object:
        return _nautilus_http_client(private_key, account_address, "MAINNET")
