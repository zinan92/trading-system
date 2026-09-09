from __future__ import annotations

import json
from pathlib import Path

import pytest


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


def test_testnet_control_tick_reattaches_new_running_activation_from_stale_terminal_wait(
    monkeypatch, tmp_path
) -> None:
    import pipelines.park_control as module
    from services.testnet_automation_coordinator import (
        TestnetAutomationCoordinator,
        activation_digest,
    )
    from services.testnet_scheduler import TestnetScheduler, TestnetSchedulerOwnershipStore

    output = tmp_path / "outputs"
    TestnetSchedulerOwnershipStore(output).initialize_local(owner_id="local-mac")
    coordinator = TestnetAutomationCoordinator(output)
    scheduler = TestnetScheduler(output, coordinator, owner_id="local-mac", runtime_mode="local")
    old_activation = {
        "strategy_family": "grid", "strategy_session_id": "old-session", "strategy_revision_id": "old-revision",
        "plan_digest": "sha256:" + "a" * 64, "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP", "runtime_id": "runtime", "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-runtime-v1",
    }
    old_activation_id = activation_digest(old_activation)
    scheduler._save_state({
        "status": "awaiting_operator",
        "event": "scheduler_terminal_wait",
        "activation_id": old_activation_id,
        "coordinator_status": "grid_terminal",
        "coordinator": {"status": "grid_terminal", "activation_id": old_activation_id},
    })
    new_activation = {
        **old_activation,
        "strategy_session_id": "new-session",
        "strategy_revision_id": "new-revision",
        "plan_digest": "sha256:" + "d" * 64,
    }
    coordinator.activate(new_activation, command_id="activate-new")
    coordinator._record({
        **coordinator.status(),
        "status": "grid_running",
        "execution_enabled": True,
    })
    monkeypatch.setattr(
        module,
        "_build_testnet_tick_callbacks",
        lambda *_args: (lambda _event: {"status": "observed"}, None),
    )

    result = module.run_testnet_control_tick(output)
    current = scheduler.status()

    assert result["status"] == "active"
    assert current["activation_id"] == activation_digest(new_activation)
    assert current["previous_activation_id"] == old_activation_id


def test_testnet_control_tick_fails_closed_on_active_activation_conflict(
    monkeypatch, tmp_path
) -> None:
    import pipelines.park_control as module

    class Coordinator:
        def __init__(self, _root):
            pass

        def status(self):
            return {"status": "grid_running", "activation_id": "activation-new"}

    class Scheduler:
        def __init__(self, *_args, **_kwargs):
            self.guard = self

        def verify(self):
            return {"ok": True}

        def status(self):
            return {"status": "active", "activation_id": "activation-old"}

        def can_reattach(self, _activation_id):
            return False

        def tick(self, **_kwargs):
            raise AssertionError("conflicting activation must not be advanced")

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    monkeypatch.setattr(module, "TestnetScheduler", Scheduler)

    result = module.run_testnet_control_tick(tmp_path / "outputs")

    assert result["status"] == "blocked"
    assert result["reason"] == "testnet_scheduler_active_session_conflict"
    assert result["scheduler_activation_id"] == "activation-old"
    assert result["coordinator_activation_id"] == "activation-new"


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


def test_hydrate_order_identities_recovers_five_active_orders() -> None:
    import pipelines.park_control as module

    class Broker:
        def __init__(self):
            self.recovered = []

        def recover(self, request, *, broker_order_id, state):
            self.recovered.append((request, broker_order_id, state))

    broker = Broker()
    orders = [
        {
            "ticket_id": f"grid:rung-{index}:entry",
            "instrument_id": "BTC-USD-PERP",
            "side": "buy",
            "quantity": "0.001",
            "order_type": "limit",
            "limit_price": str(60_000 - index * 100),
            "idempotency_key": f"grid:rung-{index}:entry",
            "client_order_id": "0x" + f"{index + 1:032x}",
            "broker_order_id": str(1000 + index),
            "state": "accepted",
        }
        for index in range(5)
    ]

    module.hydrate_order_identities(
        broker,
        {"cycle_id": "dashboard-preview", "orders": orders},
    )

    assert len(broker.recovered) == 5
    assert [row[1] for row in broker.recovered] == [str(1000 + index) for index in range(5)]
    assert [row[2] for row in broker.recovered] == ["resting"] * 5
    assert [row[0].ticket for row in broker.recovered] == orders


