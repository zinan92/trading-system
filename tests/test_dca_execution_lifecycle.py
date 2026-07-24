from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from services.dca_execution_lifecycle import DcaPaperLifecycle
from services.dca_plan import build_dca_preview, build_dca_strategy_plan
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.journal_store import load_json, write_json
from services.legacy_paper_execution_adapter import LegacyPaperExecutionAdapter


CYCLE_ID = "2026-07-22_NIGHT"


def _config() -> dict:
    return deepcopy(DEFAULT_DUALTRACK_CONFIG)


def _market(price: float = 4_010.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T15:59:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T15:{index:02d}:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for index in range(20)
        ],
    }


def _plan(direction: str = "long") -> dict:
    if direction == "long":
        levels, target, stop = [4_004.0, 3_996.0, 3_988.0], 4_050.0, 3_970.0
    else:
        levels, target, stop = [4_016.0, 4_024.0, 4_032.0], 3_970.0, 4_050.0
    preview = build_dca_preview(
        CYCLE_ID,
        {
            "direction": direction,
            "dca": {
                "entry_levels": levels,
                "target_price": target,
                "stop_price": stop,
                "notional_per_addition": 2_000.0,
                "max_additions": 3,
                "loop_enabled": False,
            },
            "risk_budget": {"leverage": 10},
        },
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )
    return build_dca_strategy_plan(
        preview,
        strategy_plan_id=f"strategy-plan-dca-{direction}",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )


def _event(sequence: int, price: float) -> dict:
    minute = sequence + 1
    return {
        "cycle_id": CYCLE_ID,
        "ts_event": f"2026-07-22T16:{minute:02d}:00+00:00",
        "event_started_at": f"2026-07-22T16:{sequence:02d}:00+00:00",
        "price": price,
        "open": price,
        "high": price,
        "low": price,
        "fresh": True,
        "is_synthetic": False,
        "source": "dca_lifecycle_test",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
        "event_id": f"dca-event-{sequence}-{price}",
    }


def _lifecycle(tmp_path: Path):
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=_config())
    return DcaPaperLifecycle(tmp_path / "outputs", adapter), adapter


class PartialFillAdapter:
    name = "partial_fill_fake"

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.positions: list[dict] = []

    def submit_order(self, command: dict) -> dict:
        source_id = str(command["source_fill_id"])
        existing = next(
            (row for row in self.orders if row["source_fill_id"] == source_id),
            None,
        )
        if existing:
            return dict(existing)
        row = {
            **command,
            "order_id": f"fake-order-{len(self.orders) + 1}",
            "state": "accepted",
            "source_fill_id": source_id,
        }
        self.orders.append(row)
        return dict(row)

    def cancel_orders(self, _cycle_id: str, *, order_ids=None, **_kwargs) -> dict:
        selected = set(order_ids or [])
        cancelled = []
        for row in self.orders:
            if row["state"] == "accepted" and row["order_id"] in selected:
                row["state"] = "cancelled"
                cancelled.append(row["order_id"])
        return {"cancelled_order_ids": cancelled, "cancelled_order_count": len(cancelled)}

    def process_market_event(self, event: dict) -> dict:
        return {"status": "ok", "event_id": event.get("event_id")}

    def snapshot(self, _cycle_id: str, **_kwargs) -> dict:
        return {
            "orders": deepcopy(self.orders),
            "fills": deepcopy(self.fills),
            "positions": deepcopy(self.positions),
        }

    def reconcile(self, _cycle_id: str) -> dict:
        return {"status": "ok", "issues": []}


def test_dca_partial_fills_resize_quantity_without_counting_extra_additions(
    tmp_path: Path,
) -> None:
    adapter = PartialFillAdapter()
    lifecycle = DcaPaperLifecycle(tmp_path / "outputs", adapter)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    plan_id = plan["strategy_plan_id"]
    first_order = adapter.orders[0]
    adapter.fills = [{
        "fill_id": "partial-1",
        "order_id": first_order["order_id"],
        "source_fill_id": first_order["source_fill_id"],
        "event": "entry",
        "strategy_plan_id": plan_id,
    }]
    adapter.positions = [{
        "trade_id": f"dca-round:{plan_id}",
        "status": "open",
        "side": "long",
        "remaining_units": 0.25,
        "entry_price": 4_004.0,
        "strategy_plan_id": plan_id,
    }]
    first = lifecycle.reconcile(plan, timestamp="2026-07-22T16:01:00+00:00")

    adapter.fills.append({
        "fill_id": "partial-2",
        "order_id": first_order["order_id"],
        "source_fill_id": first_order["source_fill_id"],
        "event": "entry",
        "strategy_plan_id": plan_id,
    })
    adapter.positions[0]["remaining_units"] = 0.499
    second = lifecycle.reconcile(plan, timestamp="2026-07-22T16:02:00+00:00")

    assert first["additions_filled"] == second["additions_filled"] == 1
    assert len(second["observed_entry_fill_ids"]) == 2
    assert len(second["target_generations"]) == 2
    assert second["target_generations"][0]["status"] == "cancelled"
    assert second["active_target"]["quantity"] == 0.499


