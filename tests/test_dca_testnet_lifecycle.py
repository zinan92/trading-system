from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.dca_testnet_lifecycle import DcaTestnetLifecycle, DcaTestnetLifecycleError


def _plan() -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "dca-testnet-plan-1",
        "strategy_session_id": "session-testnet",
        "strategy_revision_id": "revision-testnet",
        "plan_digest": "sha256:" + "a" * 64,
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
        "risk_budget": {
            "equity": 10000.0,
            "maximum_loss_at_full_depth": 1000.0,
            "leverage_limit": 10.0,
            "max_notional": 20000.0,
            "max_open_orders": 3,
            "max_open_positions": 1,
            "max_slippage": 50.0,
        },
    }


def _broker(tmp_path: Path, *, protection: bool = True):
    from standard_broker import CapabilityDescriptor, BrokerEnvironment, ExternalEnvironmentApproval
    from standard_broker.adapters.hyperliquid import NautilusAdapterMetadata

    operations = {
        "order_execution": frozenset(
            {"submit", "cancel", "replace", "query", "open_orders"}
        ),
        "account": frozenset({"read"}),
    }
    if protection:
        operations["protection_order"] = frozenset(
            {
                "submit",
                "cancel",
                "replace",
                "cancel_replace",
                "query",
                "reduce_only",
                "mark_price_trigger",
                "grouped_tp_sl",
                "sibling_cancellation",
                "position_following",
                "position_level_tpsl",
                "take_profit_limit",
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
            self.cancel_failure = False
            self.account_positions: list[dict] = []
            self.account_reads = 0
            self.protections: dict[str, dict[str, object]] = {}
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
                if operation == "cancel" and self.cancel_failure:
                    raise TimeoutError("ambiguous cancel")
                return {"status": "ok"}
            if port == "order_execution" and operation == "query":
                return {"status": "open", "oid": self.next_oid, "cloid": self.last_cloid, "timestamp": 1787313661000}
            if port == "order_execution" and operation == "open_orders":
                return {"orders": []}
            if port == "account" and operation == "read":
                from standard_broker import Provenance
                self.account_reads += 1
                return {
                    "data": {
                        "accountAddress": "testnet-account",
                            "snapshotId": f"snapshot-{self.account_reads}",
                        "marginSummary": {"accountValue": "10000", "totalNtlPos": "0"},
                        "assetPositions": list(self.account_positions),
                    },
                    "provenance": Provenance(
                        source="fixture.account",
                        execution_scope="hypercore:default",
                        transport_state="local_fixture",
                        mapping_revision="dca-testnet-v1",
                    ),
                }
            if port == "protection_order":
                protection_id = str(request.get("protectionId") or "")
                if operation in {"submit", "replace"}:
                    quantity = str(request.get("quantity") or "0")
                    self.protections[protection_id] = {
                        "quantity": quantity,
                        "state": "submitted",
                    }
                elif operation == "cancel":
                    self.protections.setdefault(protection_id, {"quantity": "0"})["state"] = "canceled"
                record = self.protections.get(protection_id, {"quantity": "0", "state": "unknown"})
                return {
                    "protection_id": protection_id,
                    "operation": operation,
                    "accepted": True,
                    "state": "active" if operation == "query" and record.get("state") != "canceled" else str(record.get("state") or "unknown"),
                    "covered_quantity": record.get("quantity", "0") if operation == "query" else "0",
                    "order_ids": [f"{protection_id}:tp", f"{protection_id}:sl"],
                }
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
            "transport_profile": "local_fixture_v1",
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
    protection_submit = next(
        request
        for port, operation, request in backend.calls
        if port == "protection_order" and operation == "submit"
    )
    assert protection_submit["quantityPolicy"] == "position_following"
    tp_leg, sl_leg = protection_submit["legs"]
    assert tp_leg["execution"] == "limit"
    assert tp_leg["limitPx"] == "66000.0"
    assert sl_leg["execution"] == "market"
    assert all(leg["reduceOnly"] for leg in (tp_leg, sl_leg))
    assert all(leg["triggerReference"] == "mark" for leg in (tp_leg, sl_leg))
    assert tp_leg["siblingId"] == sl_leg["siblingId"]
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


def test_dca_order_row_persists_native_client_order_id_when_receipt_exposes_it(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", object())
    receipt = SimpleNamespace(
        state="resting",
        order_id="canonical-order-1",
        client_order_id="canonical-cloid-1",
        native_client_order_id="0x" + "6" * 32,
        broker_order_id="2001",
        account_address="testnet-account",
        release_sha="a" * 40,
        provenance=SimpleNamespace(
            source="fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="test",
        ),
    )

    row = lifecycle._order_row({"ticket_id": "dca-entry-1"}, receipt)

    assert row["native_client_order_id"] == "0x" + "6" * 32


def test_dca_testnet_freezes_before_next_entry_when_protection_capability_missing(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=False)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()

    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    assert started["status"] == "blocked_protection"
    assert "capability_gap" in started["blocker"]
    assert started["orders"] == []


def test_dca_testnet_partial_fill_does_not_advance_or_attach_protection(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")

    partial = lifecycle.on_fill(
        plan,
        {**_fill(started["orders"][0], price=65000, tid=20), "sz": "0.04"},
        timestamp="2026-08-22T01:01:00+00:00",
    )

    assert partial["status"] == "partial_entry"
    assert partial["protection"]["status"] == "active"
    assert partial["protection"]["quantity"] == 0.04
    assert len(partial["orders"]) == 1


def test_dca_testnet_cancel_failure_stops_before_exit_submission(tmp_path: Path) -> None:
    broker, backend = _broker(tmp_path, protection=True)
    backend.cancel_failure = True
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")

    lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=30),
        timestamp="2026-08-22T01:01:00+00:00",
    )
    blocked = lifecycle.on_market_event(
        plan,
        price=64000,
        timestamp="2026-08-22T01:02:00+00:00",
    )

    assert blocked["status"] == "blocked_reconciliation"
    assert "entry_cancel_failed" in blocked["blocker"]
    assert not any(row["event"] == "stop" for row in blocked["orders"])


def test_dca_testnet_partial_exit_reduces_position_and_refreshes_protection(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000, tid=69), timestamp="2026-08-22T01:01:00+00:00")
    stopping = lifecycle.stop(plan, timestamp="2026-08-22T01:02:00+00:00", reason="strategy_stop", price=64000)
    stop_order = next(row for row in stopping["orders"] if row["event"] == "stop")
    partial = lifecycle.on_fill(
        plan,
        {**_fill(stop_order, price=64000, tid=70), "sz": str(float(stop_order["quantity"]) / 2)},
        timestamp="2026-08-22T01:03:00+00:00",
    )

    assert partial["status"] == "stopping"
    assert partial["positions"][0]["quantity"] == pytest.approx(float(stop_order["quantity"]) / 2)
    assert partial["protection"]["quantity"] == pytest.approx(float(stop_order["quantity"]) / 2)


def test_dca_testnet_exit_slippage_blocks_terminal_seal(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000, tid=72), timestamp="2026-08-22T01:01:00+00:00")
    stopping = lifecycle.stop(plan, timestamp="2026-08-22T01:02:00+00:00", reason="strategy_stop", price=64000)
    stop_order = next(row for row in stopping["orders"] if row["event"] == "stop")
    breached = lifecycle.on_fill(plan, _fill(stop_order, price=65000, tid=73), timestamp="2026-08-22T01:03:00+00:00")

    assert breached["status"] == "terminal"
    assert breached["sealed"] is True
    assert breached["closure_blocker"] == "exit_fill_slippage_exceeded"
    assert breached["next_action"] == "notify_park_and_wait"


