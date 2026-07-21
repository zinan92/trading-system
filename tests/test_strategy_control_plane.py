from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_config import dualtrack_config as load_test_config
from services.journal_store import load_json
from services.strategy_control_plane import StrategyControlPlane
import services.strategy_control_plane as strategy_control_plane_module


@pytest.fixture(autouse=True)
def _isolate_legacy_execution_engine(monkeypatch: pytest.MonkeyPatch):
    config = load_test_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    factory = lambda *args, **kwargs: deepcopy(config)
    monkeypatch.setattr(strategy_control_plane_module, "dualtrack_config", factory)
    monkeypatch.setattr("services.dualtrack_config.dualtrack_config", factory)


def proposal(cycle_id: str, source: str, direction: str = "long") -> dict:
    return {
        "cycle_id": cycle_id,
        "source": source,
        "direction": direction,
        "range": {"low": 100.0, "high": 120.0},
        "key_levels": [100.0, 110.0, 120.0],
        "grid": {"count": 4, "notional_per_grid": 250.0, "spacing": 5.0},
        "signal": {"name": "ema_trend", "confidence": 7},
        "tp_sl": {"tp": 118.0, "sl": 96.0, "r_multiple": 2.0},
        "risk_budget": {"max_loss": 100.0, "max_leverage": 3.0},
        "intraday_rules": [{"if": "range_break", "then": "stand_down"}],
    }


def market(*, close: float = 110.0, fresh: bool = True) -> dict:
    bars = []
    for index in range(40):
        bar_close = close - 2.0 + index * 0.05
        bars.append({
            "timestamp": f"2026-07-05T01:{index:02d}:00+00:00",
            "open": round(bar_close - 0.1, 4),
            "high": round(bar_close + 0.4, 4),
            "low": round(bar_close - 0.4, 4),
            "close": round(bar_close, 4),
        })
    def context_bars(timeframe: str, span: float) -> list[dict]:
        rows = []
        for index in range(20):
            bar_close = close - 1.0 + index * 0.05
            rows.append({
                "timestamp": f"2026-06-{index + 1:02d}T00:00:00+00:00" if timeframe == "1d" else f"2026-07-02T{(index % 6) * 4:02d}:00:00+00:00",
                "open": round(bar_close - 0.1, 4),
                "high": round(bar_close + span / 2.0, 4),
                "low": round(bar_close - span / 2.0, 4),
                "close": round(bar_close, 4),
            })
        return rows

    return {
        "status": "ready" if fresh else "stale",
        "fresh": fresh,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {"timeframe": "1d", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("1d", 10.0)},
            "4h": {"timeframe": "4h", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("4h", 4.0)},
        },
    }


class RecordingAdapter:
    name = "legacy_paper"

    def __init__(
        self,
        orders: list[dict],
        positions: list[dict],
        *,
        fail_on_submission: int | None = None,
        vary_transient: bool = False,
    ) -> None:
        self.orders = deepcopy(orders)
        self.positions = deepcopy(positions)
        self.fail_on_submission = fail_on_submission
        self.vary_transient = vary_transient
        self.submission_count = 0
        self.snapshot_count = 0
        self.cancel_calls: list[dict] = []

    def submit_order(self, command: dict) -> dict:
        self.submission_count += 1
        if self.fail_on_submission == self.submission_count:
            raise RuntimeError("injected extend stage failure")
        row = {
            "order_id": f"staged-{self.submission_count}",
            "state": "accepted",
            "event": command["event"],
            "order_type": command["order_type"],
            "side": command["side"],
            "price": command["price"],
            "quantity": command["quantity"],
            "notional": command["notional"],
            "sl": command["sl"],
            "tp": command["tp"],
            "strategy_plan_id": command["strategy_plan_id"],
            "strategy_plan_version": command["strategy_plan_version"],
        }
        self.orders.append(row)
        return dict(row)

    def cancel_orders(
        self,
        _cycle_id: str,
        *,
        order_ids: list[str] | None = None,
        strategy_plan_id: str | None = None,
        **_kwargs,
    ) -> dict:
        selected_ids = {str(value) for value in order_ids or []}
        cancelled: list[str] = []
        self.cancel_calls.append({
            "order_ids": list(order_ids or []),
            "strategy_plan_id": strategy_plan_id,
        })
        for row in self.orders:
            if row.get("state") != "accepted":
                continue
            if selected_ids and str(row.get("order_id") or "") not in selected_ids:
                continue
            if strategy_plan_id and str(row.get("strategy_plan_id") or "") != strategy_plan_id:
                continue
            row["state"] = "cancelled"
            cancelled.append(str(row["order_id"]))
        return {
            "cancelled_order_count": len(cancelled),
            "cancelled_order_ids": cancelled,
        }

    def snapshot(self, _cycle_id: str, **_kwargs) -> dict:
        self.snapshot_count += 1
        orders = deepcopy(self.orders)
        positions = deepcopy(self.positions)
        if self.vary_transient:
            for row in orders:
                row["observed_at"] = f"snapshot-{self.snapshot_count}"
            for row in positions:
                row["unrealized_pnl"] = float(self.snapshot_count)
                row["mark_price"] = 110.0 + self.snapshot_count
        return {
            "orders": orders,
            "positions": positions,
            "fills": [],
        }

    def reconcile(self, _cycle_id: str) -> dict:
        return {"status": "ok", "issues": []}


def _running_plane(tmp_path: Path) -> tuple[StrategyControlPlane, dict]:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )
    return plane, started


def test_plan_proposals_share_schema_and_active_plan_has_field_sources(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    human = plane.upsert_proposal(proposal(cycle_id, "human"), now="2026-07-05T01:00:00+00:00")
    ai = plane.upsert_proposal(proposal(cycle_id, "ai", "short"), now="2026-07-05T01:00:01+00:00")

    active = plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=human["proposal_id"],
        field_sources={"direction": "human", "range": "human", "grid": "human", "signal": "human"},
        now="2026-07-05T01:00:02+00:00",
    )

    assert human["schema_version"] == ai["schema_version"] == "strategy-plan-proposal-v1"
    assert active["status"] == "active"
    assert active["version"] == 1
    assert active["field_sources"]["direction"] == "human"
    diff = plane.proposal_diff(cycle_id)
    assert diff["fields"]["direction"]["different"] is True
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]