def test_dca_second_fill_atomically_replaces_one_aggregate_target(tmp_path: Path) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    started = lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    assert started["state"]["status"] == "waiting_entry"
    assert len(started["receipts"]) == 3

    first = lifecycle.process_market_event(plan, _event(1, 4_004.0))["state"]
    first_target = deepcopy(first["active_target"])
    assert first["additions_filled"] == 1
    assert first_target["status"] == "accepted"
    assert first_target["quantity"] == pytest.approx(
        adapter.snapshot(CYCLE_ID)["positions"][0]["remaining_units"]
    )

    second = lifecycle.process_market_event(plan, _event(2, 3_996.0))["state"]
    assert second["additions_filled"] == 2
    assert len(second["target_generations"]) == 2
    assert second["target_generations"][0]["status"] == "cancelled"
    assert second["target_generations"][0]["retire_reason"] == (
        "replaced_after_quantity_change"
    )
    assert second["active_target"]["target_id"] != first_target["target_id"]
    assert second["active_target"]["quantity"] > first_target["quantity"]
    assert sum(
        row["status"] == "accepted" for row in second["target_generations"]
    ) == 1


def test_dca_third_fill_keeps_exactly_one_aggregate_target(tmp_path: Path) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")

    first = lifecycle.process_market_event(plan, _event(1, 4_004.0))["state"]
    second = lifecycle.process_market_event(plan, _event(2, 3_996.0))["state"]
    third = lifecycle.process_market_event(plan, _event(3, 3_988.0))["state"]

    assert [first["additions_filled"], second["additions_filled"], third["additions_filled"]] == [1, 2, 3]
    assert len(third["target_generations"]) == 3
    assert third["active_target"]["generation"] == 3
    assert third["active_target"]["quantity"] == pytest.approx(third["open_quantity"])
    assert third["active_target"]["quantity"] > second["active_target"]["quantity"]
    assert sum(row["status"] == "accepted" for row in third["target_generations"]) == 1
    assert all(
        row["status"] == "cancelled" and row["retire_reason"] == "replaced_after_quantity_change"
        for row in third["target_generations"][:-1]
    )


def test_dca_duplicate_event_and_restart_do_not_duplicate_target(tmp_path: Path) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    event = _event(1, 4_004.0)
    first = lifecycle.process_market_event(plan, event)["state"]

    duplicate = lifecycle.process_market_event(plan, event)["state"]
    restarted_lifecycle = DcaPaperLifecycle(tmp_path / "outputs", adapter)
    restarted_lifecycle.start(
        plan,
        timestamp="2026-07-22T16:03:00+00:00",
    )
    restarted = restarted_lifecycle.reconcile(
        plan,
        timestamp="2026-07-22T16:04:00+00:00",
    )

    assert duplicate["active_target"]["target_id"] == first["active_target"]["target_id"]
    assert restarted["active_target"]["target_id"] == first["active_target"]["target_id"]
    assert len(restarted["target_generations"]) == 1
    assert restarted["observed_entry_fill_ids"] == first["observed_entry_fill_ids"]
    assert len(adapter.snapshot(CYCLE_ID)["orders"]) == 3


def test_dca_target_cancels_remaining_entries_and_closes_accumulated_round(
    tmp_path: Path,
) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    lifecycle.process_market_event(plan, _event(1, 4_004.0))
    before = lifecycle.process_market_event(plan, _event(2, 3_996.0))["state"]
    expected_quantity = before["open_quantity"]

    result = lifecycle.process_market_event(plan, _event(3, 4_050.0))
    state = result["state"]
    snapshot = adapter.snapshot(CYCLE_ID)
    target_fills = [row for row in snapshot["fills"] if row.get("event") == "target"]

    assert result["target_submission"] is not None
    assert state["status"] == "target_closed"
    assert state["open_quantity"] == 0
    assert state["active_target"] is None
    assert len(target_fills) == 1
    assert target_fills[0]["pnl_units"] == pytest.approx(expected_quantity)
    assert len(target_fills[0]["matched_entries"]) == 2
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]


