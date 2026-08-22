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
)
from standard_broker.capabilities import CapabilityDescriptor
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

    def test_terminal_query_normalizes_provider_cloid_for_same_venue_order(self) -> None:
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
