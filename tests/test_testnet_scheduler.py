from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_scheduler import (
    TESTNET_SCHEDULER_SCHEMA,
    TestnetScheduler,
    TestnetSchedulerGuard,
    TestnetSchedulerOwnershipStore,
)


NOW = "2026-08-26T01:00:00+00:00"
NOW_DT = datetime(2026, 8, 26, 1, tzinfo=UTC)


def _activation() -> dict[str, str]:
    return {
        "strategy_family": "dca",
        "strategy_session_id": "session-scheduler",
        "strategy_revision_id": "revision-scheduler-1",
        "plan_digest": "sha256:" + "a" * 64,
        "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-scheduler",
        "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }


def _scheduler(tmp_path: Path) -> tuple[TestnetScheduler, TestnetAutomationCoordinator]:
    coordinator = TestnetAutomationCoordinator(
        tmp_path / "outputs",
        clock=lambda: NOW,
    )
    owner_store = TestnetSchedulerOwnershipStore(
        tmp_path / "outputs",
        clock=lambda: NOW_DT,
    )
    owner_store.initialize_cloud(owner_id="cloud-primary")
    return (
        TestnetScheduler(
            tmp_path / "outputs",
            coordinator,
            owner_id="cloud-primary",
            runtime_mode="cloud",
            clock=lambda: NOW,
        ),
        coordinator,
    )


def test_testnet_ownership_is_cloud_only_and_epoch_bound(tmp_path: Path) -> None:
    store = TestnetSchedulerOwnershipStore(tmp_path / "outputs", clock=lambda: NOW_DT)
    active = store.initialize_cloud(owner_id="cloud-primary")

    assert active["schema_version"] == TESTNET_SCHEDULER_SCHEMA
    assert active["scope"] == "testnet_only"
    assert active["status"] == "active"
    assert active["epoch"] == 1
    assert TestnetSchedulerGuard(tmp_path / "outputs", runtime_mode="local").verify()["ok"] is False
    assert TestnetSchedulerGuard(tmp_path / "outputs", runtime_mode="local").verify()["blocker"] == "testnet_scheduler_cloud_only"
    assert TestnetSchedulerGuard(
        tmp_path / "outputs",
        runtime_mode="cloud",
        owner_id="cloud-primary",
    ).verify()["ok"] is True

    paused = store.pause(expected_owner_id="cloud-primary", expected_epoch=1)
    restored = store.activate(new_owner_id="cloud-secondary", expected_epoch=paused["epoch"])
    assert restored["epoch"] == 3
    assert restored["active_owner_id"] == "cloud-secondary"


def test_scheduler_activation_and_duplicate_tick_are_idempotent(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = _activation()

    activated = scheduler.activate(activation, command_id="activation-1", timestamp=NOW)
    assert activated["schema_version"] == TESTNET_SCHEDULER_SCHEMA
    assert activated["status"] == "active"
    assert activated["execution_enabled"] is False
    assert activated["coordinator"]["activation_id"] == coordinator.status()["activation_id"]

    advanced: list[dict] = []
    first = scheduler.tick(
        tick_id="tick-1",
        event={"kind": "market_heartbeat"},
        advance=lambda event: advanced.append(dict(event)) or {"status": "observed"},
        timestamp=NOW,
    )
    replay = scheduler.tick(
        tick_id="tick-1",
        event={"kind": "market_heartbeat"},
        advance=lambda _event: {"status": "must-not-run"},
        timestamp=NOW,
    )

    assert first["status"] == "active"
    assert first["next_action"] == "await_event_or_heartbeat"
    assert advanced == [{"kind": "market_heartbeat"}]
    assert replay == first


def test_restart_reconciles_before_event_progression(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    activation = scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    scheduler.mark_restart(timestamp="2026-08-26T01:01:00+00:00")
    calls: list[str] = []

    result = scheduler.tick(
        tick_id="tick-restart",
        event={"kind": "fill"},
        reconcile=lambda: calls.append("reconcile") or {
            "status": "pass",
            "activation_id": activation["coordinator"]["activation_id"],
            "environment": "testnet",
            "account_fingerprint": _activation()["account_fingerprint"],
            "release_sha": _activation()["release_sha"],
        },
        advance=lambda _event: {"status": "advanced"},
        timestamp="2026-08-26T01:02:00+00:00",
    )

    assert calls == ["reconcile"]
    assert result["restart_reconciled"] is True
    assert result["status"] == "active"
    assert result["coordinator_status"] == coordinator.status()["status"]


def test_restart_reconciliation_failure_holds_new_entries_and_never_advances(tmp_path: Path) -> None:
    scheduler, _coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    scheduler.mark_restart(timestamp="2026-08-26T01:01:00+00:00")
    advanced: list[dict] = []

    result = scheduler.tick(
        tick_id="tick-bad-restart",
        event={"kind": "fill"},
        reconcile=lambda: {"status": "unknown", "reason": "account_cursor_unknown"},
        advance=lambda event: advanced.append(dict(event)) or {"status": "advanced"},
        timestamp="2026-08-26T01:02:00+00:00",
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "restart_reconciliation_failed"
    assert result["next_action"] == "notify_park_and_wait"
    assert advanced == []


def test_scheduler_rejects_wrong_owner_without_touching_coordinator(tmp_path: Path) -> None:
    scheduler, coordinator = _scheduler(tmp_path)
    scheduler.activate(_activation(), command_id="activation-1", timestamp=NOW)
    wrong = TestnetScheduler(
        tmp_path / "outputs",
        coordinator,
        owner_id="cloud-secondary",
        runtime_mode="cloud",
        clock=lambda: NOW,
    )

    result = wrong.tick(tick_id="wrong-owner", timestamp=NOW)

    assert result["status"] == "blocked"
    assert result["blocker"] == "testnet_scheduler_owner_mismatch"
    assert coordinator.status()["status"] == "activated"
