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

    assert result["status"] == "active"
    assert result["warning"] == "config_not_ready:broker_config"


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


def test_dashboard_plan_loader_binds_confirmation_to_preview(tmp_path) -> None:
    import pipelines.park_control as module

    root = tmp_path / "outputs" / "dashboard_control_plane"
    root.mkdir(parents=True)
    digest = "sha256:" + "a" * 64
    (root / "previews.json").write_text(json.dumps([{"preview_digest": digest, "preview": {}}]), encoding="utf-8")
    (root / "confirmations.json").write_text(json.dumps([{"activation_id": "activation-1", "status": "confirmed", "preview_digest": digest}]), encoding="utf-8")

    result = module._load_dashboard_plan(tmp_path / "outputs", "activation-1", digest)

    assert result == ({"preview_digest": digest, "preview": {}}, {"activation_id": "activation-1", "status": "confirmed", "preview_digest": digest})


def test_dashboard_activation_tick_uses_fake_broker_for_empty_and_filled_facts(monkeypatch, tmp_path) -> None:
    import pipelines.park_control as module
    from datetime import datetime as real_datetime

    digest = "sha256:" + "a" * 64
    activation_id = "activation-1"
    root = tmp_path / "outputs" / "dashboard_control_plane"
    root.mkdir(parents=True)
    (root / "previews.json").write_text(json.dumps([{"preview_digest": digest, "preview": {}}]), encoding="utf-8")
    (root / "confirmations.json").write_text(json.dumps([{"activation_id": activation_id, "status": "confirmed", "preview_digest": digest, "confirmation_id": "dashboard-confirmation:unused", "operator_id": "park"}]), encoding="utf-8")

    class Config:
        start_ready = True
        instrument_id = "BTC-USD-PERP"
        account_address = "0x" + "1" * 40
        runtime_id = "runtime"
        release_sha = "a" * 40
        standard_broker_release_sha = "b" * 40
        capability_revision = "capability"
        secret_file = tmp_path / "fake-secret"

    class Market:
        def read(self, instrument_id):
            return {
                "source": "fake", "price": "100", "instrument_id": instrument_id,
                "observed_at": "2026-09-08T01:00:00+00:00", "execution_ready": True,
                "fresh": True, "is_synthetic": False, "fallback_policy": "none",
                "bid": "99", "ask": "101", "mid": "100", "mark": "100",
                "oracle": "100", "impact": "100", "depth_notional": "1000",
                "max_slippage": "10", "max_oracle_deviation_bps": "5",
                "cursor": "cursor", "broker_id": "hyperliquid", "environment": "testnet",
                "asset_index": 0, "mapping_revision": "mapping", "universe_revision": "universe",
                "connection_epoch": "epoch", "freshness": "fresh",
            }

    class Broker:
        transport_state = "external_testnet"
        broker_config = {"transport_profile": "hyperliquid-testnet-position-protection", "environment": "testnet", "real_money_eligible": False, "live_trading_enabled": False}
        fills = []

        def market_fact(self, *, instrument_id, now):
            return {
                "instrument_id": instrument_id, "price": "100", "source": "fake",
                "mapping_revision": "mapping", "observed_at": now.isoformat(),
            }

        def read_facts(self, **_kwargs):
            raise AssertionError("tick must use the public facts contract")

        def read_public_facts(self, **_kwargs):
            return {
                "status": "pass",
                "cursor": "fake-cursor",
                "fills": list(self.fills),
                "positions": [],
                "open_orders": [
                    {
                        "oid": "oid-1",
                        "broker_order_id": "oid-1",
                        "cloid": "cloid-1",
                        "client_order_id": "cloid-1",
                    }
                ],
            }

    broker = Broker()
    built = []
    contexts = []
    timestamps = []
    monkeypatch.setattr(module.HyperliquidTestnetRuntimeConfig, "from_environment", staticmethod(lambda: Config()))
    monkeypatch.setattr(module, "HyperliquidTestnetMarketReader", Market)
    monkeypatch.setattr(module, "build_plan", lambda preview, confirmation: built.append((preview, confirmation)) or {"strategy_type": "grid"})
    monkeypatch.setattr(module, "read_coherent_market", lambda *_args, **_kwargs: (
        {**Market().read("BTC-USD-PERP"), "observed_at": "2026-09-08T01:00:02+00:00"}, []
    ))
    class Clock:
        @staticmethod
        def now(tz):
            return real_datetime(2026, 9, 8, 1, 0, 3, tzinfo=tz)
    monkeypatch.setattr(module, "datetime", Clock)
    import services.broker_composition as composition
    monkeypatch.setattr(composition, "build_broker_execution_port", lambda context: contexts.append(context) or broker)
    monkeypatch.setattr(module.TestnetAutomationCoordinator, "advance_grid_session", lambda self, plan, **kwargs: timestamps.append(kwargs["timestamp"]) or {"status": "grid_running", "fill": kwargs.get("fill")})
    status = {"activation_id": activation_id, "plan_digest": digest, "strategy_family": "grid", "instrument_id": "BTC-USD-PERP"}

    advance, reconcile = module._build_testnet_tick_callbacks(tmp_path / "outputs", status)
    reconciled = reconcile()
    empty = advance({"kind": "market_heartbeat"})
    empty_2 = advance({"kind": "market_heartbeat"})
    empty_3 = advance({"kind": "market_heartbeat"})
    broker.fills = [{"order_id": "fake-order", "price": "99", "quantity": "1"}]
    filled = advance({"kind": "market_heartbeat"})

    assert len(built) == 1
    assert contexts[0].broker_config["approval_id"] == "dashboard-confirmation:unused"
    assert contexts[0].broker_config["approved_by"] == "park"
    assert reconciled["status"] == "pass"
    assert reconciled["cursor"] == "fake-cursor"
    assert reconciled["open_orders"] == [
        {
            "oid": "oid-1",
            "broker_order_id": "oid-1",
            "cloid": "cloid-1",
            "client_order_id": "cloid-1",
        }
    ]
    assert [row["status"] for row in (empty, empty_2, empty_3, filled)] == ["grid_running"] * 4
    assert timestamps == ["2026-09-08T01:00:03+00:00"] * 4
    assert all("warning" not in row for row in (empty, empty_2, empty_3, filled))
    assert filled["fill"]["order_id"] == "fake-order"


