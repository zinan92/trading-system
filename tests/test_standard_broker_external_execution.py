from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from services.broker_port import BrokerOrderRequest
from services.broker_composition import (
    BrokerBuildContext,
    default_broker_plugin_registry,
)
from services.standard_broker_external_execution import (
    PROTECTED_EXTERNAL_PROFILE,
    PROTECTED_CAPABILITY_REVISION,
    STANDARD_BROKER_RELEASE_SHA,
    StandardBrokerExternalExecutionAdapter,
)


ACCOUNT = "0x" + "11" * 20
RELEASE = "a" * 40


class _Matrix:
    profile_id = "hyperliquid-testnet-position-protection-v1"

    @staticmethod
    def supports(name: str) -> bool:
        return name != "take_profit_market"


class _Protection:
    protection_capabilities = _Matrix()

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def submit(self, group):
        self.calls.append(("submit", group))
        return SimpleNamespace(accepted=True, provenance=SimpleNamespace())

    def cancel(self, group):
        self.calls.append(("cancel", group))
        return SimpleNamespace(accepted=True, provenance=SimpleNamespace())

    def replace(self, group):
        self.calls.append(("replace", group))
        return SimpleNamespace(accepted=True, provenance=SimpleNamespace())

    def reconcile(self, group):
        self.calls.append(("query", group))
        return SimpleNamespace(accepted=True, provenance=SimpleNamespace())


class _Observation:
    def __init__(self, data: tuple[object, ...]) -> None:
        self.fact = SimpleNamespace(data=data)


class _Reconciliation:
    passed = True

    def __init__(self, observed_at: datetime) -> None:
        self.observed_at = observed_at
        self.cursor = SimpleNamespace(value="cursor-1")
        self.evidence_digest = "sha256:" + "e" * 64
        self.positions = _Observation(())
        self.open_orders = _Observation(())

    def require_coherent(self):
        return self