def test_dca_terminal_target_uses_only_latest_aggregate_generation(tmp_path: Path) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    first = lifecycle.process_market_event(plan, _event(1, 4_004.0))["state"]
    second = lifecycle.process_market_event(plan, _event(2, 3_996.0))["state"]
    retired = first["active_target"]
    active = second["active_target"]

    closed = lifecycle.process_market_event(plan, _event(3, 4_050.0))
    target_fills = [
        row for row in adapter.snapshot(CYCLE_ID)["fills"] if row.get("event") == "target"
    ]

    assert retired["target_id"] != active["target_id"]
    assert second["target_generations"][0]["status"] == "cancelled"
    assert closed["target_hit"] is True
    assert closed["stop_hit"] is False
    assert len(target_fills) == 1
    assert active["target_id"] in target_fills[0]["source_fill_id"]
    assert retired["target_id"] not in target_fills[0]["source_fill_id"]
    assert target_fills[0]["pnl_units"] == pytest.approx(second["open_quantity"])


def test_dca_valid_geometry_resolves_target_and_stop_as_mutually_exclusive(
    tmp_path: Path,
) -> None:
    plan = _plan()
    target_lifecycle, _ = _lifecycle(tmp_path / "target")
    target_lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    target_lifecycle.process_market_event(plan, _event(1, 4_004.0))
    target = target_lifecycle.process_market_event(plan, _event(2, 4_050.0))

    stop_lifecycle, _ = _lifecycle(tmp_path / "stop")
    stop_lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    stop_lifecycle.process_market_event(plan, _event(1, 4_004.0))
    stop = stop_lifecycle.process_market_event(plan, _event(2, 3_970.0))

    assert (target["target_hit"], target["stop_hit"]) == (True, False)
    assert target["state"]["status"] == "target_closed"
    assert (stop["target_hit"], stop["stop_hit"]) == (False, True)
    assert stop["state"]["status"] == "stop_closed"


def test_dca_aggregate_target_submits_one_exact_reduce_only_order_per_nautilus_position(
    tmp_path: Path,
) -> None:
    """One logical DCA target must not use the shared round ID as a close selector."""

    output_root = tmp_path / "outputs"
    preflight = output_root / "dualtrack" / "nautilus" / "instrument_preflight.json"
    write_json(preflight, [{
        "status": "ready_for_paper_shadow",
        "fee_model": {
            "mode": "account_observed",
            "maker_fee_rate": "0",
            "taker_fee_rate": "0.000400",
            "funding_rate": "0.0001",
            "funding_time": 1,
            "observed_at": "2026-07-22T16:00:00+00:00",
            "real_money_eligible": False,
        },
    }])
    adapter = NautilusExecutionAdapter(
        output_root,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: {},
        defer_replay=True,
        config=_config(),
    )
    lifecycle = DcaPaperLifecycle(output_root, adapter)
    plan = _plan()
    plan_id = plan["strategy_plan_id"]
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    round_id = f"dca-round:{plan_id}"
    snapshot = {
        "schema_version": "dualtrack-execution-v1",
        "engine": adapter.name,
        "cycle_id": CYCLE_ID,
        "orders": [],
        "fills": [
            {
                "fill_id": "dca-entry-a",
                "order_id": "dca-entry-a",
                "event": "entry",
                "strategy_plan_id": plan_id,
                "strategy_plan_version": 1,
            },
            {
                "fill_id": "dca-entry-b",
                "order_id": "dca-entry-b",
                "event": "entry",
                "strategy_plan_id": plan_id,
                "strategy_plan_version": 1,
            },
        ],
        "positions": [
            {
                "trade_id": round_id,
                "position_id": "POS-dca-a",
                "status": "open",
                "side": "long",
                "remaining_units": 0.25,
                "entry_price": 4_004.0,
                "strategy_plan_id": plan_id,
                "strategy_plan_version": 1,
            },
            {
                "trade_id": round_id,
                "position_id": "POS-dca-b",
                "status": "open",
                "side": "long",
                "remaining_units": 0.25,
                "entry_price": 3_996.0,
                "strategy_plan_id": plan_id,
                "strategy_plan_version": 1,
            },
        ],
        "account": {},
        "pnl": {"realized": 0.0, "unrealized": 0.0},
        "mark": {"price": 4_050.0, "fresh": True, "source": "test"},
        "capabilities": {"native_order_lifecycle": True},
    }
    adapter._persist_snapshot(CYCLE_ID, snapshot)

    result = lifecycle.process_market_event(plan, _event(3, 4_050.0))

    submitted = result["target_submission"]
    assert submitted is not None
    assert len(submitted["order_ids"]) == 2
    target_commands = [
        row["command"]
        for row in load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")
        if row.get("command", {}).get("event") == "target"
    ]
    assert {row["position_id"] for row in target_commands} == {"POS-dca-a", "POS-dca-b"}
    assert {row["quantity"] for row in target_commands} == {0.25}
    assert all(row.get("trade_id") in (None, "") for row in target_commands)
    assert all(row["reduce_only"] is True for row in target_commands)
    assert result["state"]["active_target"]["execution_order_ids"] == submitted["order_ids"]

    stopped = lifecycle.process_market_event(plan, _event(4, 3_970.0))["state"]
    assert set(stopped["cancelled_target_order_ids"]) == set(submitted["order_ids"])


