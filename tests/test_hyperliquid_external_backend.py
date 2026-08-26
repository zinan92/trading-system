from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from standard_broker.adapters.hyperliquid.credentials import LocalFileSecretProvider
from standard_broker.adapters.hyperliquid.external import (
    HyperliquidTestnetBackendConfig,
    NautilusHyperliquidTestnetBackend,
    default_testnet_capabilities,
    enabled_testnet_position_protection_capabilities,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.external_host import digest_canonical
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
)


class FakeSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return b"fixture"


class FakeClient:
    def __init__(self) -> None:
        self.cached: list[object] = []
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def load_instrument_definitions(self, **kwargs: object) -> list[object]:
        self.calls.append(("load_instrument_definitions", (), kwargs))
        return [SimpleNamespace(id="HYPE-USD-PERP.HYPERLIQUID", raw_symbol="HYPE")]

    async def get_perp_meta(self) -> str:
        self.calls.append(("get_perp_meta", (), {}))
        return json.dumps({"universe": [{"name": "HYPE", "szDecimals": 2, "maxLeverage": 10}]})

    def cache_instrument(self, instrument: object) -> None:
        self.cached.append(instrument)

    async def request_account_state(self) -> object:
        self.calls.append(("request_account_state", (), {}))
        return SimpleNamespace(to_dict=lambda: {"type": "AccountState", "account_id": "master"})

    async def request_position_status_reports(self, instrument_id: str) -> list[object]:
        self.calls.append(("request_position_status_reports", (instrument_id,), {}))
        return []

    async def request_fill_reports(self, instrument_id: str) -> list[object]:
        self.calls.append(("request_fill_reports", (instrument_id,), {}))
        return []

    async def request_order_status_reports(self, instrument_id: str | None = None) -> list[object]:
        self.calls.append(("request_order_status_reports", (instrument_id,), {}))
        return []

    async def submit_order(self, *args: object, **kwargs: object) -> object:
        self.calls.append(("submit_order", args, kwargs))
        return {
            "order_status": "ACCEPTED",
            "venue_order_id": "9001",
            "client_order_id": str(args[1]),
        }

    async def request_order_status_report(self, **kwargs: object) -> object:
        self.calls.append(("request_order_status_report", (), kwargs))
        return {
            "order_status": "RESTING",
            "venue_order_id": "9001",
            "client_order_id": str(kwargs.get("client_order_id")),
            "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
            "order_side": "BUY",
            "price": "50",
            "filled_qty": "0",
            "quantity": "0.2",
            "ts_last": 1_800_000_000_000_000_000,
        }


