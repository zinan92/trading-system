from __future__ import annotations

import json


def test_park_control_checks_scheduler_ownership_before_telegram(monkeypatch, tmp_path, capsys) -> None:
    import pipelines.park_control as module

    class Guard:
        def __init__(self, _root):
            pass

        def verify(self):
            return {"ok": False, "blocker": "scheduler_owner_id_mismatch"}

    monkeypatch.setattr(module, "SchedulerOwnershipGuard", Guard)

    assert module.main(["--output-root", str(tmp_path)]) == 79
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "scheduler_owner_id_mismatch"
    assert payload["paper_only"] is True


def test_testnet_control_tick_is_independent_and_heartbeat_only(tmp_path) -> None:
    import pipelines.park_control as module
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator
    from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore

    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    coordinator = TestnetAutomationCoordinator(output)
    activation = {
        "strategy_family": "dca", "strategy_session_id": "session", "strategy_revision_id": "revision",
        "plan_digest": "sha256:" + "a" * 64, "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP", "runtime_id": "runtime", "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }
    coordinator.activate(activation, command_id="activate")
    TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local").activate(
        activation, command_id="scheduler-activate"
    )

    result = module.run_testnet_control_tick(output)

    assert result["status"] == "active"
    assert result["heartbeat"]["status"] == "fresh"
    assert result["alerts_authorize_actions"] is False


def test_testnet_control_tick_running_session_without_broker_fails_closed(tmp_path) -> None:
    import pipelines.park_control as module
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator
    from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore

    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    coordinator = TestnetAutomationCoordinator(output)
    activation = {
        "strategy_family": "grid", "strategy_session_id": "session", "strategy_revision_id": "revision",
        "plan_digest": "sha256:" + "a" * 64, "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP", "runtime_id": "runtime", "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }
    coordinator.activate(activation, command_id="activate")
    current = coordinator.status()
    current.update({"status": "grid_running", "execution_enabled": True})
    coordinator._record(current)

    result = module.run_testnet_control_tick(output)

    assert result["status"] == "blocked"
    assert "broker" in result["blocker"]


def test_tick_plan_loader_reads_jsonl_without_treating_it_as_one_json_document(tmp_path) -> None:
    import pipelines.park_control as module

    plan_path = tmp_path / "outputs" / "park_strategy" / "plans.jsonl"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        '{"event":"plan_proposed","plan_digest":"sha256:old"}\n'
        '{"event":"plan_proposed","plan_digest":"sha256:current"}\n',
        encoding="utf-8",
    )

    result = module._load_testnet_plan(tmp_path / "outputs", "sha256:current")

    assert result == {"event": "plan_proposed", "plan_digest": "sha256:current"}
