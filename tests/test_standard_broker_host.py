from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from services.standard_broker_host import (
    STANDARD_BROKER_COMMIT,
    StandardBrokerEnvironmentIdentity,
    StandardBrokerHostError,
    build_standard_broker_environment_gate,
    build_paper_broker_binding,
    record_paper_broker_receipt,
    request_paper_broker,
)
from services.park_recording_track import ParkRecordingTrack


def test_paper_host_resolves_exact_hyperliquid_binding_without_network() -> None:
    binding = build_paper_broker_binding()

    assert binding.identity.broker_id == "hyperliquid"
    assert binding.identity.environment.value == "paper"
    assert binding.paper_only is True
    assert binding.real_money_eligible is False
    assert binding.control_plane == "telegram"
    assert binding.adapter.preflight().network_io is False
    assert binding.adapter.preflight().credential_required is False
    assert binding.adapter.transport.calls == []
    assert request_paper_broker(binding, "market_data", "read", {"request_id": "paper-1"}).network_io is False
    assert len(binding.adapter.transport.calls) == 1


def test_paper_host_rejects_unsupported_broker_without_fallback() -> None:
    with pytest.raises(StandardBrokerHostError, match="unsupported broker selection"):
        build_paper_broker_binding(broker_id="binance")


def test_paper_host_rejects_order_submit_before_transport() -> None:
    binding = build_paper_broker_binding()

    with pytest.raises(StandardBrokerHostError, match="capability_gap"):
        request_paper_broker(binding, "order_execution", "submit", {"order_id": "paper-submit-1"})
    assert len(binding.adapter.transport.calls) == 0


def test_paper_host_accepts_injected_local_fixture_and_capability_shape() -> None:
    class FixtureTransport:
        local_only = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(self, port: str, operation: str, payload: object) -> object:
            del payload
            self.calls.append((port, operation))
            return {"accepted": True}

    transport = FixtureTransport()
    binding = build_paper_broker_binding(
        transport_factory=lambda: transport,
        fixture_operations={"preflight": frozenset({"read"}), "market_data": frozenset({"read"})},
    )

    request_paper_broker(binding, "market_data", "read", {"request_id": "fixture-1"})
    assert transport.calls == [("market_data", "read")]
    with pytest.raises(StandardBrokerHostError, match="capability_gap"):
        request_paper_broker(binding, "account", "read", {"request_id": "fixture-2"})

    with pytest.raises(StandardBrokerHostError, match="read-only"):
        build_paper_broker_binding(
            fixture_operations={"order_execution": frozenset({"submit"})}
        )


def test_paper_host_is_bound_to_the_reviewed_standard_broker_commit() -> None:
    assert STANDARD_BROKER_COMMIT == "9f4d42637d48f6bf8926d85eb50ce62bd86b499e"


def test_paper_receipt_projects_into_park_recording_track(tmp_path) -> None:
    binding = build_paper_broker_binding()
    receipt = request_paper_broker(binding, "market_data", "read", {"request_id": "paper-record-1"})
    recording = ParkRecordingTrack(tmp_path / "outputs")

    event = record_paper_broker_receipt(
        recording,
        binding=binding,
        record_window_id="2026-08-21_DAY",
        strategy_session_id="session-paper",
        strategy_revision_id="revision-paper",
        occurred_at="2026-08-21T01:00:00+00:00",
        receipt=receipt,
    )

    assert event["category"] == "execution_path"
    assert event["source"] == "standard-broker.paper"
    assert event["payload"]["broker_id"] == "hyperliquid"
    assert event["payload"]["network_io"] is False


def test_paper_receipt_rejects_unsafe_paper_flags(tmp_path) -> None:
    binding = build_paper_broker_binding()
    receipt = request_paper_broker(binding, "market_data", "read", {"request_id": "paper-unsafe-1"})
    recording = ParkRecordingTrack(tmp_path / "outputs")

    with pytest.raises(StandardBrokerHostError, match="local Paper receipts"):
        record_paper_broker_receipt(
            recording,
            binding=binding,
            record_window_id="2026-08-21_DAY",
            strategy_session_id="session-paper",
            strategy_revision_id="revision-paper",
            occurred_at="2026-08-21T01:00:00+00:00",
            receipt=replace(receipt, real_money_eligible=True),
        )


def test_paper_receipt_rejects_noncanonical_spoofed_dataclass(tmp_path) -> None:
    @dataclass(frozen=True)
    class SpoofReceipt:
        broker_id: str = "hyperliquid"
        environment: str = "paper"
        network_io: bool = False
        real_money_eligible: bool = False
        provenance: dict[str, str] | None = None

    with pytest.raises(StandardBrokerHostError, match="canonical PaperReceipt"):
        record_paper_broker_receipt(
            ParkRecordingTrack(tmp_path / "outputs"),
            binding=build_paper_broker_binding(),
            record_window_id="2026-08-21_DAY",
            strategy_session_id="session-paper",
            strategy_revision_id="revision-paper",
            occurred_at="2026-08-21T01:00:00+00:00",
            receipt=SpoofReceipt(),
        )