def test_latest_plan_keeps_completed_cycle_traceability_after_next_cycle_activates(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    first_cycle = "2026-07-05_DAY"
    second_cycle = "2026-07-05_NIGHT"
    first_proposal = plane.upsert_proposal(proposal(first_cycle, "ai"))
    first_plan = plane.lock_production_plan(first_cycle, selected_proposal_id=first_proposal["proposal_id"])
    second_proposal = plane.upsert_proposal(proposal(second_cycle, "ai"))
    plane.lock_production_plan(second_cycle, selected_proposal_id=second_proposal["proposal_id"])

    assert plane.active_plan(first_cycle) is None
    assert plane.latest_plan(first_cycle)["strategy_plan_id"] == first_plan["strategy_plan_id"]


def test_plan_version_advances_from_failed_cycle_history(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    failed = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    failed["status"] = "failed"
    plane._write_plan(failed)

    retried = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    assert retried["version"] == failed["version"] + 1
    assert sorted(row["version"] for row in load_json(plane._plans_path(cycle_id))) == [1, 2]


def test_legacy_plans_are_read_as_compatible_proposals_without_erasing_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    legacy = proposal(cycle_id, "human") | {"author": "human", "locked_at": "2026-07-05T01:00:00+00:00"}
    from services.journal_store import write_json

    write_json(output / "dualtrack" / "plans" / f"{cycle_id}_human.json", [legacy])
    result = plane.read_model(cycle_id, as_of="2026-07-05T01:01:00+00:00")

    assert result["migration"]["legacy_compatible"] is True
    assert result["proposals"][0]["source"] == "human"
    assert result["production_plan"]["field_sources"]["direction"] == "human"
    assert (output / "dualtrack" / "plans" / f"{cycle_id}_human.json").exists()


def test_production_controls_are_durable_and_preserve_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    assert plane.control(
        cycle_id,
        "start",
        {"direction": "long", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )["runtime"]["desired_state"] == "running"
    revised = plane.control(cycle_id, "adjust_plan", {"range": {"low": 98.0, "high": 122.0}})["plan"]
    assert revised["version"] == 3
    assert plane.active_plan(cycle_id)["range"]["low"] == 98.0
    assert plane.control(cycle_id, "reset_statistics")["historical_records_preserved"] is True
    assert plane.control(cycle_id, "stop", now="2026-07-05T01:41:00+00:00")["runtime"]["desired_state"] == "stopped"


def test_control_plane_reads_accepted_orders_from_selected_execution_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SelectedAdapter:
        name = "nautilus_paper"

        def snapshot(self, cycle_id: str, **_kwargs) -> dict:
            return {
                "cycle_id": cycle_id,
                "orders": [
                    {"order_id": "n-accepted", "state": "accepted"},
                    {"order_id": "n-filled", "state": "filled"},
                ],
            }

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: SelectedAdapter(),
    )
    plane = StrategyControlPlane(tmp_path / "outputs")

    assert plane._accepted_orders("2026-07-05_DAY") == [
        {"order_id": "n-accepted", "state": "accepted"},
    ]


def test_preview_direction_and_style_change_grid_geometry_and_order_sides(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    account = {"ending_cash": 10_000.0}

    neutral = plane.preview(cycle_id, {"direction": "neutral", "style": "steady"}, market=market(), account=account)
    long = plane.preview(cycle_id, {"direction": "long", "style": "steady"}, market=market(), account=account)
    short = plane.preview(cycle_id, {"direction": "short", "style": "steady"}, market=market(), account=account)
    aggressive = plane.preview(cycle_id, {"direction": "neutral", "style": "aggressive"}, market=market(), account=account)

    assert neutral["grid"]["count"] == long["grid"]["count"] == short["grid"]["count"] >= 24
    assert aggressive["grid"]["count"] >= 24
    assert neutral["range"]["high"] - neutral["range"]["low"] > aggressive["range"]["high"] - aggressive["range"]["low"]
    assert neutral["range"] == long["range"] == short["range"]
    assert neutral["range"]["source_timeframe"] == "1d"
    assert neutral["grid"]["spacing_source_timeframe"] == "4h"
    assert {order["side"] for order in neutral["orders"]} == {"buy", "sell"}
    assert {order["side"] for order in long["orders"]} == {"buy"}
    assert {order["side"] for order in short["orders"]} == {"sell"}
    assert all(order["price"] < market()["latest_close"] for order in long["orders"])
    assert all(order["price"] > market()["latest_close"] for order in short["orders"])
    assert neutral["risk"]["estimated_margin"] > 0
    assert neutral["risk"]["max_loss"] > 0
    assert neutral["grid"]["notional_per_grid"] > 100
    assert neutral["risk"]["absolute_notional_ceiling"] == 100_000
    assert neutral["risk"]["capital_budget"] == 100_000
    assert neutral["grid"]["margin_utilization_cap"] == 1.0
    assert neutral["risk"]["calibration_status"] == "shadow_candidate"


def test_preview_strategy_geometry_is_independent_of_chart_timeframe(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    one_minute = market()
    thirty_minute = market()
    thirty_minute["timeframe"] = "30m"
    request = {"direction": "neutral", "style": "steady"}

    left = plane.preview("2026-07-05_DAY", request, market=one_minute, account={"ending_cash": 10_000.0})
    right = plane.preview("2026-07-05_DAY", request, market=thirty_minute, account={"ending_cash": 10_000.0})

    assert left["range"] == right["range"]
    assert left["grid"] == right["grid"]
    assert left["orders"] == right["orders"]
    assert left["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}


def test_auto_notional_revalidates_against_start_market_while_manual_notional_remains_strict(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    account = {"ending_cash": 10_000.0}
    geometry = {"low": 90.0, "high": 130.0}

    earlier = plane.preview(
        cycle_id,
        {"direction": "neutral", "style": "steady", "range": geometry, "grid": {"count": 24}},
        market=market(close=110.0),
        account=account,
    )
    stale_notional = earlier["grid"]["notional_per_grid"]

    stale_manual = {
        "direction": "neutral",
        "style": "steady",
        "range": geometry,
        "grid": {"count": 24, "notional_per_grid": stale_notional, "notional_mode": "manual"},
    }
    blocked_preview = plane.preview(
        cycle_id,
        stale_manual,
        market=market(close=120.0),
        account=account,
    )
    assert blocked_preview["orders"]
    assert blocked_preview["risk"]["risk_budget_exceeded"] is True
    assert blocked_preview["risk"]["safe_notional_cap_per_grid"] < stale_notional

    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            cycle_id,
            "start",
            stale_manual,
            market=market(close=120.0),
            account=account,
            now="2026-07-05T01:39:00+00:00",
        )

    started = plane.control(
        cycle_id,
        "start",
        {
            "direction": "neutral",
            "style": "steady",
            "range": geometry,
            "grid": {"count": 24, "notional_per_grid": stale_notional, "notional_mode": "auto"},
            "risk_budget": {"leverage": 10.0},
        },
        market=market(close=120.0),
        account=account,
        now="2026-07-05T01:40:00+00:00",
    )

    assert started["preview"]["grid"]["notional_mode"] == "auto"
    assert started["preview"]["grid"]["notional_per_grid"] < stale_notional
    assert started["preview"]["grid"]["notional_per_grid"] == started["preview"]["risk"]["safe_notional_cap_per_grid"]
    assert started["plan"]["grid"]["notional_per_grid"] == started["preview"]["grid"]["notional_per_grid"]
    assert started["accepted_orders"] > 0


def test_start_and_running_regrid_reject_nonpositive_authoritative_equity(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    locked = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    untrusted = {"equity": 0.0, "starting_cash": 1_000_000.0}

    with pytest.raises(ValueError, match="risk_evidence_missing"):
        plane.control(
            cycle_id,
            "start",
            {"direction": "neutral", "style": "steady"},
            market=market(),
            account=untrusted,
        )
    assert plane.runtime_state(cycle_id)["actual_state"] == "stopped"
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == locked["strategy_plan_id"]

    started = plane.control(
        cycle_id,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
    )
    with pytest.raises(ValueError, match="risk_evidence_missing"):
        plane.control(
            cycle_id,
            "adjust_plan",
            {"direction": "neutral", "style": "steady"},
            market=market(),
            account=untrusted,
        )
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == started["plan"]["strategy_plan_id"]


def test_preview_fails_closed_without_fixed_strategy_timeframes(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    invalid = market()
    invalid.pop("strategy_timeframes")

    with pytest.raises(ValueError, match="strategy timeframe 1d is unavailable"):
        plane.preview("2026-07-05_DAY", {"direction": "neutral", "style": "steady"}, market=invalid, account={"ending_cash": 10_000.0})


def test_start_commits_plan_and_real_versioned_paper_orders_idempotently(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = {"direction": "short", "style": "aggressive"}

    started = plane.control(cycle_id, "start", payload, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]

    assert started["runtime"]["desired_state"] == "running"
    assert started["runtime"]["actual_state"] == "running"
    assert started["created_orders"] == len(orders) > 0
    assert started["plan"]["direction"] == "short"
    assert started["plan"]["version"] == 2
    assert all(order["state"] == "accepted" for order in orders)
    assert all(order["side"] == "sell" for order in orders)
    assert all(order["strategy_plan_id"] == started["plan"]["strategy_plan_id"] for order in orders)
    assert all(order["strategy_plan_version"] == started["plan"]["version"] for order in orders)

    repeated = plane.control(cycle_id, "start", payload, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:41:00+00:00")
    repeated_orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
    assert repeated["created_orders"] == 0
    assert repeated["idempotent"] is True
    assert repeated["plan"]["strategy_plan_id"] == started["plan"]["strategy_plan_id"]
    assert [order["order_id"] for order in repeated_orders] == [order["order_id"] for order in orders]


def test_start_is_fail_closed_for_stale_market(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    with pytest.raises(ValueError, match="market data is stale"):
        plane.control(cycle_id, "start", {"direction": "neutral", "style": "steady"}, market=market(fresh=False), account={"ending_cash": 10_000.0})

    assert plane.runtime_state(cycle_id)["desired_state"] == "stopped"
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


def test_failed_start_versions_keep_advancing_and_cleanup_only_its_positions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class FailingStartAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.entry_submissions = 0
            self.orders: list[dict] = []
            self.positions = [{
                "trade_id": "existing-trade",
                "position_id": "existing-position",
                "status": "open",
                "side": "long",
                "remaining_units": 1.0,
                "entry_price": 100.0,
                "strategy_plan_id": "unrelated-plan",
                "strategy_plan_version": 9,
            }]
            self.flattened_plan_ids: list[str] = []

        def submit_order(self, command: dict) -> dict:
            if command["event"] == "flatten":
                self.flattened_plan_ids.append(command["strategy_plan_id"])
                for position in self.positions:
                    if position["position_id"] == command["position_id"]:
                        position["status"] = "closed"
                return {"order_id": command["source_fill_id"], "state": "filled"}
            self.entry_submissions += 1
            if self.entry_submissions == 2:
                raise RuntimeError("injected start failure")
            receipt = {
                "order_id": command["source_fill_id"],
                "state": "accepted",
                "strategy_plan_id": command["strategy_plan_id"],
                "strategy_plan_version": command["strategy_plan_version"],
            }
            self.orders.append(receipt)
            self.positions.append({
                "trade_id": f"trade-{command['strategy_plan_id']}",
                "position_id": f"position-{command['strategy_plan_id']}",
                "status": "open",
                "side": "long" if command["side"] == "buy" else "short",
                "remaining_units": command["quantity"],
                "entry_price": command["price"],
                "strategy_plan_id": command["strategy_plan_id"],
                "strategy_plan_version": command["strategy_plan_version"],
            })
            return dict(receipt)

        def cancel_orders(self, _cycle_id: str, *, strategy_plan_id: str | None = None, **_kwargs) -> dict:
            matching = [row for row in self.orders if row.get("strategy_plan_id") == strategy_plan_id]
            self.orders = [row for row in self.orders if row.get("strategy_plan_id") != strategy_plan_id]
            self.entry_submissions = 0
            return {
                "cancelled_order_count": len(matching),
                "cancelled_order_ids": [row["order_id"] for row in matching],
            }

        def snapshot(self, _cycle_id: str, **_kwargs) -> dict:
            return {
                "orders": [dict(row) for row in self.orders],
                "fills": [],
                "positions": [dict(row) for row in self.positions],
            }

    adapter = FailingStartAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    for timestamp in ("2026-07-05T01:40:00+00:00", "2026-07-05T01:41:00+00:00"):
        with pytest.raises(RuntimeError, match="injected start failure"):
            plane.control(
                cycle_id,
                "start",
                {"direction": "long", "style": "steady"},
                market=market(),
                account={"ending_cash": 10_000.0},
                now=timestamp,
            )

    plans = load_json(plane._plans_path(cycle_id))
    failed = sorted((row for row in plans if row["status"] == "failed"), key=lambda row: row["version"])
    unrelated = next(row for row in adapter.positions if row["position_id"] == "existing-position")
    assert [row["version"] for row in failed] == [2, 3]
    assert adapter.flattened_plan_ids == [row["strategy_plan_id"] for row in failed]
    assert unrelated["status"] == "open"
    assert plane.runtime_state(cycle_id)["last_error"] == "injected start failure"


def test_start_accepts_complete_grid_before_processing_a_legitimate_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class FillingAdapter:
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.orders: list[dict] = []
            self.filled = False

        def submit_order(self, command: dict) -> dict:
            row = {
                "order_id": command["source_fill_id"],
                "state": "accepted",
                "side": command["side"],
                "price": command["price"],
                "quantity": command["quantity"],
                "strategy_plan_id": command["strategy_plan_id"],
                "strategy_plan_version": command["strategy_plan_version"],
            }
            self.orders.append(row)
            return dict(row)

        def snapshot(self, _cycle_id: str, **_kwargs) -> dict:
            return {
                "orders": [dict(row) for row in self.orders],
                "fills": ([{"fill_id": "fill-1"}] if self.filled else []),
                "positions": [],
            }

        def process_market_event(self, _event: dict) -> dict:
            self.filled = True
            self.orders[0]["state"] = "filled"
            return {"status": "replayed"}

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            return {"cancelled_order_count": 0, "cancelled_order_ids": []}

    adapter = FillingAdapter()
    monkeypatch.setattr(
        "services.strategy_control_plane.build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    started = plane.control(
        cycle_id,
        "start",
        {"direction": "long", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )

    assert started["created_orders"] > 1
    assert started["filled_orders"] == 1
    assert started["accepted_orders"] == started["created_orders"] - 1
    assert started["runtime"]["actual_state"] == "running"


def test_runtime_state_is_scoped_to_the_requested_cycle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    from services.journal_store import write_json

    write_json(plane.root / "runtime.json", [{
        "cycle_id": "2026-07-04_NIGHT",
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": 8,
        "strategy_plan_id": "strategy-plan-old",
        "strategy_plan_version": 7,
    }])

    current = plane.runtime_state("2026-07-05_DAY")

    assert current["cycle_id"] == "2026-07-05_DAY"
    assert current["desired_state"] == "stopped"
    assert current["actual_state"] == "stopped"
    assert current["accepted_order_count"] == 0
    assert current["stale_cycle"] is True
    assert current["previous_cycle_id"] == "2026-07-04_NIGHT"
    assert current["strategy_plan_id"] is None


def test_stop_cancels_pending_orders_flattens_position_and_reconciles(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(cycle_id, "start", {"direction": "long", "style": "steady"}, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    adapter = build_execution_engine_adapter(output)
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:45:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 110.0,
        "market_price": 110.0,
        "notional": 100.0,
        "sl": 105.0,
        "tp": 115.0,
        "strategy_plan_id": started["plan"]["strategy_plan_id"],
        "strategy_plan_version": started["plan"]["version"],
        "source": "strategy_production_console",
    })

    stopped = plane.control(cycle_id, "stop", {}, market=market(close=111.0), now="2026-07-05T01:46:00+00:00")
    snapshot = adapter.snapshot(cycle_id)

    assert stopped["runtime"]["desired_state"] == "stopped"
    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["cancelled_orders"] > 0
    assert stopped["flattened_positions"] == 1
    assert stopped["reconciliation"]["status"] == "ok"
    assert not [order for order in snapshot["orders"] if order["state"] == "accepted"]
    assert not [position for position in snapshot["positions"] if position["status"] == "open"]


def test_running_adjustment_replaces_pending_grid_without_stopping_runtime(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(cycle_id, "start", {"direction": "neutral", "style": "steady"}, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    original_orders = [order for order in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] if order["state"] == "accepted"]

    adjusted = plane.control(
        cycle_id,
        "adjust_plan",
        {
            "direction": "short",
            "style": "aggressive",
            "range": {"low": 108.0, "high": 116.0},
            "grid": {"count": 12, "notional_per_grid": 50.0},
            "risk_budget": {"leverage": 2.0},
        },
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:42:00+00:00",
    )
    snapshot = build_execution_engine_adapter(output).snapshot(cycle_id)
    current_orders = [order for order in snapshot["orders"] if order["state"] == "accepted"]

    assert adjusted["runtime"]["desired_state"] == "running"
    assert adjusted["runtime"]["actual_state"] == "running"
    assert adjusted["plan"]["version"] == started["plan"]["version"] + 1
    assert adjusted["cancelled_orders"] == len(original_orders)
    assert adjusted["created_orders"] == len(current_orders) > 0
    assert all(order["side"] == "sell" for order in current_orders)
    assert all(order["strategy_plan_id"] == adjusted["plan"]["strategy_plan_id"] for order in current_orders)


def test_failed_running_adjustment_keeps_previous_grid_and_removes_staged_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )
    old_plan_id = started["plan"]["strategy_plan_id"]
    old_orders = [
        row for row in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
        if row["state"] == "accepted"
    ]
    real = build_execution_engine_adapter(output)

    class FailingStageAdapter:
        name = real.name

        def __init__(self) -> None:
            self.submissions = 0

        def submit_order(self, command: dict) -> dict:
            self.submissions += 1
            if self.submissions == 3:
                raise RuntimeError("injected staged order failure")
            return real.submit_order(command)

        def cancel_orders(self, cycle_id: str, **kwargs) -> dict:
            return real.cancel_orders(cycle_id, **kwargs)

        def snapshot(self, cycle_id: str, **kwargs) -> dict:
            return real.snapshot(cycle_id, **kwargs)

        def reconcile(self, cycle_id: str) -> dict:
            return real.reconcile(cycle_id)

    monkeypatch.setattr(
        "services.strategy_control_plane.build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: FailingStageAdapter(),
    )

    with pytest.raises(RuntimeError, match="injected staged order failure"):
        plane.control(
            cycle_id,
            "adjust_plan",
            {"direction": "short", "style": "aggressive"},
            market=market(),
            account={"ending_cash": 10_000.0},
            now="2026-07-05T01:42:00+00:00",
        )

    accepted = [
        row for row in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
        if row["state"] == "accepted"
    ]
    runtime = plane.runtime_state(cycle_id)
    assert len(accepted) == len(old_orders)
    assert all(row["strategy_plan_id"] == old_plan_id for row in accepted)
    assert runtime["actual_state"] == "running"
    assert runtime["strategy_plan_id"] == old_plan_id
    assert "injected staged order failure" in runtime["last_error"]


def test_concurrent_refresh_cannot_override_started_plan_or_create_two_active_plans(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    def work(index: int):
        if index % 2:
            return plane.read_model(cycle_id, as_of="2026-07-05T01:40:00+00:00")["production_plan"]
        return plane.control(
            cycle_id,
            "start",
            {"direction": "short", "style": "aggressive"},
            market=market(),
            account={"ending_cash": 10_000.0},
            now="2026-07-05T01:40:00+00:00",
        )["plan"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(16)))

    active = plane._all_active_plans()
    runtime = plane.runtime_state(cycle_id)
    orders = [order for order in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] if order["state"] == "accepted"]
    assert len(active) == 1
    assert runtime["strategy_plan_id"] == active[0]["strategy_plan_id"]
    assert orders
    assert all(order["strategy_plan_id"] == active[0]["strategy_plan_id"] for order in orders)
    assert all(result.get("strategy_plan_id") for result in results)


def test_extend_range_stages_only_new_edges_and_preserves_pending_protection_and_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    current = started["plan"]
    current["grid"]["notional_per_grid"] = 50.0
    current["grid"]["notional_mode"] = "auto"
    plane._write_plan(current)
    plan_orders = sorted(current["grid"]["orders"], key=lambda row: row["price"])
    retained_plan_order = plan_orders[len(plan_orders) // 2 - 1]
    filled_plan_order = plan_orders[len(plan_orders) // 2 - 2]
    outside_plan_order = plan_orders[-1]
    internal = {
        **retained_plan_order,
        "order_id": "internal-entry",
        "state": "accepted",
        "strategy_plan_id": current["strategy_plan_id"],
        "strategy_plan_version": current["version"],
    }
    outside = {
        **outside_plan_order,
        "order_id": "upper-edge-entry",
        "state": "accepted",
        "strategy_plan_id": current["strategy_plan_id"],
        "strategy_plan_version": current["version"],
    }
    protection = {
        "order_id": "protect-existing-position",
        "state": "accepted",
        "event": "target",
        "order_type": "limit",
        "side": "sell",
        "price": filled_plan_order["tp"],
        "quantity": filled_plan_order["quantity"],
        "strategy_plan_id": current["strategy_plan_id"],
    }
    position = {
        "position_id": "position-filled-edge",
        "trade_id": "trade-filled-edge",
        "status": "open",
        "side": "long" if filled_plan_order["side"] == "buy" else "short",
        "entry_price": filled_plan_order["price"],
        "remaining_units": filled_plan_order["quantity"],
        "sl": filled_plan_order["sl"],
        "tp": filled_plan_order["tp"],
        "strategy_plan_id": current["strategy_plan_id"],
    }
    adapter = RecordingAdapter([internal, outside, protection], [position], vary_transient=True)
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(current["grid"]["spacing"])

    request_body = {
        "expected_strategy_plan_id": current["strategy_plan_id"],
        "range": {
            "low": float(current["range"]["low"]) - spacing * 0.8,
            "high": float(current["range"]["high"]) - spacing * 0.8,
        },
    }
    result = plane.control(
        cycle_id,
        "extend_range",
        request_body,
        market=market(),
        account={"equity": 1_000_000.0},
        now="2026-07-05T01:42:00+00:00",
    )

    snapshot = adapter.snapshot(cycle_id)
    by_id = {row["order_id"]: row for row in snapshot["orders"]}
    staged = [row for row in snapshot["orders"] if row["order_id"].startswith("staged-")]
    assert result["steps"] == {"low": 1, "high": -1}
    assert result["created_orders"] == len(staged) == 1
    assert result["cancelled_orders"] == 1
    assert by_id["internal-entry"]["price"] == internal["price"]
    assert by_id["internal-entry"]["quantity"] == internal["quantity"]
    assert by_id["upper-edge-entry"]["state"] == "cancelled"
    assert by_id["protect-existing-position"]["price"] == protection["price"]
    assert by_id["protect-existing-position"]["quantity"] == protection["quantity"]
    assert snapshot["positions"][0]["position_id"] == position["position_id"]
    assert snapshot["positions"][0]["sl"] == position["sl"]
    assert snapshot["positions"][0]["tp"] == position["tp"]
    assert result["positions_preserved"] is True
    assert result["tp_sl_affected"] is False
    assert result["plan"]["grid"]["notional_per_grid"] == 50.0
    assert result["plan"]["grid"]["notional_mode"] == "manual"
    assert result["plan"]["direction"] == current["direction"]
    assert result["plan"]["style"] == current["style"]
    assert result["plan"]["inherited_plan_ids"] == [current["strategy_plan_id"]]
    assert not [row for row in staged if row["price"] == filled_plan_order["price"]]

    submissions_before_retry = adapter.submission_count
    cancellations_before_retry = len(adapter.cancel_calls)
    retried = plane.control(
        cycle_id,
        "extend_range",
        request_body,
        market=market(),
        account={"equity": 1_000_000.0},
        now="2026-07-05T01:43:00+00:00",
    )
    assert retried["idempotent"] is True
    assert adapter.submission_count == submissions_before_retry
    assert len(adapter.cancel_calls) == cancellations_before_retry


def test_extend_range_half_step_retry_uses_direct_request_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    original = started["plan"]
    adapter = RecordingAdapter([], [])
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(original["grid"]["spacing"])
    request_body = {
        "expected_strategy_plan_id": original["strategy_plan_id"],
        "range": {
            "low": float(original["range"]["low"]) - spacing * 1.5,
            "high": float(original["range"]["high"]),
        },
    }

    first = plane.control(
        cycle_id,
        "extend_range",
        request_body,
        market=market(),
        account={"equity": 1_000_000.0},
        now="2026-07-05T01:42:00+00:00",
    )
    snapshots_before_retry = adapter.snapshot_count
    submissions_before_retry = adapter.submission_count
    retry = plane.control(
        cycle_id,
        "extend_range",
        request_body,
        market=market(close=111.0),
        account={"equity": 1.0},
        now="2026-07-05T01:43:00+00:00",
    )

    assert first["steps"] == {"low": 2, "high": 0}
    assert first["effective_range"]["low"] == pytest.approx(
        float(original["range"]["low"]) - spacing * 2
    )
    assert first["plan"]["range_adjustment"]["requested_range"] == request_body["range"]
    assert first["plan"]["range_adjustment"]["from_plan_id"] == original["strategy_plan_id"]
    assert retry["idempotent"] is True
    assert retry["effective_range"] == first["effective_range"]
    assert retry["steps"] == first["steps"]
    assert adapter.snapshot_count == snapshots_before_retry
    assert adapter.submission_count == submissions_before_retry

    second_body = {
        "expected_strategy_plan_id": first["plan"]["strategy_plan_id"],
        "range": {
            "low": first["effective_range"]["low"],
            "high": float(first["effective_range"]["high"]) + spacing,
        },
    }
    plane.control(
        cycle_id,
        "extend_range",
        second_body,
        market=market(),
        account={"equity": 1_000_000.0},
        now="2026-07-05T01:44:00+00:00",
    )
    with pytest.raises(ValueError, match="strategy_plan_changed"):
        plane.control(
            cycle_id,
            "extend_range",
            request_body,
            market=market(),
            account={"equity": 1_000_000.0},
            now="2026-07-05T01:45:00+00:00",
        )


def test_extend_range_rejects_risk_before_any_execution_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    current["grid"]["notional_per_grid"] = 500.0
    plane._write_plan(current)
    internal = {
        **current["grid"]["orders"][0],
        "order_id": "internal-entry",
        "state": "accepted",
        "strategy_plan_id": current["strategy_plan_id"],
    }
    adapter = RecordingAdapter([internal], [])
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(current["grid"]["spacing"])

    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            "2026-07-05_DAY",
            "extend_range",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "range": {
                    "low": float(current["range"]["low"]) - spacing,
                    "high": float(current["range"]["high"]) + spacing,
                },
            },
            market=market(),
            account={"equity": 10.0},
            now="2026-07-05T01:42:00+00:00",
        )

    assert adapter.submission_count == 0
    assert adapter.cancel_calls == []
    assert plane.active_plan("2026-07-05_DAY")["strategy_plan_id"] == current["strategy_plan_id"]


def test_extend_range_stage_failure_cleans_only_new_plan_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    current["grid"]["notional_per_grid"] = 50.0
    plane._write_plan(current)
    plan_order = current["grid"]["orders"][0]
    internal = {
        **plan_order,
        "order_id": "internal-entry",
        "state": "accepted",
        "strategy_plan_id": current["strategy_plan_id"],
    }
    protection = {
        "order_id": "protection-order",
        "state": "accepted",
        "event": "stop",
        "order_type": "market",
        "side": "sell",
        "price": plan_order["sl"],
        "quantity": plan_order["quantity"],
        "strategy_plan_id": current["strategy_plan_id"],
    }
    position = {
        "position_id": "position-1",
        "status": "open",
        "side": "long",
        "entry_price": plan_order["price"],
        "remaining_units": plan_order["quantity"],
        "sl": plan_order["sl"],
        "tp": plan_order["tp"],
        "strategy_plan_id": current["strategy_plan_id"],
    }
    adapter = RecordingAdapter([internal, protection], [position], fail_on_submission=2)
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(current["grid"]["spacing"])

    with pytest.raises(RuntimeError, match="injected extend stage failure"):
        plane.control(
            "2026-07-05_DAY",
            "extend_range",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "range": {
                    "low": float(current["range"]["low"]) - spacing,
                    "high": float(current["range"]["high"]) + spacing,
                },
            },
            market=market(),
            account={"equity": 1_000_000.0},
            now="2026-07-05T01:42:00+00:00",
        )

    snapshot = adapter.snapshot("2026-07-05_DAY")
    by_id = {row["order_id"]: row for row in snapshot["orders"]}
    assert by_id["internal-entry"] == internal
    assert by_id["protection-order"] == protection
    assert snapshot["positions"] == [position]
    assert not [
        row
        for row in snapshot["orders"]
        if row["order_id"].startswith("staged-") and row["state"] == "accepted"
    ]
    assert plane.active_plan("2026-07-05_DAY")["strategy_plan_id"] == current["strategy_plan_id"]
    assert plane.runtime_state("2026-07-05_DAY")["actual_state"] == "running"
    audit = plane.runtime_state("2026-07-05_DAY")["last_control_event"]
    assert audit["action"] == "extend_range"
    assert audit["result"] == "failed"
    assert "injected extend stage failure" in audit["error"]


def test_extend_range_cleans_a_position_created_by_a_failed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    current["grid"]["notional_per_grid"] = 50.0
    plane._write_plan(current)

    class ImmediateFillAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.orders: list[dict] = []
            self.positions: list[dict] = []
            self.submission_count = 0

        def submit_order(self, command: dict) -> dict:
            self.submission_count += 1
            event = str(command.get("event") or "entry")
            if event == "flatten":
                for position in self.positions:
                    if position["position_id"] == command.get("position_id"):
                        position["status"] = "closed"
                        position["remaining_units"] = 0.0
                row = {
                    "order_id": f"filled-{self.submission_count}",
                    "state": "filled",
                    "event": "flatten",
                    "strategy_plan_id": command["strategy_plan_id"],
                }
                self.orders.append(row)
                return dict(row)
            row = {
                "order_id": f"filled-{self.submission_count}",
                "state": "filled",
                "event": "entry",
                "side": command["side"],
                "price": command["price"],
                "quantity": command["quantity"],
                "notional": command["notional"],
                "sl": command["sl"],
                "tp": command["tp"],
                "strategy_plan_id": command["strategy_plan_id"],
            }
            self.orders.append(row)
            self.positions.append({
                "position_id": "staged-position",
                "trade_id": "staged-trade",
                "status": "open",
                "side": "long" if command["side"] == "buy" else "short",
                "entry_price": command["price"],
                "remaining_units": command["quantity"],
                "sl": command["sl"],
                "tp": command["tp"],
                "strategy_plan_id": command["strategy_plan_id"],
            })
            return dict(row)

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            return {"cancelled_order_count": 0, "cancelled_order_ids": []}

        def snapshot(self, _cycle_id: str, **_kwargs) -> dict:
            return {"orders": deepcopy(self.orders), "positions": deepcopy(self.positions), "fills": []}

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

    adapter = ImmediateFillAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(current["grid"]["spacing"])

    with pytest.raises(ValueError, match="did not accept"):
        plane.control(
            "2026-07-05_DAY",
            "extend_range",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "range": {
                    "low": float(current["range"]["low"]) - spacing,
                    "high": float(current["range"]["high"]),
                },
            },
            market=market(),
            account={"equity": 1_000_000.0},
        )

    assert not [position for position in adapter.positions if position["status"] == "open"]
    assert plane.active_plan("2026-07-05_DAY")["strategy_plan_id"] == current["strategy_plan_id"]
    assert plane.runtime_state("2026-07-05_DAY")["actual_state"] == "running"


def test_extend_range_activation_failure_cleans_staged_orders_and_restores_old_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    current["grid"]["notional_per_grid"] = 50.0
    plane._write_plan(current)
    adapter = RecordingAdapter([], [])
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        plane,
        "_activate_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected activation failure")),
    )
    spacing = float(current["grid"]["spacing"])

    with pytest.raises(OSError, match="injected activation failure"):
        plane.control(
            "2026-07-05_DAY",
            "extend_range",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "range": {
                    "low": float(current["range"]["low"]) - spacing,
                    "high": float(current["range"]["high"]),
                },
            },
            market=market(),
            account={"equity": 1_000_000.0},
        )

    assert not [row for row in adapter.orders if row["state"] == "accepted"]
    assert plane.active_plan("2026-07-05_DAY")["strategy_plan_id"] == current["strategy_plan_id"]
    assert plane.runtime_state("2026-07-05_DAY")["actual_state"] == "running"


def test_extend_range_requires_running_expected_plan_before_building_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("adapter must not be built")),
    )

    with pytest.raises(ValueError, match="strategy_not_running"):
        plane.control(
            "2026-07-05_DAY",
            "extend_range",
            {"expected_strategy_plan_id": "plan", "range": {"low": 100, "high": 120}},
            market=market(),
            account={"equity": 10_000.0},
        )


def test_extend_range_no_change_snap_is_idempotent_without_execution_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    spacing = float(current["grid"]["spacing"])
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no-change must not access execution")),
    )

    result = plane.control(
        "2026-07-05_DAY",
        "extend_range",
        {
            "expected_strategy_plan_id": current["strategy_plan_id"],
            "range": {
                "low": float(current["range"]["low"]) + spacing * 0.49,
                "high": float(current["range"]["high"]) - spacing * 0.49,
            },
        },
        market={},
        account={},
    )

    assert result["idempotent"] is True
    assert result["steps"] == {"low": 0, "high": 0}
    assert result["effective_range"] == {
        "low": float(current["range"]["low"]),
        "high": float(current["range"]["high"]),
    }


def test_replace_grid_stops_then_starts_and_retry_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    old_plan = started["plan"]
    adapter = build_execution_engine_adapter(tmp_path / "outputs")
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:41:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 110.0,
        "market_price": 110.0,
        "notional": 100.0,
        "sl": 105.0,
        "tp": 115.0,
        "strategy_plan_id": old_plan["strategy_plan_id"],
        "strategy_plan_version": old_plan["version"],
        "source": "strategy_production_console",
    })
    payload = {
        "direction": old_plan["direction"],
        "style": old_plan["style"],
        "range": dict(old_plan["range"]),
        "grid": {
            "mode": old_plan["grid"]["mode"],
            "count": old_plan["grid"]["count"],
            "notional_per_grid": old_plan["grid"]["notional_per_grid"],
            "notional_mode": "manual",
        },
        "risk_budget": {"leverage": old_plan["grid"]["leverage"]},
    }
    draft_preview = plane.preview(cycle_id, payload, market=market(), account={"ending_cash": 10_000.0})
    risk_recalculated = bool(draft_preview["risk"]["risk_budget_exceeded"])
    if risk_recalculated:
        payload["grid"]["notional_per_grid"] = draft_preview["risk"]["safe_notional_cap_per_grid"]
    requested_preview = plane.preview(cycle_id, payload, market=market(), account={"ending_cash": 10_000.0})
    body = {
        "expected_strategy_plan_id": old_plan["strategy_plan_id"],
        "expected_preview_id": requested_preview["preview_id"],
        "preview": requested_preview,
        "risk_recalculated": risk_recalculated,
        "expected_execution": {
            "accepted_order_ids": sorted(
                row["order_id"]
                for row in adapter.snapshot(cycle_id)["orders"]
                if row["state"] == "accepted"
            ),
            "open_position_ids": sorted(
                row.get("position_id") or row["trade_id"]
                for row in adapter.snapshot(cycle_id)["positions"]
                if row["status"] == "open"
            ),
        },
    }

    replaced = plane.control(
        cycle_id,
        "replace_grid",
        body,
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:42:00+00:00",
    )
    monkeypatch.setattr(
        plane,
        "_stop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry must not stop again")),
    )
    retried = plane.control(
        cycle_id,
        "replace_grid",
        body,
        market=market(close=111.0),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:43:00+00:00",
    )

    assert replaced["stopped"] is True
    assert replaced["flattened_positions"] == 1
    assert replaced["cancelled_orders"] > 0
    assert replaced["created_orders"] == replaced["accepted_orders"] > 0
    assert replaced["plan"]["preview_id"] == requested_preview["preview_id"]
    assert replaced["plan"]["grid"]["notional_per_grid"] == requested_preview["grid"]["notional_per_grid"]
    assert replaced["plan"]["grid"]["notional_per_grid"] <= old_plan["grid"]["notional_per_grid"]
    assert replaced["plan"]["grid"]["notional_mode"] == "manual"
    assert replaced["runtime"]["last_action"] == "replace_grid"
    assert retried["idempotent"] is True
    assert retried["stopped"] is False
    with pytest.raises(ValueError, match="strategy_plan_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            {**body, "expected_strategy_plan_id": "unrelated-stale-plan"},
            market=market(),
            account={"ending_cash": 10_000.0},
        )
    without_execution = {key: value for key, value in body.items() if key != "expected_execution"}
    with pytest.raises(ValueError, match="execution_state_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            without_execution,
            market=market(),
            account={"ending_cash": 10_000.0},
        )


