"""Nautilus-backed Hyperliquid Testnet transport behind the Broker boundary."""

from collections.abc import Callable, Mapping
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
import inspect
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import re
from concurrent.futures import ThreadPoolExecutor
from time import time_ns

from ...capabilities import CapabilityDescriptor
from ...models import BrokerEnvironment, Provenance, SignerKind
from ...runtime import BrokerRuntimeSession, RuntimeBoundaryError, SignerReference
from .bridge import NautilusAdapterMetadata
from .credentials import LocalFileSecretProvider

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

    def __init__(
        self,
        *,
        session: BrokerRuntimeSession,
        config: HyperliquidTestnetBackendConfig,
        secrets: LocalFileSecretProvider,
        client_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        if session.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "external_environment_invalid",
                "Nautilus Hyperliquid external backend is Testnet-only",
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
        reported_cloid = event.get("cloid")
        if requested_cloid and reported_cloid is None:
            event["cloid"] = requested_cloid
        elif requested_cloid and reported_cloid != requested_cloid:
            requested_oid = self._optional_venue_order_id_text(request)
            reported_oid = event.get("oid")
            same_venue_order = not requested_oid or not reported_oid or reported_oid == requested_oid
            if event.get("status") in {"canceled", "rejected", "filled", "partially_filled"} and same_venue_order:
                # A cancel-replace terminal report may carry the old CLOID
                # while the query is keyed by the replacement identity.  The
                # same-OID/status pair is terminal or fill-bearing and the
                # requested identity is the only safe canonical owner at this
                # point.  Non-terminal or cross-OID conflicts still fail
                # closed below.
                event["cloid"] = requested_cloid
            else:
                raise RuntimeBoundaryError(
                    "order_identity_conflict",
                    "Hyperliquid status report client identity conflicts with the requested identity",
                )
        return self._with_provenance(self._attach_fill(event, request))

    def _open_orders(self, request: Mapping[str, object]) -> Mapping[str, object]:
        instrument_id = request.get("instrument_id")
        result = self._call(
            "request_order_status_reports",
            str(self._instrument_id(instrument_id)) if instrument_id else None,
        )
        rows = [self._order_event(item) for item in (result or [])]
        return self._with_provenance({"orders": rows})

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
        rows: list[dict[str, object]] = []
        for report in result or []:
            event = self._fill_event(report, include_fee=True)
            reported_oid = str(event.get("oid") or "")
            reported_cloid = str(event.get("cloid") or "")
            if requested_oid and reported_oid != requested_oid and requested_cloid and reported_cloid != requested_cloid:
                continue
            if requested_oid and not requested_cloid and reported_oid != requested_oid:
                continue
            if requested_cloid and not requested_oid and reported_cloid != requested_cloid:
                continue
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

        client = HyperliquidWebSocketClient(environment=HyperliquidEnvironment.TESTNET)
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
            environment=HyperliquidEnvironment.TESTNET,
            include_builder_attribution=False,
        )
        client.set_account_id(f"{account_address}-HYPERLIQUID")
        return client

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
        for instrument in result or []:
            cache = getattr(self._client_for_use(), "cache_instrument", None)
            if callable(cache):
                cache(instrument)
            instrument_id = str(instrument.id)
            if not instrument_id.endswith("-USD-PERP.HYPERLIQUID") or ":" in instrument_id:
                continue
            self._instruments[str(instrument.id)] = instrument
            self._instruments[str(instrument.raw_symbol)] = instrument

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
        value = request.get("instrument_id") or request.get("coin")
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
        if status in {"FILLED", "PARTIALLY_FILLED", "PARTIAL"}:
            event = self._order_event(report)
            event = self._attach_fill(event, request)
            if event.get("tid") is not None:
                filled = dict(event)
                filled["totalSz"] = event.get("sz")
                filled["avgPx"] = event.get("px")
                return {"response": {"data": {"statuses": [{"filled": filled}]}}}
        if status in {"REJECTED", "DENIED", "ERROR"}:
            return {
                "response": {
                    "data": {
                        "statuses": [{"error": self._string_value(report, mapping, "cancel_reason") or status}]
                    }
                }
            }
        if status in {"CANCELED", "CANCELLED"}:
            return {"response": {"data": {"statuses": ["waitingForFill"]}}}
        if not venue_order_id:
            return {"response": {"data": {"statuses": ["waitingForFill"]}}}
        return {"response": {"data": {"statuses": [{"resting": {"oid": venue_order_id}}]}}}

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
        for report in reports or []:
            mapping = self._to_mapping(report)
            report_oid = self._string_value(report, mapping, "venue_order_id", "oid")
            report_cloid = self._string_value(report, mapping, "client_order_id", "cloid")
            if order_id and report_oid != order_id and client_id and report_cloid != client_id:
                continue
            fill = self._fill_event(report, include_fee=True)
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
            source="nautilus-hyperliquid.testnet",
            execution_scope=self._session.execution_scope,
            transport_state="external_testnet",
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
