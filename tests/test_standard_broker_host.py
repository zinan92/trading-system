from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from services.standard_broker_host import (
    STANDARD_BROKER_COMMIT,
    StandardBrokerHostError,
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