def test_paper_receipt_cannot_record_unsupported_operation_as_success(tmp_path) -> None:
    binding = build_paper_broker_binding()
    receipt = request_paper_broker(binding, "market_data", "read", {"request_id": "paper-op-1"})

    with pytest.raises(StandardBrokerHostError, match="capability_gap receipt"):
        record_paper_broker_receipt(
            ParkRecordingTrack(tmp_path / "outputs"),
            binding=binding,
            record_window_id="2026-08-21_DAY",
            strategy_session_id="session-paper",
            strategy_revision_id="revision-paper",
            occurred_at="2026-08-21T01:00:00+00:00",
            receipt=replace(receipt, port="order_execution", operation="submit"),
        )


def test_paper_host_does_not_read_legacy_state(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "outputs" / "dualtrack" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "sentinel.json").write_text('{"must_not_be_read": true}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    binding = build_paper_broker_binding()

    assert binding.adapter.transport.calls == []


def _environment_identity_kwargs(environment: str = "testnet") -> dict[str, str]:
    return {
        "broker_id": "hyperliquid",
        "environment": environment,
        "execution_scope": "hypercore:default",
        "environment_fingerprint": f"hyperliquid:{environment}:fingerprint",
        "account_id": f"{environment}-account",
        "credential_source": f"HL_{environment.upper()}_CREDENTIAL",
        "runtime_id": f"runtime-{environment}",
        "ledger_namespace": f"ledger.standard-broker.{environment}",
        "release_sha": "a" * 40,
    }


def test_environment_identity_requires_explicit_nonpaper_fields() -> None:
    with pytest.raises(ValueError, match="account_id"):
        StandardBrokerEnvironmentIdentity(
            broker_id="hyperliquid",
            environment="testnet",
            execution_scope="hypercore:default",
            environment_fingerprint="hyperliquid:testnet:fingerprint",
            account_id="",
            credential_source="HL_TESTNET_CREDENTIAL",
            runtime_id="runtime-testnet",
            ledger_namespace="ledger.testnet",
            release_sha="a" * 40,
        )


@pytest.mark.parametrize("environment", ["testnet", "mainnet", "live"])
def test_environment_gate_resolves_distinct_identity_without_transport(environment: str) -> None:
    gate = build_standard_broker_environment_gate(**_environment_identity_kwargs(environment))

    assert isinstance(gate.identity, StandardBrokerEnvironmentIdentity)
    assert gate.identity.broker_id == "hyperliquid"
    assert gate.identity.environment == ("mainnet" if environment == "live" else environment)
    assert gate.identity.account_id == f"{environment}-account"
    assert gate.capabilities.names == ("preflight",)
    assert gate.preflight()["ready"] is False
    assert gate.preflight()["network_io"] is False
    assert gate.preflight()["real_money_eligible"] is False
    assert gate.preflight()["blocker"] == "capability_gate_pending"


def test_environment_gate_never_reads_credential_value_or_submits() -> None:
    gate = build_standard_broker_environment_gate(**_environment_identity_kwargs())

    assert "credential_value" not in gate.broker_config
    assert gate.broker_config["credential_source"] == "HL_TESTNET_CREDENTIAL"
    with pytest.raises(RuntimeError, match="capability gate"):
        gate.submit_order(object())


def test_environment_gate_rejects_non_default_execution_scope() -> None:
    with pytest.raises(ValueError, match="execution_scope"):
        identity = _environment_identity_kwargs()
        identity["execution_scope"] = "hip3:custom"
        build_standard_broker_environment_gate(
            **identity,
        )


def test_environment_gate_capability_gap_receipt_preserves_public_identity(tmp_path) -> None:
    gate = build_standard_broker_environment_gate(**_environment_identity_kwargs())
    event = gate.record_capability_gap(
        ParkRecordingTrack(tmp_path / "outputs"),
        record_window_id="2026-08-22_DAY",
        strategy_session_id="session-testnet",
        strategy_revision_id="revision-testnet",
        occurred_at="2026-08-22T01:00:00+00:00",
        port="order_execution",
        operation="submit",
        reason="capability_gate_pending",
    )

    assert event["source"] == "standard-broker.testnet"
    assert event["payload"]["environment"] == "testnet"
    assert event["payload"]["execution_scope"] == "hypercore:default"
    assert "credential_value" not in event["payload"]


def test_environment_identity_rejects_shared_boundary_identifiers() -> None:
    identity = _environment_identity_kwargs()
    identity.update(
        {
            "account_id": "shared-account",
            "credential_source": "SHARED_CREDENTIAL",
            "runtime_id": "shared-runtime",
            "ledger_namespace": "ledger.shared",
            "environment_fingerprint": "shared-fingerprint",
        }
    )

    with pytest.raises(ValueError, match="environment-bound"):
        build_standard_broker_environment_gate(**identity)