def test_dca_testnet_restart_does_not_reopen_terminal_revision(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=40),
        timestamp="2026-08-22T01:01:00+00:00",
    )
    stopping = lifecycle.stop(
        plan,
        timestamp="2026-08-22T01:02:00+00:00",
        reason="strategy_stop",
        price=64000,
    )
    terminal = lifecycle.on_fill(
        plan,
        _fill(next(row for row in stopping["orders"] if row["event"] == "stop"), price=64000, tid=41),
        timestamp="2026-08-22T01:03:00+00:00",
    )
    restarted = DcaTestnetLifecycle(tmp_path / "outputs", broker).start(
        plan,
        timestamp="2026-08-22T02:00:00+00:00",
    )

    assert terminal["status"] == "terminal"
    assert restarted["status"] == "terminal"
    assert restarted["sealed"] is True
    assert restarted["park_notification"]["status"] == "queued"
    assert {event["event"] for event in restarted["events"]} >= {"revision_sealed", "park_notification_queued"}


def test_dca_testnet_terminal_does_not_seal_when_broker_position_remains(tmp_path: Path) -> None:
    broker, backend = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65000, tid=60),
        timestamp="2026-08-22T01:01:00+00:00",
    )
    backend.account_positions = [
        {
            "position": {
                "coin": "BTC",
                "szi": "0.1",
                "positionId": "remote-position-1",
                "leverage": {"type": "cross", "value": "1"},
            }
        }
    ]
    stopping = lifecycle.stop(plan, timestamp="2026-08-22T01:02:00+00:00", reason="strategy_stop", price=64000)
    terminal = lifecycle.on_fill(
        plan,
        _fill(next(row for row in stopping["orders"] if row["event"] == "stop"), price=64000, tid=61),
        timestamp="2026-08-22T01:03:00+00:00",
    )

    assert terminal["status"] == "blocked_reconciliation"
    assert terminal.get("sealed") is not True
    assert terminal["reconciliation"]["broker_position_count"] == 1