def test_hydrate_order_identities_passes_native_client_order_id() -> None:
    import pipelines.park_control as module

    recovered = []

    class Broker:
        def recover(self, request, *, broker_order_id, state, native_client_order_id):
            recovered.append((request, broker_order_id, state, native_client_order_id))

    order = {
        "ticket_id": "grid:rung-0:entry",
        "instrument_id": "BTC-USD-PERP",
        "side": "buy",
        "quantity": "0.001",
        "order_type": "limit",
        "limit_price": "60000",
        "client_order_id": "canonical-cloid",
        "native_client_order_id": "0x" + "7" * 32,
        "broker_order_id": "1000",
        "state": "accepted",
    }

    module.hydrate_order_identities(
        Broker(), {"cycle_id": "dashboard-preview", "orders": [order]}
    )

    assert recovered[0][1:] == ("1000", "resting", "0x" + "7" * 32)


def test_hydrate_order_identities_falls_back_for_legacy_recover_binding() -> None:
    import pipelines.park_control as module

    recovered = []

    class LegacyBroker:
        def recover(self, request, *, broker_order_id, state):
            recovered.append((request.ticket, broker_order_id, state))

    order = {
        "ticket_id": "grid:rung-0:entry",
        "instrument_id": "BTC-USD-PERP",
        "side": "buy",
        "quantity": "0.001",
        "order_type": "limit",
        "limit_price": "60000",
        "client_order_id": "canonical-cloid",
        "native_client_order_id": "0x" + "9" * 32,
        "broker_order_id": "1000",
        "state": "accepted",
    }

    module.hydrate_order_identities(
        LegacyBroker(), {"cycle_id": "dashboard-preview", "orders": [order]}
    )

    assert recovered == [(order, "1000", "resting")]


def test_testnet_facts_failure_returns_typed_redacted_reason() -> None:
    import pipelines.park_control as module

    class Broker:
        def read_public_facts(self, *, instrument_id):
            assert instrument_id == "BTC-USD-PERP"
            raise RuntimeError("provider token=do-not-persist unavailable")

    result = module._read_testnet_facts(Broker(), "BTC-USD-PERP")

    assert result == {
        "status": "failed",
        "reason": "testnet_facts_unavailable:RuntimeError:provider token=[REDACTED] unavailable",
    }


def test_hydrate_order_identities_rejects_active_order_without_oid() -> None:
    import pipelines.park_control as module

    state = {
        "cycle_id": "dashboard-preview",
        "orders": [
            {
                "ticket_id": "grid:rung-0:entry",
                "instrument_id": "BTC-USD-PERP",
                "side": "buy",
                "quantity": "0.001",
                "order_type": "limit",
                "limit_price": "60000",
                "client_order_id": "0x" + "1" * 32,
                "state": "accepted",
            }
        ],
    }

    with pytest.raises(
        module.OrderIdentityHydrationError,
        match=r"^order\[0\]:broker_order_id_missing$",
    ):
        module.hydrate_order_identities(object(), state)


def test_hydrate_order_identities_maps_dca_active_states_and_skips_terminal_orders() -> None:
    import pipelines.park_control as module

    recovered = []

    class Broker:
        def recover(self, request, *, broker_order_id, state):
            recovered.append((request.ticket["state"], broker_order_id, state))

    orders = []
    for index, lifecycle_state in enumerate(
        ("accepted", "cancel_pending", "partially_filled", "filled", "cancelled")
    ):
        orders.append(
            {
                "ticket_id": f"dca:entry:{index}",
                "instrument_id": "BTC-USD-PERP",
                "side": "buy",
                "quantity": "0.001",
                "order_type": "limit",
                "limit_price": "60000",
                "client_order_id": "0x" + f"{index + 1:032x}",
                "broker_order_id": str(2000 + index),
                "state": lifecycle_state,
            }
        )

    module.hydrate_order_identities(
        Broker(),
        {"cycle_id": "dashboard-preview", "orders": orders},
    )

    assert recovered == [
        ("accepted", "2000", "resting"),
        ("cancel_pending", "2001", "cancel_pending"),
        ("partially_filled", "2002", "partially_filled"),
    ]