def test_dca_stop_cancels_remaining_entries_and_retires_target(tmp_path: Path) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    lifecycle.process_market_event(plan, _event(1, 4_004.0))

    result = lifecycle.process_market_event(plan, _event(2, 3_970.0))
    snapshot = adapter.snapshot(CYCLE_ID)

    assert result["state"]["status"] == "stop_closed"
    assert result["state"]["active_target"] is None
    assert result["state"]["open_quantity"] == 0
    assert len([row for row in snapshot["fills"] if row.get("event") == "stop"]) == 1
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]


@pytest.mark.parametrize(
    ("direction", "entry_price", "target_price", "expected_side"),
    [
        ("long", 4_004.0, 4_050.0, "sell"),
        ("short", 4_016.0, 3_970.0, "buy"),
    ],
)
def test_dca_lifecycle_is_symmetric_for_long_and_short(
    tmp_path: Path,
    direction: str,
    entry_price: float,
    target_price: float,
    expected_side: str,
) -> None:
    lifecycle, adapter = _lifecycle(tmp_path)
    plan = _plan(direction)
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    opened = lifecycle.process_market_event(plan, _event(1, entry_price))["state"]

    assert opened["active_target"]["side"] == expected_side
    closed = lifecycle.process_market_event(plan, _event(2, target_price))["state"]
    assert closed["status"] == "target_closed"
    assert adapter.reconcile(CYCLE_ID)["status"] == "ok"


class FailingTargetSubmitAdapter:
    """Delegate to the real Paper adapter but fail the first target submit."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.target_submit_attempts = 0

    @property
    def name(self) -> str:
        return self._inner.name

    def submit_order(self, command: dict) -> dict:
        if str(command.get("event") or "") == "target":
            self.target_submit_attempts += 1
            if self.target_submit_attempts == 1:
                raise RuntimeError("paper engine submit transport failed")
        return self._inner.submit_order(command)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def test_dca_target_submit_failure_stays_fail_closed_and_retries_exactly_once(
    tmp_path: Path,
) -> None:
    inner = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=_config())
    adapter = FailingTargetSubmitAdapter(inner)
    lifecycle = DcaPaperLifecycle(tmp_path / "outputs", adapter)
    plan = _plan()
    lifecycle.start(plan, timestamp="2026-07-22T16:00:00+00:00")
    lifecycle.process_market_event(plan, _event(1, 4_004.0))
    accumulated = lifecycle.process_market_event(plan, _event(2, 3_996.0))["state"]
    expected_quantity = accumulated["open_quantity"]
    generations_before = len(accumulated["target_generations"])

    with pytest.raises(RuntimeError, match="submit transport failed"):
        lifecycle.process_market_event(plan, _event(3, 4_050.0))

    # Reload from disk exactly as a restarted process would.
    recovered = DcaPaperLifecycle(tmp_path / "outputs", adapter).reconcile(
        plan,
        timestamp="2026-07-22T16:04:00+00:00",
    )
    active = recovered["active_target"]
    assert recovered["status"] != "target_triggered"
    assert active is not None
    assert active["status"] == "accepted"
    assert not active.get("triggered_at")
    assert not active.get("execution_order_id")
    assert len(recovered["target_generations"]) == generations_before
    assert recovered["open_quantity"] == pytest.approx(expected_quantity)
    snapshot = adapter.snapshot(CYCLE_ID)
    assert [row for row in snapshot["fills"] if row.get("event") == "target"] == []
    assert adapter.target_submit_attempts == 1

    retry = lifecycle.process_market_event(plan, _event(4, 4_050.0))
    state = retry["state"]
    snapshot = adapter.snapshot(CYCLE_ID)
    target_fills = [row for row in snapshot["fills"] if row.get("event") == "target"]

    assert adapter.target_submit_attempts == 2
    assert retry["target_submission"] is not None
    assert state["status"] == "target_closed"
    assert state["open_quantity"] == 0
    assert state["active_target"] is None
    assert len(target_fills) == 1
    assert target_fills[0]["pnl_units"] == pytest.approx(expected_quantity)
    assert not [row for row in snapshot["orders"] if row.get("state") == "accepted"]