def test_replace_grid_retry_recovers_when_final_runtime_write_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    current = started["plan"]
    spacing = float(current["grid"]["spacing"])
    payload = {
        "direction": current["direction"],
        "style": current["style"],
        "range": {
            "low": float(current["range"]["low"]) - spacing,
            "high": float(current["range"]["high"]) + spacing,
        },
        "grid": {
            "mode": current["grid"]["mode"],
            "count": current["grid"]["count"],
            "notional_per_grid": current["grid"]["notional_per_grid"],
            "notional_mode": "manual",
        },
        "out_of_range": current["grid"]["out_of_range"],
        "risk_budget": {"leverage": current["grid"]["leverage"]},
    }
    draft = plane.preview(cycle_id, payload, market=market(), account={"ending_cash": 10_000.0})
    risk_recalculated = bool(draft["risk"]["risk_budget_exceeded"])
    if risk_recalculated:
        payload["grid"]["notional_per_grid"] = draft["risk"]["safe_notional_cap_per_grid"]
    requested = plane.preview(cycle_id, payload, market=market(), account={"ending_cash": 10_000.0})
    execution = build_execution_engine_adapter(tmp_path / "outputs").snapshot(cycle_id)
    body = {
        "expected_strategy_plan_id": current["strategy_plan_id"],
        "expected_preview_id": requested["preview_id"],
        "preview": requested,
        "risk_recalculated": risk_recalculated,
        "expected_execution": {
            "accepted_order_ids": sorted(
                row["order_id"] for row in execution["orders"] if row["state"] == "accepted"
            ),
            "open_position_ids": sorted(
                row.get("position_id") or row["trade_id"]
                for row in execution["positions"]
                if row["status"] == "open"
            ),
        },
    }
    real_write_runtime = plane._write_runtime
    failed_once = False

    def fail_final_replace_write(row: dict) -> None:
        nonlocal failed_once
        if row.get("last_action") == "replace_grid" and not failed_once:
            failed_once = True
            raise OSError("injected final runtime write failure")
        real_write_runtime(row)

    monkeypatch.setattr(plane, "_write_runtime", fail_final_replace_write)
    with pytest.raises(OSError, match="injected final runtime write failure"):
        plane.control(
            cycle_id,
            "replace_grid",
            body,
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    running_plan = plane.active_plan(cycle_id)
    assert running_plan["preview_id"] == requested["preview_id"]
    assert running_plan["replacement_request"]["from_plan_id"] == current["strategy_plan_id"]
    monkeypatch.setattr(
        plane,
        "_stop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry must not stop again")),
    )
    retry = plane.control(
        cycle_id,
        "replace_grid",
        body,
        market=market(),
        account={"ending_cash": 10_000.0},
    )

    assert retry["idempotent"] is True
    assert retry["plan"]["strategy_plan_id"] == running_plan["strategy_plan_id"]
    assert retry["runtime"]["last_action"] == "replace_grid"