def test_dca_testnet_slippage_records_actual_fill_and_submits_reduce_only_recovery(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    slipped = lifecycle.on_fill(
        plan,
        _fill(started["orders"][0], price=65100, tid=62),
        timestamp="2026-08-22T01:01:00+00:00",
    )

    assert slipped["status"] == "blocked_risk_flattening"
    assert slipped["positions"][0]["entry_price"] == 65100
    recovery = next(row for row in slipped["orders"] if row["event"] == "risk_recovery")
    assert recovery["reduce_only"] is True
    assert slipped["fills"][0]["slippage"] == 100
    recovered = lifecycle.on_fill(plan, _fill(recovery, price=64000, tid=67), timestamp="2026-08-22T01:02:00+00:00")
    assert recovered["status"] == "terminal"
    assert recovered["sealed"] is True


def test_dca_testnet_loss_budget_breach_submits_recovery_after_recording_fill(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["risk_budget"] = {**plan["risk_budget"], "maximum_loss_at_full_depth": 100.0}
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    breached = lifecycle.on_fill(plan, _fill(started["orders"][0], price=65010, tid=68), timestamp="2026-08-22T01:01:00+00:00")

    assert breached["status"] == "blocked_risk_flattening"
    assert breached["positions"][0]["quantity"] > 0
    assert any(row["event"] == "risk_recovery" and row["reduce_only"] for row in breached["orders"])


def test_dca_testnet_terminal_stop_is_immutable(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000, tid=63), timestamp="2026-08-22T01:01:00+00:00")
    stopping = lifecycle.stop(plan, timestamp="2026-08-22T01:02:00+00:00", reason="strategy_stop", price=64000)
    terminal = lifecycle.on_fill(plan, _fill(next(row for row in stopping["orders"] if row["event"] == "stop"), price=64000, tid=64), timestamp="2026-08-22T01:03:00+00:00")
    replay = lifecycle.stop(plan, timestamp="2026-08-22T01:04:00+00:00", reason="replay", price=63900)

    assert replay == terminal


def test_dca_testnet_event_rejects_same_id_with_changed_revision(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    changed = {**plan, "plan_digest": "sha256:" + "b" * 64}

    with pytest.raises(DcaTestnetLifecycleError, match="strategy_revision_mismatch"):
        lifecycle.on_fill(changed, _fill(started["orders"][0], price=65000, tid=65), timestamp="2026-08-22T01:01:00+00:00")


def test_dca_testnet_crossed_entry_uses_bounded_market_catch_up(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    plan["dca"] = {**plan["dca"], "stop_price": 63000.0}
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    lifecycle.on_fill(plan, _fill(started["orders"][0], price=65000, tid=66), timestamp="2026-08-22T01:01:00+00:00")

    caught_up = lifecycle.on_market_event(plan, price=63980.0, timestamp="2026-08-22T01:02:00+00:00")

    catch_up = next(row for row in caught_up["orders"] if row["event"] == "entry_catch_up")
    assert catch_up["order_type"] == "limit"
    assert catch_up["time_in_force"] == "ioc"
    assert catch_up["execution_semantics"] == "aggressive_ioc_market"
    assert catch_up["planned_price"] == 64000.0
    assert catch_up["price"] == 63980.0
    assert catch_up["state"] == "accepted"


def test_dca_testnet_stop_before_first_fill_reconciles_before_sealing(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")

    stopped = lifecycle.on_market_event(plan, price=64000.0, timestamp="2026-08-22T01:01:00+00:00")

    assert stopped["status"] == "stopped"
    assert stopped["sealed"] is True
    assert stopped["reconciliation"]["status"] == "ok"


def test_dca_testnet_hash_only_fill_is_idempotent(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    raw = _fill(started["orders"][0], price=65000, tid=50)
    raw.pop("tid")
    raw["hash"] = "fill-hash-50"

    first = lifecycle.on_fill(plan, raw, timestamp="2026-08-22T01:01:00+00:00")
    second = lifecycle.on_fill(plan, raw, timestamp="2026-08-22T01:02:00+00:00")

    assert len(first["fills"]) == len(second["fills"]) == 1
    assert second["positions"][0]["quantity"] == first["positions"][0]["quantity"]


def test_dca_testnet_tid_and_hash_aliases_are_one_fill_identity(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    first_raw = _fill(started["orders"][0], price=65000, tid=71)
    first_raw["hash"] = "fill-hash-71"
    first = lifecycle.on_fill(plan, {key: value for key, value in first_raw.items() if key != "tid"}, timestamp="2026-08-22T01:01:00+00:00")
    second = lifecycle.on_fill(plan, first_raw, timestamp="2026-08-22T01:02:00+00:00")

    assert len(second["fills"]) == 1
    assert second["positions"][0]["quantity"] == first["positions"][0]["quantity"]


def test_dca_testnet_revision_change_is_rejected_on_restart(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, protection=True)
    lifecycle = DcaTestnetLifecycle(tmp_path / "outputs", broker)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-08-22T01:00:00+00:00")
    changed = {**plan, "plan_digest": "sha256:" + "b" * 64}

    with pytest.raises(DcaTestnetLifecycleError, match="strategy_revision_mismatch"):
        lifecycle.start(changed, timestamp="2026-08-22T01:01:00+00:00")


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