def test_setup_exception_is_recorded_without_deferred_name_error(monkeypatch, tmp_path) -> None:
    import pipelines.park_control as module

    class Coordinator:
        def __init__(self, _root):
            pass

        def status(self):
            return {"status": "grid_running"}

    class Scheduler:
        def __init__(self, *_args, **_kwargs):
            self.guard = self

        def verify(self):
            return {"ok": True}

        def status(self):
            return {"status": "active"}

        def tick(self, **kwargs):
            return kwargs["advance"]({"kind": "market_heartbeat"})

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    monkeypatch.setattr(module, "TestnetScheduler", Scheduler)
    monkeypatch.setattr(module, "_build_testnet_tick_callbacks", lambda *_args: (_ for _ in ()).throw(RuntimeError("secret=should-not-appear")))

    result = module.run_testnet_control_tick(tmp_path / "outputs")

    assert result == {"status": "blocked", "reason": "testnet_tick_setup_failed:RuntimeError"}


def test_testnet_control_tick_attaches_and_builds_callbacks_for_grid_blocked(monkeypatch, tmp_path) -> None:
    import pipelines.park_control as module

    class Coordinator:
        def __init__(self, _root):
            pass

        def status(self):
            return {"status": "grid_blocked", "activation_id": "activation-1"}

    class Scheduler:
        def __init__(self, *_args, **_kwargs):
            self.guard = self

        def verify(self):
            return {"ok": True}

        def status(self):
            return {"status": "active"}

        def tick(self, **kwargs):
            return {"advance_was_supplied": callable(kwargs["advance"])}

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    monkeypatch.setattr(module, "TestnetScheduler", Scheduler)
    monkeypatch.setattr(module, "_build_testnet_tick_callbacks", lambda *_args: (lambda _event: {"status": "grid_terminal"}, None))

    result = module.run_testnet_control_tick(tmp_path / "outputs")

    assert result == {"advance_was_supplied": True}