def test_replace_grid_execution_drift_rejects_before_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    current = started["plan"]
    adapter = build_execution_engine_adapter(tmp_path / "outputs")
    before = adapter.snapshot(cycle_id)
    expected_execution = {
        "accepted_order_ids": sorted(
            row["order_id"] for row in before["orders"] if row["state"] == "accepted"
        ),
        "open_position_ids": sorted(
            row.get("position_id") or row["trade_id"]
            for row in before["positions"]
            if row["status"] == "open"
        ),
    }
    spacing = float(current["grid"]["spacing"])
    payload = {
        "direction": current["direction"],
        "style": current["style"],
        "range": {
            "low": float(current["range"]["low"]) - spacing,
            "high": float(current["range"]["high"]) + spacing,
        },
        "grid": {
            "mode": current["grid"]["mode"],
            "count": current["grid"]["count"],
            "notional_per_grid": current["grid"]["notional_per_grid"],
            "notional_mode": "manual",
        },
        "out_of_range": current["grid"]["out_of_range"],
        "risk_budget": {"leverage": current["grid"]["leverage"]},
    }
    requested = plane.preview(
        cycle_id,
        payload,
        market=market(),
        account={"ending_cash": 1_000_000.0},
    )
    adapter.cancel_orders(cycle_id, order_ids=[expected_execution["accepted_order_ids"][0]])
    monkeypatch.setattr(
        plane,
        "_stop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("drift must reject before stop")),
    )

    with pytest.raises(ValueError, match="execution_state_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": requested["preview_id"],
                "preview": requested,
                "expected_execution": expected_execution,
            },
            market=market(),
            account={"ending_cash": 1_000_000.0},
        )

    with pytest.raises(ValueError, match="execution_state_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": requested["preview_id"],
                "preview": requested,
            },
            market=market(),
            account={"ending_cash": 1_000_000.0},
        )


