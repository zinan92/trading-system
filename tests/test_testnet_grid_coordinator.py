from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from services.testnet_automation_coordinator import TestnetAutomationCoordinator


NOW = "2026-08-26T01:00:00+00:00"


def _market_at(market: dict, timestamp: str) -> dict:
    return {**market, "observed_at": timestamp}


def _setup(tmp_path: Path):
    from tests.test_grid_testnet_lifecycle import _broker, _fill, _plan
    from tests.test_testnet_candidate_selection import _candidate, _policy, _snapshot

    plan = _plan(direction="long")
    plan.update(
        {
            "strategy_session_id": "session-btc-grid",
            "strategy_revision_id": "revision-btc-grid-1",
        }
    )
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs", clock=lambda: NOW)
    activation = {
        "strategy_family": "grid",
        "strategy_session_id": plan["strategy_session_id"],
        "strategy_revision_id": plan["strategy_revision_id"],
        "plan_digest": plan["plan_digest"],
        "account_fingerprint": "sha256:" + hashlib.sha256(b"testnet-account").hexdigest(),
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-testnet",
        "release_sha": "a" * 40,
        "capability_revision": "dca-testnet-v1",
    }
    coordinator.activate(activation, command_id="activate-grid")
    coordinator.command(
        "select_candidate",
        {
            "candidates": [_candidate("BTC", rank=1)],
            "snapshot": _snapshot(),
            "policy": _policy(),
        },
        command_id="select-grid",
    )
    confirmation = {
        "event": "confirmed",
        "execution_authorized": True,
        "execution_environment": "testnet",
        "plan_digest": plan["plan_digest"],
        "activation_id": coordinator.status()["activation_id"],
        "confirmation_id": "confirmation-grid-1",
    }
    broker, backend = _broker(tmp_path, protection=True)
    market = {
        "execution_ready": True,
        "fresh": True,
        "is_synthetic": False,
        "fallback_policy": "none",
        "observed_at": NOW,
        "bid": "64999",
        "ask": "65001",
        "mid": "65000",
        "mark": "65000",
        "oracle": "65000",
        "impact": "65001",
        "depth_notional": "100000",
        "max_slippage": "50",
        "max_oracle_deviation_bps": "50",
        "source": "hyperliquid.external_testnet",
        "cursor": "market-cursor-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "instrument_id": "BTC-USD-PERP",
        "asset_index": 0,
        "mapping_revision": "dca-testnet-v1",
        "universe_revision": "hyperliquid-default-perp-v1",
        "connection_epoch": "epoch-1",
    }
    return coordinator, plan, confirmation, broker, backend, market, _fill


def test_coordinator_runs_grid_rung_cycle_and_rearm(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)

    started = coordinator.start_grid_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    assert started["status"] == "grid_running"
    assert started["lifecycle"]["status"] == "active"
    allocation = started["execution_slice"]["allocation"]
    total_quantity = sum(float(row["quantity"]) for row in started["lifecycle"]["orders"])
    assert total_quantity <= float(allocation["effective_quantity"]) + 1e-9

    opened = coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000.0, tid=1),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    tp = next(row for row in opened["lifecycle"]["orders"] if row["event"] == "tp")
    assert opened["lifecycle"]["hard_stop_protection"]["status"] == "active"

    closed = coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(tp, price=65500.0, tid=2),
        market=_market_at(market, "2026-08-26T01:02:00+00:00"),
        timestamp="2026-08-26T01:02:00+00:00",
    )
    assert closed["status"] == "grid_running"
    assert closed["lifecycle"]["rungs"][0]["line"]["state"] == "rearmed"
    assert any(row["event"] == "entry_rearm" for row in closed["lifecycle"]["orders"])


def test_grid_hard_stop_is_terminal_and_not_rearmed(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_grid_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000.0, tid=3),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    triggered = coordinator.advance_grid_session(
        plan,
        broker=broker,
        price=63000.0,
        market=_market_at(market, "2026-08-26T01:02:00+00:00"),
        timestamp="2026-08-26T01:02:00+00:00",
    )
    hard_stop = next(row for row in triggered["lifecycle"]["orders"] if row["event"] == "hard_stop")
    terminal = coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(hard_stop, price=63000.0, tid=4),
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
        timestamp="2026-08-26T01:03:00+00:00",
    )

    assert terminal["status"] == "grid_terminal"
    assert terminal["lifecycle"]["status"] == "terminal"
    assert terminal["lifecycle"]["sealed"] is True
    assert terminal["next_action"] == "notify_park_and_wait"


def test_manual_grid_interrupt_preserves_position_and_resume_revalidates(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_grid_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    opened = coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000.0, tid=5),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    interrupted = coordinator.interrupt_grid_session(
        plan,
        broker=broker,
        reason="operator_interrupt",
        timestamp="2026-08-26T01:02:00+00:00",
    )
    assert interrupted["status"] == "grid_interrupted"
    assert interrupted["lifecycle"]["status"] == "interrupted"
    assert interrupted["lifecycle"]["rungs"][0]["line"]["entry_filled_quantity"] == pytest.approx(
        opened["lifecycle"]["rungs"][0]["line"]["entry_filled_quantity"]
    )
    assert any(
        row["event"] == "tp" and row["state"] == "accepted"
        for row in interrupted["lifecycle"]["orders"]
    )
    assert interrupted["execution_enabled"] is False

    resumed = coordinator.resume_grid_session(
        plan,
        broker=broker,
        confirmation=confirmation,
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
        timestamp="2026-08-26T01:03:00+00:00",
    )
    assert resumed["status"] == "grid_running"
    assert resumed["lifecycle"]["status"] == "active"
    assert resumed["execution_enabled"] is True


def test_grid_resume_blocks_when_external_broker_reports_foreign_open_order(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_grid_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    coordinator.advance_grid_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000.0, tid=21),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    coordinator.interrupt_grid_session(
        plan,
        broker=broker,
        timestamp="2026-08-26T01:02:00+00:00",
    )
    broker.broker_config["transport_state"] = "external_testnet"
    original_request = broker.request

    def foreign_orders(port, operation, payload=None):
        if port == "order_execution" and operation == "open_orders":
            return ({"order_id": "foreign-order"},)
        return original_request(port, operation, payload)

    broker.request = foreign_orders
    blocked = coordinator.resume_grid_session(
        plan,
        broker=broker,
        confirmation=confirmation,
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
        timestamp="2026-08-26T01:03:00+00:00",
    )
    assert blocked["status"] == "grid_blocked"
    assert "foreign_open_order_detected" in blocked["blocker"]