class _Binding:
    profile_id = PROTECTED_EXTERNAL_PROFILE
    transport_state = "external_testnet"
    protection_capabilities = _Matrix()
    protection = _Protection()
    runtime_session = SimpleNamespace(
        account=SimpleNamespace(address=ACCOUNT),
        broker_id="hyperliquid",
        environment="testnet",
        lifecycle_id="runtime-1",
        capability_revision=PROTECTED_CAPABILITY_REVISION,
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        observed_at = datetime.now(timezone.utc).replace(microsecond=0)
        provenance = SimpleNamespace(
            source="nautilus-hyperliquid.testnet",
            transport_state="external_testnet",
            mapping_revision=PROTECTED_CAPABILITY_REVISION,
            execution_scope="hypercore:default",
            received_at=observed_at,
        )
        self.account = SimpleNamespace(
            account_address=ACCOUNT,
            broker_id="hyperliquid",
            environment="testnet",
            equity=Decimal("1000"),
            balance=Decimal("1000"),
            withdrawable=Decimal("1000"),
            exposure=Decimal("0"),
            margin_used=Decimal("0"),
            positions=(),
            provenance=provenance,
        )
        self.reconciliation = _Reconciliation(observed_at)
        self.reconciliation.account = _Observation(self.account)
        self.reconciliation.identity = SimpleNamespace(
            broker_id="hyperliquid",
            environment="testnet",
            account_address=ACCOUNT,
            lifecycle_id="runtime-1",
            release_sha=RELEASE,
            capability_revision=PROTECTED_CAPABILITY_REVISION,
        )
        self.receipt = SimpleNamespace(
            order_id="order-1",
            client_order_id="client-1",
            broker_order_id="broker-1",
            state="resting",
            account_address=ACCOUNT,
            release_sha=RELEASE,
            provenance=provenance,
        )

    def preflight(self):
        return {
            "canary_ready": True,
            "host_ready": True,
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "transport_state": "external_testnet",
            "account_fingerprint": "sha256:" + "b" * 64,
            "runtime_id": "runtime-1",
            "release_sha": RELEASE,
            "capability_revision": PROTECTED_CAPABILITY_REVISION,
            "network_io": True,
            "real_money_eligible": False,
            "protection_ready": False,
            "market_fact_reader_available": True,
        }

    def submit(self, intent):
        self.calls.append(("submit", intent))
        submit_count = sum(1 for name, _ in self.calls if name == "submit")
        if submit_count > 1:
            self.receipt = SimpleNamespace(
                order_id=f"order-{submit_count}",
                client_order_id=f"client-{submit_count}",
                broker_order_id=f"broker-{submit_count}",
                state="resting",
                account_address=ACCOUNT,
                release_sha=RELEASE,
                provenance=self.receipt.provenance,
            )
        return self.receipt

    def query(self, order_id):
        self.calls.append(("query", order_id))
        return self.receipt

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        return self.receipt

    def replace(self, order_id, intent):
        self.calls.append(("replace", (order_id, intent)))
        return self.receipt

    def read_facts(self, *, order_id, instrument_id, now, client_order_id=None):
        self.calls.append(("facts", (order_id, instrument_id, now, client_order_id)))
        return SimpleNamespace(
            account=self.account,
            positions=(),
            open_orders=(),
            fills=(),
            fees=(),
            reconciliation=self.reconciliation,
        )

    def market_fact(self, *, instrument_id, now):
        return {
            "instrument_id": instrument_id,
            "price": "60000",
            "freshness": "fresh",
            "observed_at": now.isoformat(),
            "max_age_seconds": "120",
            "source": "hyperliquid.external_testnet",
            "transport_state": "external_testnet",
            "mapping_revision": PROTECTED_CAPABILITY_REVISION,
        }


def _adapter() -> tuple[StandardBrokerExternalExecutionAdapter, _Binding, list[str]]:
    binding = _Binding()
    closed: list[str] = []
    runtime = SimpleNamespace(close=lambda: closed.append("closed"))
    return (
        StandardBrokerExternalExecutionAdapter(
            binding,
            runtime=runtime,
            broker_config={"instrument_binding": {"instrument_id": "BTC-USD-PERP"}},
        ),
        binding,
        closed,
    )


def test_external_execution_adapter_proves_opt_in_preflight_without_secret_or_native_payload() -> None:
    adapter, _binding, _closed = _adapter()

    result = adapter.preflight()

    assert result["ready"] is True
    assert result["protection_ready"] is True
    assert result["account_read_ready"] is True
    assert result["environment"] == "testnet"
    assert result["transport_profile"] == PROTECTED_EXTERNAL_PROFILE
    assert result["capability_revision"] == PROTECTED_CAPABILITY_REVISION
    assert result["real_money_eligible"] is False


def test_external_execution_adapter_maps_canonical_ticket_and_reads_public_facts() -> None:
    adapter, binding, _closed = _adapter()
    request = BrokerOrderRequest(
        run_date="cycle-1",
        ticket={
            "ticket_id": "entry-1",
            "instrument_id": "BTC-USD-PERP",
            "side": "buy",
            "quantity": "0.001",
            "order_type": "limit",
            "limit_price": "60000",
            "time_in_force": "gtc",
            "idempotency_key": "entry-key-1",
        },
        latest_price=60000,
        actual_size=0.001,
    )

    receipt = adapter.submit_order(request)
    assert receipt.order_id == "order-1"
    intent = binding.calls[-1][1]
    assert intent.instrument_id == "BTC-USD-PERP"
    assert intent.quantity == Decimal("0.001")
    assert intent.order_type.value == "limit"

    assert adapter.request("order_execution", "open_orders", "BTC-USD-PERP") == ()
    account = adapter.request("account", "read", ACCOUNT)
    assert account.account_address == ACCOUNT
    assert adapter.canonical_order_adapter.apply_fill({"order_id": "order-1"}).order_id == "order-1"


def test_external_execution_adapter_closes_owned_runtime() -> None:
    adapter, _binding, closed = _adapter()

    adapter.close()

    assert closed == ["closed"]


def test_external_execution_adapter_maps_lifecycle_query_to_public_reconcile() -> None:
    adapter, binding, _closed = _adapter()

    adapter.request("protection_order", "query", object())

    assert binding.protection.calls[-1][0] == "query"


def test_registry_resolves_opt_in_protected_testnet_profile() -> None:
    context = BrokerBuildContext(
        output_root="outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
        },
    )

    plugin = default_broker_plugin_registry().resolve(context)

    assert plugin.key.transport_profile == PROTECTED_EXTERNAL_PROFILE
    assert plugin.capabilities.supports("submit_order") is True


def test_registry_builds_opt_in_execution_from_an_injected_public_binding(tmp_path) -> None:
    binding = _Binding()
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "external_binding": binding,
            "external_binding_test_only": True,
            "release_sha": RELEASE,
            "standard_broker_release_sha": STANDARD_BROKER_RELEASE_SHA,
        },
    )

    adapter = default_broker_plugin_registry().build_execution(context)

    assert isinstance(adapter, StandardBrokerExternalExecutionAdapter)
    assert adapter.preflight()["ready"] is True
    adapter.close()


def test_build_context_constructs_opt_in_profile_without_resolving_secret(tmp_path) -> None:
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "account_id": ACCOUNT,
            "runtime_id": "runtime-context",
            "release_sha": RELEASE,
            "standard_broker_release_sha": STANDARD_BROKER_RELEASE_SHA,
            "approval_id": "approval-context",
            "approved_by": "park",
            "secret_file": tmp_path / "secret-not-read",
        },
    )

    result = StandardBrokerExternalExecutionAdapter.preflight_build_context(context)
    assert result["status"] == "PREFLIGHT_READY"
    assert result["external_network"] is True
    assert result["real_money_eligible"] is False
    assert result["capability_gaps"] == []
    assert result["standard_broker_release_sha"] == STANDARD_BROKER_RELEASE_SHA
    assert result["secret_resolved"] is False
