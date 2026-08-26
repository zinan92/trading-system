from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from services.strategy_control_plane import StrategyControlMachineError
from services.testnet_automation_coordinator import TestnetAutomationCoordinator


NOW = "2026-08-26T01:00:00+00:00"


def _market_at(market: dict, timestamp: str) -> dict:
    return {**market, "observed_at": timestamp}


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
        "account_fingerprint": "sha256:" + hashlib.sha256(b"testnet-account").hexdigest(),
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime-testnet",
        "release_sha": "a" * 40,
        "capability_revision": "dca-testnet-v1",
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


def test_coordinator_runs_canonical_dca_to_terminal_notification(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)

    started = coordinator.start_dca_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    assert started["status"] == "dca_running"
    assert started["lifecycle"]["status"] == "waiting_entry"
    allocation = started["execution_slice"]["allocation"]
    assert started["execution_slice"]["execution_slice_id"] == allocation["execution_slice_id"]
    assert float(started["lifecycle"]["orders"][0]["price"]) * float(started["lifecycle"]["orders"][0]["quantity"]) <= float(allocation["effective_notional"])
    assert started["lifecycle"]["risk_budget"]["max_notional"] == float(allocation["effective_notional"])
    assert float(started["lifecycle"]["orders"][0]["price"]) * float(started["lifecycle"]["orders"][0]["quantity"]) <= float(allocation["effective_notional"])

    opened = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000, tid=1),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    assert opened["lifecycle"]["status"] == "open"
    assert opened["lifecycle"]["protection"]["status"] == "active"

    target_pending = coordinator.advance_dca_session(
        plan,
        broker=broker,
        price=66000,
        market=_market_at(market, "2026-08-26T01:02:00+00:00"),
        timestamp="2026-08-26T01:02:00+00:00",
    )
    target_order = next(
        row for row in target_pending["lifecycle"]["orders"] if row["event"] == "target"
    )
    terminal = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(target_order, price=66000, tid=2),
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
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
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    opened = coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000, tid=3),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
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
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
        timestamp="2026-08-26T01:03:00+00:00",
    )
    assert resumed["status"] == "dca_running"
    assert resumed["lifecycle"]["status"] == "open"
    assert resumed["lifecycle"]["positions"] == opened["lifecycle"]["positions"]
    assert resumed["execution_enabled"] is True
    effective_cap = float(resumed["execution_slice"]["allocation"]["effective_notional"])
    assert sum(
        float(row["price"]) * float(row["quantity"])
        for row in resumed["lifecycle"]["orders"]
        if row["event"] in {"entry", "entry_rearm"}
    ) <= effective_cap + 1e-9


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
            market={**_market_at(market, NOW), "broker_id": "binance"},
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


def test_dca_resume_blocks_when_external_broker_reports_foreign_open_order(tmp_path: Path) -> None:
    coordinator, plan, confirmation, broker, _backend, market, fill = _setup(tmp_path)
    started = coordinator.start_dca_session(
        plan,
        confirmation=confirmation,
        market=_market_at(market, NOW),
        broker=broker,
        timestamp=NOW,
    )
    coordinator.advance_dca_session(
        plan,
        broker=broker,
        fill=fill(started["lifecycle"]["orders"][0], price=65000, tid=11),
        market=_market_at(market, "2026-08-26T01:01:00+00:00"),
        timestamp="2026-08-26T01:01:00+00:00",
    )
    coordinator.interrupt_dca_session(
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
    blocked = coordinator.resume_dca_session(
        plan,
        broker=broker,
        confirmation=confirmation,
        market=_market_at(market, "2026-08-26T01:03:00+00:00"),
        timestamp="2026-08-26T01:03:00+00:00",
    )
    assert blocked["status"] == "dca_blocked"
    assert "foreign_open_order_detected" in blocked["blocker"]