def test_replace_grid_over_budget_manual_preview_is_visible_but_mutation_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    cycle_id = "2026-07-05_DAY"
    current = started["plan"]
    spacing = float(current["grid"]["spacing"])
    payload = {
        "direction": current["direction"],
        "style": current["style"],
        "range": {
            "low": float(current["range"]["low"]) + spacing,
            "high": float(current["range"]["high"]),
        },
        "grid": {
            "mode": current["grid"]["mode"],
            "count": current["grid"]["count"],
            "notional_per_grid": current["grid"]["notional_per_grid"],
            "notional_mode": "manual",
        },
        "out_of_range": current["grid"]["out_of_range"],
        "risk_budget": {"leverage": current["grid"]["leverage"]},
    }
    requested = plane.preview(
        cycle_id,
        payload,
        market=market(),
        account={"ending_cash": 1_000.0},
    )
    snapshot = build_execution_engine_adapter(tmp_path / "outputs").snapshot(cycle_id)
    expected_execution = {
        "accepted_order_ids": sorted(
            row["order_id"] for row in snapshot["orders"] if row["state"] == "accepted"
        ),
        "open_position_ids": sorted(
            row.get("position_id") or row["trade_id"]
            for row in snapshot["positions"]
            if row["status"] == "open"
        ),
    }
    monkeypatch.setattr(
        plane,
        "_stop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked preview must not stop")),
    )

    assert requested["orders"]
    assert requested["risk"]["risk_budget_exceeded"] is True
    assert requested["risk"]["safe_notional_cap_per_grid"] > 0
    assert requested["risk"]["safe_notional_cap_per_grid"] < current["grid"]["notional_per_grid"]
    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            cycle_id,
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": requested["preview_id"],
                "preview": requested,
                "expected_execution": expected_execution,
            },
            market=market(),
            account={"ending_cash": 1_000.0},
        )