class HyperliquidExternalBackendTests(unittest.TestCase):
    def session(self, capabilities=None) -> BrokerRuntimeSession:
        selected = capabilities or default_testnet_capabilities()
        signer = SignerReference(
            SignerKind.API_AGENT,
            "file",
            "file-secret://hyperliquid-testnet",
        )
        return BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            account=AccountReference(AccountScope.MASTER, "0x" + "11" * 20),
            signer=signer,
            signer_provider=FakeSignerProvider(),
            capabilities=selected,
            execution_scope="hypercore:default",
            lifecycle_id="testnet-lifecycle-1",
        )

    def provider(self, directory: str) -> LocalFileSecretProvider:
        path = Path(directory) / "key"
        path.write_text(
            "0x" + hashlib.sha256(b"external-backend-fixture").hexdigest(),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return LocalFileSecretProvider({"file-secret://hyperliquid-testnet": path})

    def policy(self) -> RuntimeActivationPolicy:
        return RuntimeActivationPolicy(
            testnet_approval=ExternalEnvironmentApproval(
                environment=BrokerEnvironment.TESTNET,
                approval_id="approval-testnet-1",
                release_sha="a" * 40,
                approved_by="park",
                approved_at=datetime.now(UTC),
                account_address="0x" + "11" * 20,
                lifecycle_id="testnet-lifecycle-1",
            )
        )

    def test_external_backend_is_not_local_and_exposes_testnet_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            with self.assertRaises(Exception) as raised:
                backend.invoke("instrument", "read", {})
            self.assertEqual(getattr(raised.exception, "reason_code", None), "external_backend_not_activated")
            backend.activate(release_sha="a" * 40)
            result = backend.invoke("instrument", "read", {})

            self.assertFalse(backend.local_only)
            self.assertTrue(backend.external_network)
            self.assertEqual(result["provenance"].transport_state, "external_testnet")
            self.assertEqual(len(client.cached), 1)

    def test_account_read_keeps_native_payload_behind_provenance_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke("account", "read", {"account_address": session.account.address})

            self.assertEqual(result["data"]["type"], "AccountState")
            self.assertEqual(result["provenance"].execution_scope, session.execution_scope)
            self.assertEqual(client.calls[0][0], "request_account_state")

    def test_position_read_passes_string_instrument_id_to_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "account",
                "positions",
                {"instrument_id": "HYPE-USD-PERP"},
            )

            self.assertEqual(result["positions"], [])
            self.assertEqual(
                client.calls[-1],
                (
                    "request_position_status_reports",
                    ("HYPE-USD-PERP.HYPERLIQUID",),
                    {},
                ),
            )

    def test_fill_read_passes_string_instrument_id_to_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            with self.assertRaises(Exception) as raised:
                backend.invoke(
                    "fee",
                    "fill",
                    {"instrument_id": "HYPE-USD-PERP", "fill_id": "missing"},
                )

            self.assertEqual(getattr(raised.exception, "reason_code", None), "fill_not_found")
            self.assertEqual(
                client.calls[-1],
                (
                    "request_fill_reports",
                    ("HYPE-USD-PERP.HYPERLIQUID",),
                    {},
                ),
            )

    def test_open_orders_passes_string_instrument_id_to_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "open_orders",
                {"instrument_id": "HYPE-USD-PERP"},
            )

            self.assertEqual(result["orders"], [])
            self.assertEqual(
                client.calls[-1],
                (
                    "request_order_status_reports",
                    ("HYPE-USD-PERP.HYPERLIQUID",),
                    {},
                ),
            )

    def test_external_fill_query_passes_string_instrument_id_to_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "fills",
                {"instrument_id": "HYPE-USD-PERP", "oid": "9001"},
            )

            self.assertEqual(result["fills"], [])
            self.assertEqual(
                client.calls[-1],
                (
                    "request_fill_reports",
                    ("HYPE-USD-PERP.HYPERLIQUID",),
                    {},
                ),
            )

    def test_filled_protection_leg_is_not_reported_as_active_coverage(self) -> None:
        self.assertEqual(
            NautilusHyperliquidTestnetBackend._protection_state(
                [{"status": "filled"}, {"status": "resting"}]
            ),
            "unknown",
        )
        self.assertEqual(
            NautilusHyperliquidTestnetBackend._protection_state(
                [{"status": "partially_filled"}, {"status": "resting"}]
            ),
            "unknown",
        )

    def test_submit_protection_rejects_non_active_leg_observations(self) -> None:
        class ProtectionSubmitClient(FakeClient):
            def __init__(self, status: str) -> None:
                super().__init__()
                self.status = status

            async def submit_orders(self, orders: list[object]) -> list[object]:
                self.calls.append(("submit_orders", tuple(orders), {}))
                return [
                    {
                        "order_status": self.status,
                        "venue_order_id": str(9001 + index),
                        "client_order_id": f"cloid-{index}",
                        "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                        "order_side": "SELL",
                        "price": "50",
                        "filled_qty": "0",
                        "quantity": "0.2",
                        "ts_last": 1_800_000_000_000_000_000,
                    }
                    for index, _ in enumerate(orders)
                ]

        class FixtureProtectionBackend(NautilusHyperliquidTestnetBackend):
            @staticmethod
            def _build_protection_orders(request: Mapping[str, object]) -> list[object]:
                return [object(), object()]

        request = {
            "protectionId": "protect-1",
            "instrumentId": "HYPE-USD-PERP",
            "grouping": "positionTpsl",
            "quantity": "0.2",
            "quantityPolicy": "position_following",
            "legs": [{"tpsl": "tp"}, {"tpsl": "sl"}],
        }
        for status in ("CANCELED", "FILLED", "PARTIALLY_FILLED"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                client = ProtectionSubmitClient(status)
                capabilities = enabled_testnet_position_protection_capabilities()
                session = self.session(capabilities)
                backend = FixtureProtectionBackend(
                    session=session,
                    config=HyperliquidTestnetBackendConfig(
                        account_address=session.account.address,
                        capabilities=capabilities,
                        capability_revision=capabilities.revision,
                    ),
                    secrets=self.provider(directory),
                    client_factory=lambda private_key, account: client,
                )
                backend.activate(release_sha="a" * 40)

                result = backend.invoke("protection_order", "submit", request)

                self.assertEqual(result["state"], "unknown")
                self.assertFalse(result["accepted"])
                self.assertEqual(result["covered_quantity"], "0")

    def test_cancel_protection_reseals_digest_and_rejects_failed_cancel(self) -> None:
        class StatusClient(FakeClient):
            def __init__(self, status: str) -> None:
                super().__init__()
                self.status = status

            async def cancel_order(self, *args: object, **kwargs: object) -> object:
                self.calls.append(("cancel_order", args, kwargs))
                return {"status": "ok"}

            async def request_order_status_report(self, **kwargs: object) -> object:
                self.calls.append(("request_order_status_report", (), kwargs))
                return {
                    "order_status": self.status,
                    "venue_order_id": "9001",
                    "client_order_id": "cloid-1",
                    "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                    "order_side": "SELL",
                    "price": "50",
                    "filled_qty": "0",
                    "quantity": "0.2",
                    "ts_last": 1_800_000_000_000_000_000,
                }

        request = {
            "protectionId": "protect-1",
            "instrumentId": "HYPE-USD-PERP",
            "quantity": "0.2",
        }
        for status, expected_state, expected_accepted in (
            ("CANCELED", "canceled", True),
            ("RESTING", "unknown", False),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                client = StatusClient(status)
                session = self.session()
                backend = NautilusHyperliquidTestnetBackend(
                    session=session,
                    config=HyperliquidTestnetBackendConfig(
                        account_address=session.account.address,
                        capabilities=session.capabilities,
                    ),
                    secrets=self.provider(directory),
                    client_factory=lambda private_key, account: client,
                )
                backend._protection_orders["protect-1"] = {
                    "request": request,
                    "rows": ({"oid": "9001", "cloid": "cloid-1"},),
                }
                backend.activate(release_sha="a" * 40)

                result = backend.invoke(
                    "protection_order",
                    "cancel",
                    {"protectionId": "protect-1"},
                )

                self.assertEqual(result["state"], expected_state)
                self.assertEqual(result["accepted"], expected_accepted)
                self.assertEqual(result["covered_quantity"], "0")
                digest_input = {
                    key: value
                    for key, value in result.items()
                    if key != "observation_digest"
                }
                self.assertEqual(result["observation_digest"], digest_canonical(digest_input))
    def test_external_fill_query_filters_conflicting_client_identity_even_when_oid_matches(self) -> None:
        class ConflictingFillClient(FakeClient):
            async def request_fill_reports(self, instrument_id: str) -> list[object]:
                self.calls.append(("request_fill_reports", (instrument_id,), {}))
                return [
                    {
                        "trade_id": "tid-conflicting-cloid",
                        "venue_order_id": "9001",
                        "client_order_id": "0xother-cloid",
                        "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                        "order_side": "BUY",
                        "last_px": "50",
                        "last_qty": "0.2",
                        "ts_last": 1_800_000_000_000_000_000,
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            client = ConflictingFillClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "fills",
                {
                    "instrument_id": "HYPE-USD-PERP",
                    "oid": "9001",
                    "cloid": "0xrequested-cloid",
                },
            )

            self.assertEqual(result["fills"], [])

    def test_external_fill_query_filters_conflicting_venue_identity_even_when_cloid_matches(self) -> None:
        class ConflictingVenueClient(FakeClient):
            async def request_fill_reports(self, instrument_id: str) -> list[object]:
                self.calls.append(("request_fill_reports", (instrument_id,), {}))
                return [
                    {
                        "trade_id": "tid-conflicting-oid",
                        "venue_order_id": "9999",
                        "client_order_id": "0xrequested-cloid",
                        "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                        "order_side": "BUY",
                        "last_px": "50",
                        "last_qty": "0.2",
                        "ts_last": 1_800_000_000_000_000_000,
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            client = ConflictingVenueClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "fills",
                {
                    "instrument_id": "HYPE-USD-PERP",
                    "oid": "9001",
                    "cloid": "0xrequested-cloid",
                },
            )

            self.assertEqual(result["fills"], [])

    @unittest.skipUnless(
        importlib.util.find_spec("nautilus_trader") is not None,
        "native CLOID normalization requires the pinned Nautilus dependency",
    )
    def test_recovered_native_cloid_is_normalized_for_client_scoped_fill(self) -> None:
        from nautilus_trader.core import nautilus_pyo3

        canonical = "0x" + "ab" * 16
        native = str(
            nautilus_pyo3.hyperliquid_cloid_from_client_order_id(
                nautilus_pyo3.ClientOrderId(canonical)
            )
        )

        class NativeCloidFillClient(FakeClient):
            async def request_fill_reports(self, instrument_id: str) -> list[object]:
                self.calls.append(("request_fill_reports", (instrument_id,), {}))
                return [
                    {
                        "trade_id": "tid-native-cloid",
                        "venue_order_id": "9001",
                        "client_order_id": native,
                        "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                        "order_side": "BUY",
                        "last_px": "50",
                        "last_qty": "0.2",
                        "ts_last": 1_800_000_000_000_000_000,
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            client = NativeCloidFillClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "fills",
                {"instrument_id": "HYPE-USD-PERP", "cloid": canonical},
            )

            self.assertEqual(len(result["fills"]), 1)
            self.assertEqual(result["fills"][0]["cloid"], canonical)

    def test_terminal_query_rejects_unrecognized_provider_cloid_for_same_venue_order(self) -> None:
        class TerminalIdentityClient(FakeClient):
            async def request_order_status_report(self, **kwargs: object) -> object:
                self.calls.append(("request_order_status_report", (), kwargs))
                return {
                    "order_status": "FILLED",
                    "venue_order_id": "9001",
                    "client_order_id": "0xprovider-cloid",
                    "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                    "order_side": "BUY",
                    "price": "50",
                    "filled_qty": "0.2",
                    "quantity": "0.2",
                    "ts_last": 1_800_000_000_000_000_000,
                }

        with tempfile.TemporaryDirectory() as directory:
            client = TerminalIdentityClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            with self.assertRaises(RuntimeBoundaryError) as raised:
                backend.invoke(
                    "order_execution",
                    "query",
                    {
                        "instrument_id": "HYPE-USD-PERP",
                        "oid": "9001",
                        "cloid": "0xrequested-cloid",
                    },
                )

            self.assertEqual(raised.exception.reason_code, "order_identity_conflict")

    def test_fixture_query_normalizes_a_derived_native_cloid(self) -> None:
        class NativeQueryClient(FakeClient):
            async def request_order_status_report(self, **kwargs: object) -> object:
                self.calls.append(("request_order_status_report", (), kwargs))
                return {
                    "order_status": "FILLED",
                    "venue_order_id": "9001",
                    "client_order_id": "0xnative-cloid",
                    "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                    "order_side": "BUY",
                    "price": "50",
                    "filled_qty": "0.2",
                    "quantity": "0.2",
                    "ts_last": 1_800_000_000_000_000_000,
                }

        class FixtureNativeCloidBackend(NautilusHyperliquidTestnetBackend):
            @staticmethod
            def _client_order_id_candidates(value: object) -> tuple[str, ...]:
                return (str(value), "0xnative-cloid")

        with tempfile.TemporaryDirectory() as directory:
            client = NativeQueryClient()
            session = self.session()
            backend = FixtureNativeCloidBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "query",
                {
                    "instrument_id": "HYPE-USD-PERP",
                    "oid": "9001",
                    "cloid": "0xrequested-cloid",
                },
            )

            self.assertEqual(result["cloid"], "0xrequested-cloid")
            self.assertEqual(result["status"], "filled")

    def test_terminal_query_rejects_missing_cloid_for_conflicting_venue_order(self) -> None:
        class MissingCloidClient(FakeClient):
            async def request_order_status_report(self, **kwargs: object) -> object:
                self.calls.append(("request_order_status_report", (), kwargs))
                return {
                    "order_status": "FILLED",
                    "venue_order_id": "9999",
                    "instrument_id": "HYPE-USD-PERP.HYPERLIQUID",
                    "order_side": "BUY",
                    "price": "50",
                    "filled_qty": "0.2",
                    "quantity": "0.2",
                    "ts_last": 1_800_000_000_000_000_000,
                }

        with tempfile.TemporaryDirectory() as directory:
            client = MissingCloidClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            with self.assertRaises(RuntimeBoundaryError) as raised:
                backend.invoke(
                    "order_execution",
                    "query",
                    {
                        "instrument_id": "HYPE-USD-PERP",
                        "oid": "9001",
                        "cloid": "0xrequested-cloid",
                    },
                )

            self.assertEqual(raised.exception.reason_code, "order_identity_conflict")

    @unittest.skipUnless(
        importlib.util.find_spec("nautilus_trader") is not None,
        "external order mapping requires the optional testnet dependency",
    )
    def test_order_submit_maps_canonical_wire_to_nautilus_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            session = self.session()
            backend = NautilusHyperliquidTestnetBackend(
                session=session,
                config=HyperliquidTestnetBackendConfig(
                    account_address=session.account.address,
                    capabilities=session.capabilities,
                ),
                secrets=self.provider(directory),
                client_factory=lambda private_key, account: client,
            )

            backend.activate(release_sha="a" * 40)
            result = backend.invoke(
                "order_execution",
                "submit",
                {
                    "coin": "HYPE",
                    "side": "B",
                    "sz": "0.2",
                    "limitPx": "50",
                    "tif": "GTC",
                    "reduceOnly": False,
                    "cloid": "0x" + "ab" * 16,
                },
            )

            self.assertEqual(result["response"]["data"]["statuses"][0]["resting"]["oid"], "9001")
            self.assertEqual(client.calls[-1][0], "submit_order")
            self.assertEqual(str(client.calls[-1][1][1]), "0x" + "ab" * 16)

    def test_backend_rejects_paper_session_before_client_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paper_capabilities = CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                operations={"instrument": frozenset({"read"})},
                revision="paper-v1",
            )
            paper = BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0x" + "11" * 20),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=paper_capabilities,
                execution_scope="hypercore:default",
                lifecycle_id="paper-lifecycle-1",
            )

            with self.assertRaises(Exception) as raised:
                NautilusHyperliquidTestnetBackend(
                    session=paper,
                    config=HyperliquidTestnetBackendConfig(
                        account_address=paper.account.address,
                        capabilities=default_testnet_capabilities(),
                    ),
                    secrets=self.provider(directory),
                )

            self.assertEqual(getattr(raised.exception, "reason_code", None), "external_environment_invalid")


if __name__ == "__main__":
    unittest.main()
