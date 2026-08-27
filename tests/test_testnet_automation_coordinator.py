from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.strategy_control_plane import StrategyControlMachineError
from services.testnet_automation_coordinator import (
    COORDINATOR_SCHEMA,
    TestnetAutomationCoordinator,
    TestnetCoordinatorError,
    activation_digest,
)


def _activation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "strategy_family": "dca",
        "strategy_session_id": "session-btc-dca",
        "strategy_revision_id": "revision-btc-dca-1",
        "plan_digest": "sha256:" + "a" * 64,
        "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "nautilus-hyperliquid-testnet-1",
        "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }
    value.update(overrides)
    return value


def _coordinator(tmp_path: Path) -> TestnetAutomationCoordinator:
    return TestnetAutomationCoordinator(
        tmp_path / "outputs",
        clock=lambda: "2026-08-26T01:00:00+00:00",
    )


def test_activation_persists_exact_testnet_identity_and_stays_execution_blocked(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)

    result = coordinator.command(
        "activate",
        _activation(),
        command_id="activate-1",
    )

    assert result["schema_version"] == COORDINATOR_SCHEMA
    assert result["event"] == "activated"
    assert result["status"] == "activated"
    assert result["execution_enabled"] is False
    assert result["execution_blocker"] == "capability_gap:execution"
    assert result["environment"] == "testnet"
    assert result["broker_id"] == "hyperliquid"
    assert result["instrument_id"] == "BTC-USD-PERP"
    assert result["activation_id"] == activation_digest(_activation())
    assert result["network_operation_invoked"] is False
    assert result["secret_material_present"] is False

    current = json.loads(
        (tmp_path / "outputs" / "testnet_automation" / "current.json").read_text()
    )[-1]
    assert current == result


def test_activation_accepts_reviewed_protected_testnet_profile(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    result = coordinator.activate(
        _activation(transport_profile="hyperliquid-testnet-position-protection"),
        command_id="activate-protected-1",
    )

    assert result["status"] == "activated"
    assert result["transport_profile"] == "hyperliquid-testnet-position-protection"


def test_status_and_preflight_are_authoritative_and_do_not_invoke_a_broker(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.activate(_activation(), command_id="activate-1")

    status = coordinator.command("status")
    preflight = coordinator.command("preflight")

    assert status["activation_id"] == preflight["activation_id"]
    assert status["strategy_session_id"] == "session-btc-dca"
    assert preflight["ready"] is True
    assert preflight["execution_ready"] is False
    assert preflight["broker_operation_invoked"] is False
    assert preflight["network_operation_invoked"] is False
    assert preflight["next_action"] == "await_execution_capability"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("environment", "mainnet", "testnet_only"),
        ("transport_profile", "hyperliquid-mainnet", "testnet_profile_required"),
        ("broker_id", "binance", "hyperliquid_broker_required"),
        ("strategy_family", "macd", "strategy_family_invalid"),
        ("plan_digest", "not-a-digest", "plan_digest_invalid"),
        ("account_fingerprint", "0xabc", "account_fingerprint_invalid"),
        ("release_sha", "short", "release_sha_invalid"),
    ],
)
def test_activation_rejects_inexact_or_unsafe_identity(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    coordinator = _coordinator(tmp_path)
    payload = _activation(**{field: value})

    with pytest.raises(TestnetCoordinatorError, match=message):
        coordinator.activate(payload, command_id="activate-invalid")

    assert coordinator.status()["status"] == "idle"


def test_repeated_command_id_is_idempotent_but_conflicting_activation_fails_closed(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.activate(_activation(), command_id="activate-1")
    replay = coordinator.activate(_activation(), command_id="activate-1")

    assert replay == first

    with pytest.raises(TestnetCoordinatorError, match="active_session_conflict"):
        coordinator.activate(
            _activation(
                strategy_session_id="session-eth-dca",
                strategy_revision_id="revision-eth-dca-1",
                instrument_id="ETH-USD-PERP",
            ),
            command_id="activate-2",
        )

    assert coordinator.status()["strategy_session_id"] == "session-btc-dca"


def test_pause_interrupt_and_resume_intent_are_durable_without_execution_side_effects(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.activate(_activation(), command_id="activate-1")

    paused = coordinator.command("pause", {"reason": "operator_pause"}, command_id="pause-1")
    interrupted = coordinator.command(
        "interrupt",
        {"reason": "operator_interrupt"},
        command_id="interrupt-1",
    )
    resumed = coordinator.command("resume", command_id="resume-1")

    assert paused["status"] == "paused"
    assert paused["execution_enabled"] is False
    assert interrupted["status"] == "interrupted"
    assert interrupted["execution_mutation"] is False
    assert resumed["status"] == "resume_pending"
    assert resumed["execution_blocker"] == "revalidation_required"
    assert resumed["execution_mutation"] is False
    assert coordinator.status()["status"] == "resume_pending"


def test_candidate_selection_locks_after_activation_owns_a_slice(tmp_path: Path) -> None:
    from tests.test_testnet_candidate_selection import _candidate, _policy, _snapshot

    coordinator = _coordinator(tmp_path)
    coordinator.activate(_activation(), command_id="activate-1")
    coordinator.command(
        "select_candidate",
        {"candidates": [_candidate("BTC", rank=1)], "snapshot": _snapshot(), "policy": _policy()},
        command_id="select-1",
    )

    with pytest.raises(TestnetCoordinatorError, match="candidate_selection_locked"):
        coordinator.command(
            "select_candidate",
            {"candidates": [_candidate("ETH", rank=1)], "snapshot": _snapshot(), "policy": _policy()},
            command_id="select-2",
        )


def test_dca_capability_exception_does_not_mask_another_gap() -> None:
    class Broker:
        protection_adapter = object()

        def preflight(self, *, strategy_family: str):
            assert strategy_family == "dca"
            return {
                "ready": False,
                "environment": "testnet",
                "real_money_eligible": False,
                "protection_ready": False,
                "account_read_ready": True,
                "capability_gaps": [
                    "protection_order.take_profit_market",
                    "protection_order.stop_loss_market",
                ],
            }

    with pytest.raises(StrategyControlMachineError, match="testnet_preflight_blocked"):
        TestnetAutomationCoordinator._validate_lifecycle_preflight(
            Broker(),
            strategy_family="dca",
        )


def test_soak_evidence_cannot_be_replayed_for_another_strategy_family(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.activate(_activation(), command_id="activate-1")

    with pytest.raises(TestnetCoordinatorError, match="soak_strategy_family_mismatch"):
        coordinator.finalize_soak(
            strategy_family="grid",
            instrument_id="BTC-USD-PERP",
            now="2026-08-26T01:00:00+00:00",
        )
    assert coordinator.status()["status"] == "activated"


def test_unknown_action_and_corrupt_state_fail_closed(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    with pytest.raises(TestnetCoordinatorError, match="action_invalid"):
        coordinator.command("submit")

    coordinator.activate(_activation(), command_id="activate-1")
    current = tmp_path / "outputs" / "testnet_automation" / "current.json"
    current.write_text("not-json\n", encoding="utf-8")

    blocked = coordinator.status()
    assert blocked["status"] == "blocked"
    assert blocked["blocker"] == "coordinator_state_corrupt"
    assert blocked["execution_enabled"] is False