def test_replace_grid_preflight_rejects_before_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane, started = _running_plane(tmp_path)
    current = started["plan"]
    requested = dict(started["preview"])
    requested["preview_id"] = "preview-requested"
    monkeypatch.setattr(
        plane,
        "_stop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preflight rejection must not stop")),
    )

    with pytest.raises(ValueError, match="strategy_plan_changed"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": "stale-plan",
                "expected_preview_id": requested["preview_id"],
                "preview": requested,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    changed_direction = deepcopy(requested)
    changed_direction["direction"] = "short" if current["direction"] != "short" else "long"
    with pytest.raises(ValueError, match="strategy_preview_changed"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": changed_direction["preview_id"],
                "preview": changed_direction,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    changed_policy = deepcopy(requested)
    changed_policy["grid"]["out_of_range"] = "pause" if current["grid"]["out_of_range"] != "pause" else "exit_only"
    with pytest.raises(ValueError, match="strategy_preview_changed"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": changed_policy["preview_id"],
                "preview": changed_policy,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    silently_lowered = deepcopy(requested)
    silently_lowered["grid"]["notional_per_grid"] = min(
        float(current["grid"]["notional_per_grid"]) / 2,
        float(silently_lowered["risk"]["risk_notional_cap_per_grid"]) / 2,
    )
    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": silently_lowered["preview_id"],
                "preview": silently_lowered,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    rounded_boundary = deepcopy(requested)
    rounded_boundary["grid"]["notional_per_grid"] = float(current["grid"]["notional_per_grid"]) / 2
    rounded_boundary["risk"] = {
        "risk_budget_exceeded": True,
        "safe_notional_cap_per_grid": rounded_boundary["grid"]["notional_per_grid"],
        "max_loss": 100.0,
        "max_loss_budget": 100.0,
    }
    monkeypatch.setattr(plane, "preview", lambda *_args, **_kwargs: rounded_boundary)
    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": rounded_boundary["preview_id"],
                "preview": rounded_boundary,
                "risk_recalculated": True,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    risky = deepcopy(requested)
    risky["risk"] = {"risk_budget_exceeded": True}
    monkeypatch.setattr(plane, "preview", lambda *_args, **_kwargs: risky)
    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": risky["preview_id"],
                "preview": risky,
            },
            market=market(),
            account={"ending_cash": 10_000.0},
        )

    outside = deepcopy(requested)
    outside["risk"] = {"risk_budget_exceeded": False}
    outside["range"] = {"low": 90.0, "high": 100.0}
    monkeypatch.setattr(plane, "preview", lambda *_args, **_kwargs: outside)
    with pytest.raises(ValueError, match="market_outside_requested_range"):
        plane.control(
            "2026-07-05_DAY",
            "replace_grid",
            {
                "expected_strategy_plan_id": current["strategy_plan_id"],
                "expected_preview_id": outside["preview_id"],
                "preview": outside,
            },
            market=market(close=110.0),
            account={"ending_cash": 10_000.0},
        )
