from __future__ import annotations

from pathlib import Path

import pytest

from services.strategy_control_plane import StrategyControlMachineError
from services.testnet_automation_coordinator import TestnetAutomationCoordinator


NOW = "2026-08-26T01:00:00+00:00"


def _setup(tmp_path: Path):
    from tests.test_dca_testnet_lifecycle import _broker, _fill, _plan
    from tests.test_testnet_candidate_selection import _candidate, _policy, _snapshot

    plan = _plan()
    plan.update(
        {
            "strategy_session_id": "session-btc-dca",
            "strategy_revision_id": "revision-btc-dca-1",
        }
    )
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs", clock=lambda: NOW)
    activation = {
        "strategy_family": "dca",
        "strategy_session_id": plan["strategy_session_id"],
        "strategy_revision_id": plan["strategy_revision_id"],
        "plan_digest": plan["plan_digest"],
        "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-dca",
        "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }
    coordinator.activate(activation, command_id="activate-dca")
    coordinator.command(
        "select_candidate",
        {
            "candidates": [_candidate("BTC", rank=1)],
            "snapshot": _snapshot(),
            "policy": _policy(),
        },
        command_id="select-dca",
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "plan_digest": plan["plan_digest"],
        "activation_id": coordinator.status()["activation_id"],
        "confirmation_id": "confirmation-dca-1",
    }
    broker, backend = _broker(tmp_path, protection=True)
    market = {
        "execution_ready": True,
        "fresh": True,
        "is_synthetic": False,
        "fallback_policy": "none",
    }
    return coordinator, plan, confirmation, broker, backend, market, _fill


def test_coordinator_runs_canonical_dca_to_terminal_notification(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)

    started = coordinator.start_dca_session(
        plan,
        confirmation=confirmation,
        market=market,
        broker=broker,
        timestamp=NOW,
    )
    assert started["status"] == "dca_running"
    assert started["lifecycle"]["status"] == "waiting_entry"

    opened = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000, tid=1),
        market=market,
        timestamp="2026-08-26T01:01:00+00:00",
    )
    assert opened["lifecycle"]["status"] == "open"
    assert opened["lifecycle"]["protection"]["status"] == "active"

    target_pending = coordinator.advance_dca_session(
        plan,
        broker=broker,
        price=66000,
        market=market,
        timestamp="2026-08-26T01:02:00+00:00",
    )
    target_order = next(
        row for row in target_pending["lifecycle"]["orders"] if row["event"] == "target"
    )
    terminal = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(target_order, price=66000, tid=2),
        market=market,
        timestamp="2026-08-26T01:03:00+00:00",
    )

    assert terminal["status"] == "dca_terminal"
    assert terminal["lifecycle"]["status"] == "terminal"
    assert terminal["lifecycle"]["sealed"] is True
    assert terminal["lifecycle"]["park_notification"]["status"] == "queued"
    assert terminal["next_action"] == "notify_park_and_wait"


def test_manual_interrupt_preserves_position_and_resume_requires_revalidation(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_dca_session(
        plan,
        confirmation=confirmation,
        market=market,
        broker=broker,
        timestamp=NOW,
    )
    opened = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000, tid=3),
        market=market,
        timestamp="2026-08-26T01:01:00+00:00",
    )

    interrupted = coordinator.interrupt_dca_session(
        plan,
        broker=broker,
        reason="operator_interrupt",
        timestamp="2026-08-26T01:02:00+00:00",
    )
    assert interrupted["status"] == "dca_interrupted"
    assert interrupted["lifecycle"]["status"] == "interrupted"
    assert interrupted["lifecycle"]["positions"] == opened["lifecycle"]["positions"]
    assert interrupted["execution_enabled"] is False
    assert interrupted["next_action"] == "await_resume"

    resumed = coordinator.resume_dca_session(
        plan,
        broker=broker,
        confirmation=confirmation,
        market=market,
        timestamp="2026-08-26T01:03:00+00:00",
    )
    assert resumed["status"] == "dca_running"
    assert resumed["lifecycle"]["status"] == "open"
    assert resumed["lifecycle"]["positions"] == opened["lifecycle"]["positions"]
    assert resumed["execution_enabled"] is True


def test_dca_start_rejects_confirmation_or_market_mismatch_before_broker_mutation(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, _fill = _setup(tmp_path)

    with pytest.raises(StrategyControlMachineError, match="testnet_confirmation_blocked"):
        coordinator.start_dca_session(
            plan,
            confirmation={**confirmation, "plan_digest": "sha256:" + "d" * 64},
            market=market,
            broker=broker,
            timestamp=NOW,
        )
    assert coordinator.status()["status"] == "candidate_selected"

    with pytest.raises(StrategyControlMachineError, match="testnet_market_not_authoritative"):
        coordinator.start_dca_session(
            plan,
            confirmation=confirmation,
            market={"execution_ready": False, "fresh": False},
            broker=broker,
            timestamp=NOW,
        )
    assert coordinator.status()["status"] == "candidate_selected"