def test_dashboard_activation_tick_uses_fake_broker_for_empty_and_filled_facts(monkeypatch, tmp_path) -> None:
    import pipelines.park_control as module
    from datetime import datetime as real_datetime

    digest = "sha256:" + "a" * 64
    activation_id = "activation-1"
    output_root = tmp_path / "outputs"
    root = output_root / "dashboard_control_plane"
    root.mkdir(parents=True)
    (root / "previews.json").write_text(json.dumps([{"preview_digest": digest, "preview": {}}]), encoding="utf-8")
    (root / "confirmations.json").write_text(json.dumps([{"activation_id": activation_id, "status": "confirmed", "preview_digest": digest, "confirmation_id": "dashboard-confirmation:unused", "operator_id": "park"}]), encoding="utf-8")
    lifecycle_path = output_root / "dualtrack" / "grid_testnet_lifecycle" / "dashboard-plan:fixture.json"
    lifecycle_path.parent.mkdir(parents=True)
    lifecycle_path.write_text(
        (Path(__file__).parent / "fixtures" / "grid_testnet_lifecycle_active_5_orders.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

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

        def __init__(self):
            self.recovered = []

        def recover(self, request, *, broker_order_id, state):
            self.recovered.append((request, broker_order_id, state))

        def market_fact(self, *, instrument_id, now):
            return {
                "instrument_id": instrument_id, "price": "100", "source": "fake",
                "mapping_revision": "mapping", "observed_at": now.isoformat(),
            }

        def read_facts(self, **_kwargs):
            raise AssertionError("tick must use the public facts contract")

        def read_public_facts(self, **_kwargs):
            assert len(self.recovered) == 5
            return {
                "status": "pass",
                "cursor": "fake-cursor",
                "fills": list(self.fills),
                "positions": [],
                "open_orders": [
                    {
                        "oid": broker_order_id,
                        "broker_order_id": broker_order_id,
                        "cloid": request.ticket["client_order_id"],
                        "client_order_id": request.ticket["client_order_id"],
                    }
                    for request, broker_order_id, _state in self.recovered
                ],
            }

    broker = Broker()
    built = []
    contexts = []
    timestamps = []
    monkeypatch.setattr(module.HyperliquidTestnetRuntimeConfig, "from_environment", staticmethod(lambda: Config()))
    monkeypatch.setattr(module, "HyperliquidTestnetMarketReader", Market)
    monkeypatch.setattr(module, "build_plan", lambda preview, confirmation: built.append((preview, confirmation)) or {"strategy_type": "grid", "strategy_plan_id": "dashboard-plan:fixture", "plan_digest": digest})
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

    advance, reconcile = module._build_testnet_tick_callbacks(output_root, status)
    reconciled = reconcile()
    empty = advance({"kind": "market_heartbeat"})
    empty_2 = advance({"kind": "market_heartbeat"})
    empty_3 = advance({"kind": "market_heartbeat"})
    broker.fills = [{"order_id": "fake-order", "price": "99", "quantity": "1"}]
    filled = advance({"kind": "market_heartbeat"})

    assert len(built) == 1
    assert len(broker.recovered) == 5
    assert contexts[0].broker_config["approval_id"] == "dashboard-confirmation:unused"
    assert contexts[0].broker_config["approved_by"] == "park"
    assert reconciled["status"] == "pass"
    assert reconciled["cursor"] == "fake-cursor"
    assert len(reconciled["open_orders"]) == 5
    assert [row["oid"] for row in reconciled["open_orders"]] == [
        "1000", "1001", "1002", "1003", "1004",
    ]
    assert [row["status"] for row in (empty, empty_2, empty_3, filled)] == ["grid_running"] * 4
    assert timestamps == ["2026-09-08T01:00:03+00:00"] * 4
    assert all("warning" not in row for row in (empty, empty_2, empty_3, filled))
    assert filled["fill"]["order_id"] == "fake-order"


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("missing_oid", "order_identity_hydration_failed:order[0]:broker_order_id_missing"),
        ("recover_error", "order_identity_hydration_failed:order[0]:recover_RuntimeError"),
    ],
)
def test_dashboard_activation_tick_blocks_before_facts_when_identity_hydration_fails(
    monkeypatch,
    tmp_path,
    failure_mode,
    expected_reason,
) -> None:
    import pipelines.park_control as module

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
            return {"source": "fake", "instrument_id": instrument_id}

    class Broker:
        facts_calls = 0

        def recover(self, *_args, **_kwargs):
            if failure_mode == "recover_error":
                raise RuntimeError("provider details must not leak")

        def read_public_facts(self, **_kwargs):
            self.facts_calls += 1
            raise AssertionError("facts must not run after failed hydration")

    order = {
        "ticket_id": "grid:rung-0:entry",
        "instrument_id": "BTC-USD-PERP",
        "side": "buy",
        "quantity": "0.001",
        "order_type": "limit",
        "limit_price": "60000",
        "client_order_id": "0x" + "1" * 32,
        "broker_order_id": "1000",
        "state": "accepted",
    }
    if failure_mode == "missing_oid":
        order.pop("broker_order_id")

    broker = Broker()
    monkeypatch.setattr(module.HyperliquidTestnetRuntimeConfig, "from_environment", staticmethod(lambda: Config()))
    monkeypatch.setattr(module, "HyperliquidTestnetMarketReader", Market)
    monkeypatch.setattr(module, "_load_dashboard_plan", lambda *_args: ({}, {"confirmation_id": "confirmation", "operator_id": "park"}))
    monkeypatch.setattr(module, "build_plan", lambda *_args: {"strategy_type": "grid", "strategy_plan_id": "dashboard-plan:fixture"})
    monkeypatch.setattr(module, "_load_testnet_lifecycle_state", lambda *_args, **_kwargs: {"cycle_id": "dashboard-preview", "orders": [order]})
    import services.broker_composition as composition
    monkeypatch.setattr(composition, "build_broker_execution_port", lambda _context: broker)

    advance, reconcile = module._build_testnet_tick_callbacks(
        tmp_path / "outputs",
        {
            "activation_id": "activation-1",
            "plan_digest": "sha256:" + "a" * 64,
            "strategy_family": "grid",
            "instrument_id": "BTC-USD-PERP",
        },
    )

    assert advance({"kind": "market_heartbeat"}) == {
        "status": "blocked",
        "reason": expected_reason,
    }
    assert reconcile is None
    assert broker.facts_calls == 0


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

        def can_reattach(self, _activation_id):
            return False

        def tick(self, **kwargs):
            return kwargs["advance"]({"kind": "market_heartbeat"})

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    monkeypatch.setattr(module, "TestnetScheduler", Scheduler)
    monkeypatch.setattr(module, "_build_testnet_tick_callbacks", lambda *_args: (_ for _ in ()).throw(RuntimeError("secret=should-not-appear")))

    result = module.run_testnet_control_tick(tmp_path / "outputs")

    assert result == {"status": "blocked", "reason": "testnet_tick_setup_failed:RuntimeError"}


@pytest.mark.parametrize("coordinator_state", ["grid_blocked", "grid_paused_range"])
def test_testnet_control_tick_attaches_and_builds_callbacks_for_tickable_grid_state(
    monkeypatch, tmp_path, coordinator_state
) -> None:
    import pipelines.park_control as module

    class Coordinator:
        def __init__(self, _root):
            pass

        def status(self):
            return {"status": coordinator_state, "activation_id": "activation-1"}

    class Scheduler:
        def __init__(self, *_args, **_kwargs):
            self.guard = self

        def verify(self):
            return {"ok": True}

        def status(self):
            return {"status": "active"}

        def can_reattach(self, _activation_id):
            return False

        def tick(self, **kwargs):
            return {"advance_was_supplied": callable(kwargs["advance"])}

    monkeypatch.setattr(module, "TestnetAutomationCoordinator", Coordinator)
    monkeypatch.setattr(module, "TestnetScheduler", Scheduler)
    monkeypatch.setattr(module, "_build_testnet_tick_callbacks", lambda *_args: (lambda _event: {"status": "grid_terminal"}, None))

    result = module.run_testnet_control_tick(tmp_path / "outputs")

    assert result == {"advance_was_supplied": True}
