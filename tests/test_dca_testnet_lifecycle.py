from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.dca_testnet_lifecycle import DcaTestnetLifecycle, DcaTestnetLifecycleError


def _plan() -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "dca-testnet-plan-1",
        "version": 1,
        "cycle_id": "2026-08-22_DAY",
        "direction": "long",
        "instrument_id": "BTC-USD-PERP",
        "dca": {
            "entry_levels": [65000.0, 64000.0],
            "notional_per_addition": 6500.0,
            "target_price": 66000.0,
            "stop_price": 64000.0,
        },
    }


def _broker(tmp_path: Path, *, protection: bool = True):
    from standard_broker import CapabilityDescriptor, BrokerEnvironment, ExternalEnvironmentApproval
    from standard_broker.adapters.hyperliquid import NautilusAdapterMetadata

    operations = {
        "order_execution": frozenset(
            {"submit", "cancel", "replace", "query", "open_orders"}
        )
    }
    if protection:
        operations["protection_order"] = frozenset(
            {
                "submit",
                "cancel",
                "replace",
                "query",
                "reduce_only",
                "mark_price_trigger",
                "grouped_tp_sl",
                "sibling_cancellation",
                "position_following",
                "position_level_tpsl",
                "take_profit_market",
                "stop_loss_market",
            }
        )
    profile = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        operations=operations,
        revision="dca-testnet-v1",
    )

    class Backend:
        local_only = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, object]] = []
            self.next_oid = 100
            self.last_cloid = ""
            self.metadata = NautilusAdapterMetadata(
                package="nautilus-hyperliquid",
                version="1.230.0",
                commit="dca-testnet-commit",
                capabilities=profile,
            )

        def invoke(self, port: str, operation: str, request: object):
            self.calls.append((port, operation, request))
            if port == "order_execution" and operation == "submit":
                self.next_oid += 1
                self.last_cloid = str(request["cloid"])
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {"statuses": [{"resting": {"oid": self.next_oid}}]},
                    },
                }
            if port == "order_execution" and operation in {"cancel", "replace"}:
                return {"status": "ok"}
            if port == "order_execution" and operation == "query":
                return {"status": "open", "oid": self.next_oid, "cloid": self.last_cloid, "timestamp": 1787313661000}
            if port == "order_execution" and operation == "open_orders":
                return {"orders": []}
            if port == "protection_order":
                return {"accepted": True}
            return {"status": "unknown"}

    backend = Backend()
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id="dca-testnet-approval",
        release_sha="a" * 40,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address="testnet-account",
        lifecycle_id="runtime-testnet",
    )
    context = BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "environment_fingerprint": "hyperliquid:testnet:dca",
            "account_id": "testnet-account",
            "credential_source": "HL_TESTNET_CREDENTIAL",
            "runtime_id": "runtime-testnet",
            "ledger_namespace": "ledger.standard-broker.testnet.dca",
            "release_sha": "a" * 40,
            "execution_scope": "hypercore:default",
            "backend": backend,
            "testnet_approval": approval,
            "instrument_meta": {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            "nautilus_expected_version": "1.230.0",
            "nautilus_expected_commit": "dca-testnet-commit",
        },
    )
    return build_broker_execution_port(context), backend


def _fill(order: dict, *, price: float, tid: int) -> dict:
    return {
        "coin": "BTC",
        "px": str(price),
        "sz": str(order["quantity"]),
        "side": "B" if order["side"] == "buy" else "A",
        "time": 1787313661000 + tid,
        "oid": int(order["broker_order_id"]),
        "cloid": order["client_order_id"],
        "tid": tid,
    }


def test_dca_testnet_is_sequential_and_confirms_aggregate_protection(tmp_path: Path) -> None:
    broker, backend = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()

    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    assert len(started["orders"]) == 1
    assert started["status"] == "waiting_entry"

    first = lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=1),
        timestamp="2026-08-22T01:01:00+00:00",
    )
    assert first["status"] == "open"
    assert first["protection"]["status"] == "active"
    assert first["protection"]["reduce_only"] is True
    assert len(first["orders"]) == 2
    assert [call[0:2] for call in backend.calls if call[0] == "protection_order"] == [
        ("protection_order", "submit"),
        ("protection_order", "query"),
    ]

    second = lifecycle.on_fill(
        plan,
        _fill(first["orders"][1], price=64000, tid=2),
        timestamp="2026-08-22T01:02:00+00:00",
    )
    assert second["status"] == "open"
    assert second["protection"]["quantity"] > first["protection"]["quantity"]
    assert second["protection"]["confirmed_operation"] == "query"


def test_dca_testnet_freezes_before_next_entry_when_protection_capability_missing(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=False)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()

    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    blocked = lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=3),
        timestamp="2026-08-22T01:01:00+00:00",
    )

    assert blocked["status"] == "blocked_protection"
    assert "capability_gap" in blocked["blocker"]
    assert len(blocked["orders"]) == 1


def test_dca_testnet_stop_cancels_remaining_entries_and_reaches_terminal(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    opened = lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=10),
        timestamp="2026-08-22T01:01:00+00:00",
    )

    stopping = lifecycle.on_market_event(
        plan,
        price=64000,
        timestamp="2026-08-22T01:02:00+00:00",
    )
    assert stopping["status"] == "stopping"
    assert any(row["event"] == "stop" and row["reduce_only"] for row in stopping["orders"])
    assert all(
        row["state"] != "accepted"
        for row in stopping["orders"]
        if row["event"] == "entry" and row["order_id"] != opened["orders"][0]["order_id"]
    )

    stop_order = next(row for row in stopping["orders"] if row["event"] == "stop")
    terminal = lifecycle.on_fill(
        plan,
        _fill(stop_order, price=64000, tid=11),
        timestamp="2026-08-22T01:03:00+00:00",
    )
    assert terminal["status"] == "terminal"
    assert terminal["positions"] == []
