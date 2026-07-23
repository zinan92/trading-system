import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

import pipelines.dashboard_server as dashboard_server
from schemas.accounting import build_accounting_snapshot
from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_config import dualtrack_config as load_test_config
from services.journal_store import load_json, write_json
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
    def factory(*_args, **_kwargs):
        return deepcopy(config)

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
        "provider": "binance_usdm_futures",
        "source_mode": "binance_usdm_futures",
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


def account_context(equity: float = 10_000.0) -> dict:
    snapshot = build_accounting_snapshot(
        source_type="production_history",
        source_name="production_history",
        source_schema_version="dualtrack-execution-v1",
        scope={"strategy_plan_scope": "test"},
        currency="USDT",
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts={},
        pnl={"net_realized_pnl": 0.0, "unrealized_pnl": 0.0},
        account={"starting_balance": equity, "ending_cash": equity, "equity": equity},
        completeness={"status": "complete", "limitations": []},
        reconciliation={"status": "pass", "issues": []},
    ).to_dict()
    return {"equity": equity, "ending_cash": equity, "accounting_snapshot": snapshot}


def safe_grid(direction: str = "neutral", style: str = "steady") -> dict:
    return {
        "direction": direction,
        "style": style,
    }


def operating_grid(direction: str = "long", style: str = "steady") -> dict:
    return {**safe_grid(direction, style), "grid": {"count": 60}}


def adaptive_grid_payload() -> dict:
    return {
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4_040.0, "high": 4_200.0},
        "grid": {
            "mode": "arithmetic",
            "target_net_profit_per_grid_usd": 10.0,
        },
        "solver": {"mode": "manual_adaptive", "locked": ["range"]},
    }


@pytest.mark.parametrize(
    (
        "direction",
        "mode",
        "low",
        "high",
        "grid_count",
        "profit_target",
        "notional_per_grid",
        "leverage",
        "expected_count",
        "expects_profit_warning",
    ),
    [
        ("neutral", "arithmetic", 4_000.0, 4_200.0, None, 10.0, None, None, 25, False),
        ("neutral", "arithmetic", 4_000.0, 4_200.0, 30, 10.0, None, None, 30, False),
        ("neutral", "arithmetic", 3_900.0, 4_300.0, 70, 10.0, None, None, 70, True),
        ("neutral", "geometric", 3_900.0, 4_300.0, 40, 10.0, None, None, 40, False),
        ("long", "arithmetic", 4_040.0, 4_200.0, 30, 10.0, None, None, 30, True),
        ("short", "geometric", 4_000.0, 4_145.0, 30, 10.0, None, None, 30, True),
        ("neutral", "arithmetic", 4_000.0, 4_200.0, 40, 5.0, None, None, 40, False),
        ("neutral", "arithmetic", 3_900.0, 4_300.0, 40, 15.0, None, None, 40, False),
        ("neutral", "arithmetic", 4_000.0, 4_200.0, 30, 10.0, 4_000.0, None, 30, True),
        ("neutral", "arithmetic", 4_000.0, 4_200.0, 30, 10.0, None, 8.0, 30, True),
    ],
)
def test_adaptive_preview_handles_user_parameter_combinations_without_starting(
    tmp_path: Path,
    direction: str,
    mode: str,
    low: float,
    high: float,
    grid_count: int | None,
    profit_target: float,
    notional_per_grid: float | None,
    leverage: float | None,
    expected_count: int,
    expects_profit_warning: bool,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    locked = ["range", "profit_target"]
    payload = {
        "direction": direction,
        "style": "steady",
        "range": {"low": low, "high": high},
        "grid": {
            "mode": mode,
            "target_net_profit_per_grid_usd": profit_target,
        },
        "solver": {"mode": "manual_adaptive", "locked": locked},
    }
    if grid_count is not None:
        payload["grid"]["count"] = grid_count
        locked.append("grid_count")
    if notional_per_grid is not None:
        payload["grid"].update({
            "notional_per_grid": notional_per_grid,
            "notional_mode": "manual",
        })
        locked.append("notional_per_grid")
    if leverage is not None:
        payload["risk_budget"] = {"leverage": leverage}
        locked.append("leverage")

    preview = plane.control(
        cycle_id,
        "preview",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
    )["preview"]

    assert preview["direction"] == direction
    assert preview["grid"]["mode"] == mode
    assert preview["grid"]["count"] == expected_count
    assert len(preview["orders"]) == expected_count
    assert preview["grid"]["target_net_profit_per_grid_usd"] == profit_target
    warning_codes = {row["code"] for row in preview["solver"]["risk_flags"]}
    assert ("grid_profit_target_not_met" in warning_codes) is expects_profit_warning
    assert preview["manual_confirmation"]["available"] is True
    if notional_per_grid is not None:
        assert preview["grid"]["notional_per_grid"] == notional_per_grid
    if leverage is not None:
        assert preview["risk"]["actual_leverage"] <= leverage + 0.01
    assert build_execution_engine_adapter(tmp_path / "outputs").snapshot(cycle_id)[
        "orders"
    ] == []
    if direction == "long":
        assert preview["range"]["low"] == low
        assert preview["range"]["high"] == 4_137.44
        assert all(order["side"] == "buy" for order in preview["orders"])
        assert all(order["price"] < 4_137.44 for order in preview["orders"])
    elif direction == "short":
        assert preview["range"]["low"] == 4_137.44
        assert preview["range"]["high"] == high
        assert all(order["side"] == "sell" for order in preview["orders"])
        assert all(order["price"] > 4_137.44 for order in preview["orders"])


def test_adaptive_preview_smart_fills_a_new_cycle_without_writing_a_plan(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    plane.upsert_proposal(proposal(cycle_id, "ai"))

    preview = plane.control(
        cycle_id,
        "preview",
        adaptive_grid_payload(),
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )["preview"]

    assert preview["schema_version"] == "strategy-grid-preview-v1"
    assert preview["range"]["low"] < 4_137.44 < preview["range"]["high"]
    assert preview["grid"]["count"] > 0
    assert preview["grid"]["notional_per_grid"] > 0
    assert plane.active_plan(cycle_id) is None
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


@pytest.mark.parametrize(
    ("payload_patch", "expected_code", "expected_evidence"),
    [
        (
            {"grid": {"count": 201}, "solver": {"mode": "manual_adaptive", "locked": ["grid_count"]}},
            "adaptive_grid_count_out_of_bounds",
            {"requested": 201, "minimum": 2, "maximum": 200},
        ),
        (
            {"grid": {"count": 2.5}, "solver": {"mode": "manual_adaptive", "locked": ["grid_count"]}},
            "adaptive_grid_count_invalid",
            {"requested": 2.5, "minimum": 2, "maximum": 200},
        ),
        (
            {"risk_budget": {"leverage": 21}, "solver": {"mode": "manual_adaptive", "locked": ["leverage"]}},
            "adaptive_manual_leverage_out_of_bounds",
            {"requested": 21, "minimum": 1.0, "maximum": 20.0},
        ),
    ],
)
def test_adaptive_hard_input_errors_return_a_non_overridable_preview_card(
    tmp_path: Path,
    payload_patch: dict,
    expected_code: str,
    expected_evidence: dict,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    preview = plane.control(
        cycle_id,
        "prepare_start",
        {"direction": "neutral", "style": "steady", **payload_patch},
        market=market(close=4_137.44),
        account=account_context(),
    )["preview"]

    manual = preview["manual_confirmation"]
    assert manual["available"] is False
    assert manual["non_overridable_blocker_codes"] == [expected_code]
    blocker = manual["non_overridable_blockers"]
    assert len(blocker) == 1
    assert blocker[0]["code"] == expected_code
    assert blocker[0]["source"] == "adaptive_grid_solver"
    assert blocker[0]["message"]
    assert blocker[0]["evidence"] == expected_evidence
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


def test_adaptive_start_requires_exact_risk_consent_and_then_starts_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def submit_order(self, command: dict) -> dict:
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

        def process_market_event(self, event: dict) -> dict:
            return real.process_market_event(event)

        def flush(self, requested_cycle: str) -> dict:
            return {"cycle_id": requested_cycle, "status": "flushed"}

        def flush_commands(self, requested_cycle: str) -> dict:
            return {"cycle_id": requested_cycle, "status": "flushed"}

    adapter = NautilusPaperFacade()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    payload = adaptive_grid_payload()
    preview = plane.control(
        cycle_id,
        "preview",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )["preview"]

    assert preview["grid"]["count"] < 30
    assert preview["manual_confirmation"]["required"] is True
    assert preview["manual_confirmation"]["available"] is True
    assert "candidate_grid_count_out_of_bounds" in preview[
        "manual_confirmation"
    ]["overridable_blocker_codes"]
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert adapter.snapshot(cycle_id)["orders"] == []

    with pytest.raises(ValueError, match="strategy_preview_changed"):
        plane.control(
            cycle_id,
            "start",
            payload,
            market=market(close=4_137.44),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    with pytest.raises(ValueError, match="range_risk_acknowledgements_incomplete"):
        plane.control(
            cycle_id,
            "start",
            {**payload, "expected_preview_id": preview["preview_id"]},
            market=market(close=4_137.44),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    manual = preview["manual_confirmation"]
    result = plane.control(
        cycle_id,
        "start",
        {
            **payload,
            "expected_preview_id": preview["preview_id"],
            "risk_acknowledgements": {
                "schema_version": "grid-range-risk-ack-v1",
                "preview_id": preview["preview_id"],
                "facts_digest": manual["facts_digest"],
                "risk_snapshot_digest": manual["risk_snapshot_digest"],
                "codes": sorted(
                    row["code"] for row in manual["required_acknowledgements"]
                ),
            },
        },
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )

    assert result["runtime"]["actual_state"] == "running"
    assert result["accepted_orders"] > 0
    assert result["risk_decision"]["operator_override"][
        "overridden_blocker_codes"
    ] == ["candidate_grid_count_out_of_bounds"]


def test_prepared_start_survives_tick_drift_without_weakening_market_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: NautilusPaperFacade(),
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    payload = {
        "direction": "long",
        "style": "steady",
        "grid": {"mode": "arithmetic", "notional_mode": "auto"},
        "solver": {
            "mode": "manual_adaptive",
            "locked": [],
            "current_grid_count": 40,
            "current_direction": "neutral",
        },
    }
    prepared = plane.control(
        cycle_id,
        "prepare_start",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    preview = prepared["preview"]

    assert prepared["side_effects"] == {
        "strategy_plan_written": False,
        "orders_created": 0,
        "positions_changed": 0,
        "risk_decision_persisted": False,
    }
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active[
        "strategy_plan_id"
    ]
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []
    receipts = load_json(plane._prepared_starts_path(cycle_id))
    assert receipts[-1]["prepared_start_id"] == prepared["prepared_start_id"]

    # An auto-range preview rebuilt from the next tick has a different ID, but
    # the server-prepared candidate remains valid while price stays in the same
    # grid cell and every entry remains non-marketable.
    rebuilt = plane.control(
        cycle_id,
        "preview",
        payload,
        market=market(close=4_137.45),
        account=account_context(),
        now="2026-07-05T01:40:01+00:00",
    )["preview"]
    assert rebuilt["preview_id"] != preview["preview_id"]
    manual = preview["manual_confirmation"]
    assert manual["required"] is True

    started = plane.control(
        cycle_id,
        "start",
        {
            **payload,
            "expected_preview_id": preview["preview_id"],
            "prepared_start_id": prepared["prepared_start_id"],
            "risk_acknowledgements": {
                "schema_version": "grid-range-risk-ack-v1",
                "preview_id": preview["preview_id"],
                "facts_digest": manual["facts_digest"],
                "risk_snapshot_digest": manual["risk_snapshot_digest"],
                "codes": sorted(
                    row["code"]
                    for row in manual["required_acknowledgements"]
                ),
            },
        },
        market=market(close=4_137.45),
        account=account_context(),
        now="2026-07-05T01:40:01+00:00",
    )

    assert started["runtime"]["actual_state"] == "running"
    assert started["runtime"]["prepared_start_id"] == prepared[
        "prepared_start_id"
    ]
    assert started["accepted_orders"] > 0


def test_prepared_start_rejects_price_that_crossed_a_grid_line(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    payload = adaptive_grid_payload()
    prepared = plane.control(
        cycle_id,
        "prepare_start",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    preview = prepared["preview"]
    sell_prices = [
        float(row["price"])
        for row in preview["orders"]
        if row["side"] == "sell"
    ]
    assert sell_prices
    manual = preview["manual_confirmation"]

    with pytest.raises(ValueError, match="prepared_start_market_moved"):
        plane.control(
            cycle_id,
            "start",
            {
                **payload,
                "expected_preview_id": preview["preview_id"],
                "prepared_start_id": prepared["prepared_start_id"],
                "risk_acknowledgements": {
                    "schema_version": "grid-range-risk-ack-v1",
                    "preview_id": preview["preview_id"],
                    "facts_digest": manual["facts_digest"],
                    "risk_snapshot_digest": manual["risk_snapshot_digest"],
                    "codes": sorted(
                        row["code"]
                        for row in manual["required_acknowledgements"]
                    ),
                },
            },
            market=market(close=min(sell_prices) + 0.01),
            account=account_context(),
            now="2026-07-05T01:40:01+00:00",
        )

    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []
    assert plane.runtime_state(cycle_id)["actual_state"] == "stopped"


def test_nautilus_paper_start_requires_a_fresh_execution_tick_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: NautilusPaperFacade(),
    )
    plane = StrategyControlPlane(output)
    plane.config["execution_engine"] = {
        "authoritative": "nautilus_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    with pytest.raises(ValueError, match="paper_execution_tick_unavailable:heartbeat_missing"):
        plane.control(
            cycle_id,
            "prepare_start",
            safe_grid(),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    write_json(
        output / "dualtrack" / "runner" / f"{cycle_id}.json",
        [{
            "ts": "2026-07-05T01:39:00+00:00",
            "cycle_id": cycle_id,
            "event": "live_tick_heartbeat",
            "detail": {"runner": "dualtrack-live-tick"},
        }],
    )
    prepared = plane.control(
        cycle_id,
        "prepare_start",
        safe_grid(),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )

    assert prepared["action"] == "prepare_start"
    assert plane.paper_execution_tick_health(
        cycle_id,
        now="2026-07-05T01:43:01+00:00",
    )["status"] == "blocked"


def test_running_nautilus_runtime_exposes_stale_execution_tick_health(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    plane.config["execution_engine"] = {
        "authoritative": "nautilus_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    plane._write_runtime({
        "cycle_id": cycle_id,
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": 1,
    })

    stale = plane.runtime_state(cycle_id, now="2026-07-05T01:40:00+00:00")

    assert stale["execution_tick_health"]["status"] == "blocked"
    assert stale["execution_tick_health"]["reason"] == "heartbeat_missing"

    write_json(
        output / "dualtrack" / "runner" / f"{cycle_id}.json",
        [{
            "ts": "2026-07-05T01:39:00+00:00",
            "cycle_id": cycle_id,
            "event": "live_tick_heartbeat",
            "detail": {"runner": "dualtrack-live-tick", "ledger_refreshed": True},
        }],
    )

    fresh = plane.runtime_state(cycle_id, now="2026-07-05T01:40:00+00:00")

    assert fresh["execution_tick_health"]["status"] == "ready"


@pytest.mark.parametrize(
    ("direction", "moved_close", "inside_source_envelope"),
    [
        ("long", 4_150.0, True),
        ("short", 4_125.0, True),
        ("long", 4_500.0, False),
        ("short", 3_800.0, False),
    ],
)
def test_prepared_start_allows_non_entry_side_drift_even_beyond_source_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    moved_close: float,
    inside_source_envelope: bool,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: NautilusPaperFacade(),
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    payload = {
        "direction": direction,
        "style": "steady",
        "grid": {"mode": "arithmetic", "notional_mode": "auto"},
        "solver": {
            "mode": "manual_adaptive",
            "locked": [],
            "current_grid_count": 40,
            "current_direction": "neutral",
        },
    }
    prepared = plane.control(
        cycle_id,
        "prepare_start",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    preview = prepared["preview"]
    source_envelope = preview["range"]["source_envelope"]
    assert (
        source_envelope["low"] < moved_close < source_envelope["high"]
    ) is inside_source_envelope
    assert not preview["range"]["low"] < moved_close < preview["range"]["high"]
    manual = preview["manual_confirmation"]

    started = plane.control(
        cycle_id,
        "start",
        {
            **payload,
            "expected_preview_id": preview["preview_id"],
            "prepared_start_id": prepared["prepared_start_id"],
            "risk_acknowledgements": {
                "schema_version": "grid-range-risk-ack-v1",
                "preview_id": preview["preview_id"],
                "facts_digest": manual["facts_digest"],
                "risk_snapshot_digest": manual["risk_snapshot_digest"],
                "codes": sorted(
                    row["code"]
                    for row in manual["required_acknowledgements"]
                ),
            },
        },
        market=market(close=moved_close),
        account=account_context(),
        now="2026-07-05T01:40:01+00:00",
    )

    assert started["runtime"]["actual_state"] == "running"
    assert started["accepted_orders"] == preview["grid"]["count"]


def test_prepared_start_rejects_a_tampered_candidate_receipt(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    payload = adaptive_grid_payload()
    prepared = plane.control(
        cycle_id,
        "prepare_start",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    path = plane._prepared_starts_path(cycle_id)
    rows = load_json(path)
    rows[-1]["preview"]["orders"][0]["price"] += 1.0
    write_json(path, rows)

    with pytest.raises(ValueError, match="prepared_start_changed"):
        plane.control(
            cycle_id,
            "start",
            {
                **payload,
                "expected_preview_id": prepared["preview"]["preview_id"],
                "prepared_start_id": prepared["prepared_start_id"],
            },
            market=market(close=4_137.44),
            account=account_context(),
            now="2026-07-05T01:40:01+00:00",
        )

    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="prepared_start_changed"):
        plane.control(
            cycle_id,
            "start",
            {
                **payload,
                "expected_preview_id": prepared["preview"]["preview_id"],
                "prepared_start_id": prepared["prepared_start_id"],
            },
            market=market(close=4_137.44),
            account=account_context(),
            now="2026-07-05T01:40:02+00:00",
        )


def test_adaptive_preview_cannot_override_untrusted_market_or_account_reconciliation(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    with pytest.raises(ValueError, match="market data is stale"):
        plane.control(
            cycle_id,
            "preview",
            adaptive_grid_payload(),
            market=market(close=4_137.44, fresh=False),
            account=account_context(),
        )

    broken_account = account_context()
    broken_account["accounting_snapshot"]["reconciliation"]["status"] = "fail"
    broken_account["accounting_snapshot"]["reconciliation"]["issues"] = [
        "test_drift"
    ]
    preview = plane.control(
        cycle_id,
        "preview",
        adaptive_grid_payload(),
        market=market(close=4_137.44),
        account=broken_account,
    )["preview"]

    assert preview["manual_confirmation"]["available"] is False
    assert "account_snapshot_unavailable" in preview["manual_confirmation"][
        "non_overridable_blocker_codes"
    ]
    blocker = preview["manual_confirmation"]["non_overridable_blockers"][0]
    assert blocker["code"] == "account_snapshot_unavailable"
    assert blocker["source"] == "canonical_account"
    assert blocker["evidence"]["status"] == "drift"


def test_adaptive_preview_carries_solver_only_leverage_capacity_evidence(
    tmp_path: Path,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = adaptive_grid_payload()
    payload["grid"].update({"count": 50, "notional_per_grid": 10_000.0})
    payload["solver"]["locked"] = ["range", "grid_count", "notional_per_grid"]

    preview = plane.control(
        cycle_id,
        "preview",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
    )["preview"]
    manual = preview["manual_confirmation"]
    blocker = next(
        row
        for row in manual["non_overridable_blockers"]
        if row["code"] == "manual_leverage_capacity_exceeded"
    )

    assert manual["available"] is False
    assert "manual_leverage_capacity_exceeded" in manual[
        "non_overridable_blocker_codes"
    ]
    assert blocker["source"] == "adaptive_grid_solver"
    assert blocker["evidence"]["actual_leverage"] > 20.0
    assert blocker["evidence"]["manual_paper_leverage_limit"] == 20.0
    assert "超过 Paper 手动容量 20x" in blocker["message"]


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


def test_legacy_plans_are_read_as_compatible_proposals_without_erasing_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    legacy = proposal(cycle_id, "human") | {"author": "human", "locked_at": "2026-07-05T01:00:00+00:00"}
    from services.journal_store import write_json

    write_json(output / "dualtrack" / "plans" / f"{cycle_id}_human.json", [legacy])
    result = plane.read_model(cycle_id, as_of="2026-07-05T01:01:00+00:00")

    assert result["migration"]["legacy_compatible"] is True
    assert result["migration"]["legacy_migration_required"] is True
    assert result["proposals"][0]["source"] == "human"
    assert result["production_plan"] is None
    assert not (output / "dualtrack" / "strategy_control" / "plans" / f"{cycle_id}.json").exists()
    assert (output / "dualtrack" / "plans" / f"{cycle_id}_human.json").exists()

    migrated = plane.ensure_compatible_active_plan(cycle_id, as_of="2026-07-05T01:01:00+00:00")

    assert migrated["field_sources"]["direction"] == "human"
    assert plane.read_model(cycle_id)["migration"]["legacy_migration_required"] is False


def test_production_controls_are_durable_and_preserve_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    assert plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
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
    account = account_context()

    neutral = plane.preview(cycle_id, {"direction": "neutral", "style": "steady"}, market=market(), account=account)
    long = plane.preview(cycle_id, {"direction": "long", "style": "steady"}, market=market(), account=account)
    short = plane.preview(cycle_id, {"direction": "short", "style": "steady"}, market=market(), account=account)
    aggressive = plane.preview(cycle_id, {"direction": "neutral", "style": "aggressive"}, market=market(), account=account)

    assert long["grid"]["count"] == short["grid"]["count"] == (
        neutral["grid"]["count"] + 1
    ) // 2
    assert aggressive["grid"]["count"] >= 24
    assert neutral["range"]["high"] - neutral["range"]["low"] > aggressive["range"]["high"] - aggressive["range"]["low"]
    assert long["range"]["low"] == neutral["range"]["low"]
    assert long["range"]["high"] == market()["latest_close"]
    assert short["range"]["low"] == market()["latest_close"]
    assert short["range"]["high"] == neutral["range"]["high"]
    assert len(long["orders"]) == long["grid"]["count"]
    assert len(short["orders"]) == short["grid"]["count"]
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
    account = account_context()
    geometry = {"low": 90.0, "high": 130.0}

    earlier = plane.preview(
        cycle_id,
        {"direction": "neutral", "style": "steady", "range": geometry, "grid": {"count": 30}},
        market=market(close=110.0),
        account=account,
    )
    stale_notional = earlier["grid"]["notional_per_grid"]

    with pytest.raises(ValueError, match="planned net profit of 10.00 USD.*within 10x capacity"):
        plane.preview(
            cycle_id,
            {
                "direction": "neutral",
                "style": "steady",
                "range": geometry,
                "grid": {"count": 30, "notional_per_grid": stale_notional, "notional_mode": "manual"},
            },
            market=market(close=120.0),
            account=account,
        )

    auto_payload = {
        "direction": "neutral",
        "style": "steady",
        "range": geometry,
        "grid": {"count": 30, "notional_per_grid": stale_notional, "notional_mode": "auto"},
        "risk_budget": {"leverage": 10.0},
    }
    recalculated = plane.preview(cycle_id, auto_payload, market=market(close=120.0), account=account)

    assert recalculated["grid"]["notional_mode"] == "auto"
    assert recalculated["grid"]["notional_per_grid"] < stale_notional
    assert recalculated["grid"]["notional_per_grid"] == recalculated["risk"]["capital_notional_cap_per_grid"]

    started = plane.control(
        cycle_id,
        "start",
        auto_payload,
        market=market(close=120.0),
        account=account,
        now="2026-07-05T01:40:00+00:00",
    )

    assert started["preview"]["grid"]["notional_mode"] == "auto"
    assert started["preview"]["grid"]["notional_per_grid"] == recalculated["grid"]["notional_per_grid"]
    assert started["plan"]["grid"]["notional_per_grid"] == started["preview"]["grid"]["notional_per_grid"]
    assert started["plan"]["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert started["plan"]["grid"]["actual_leverage"] <= 10.0
    assert started["accepted_orders"] > 0


@pytest.mark.parametrize(
    ("direction", "range_low", "range_high", "expected_side"),
    [
        ("long", 3_900.0, 4_000.0, "buy"),
        ("short", 4_200.0, 4_300.0, "sell"),
    ],
)
def test_directional_preview_outside_range_on_non_entry_side_is_not_a_blocker(
    tmp_path: Path,
    direction: str,
    range_low: float,
    range_high: float,
    expected_side: str,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = {
        "direction": direction,
        "style": "steady",
        "range": {"low": range_low, "high": range_high},
        "grid": {"mode": "arithmetic", "count": 30},
        "solver": {
            "mode": "manual_adaptive",
            "locked": ["range", "grid_count"],
        },
    }

    preview = plane.control(
        cycle_id,
        "preview",
        payload,
        market=market(close=4_137.44),
        account=account_context(),
    )["preview"]

    assert preview["range"]["low"] == range_low
    assert preview["range"]["high"] == range_high
    assert {row["side"] for row in preview["orders"]} == {expected_side}
    assert "market_price_outside_range" not in {
        row["code"] for row in preview["solver"]["risk_flags"]
    }
    assert "market_price_outside_range" not in preview[
        "manual_confirmation"
    ]["overridable_blocker_codes"]


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
    payload = safe_grid("short", "aggressive")

    started = plane.control(cycle_id, "start", payload, market=market(), account=account_context(), now="2026-07-05T01:40:00+00:00")
    orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]

    assert started["runtime"]["desired_state"] == "running"
    assert started["runtime"]["actual_state"] == "running"
    assert started["created_orders"] == len(orders) > 0
    assert started["plan"]["direction"] == "short"
    assert started["plan"]["version"] == 2
    assert started["plan"]["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    assert started["plan"]["grid"]["target_net_profit_per_grid_usd"] == 10.0
    assert started["plan"]["grid"]["actual_leverage"] == started["preview"]["risk"]["actual_leverage"]
    assert started["plan"]["risk_budget"]["actual_leverage"] == started["preview"]["risk"]["actual_leverage"]
    assert all(order["state"] == "accepted" for order in orders)
    assert all(order["side"] == "sell" for order in orders)
    assert all(order["strategy_plan_id"] == started["plan"]["strategy_plan_id"] for order in orders)
    assert all(order["strategy_plan_version"] == started["plan"]["version"] for order in orders)

    repeated = plane.control(cycle_id, "start", payload, market=market(), account=account_context(), now="2026-07-05T01:41:00+00:00")
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


def test_max_loss_is_advisory_and_does_not_block_a_valid_profit_grid(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        {"direction": "neutral", "style": "steady"},
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )

    assert plane.active_plan(cycle_id)["strategy_plan_id"] != active["strategy_plan_id"]
    assert started["runtime"]["actual_state"] == "running"
    assert started["plan"]["grid"]["min_net_profit_per_grid_usd"] >= 10.0
    decision = load_json(output / "dualtrack" / "risk_decisions" / "current.json")[-1]
    assert decision["outcome"] == "allow"
    assert decision["metrics"]["projected_max_loss"] > 0


def test_unknown_account_blocks_before_mutation_even_when_preview_used_fallback(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    preview = plane.preview(cycle_id, safe_grid(), market=market(), account={})
    assert preview["risk"]["equity"] == 10_000.0  # display compatibility only
    with pytest.raises(ValueError, match="account_snapshot_unavailable"):
        plane.control(
            cycle_id,
            "start",
            safe_grid(),
            market=market(),
            account={},
            now="2026-07-05T01:40:00+00:00",
        )

    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert plane.runtime_state(cycle_id)["desired_state"] == "stopped"
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


def test_pre_submit_recheck_reads_same_state_source_and_rejects_drift_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class DriftingAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.submissions = 0

        def snapshot(self, requested_cycle: str, **_kwargs) -> dict:
            self.snapshot_calls += 1
            unexpected = self.snapshot_calls >= 3
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": requested_cycle,
                "orders": ([{
                    "order_id": "concurrent-order",
                    "state": "accepted",
                    "side": "buy",
                    "event": "entry",
                    "order_type": "limit",
                    "price": 90.0,
                    "quantity": 1.0,
                }] if unexpected else []),
                "fills": [],
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

        def submit_order(self, _command: dict) -> dict:
            self.submissions += 1
            raise AssertionError("stale decision must fail before submit")

    adapter = DriftingAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(ValueError, match="risk decision stale"):
        plane.control(
            cycle_id,
            "start",
            safe_grid("long", "steady"),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    assert adapter.submissions == 0
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert plane.runtime_state(cycle_id)["desired_state"] == "stopped"


def test_rollover_start_guard_rechecks_operator_runtime_after_risk_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    previous_cycle_id = "2026-07-04_NIGHT"
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    runtime_path = output / "dualtrack" / "strategy_control" / "runtime.json"
    write_json(runtime_path, [{
        "cycle_id": previous_cycle_id,
        "desired_state": "stopped",
        "actual_state": "stopped",
        "updated_at": "2026-07-05T01:00:00+00:00",
    }])

    def operator_stops_during_risk_work(*_args, **_kwargs):
        write_json(runtime_path, [{
            "cycle_id": cycle_id,
            "desired_state": "stopped",
            "actual_state": "stopped",
            "updated_at": "2026-07-05T01:00:30+00:00",
        }])
        return {"decision_id": "unused", "policy": {"policy_id": "unused"}}

    monkeypatch.setattr(plane, "_authorize_grid_mutation", operator_stops_during_risk_work)
    payload = {
        **safe_grid("neutral", "steady"),
        "rollover_guard": {
            "previous_cycle_id": previous_cycle_id,
            "expected_runtime_updated_at": "2026-07-05T01:00:00+00:00",
        },
    }

    with pytest.raises(ValueError, match="runtime changed before rollover start"):
        plane.control(
            cycle_id,
            "start",
            payload,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:01:00+00:00",
        )

    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert plane.persisted_runtime_state()["cycle_id"] == cycle_id


def test_start_cleanup_snapshot_failure_still_persists_error_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class SnapshotFailsAfterSubmitAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.submission_attempted = False

        def snapshot(self, requested_cycle: str, **_kwargs) -> dict:
            if self.submission_attempted:
                raise RuntimeError("snapshot unavailable after submit")
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": requested_cycle,
                "orders": [],
                "fills": [],
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def submit_order(self, _command: dict) -> dict:
            self.submission_attempted = True
            raise RuntimeError("submit exploded")

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            return {"cancelled_order_count": 0, "cancelled_order_ids": []}

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

    adapter = SnapshotFailsAfterSubmitAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(RuntimeError, match="submit exploded"):
        plane.control(
            cycle_id,
            "start",
            safe_grid("long", "steady"),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    runtime = plane.persisted_runtime_state()
    assert runtime["actual_state"] == "error"
    assert runtime["desired_state"] == "stopped"
    assert runtime["accepted_order_count_known"] is False
    assert "final_snapshot: snapshot unavailable after submit" in runtime["last_error"]


def test_start_requires_plan_selection_and_does_not_auto_lock_on_risk_path(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    plane.upsert_proposal(proposal(cycle_id, "ai"))

    with pytest.raises(ValueError, match="already selected active StrategyPlan"):
        plane.control(
            cycle_id,
            "start",
            safe_grid(),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    assert plane.active_plan(cycle_id) is None
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


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

        def snapshot(self, cycle_id: str, **_kwargs) -> dict:
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": cycle_id,
                "orders": [dict(row) for row in self.orders],
                "fills": ([{"fill_id": "fill-1"}] if self.filled else []),
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
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
            safe_grid("long", "steady"),
            market=market(),
            account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )

    assert started["created_orders"] > 1
    assert started["filled_orders"] == 1
    assert started["accepted_orders"] == started["created_orders"] - 1
    assert started["runtime"]["actual_state"] == "running"


def test_start_accepts_order_filled_between_submit_and_first_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class ReadbackRaceAdapter:
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.orders: list[dict] = []
            self.advanced_on_readback = False

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

        def snapshot(self, requested_cycle: str, **_kwargs) -> dict:
            if self.orders and not self.advanced_on_readback:
                self.orders[0]["state"] = "filled"
                self.advanced_on_readback = True
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": requested_cycle,
                "orders": [dict(row) for row in self.orders],
                "fills": ([{"fill_id": "fill-during-readback"}] if self.advanced_on_readback else []),
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def process_market_event(self, _event: dict) -> dict:
            return {"status": "replayed"}

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            return {"cancelled_order_count": 0, "cancelled_order_ids": []}

    adapter = ReadbackRaceAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )

    assert adapter.advanced_on_readback is True
    assert started["created_orders"] > 1
    assert started["filled_orders"] == 1
    assert started["accepted_orders"] == started["created_orders"] - 1
    assert started["runtime"]["actual_state"] == "running"


@pytest.mark.parametrize("ambiguous_order_id", ["", "   ", "duplicate-order"])
def test_start_rejects_empty_or_duplicate_submission_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_order_id: str,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class AmbiguousIdentityAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.orders: list[dict] = []

        def submit_order(self, command: dict) -> dict:
            row = {
                "order_id": ambiguous_order_id,
                "state": "accepted",
                "side": command["side"],
                "price": command["price"],
                "quantity": command["quantity"],
            }
            self.orders.append(row)
            return dict(row)

        def snapshot(self, requested_cycle: str, **_kwargs) -> dict:
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": requested_cycle,
                "orders": [dict(row) for row in self.orders],
                "fills": [],
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            cancelled = 0
            for row in self.orders:
                if row["state"] == "accepted":
                    row["state"] = "cancelled"
                    cancelled += 1
            return {"cancelled_order_count": cancelled, "cancelled_order_ids": []}

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

    adapter = AmbiguousIdentityAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(ValueError, match="receipts require valid unique order IDs"):
        plane.control(
            cycle_id,
            "start",
            safe_grid("long", "steady"),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    assert plane.runtime_state(cycle_id)["actual_state"] == "error"
    assert not [row for row in adapter.orders if row["state"] == "accepted"]


def test_grid_snapshot_validation_remains_python39_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_zip = zip

    def python39_zip(*iterables):
        return original_zip(*iterables)

    monkeypatch.setattr("builtins.zip", python39_zip)

    class SnapshotAdapter:
        def snapshot(self, _cycle_id: str) -> dict:
            return {
                "orders": [{
                    "order_id": "paper-order-1",
                    "state": "accepted",
                    "event": "entry",
                    "strategy_plan_id": "strategy-plan-1",
                }],
                "positions": [],
            }

    adapter = SnapshotAdapter()
    submitted_ids = {"paper-order-1"}

    assert StrategyControlPlane._validate_start_grid_snapshot(
        adapter,
        "2026-07-05_DAY",
        submitted_ids=submitted_ids,
    )["paper-order-1"]["state"] == "accepted"
    assert StrategyControlPlane._validate_replacement_recovery_snapshot(
        adapter,
        "2026-07-05_DAY",
        submitted_ids=submitted_ids,
        strategy_plan_id="strategy-plan-1",
    )["paper-order-1"]["state"] == "accepted"


def test_start_snapshot_ignores_pending_safe_action_command_receipts() -> None:
    class SnapshotAdapter:
        def snapshot(self, _cycle_id: str) -> dict:
            return {
                "orders": [
                    {"order_id": "entry-1", "state": "accepted", "event": "entry"},
                    {"order_id": "cancel-1", "state": "accepted", "event": "cancel"},
                ],
                "positions": [],
            }

    rows = StrategyControlPlane._validate_start_grid_snapshot(
        SnapshotAdapter(),
        "2026-07-05_DAY",
        submitted_ids={"entry-1"},
    )

    assert set(rows) == {"entry-1", "cancel-1"}


def test_start_rejects_unexpected_active_order_on_terminal_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class LateOrderAdapter:
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.orders: list[dict] = []
            self.injected = False

        def submit_order(self, command: dict) -> dict:
            row = {
                "order_id": command["source_fill_id"],
                "state": "accepted",
                "side": command["side"],
                "price": command["price"],
                "quantity": command["quantity"],
            }
            self.orders.append(row)
            return dict(row)

        def snapshot(self, requested_cycle: str, **_kwargs) -> dict:
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": self.name,
                "cycle_id": requested_cycle,
                "orders": [dict(row) for row in self.orders],
                "fills": [],
                "positions": [],
                "account": {
                    "starting_cash": 10_000.0,
                    "realized_pnl": 0.0,
                    "ending_cash": 10_000.0,
                    "equity": 10_000.0,
                    "margin": 0.0,
                    "exposure": 0.0,
                    "slippage": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                },
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def process_market_event(self, _event: dict) -> dict:
            if not self.injected:
                self.orders.append({
                    "order_id": "concurrent-order",
                    "state": "accepted",
                    "side": "buy",
                    "price": 90.0,
                    "quantity": 1.0,
                })
                self.injected = True
            return {"status": "replayed"}

        def cancel_orders(self, _cycle_id: str, **_kwargs) -> dict:
            cancelled_ids = []
            for row in self.orders:
                if row["state"] == "accepted":
                    row["state"] = "cancelled"
                    cancelled_ids.append(row["order_id"])
            return {
                "cancelled_order_count": len(cancelled_ids),
                "cancelled_order_ids": cancelled_ids,
            }

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "ok", "issues": []}

    adapter = LateOrderAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(ValueError, match="unexpected_accepted=\\['concurrent-order'\\]"):
        plane.control(
            cycle_id,
            "start",
            safe_grid("long", "steady"),
            market=market(),
            account=account_context(),
            now="2026-07-05T01:40:00+00:00",
        )

    assert adapter.injected is True
    assert plane.runtime_state(cycle_id)["actual_state"] == "error"
    assert not [row for row in adapter.orders if row["state"] == "accepted"]


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
    started = plane.control(cycle_id, "start", safe_grid("long", "steady"), market=market(), account=account_context(), now="2026-07-05T01:40:00+00:00")
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


def test_stale_market_safe_controls_cancel_and_flatten_with_audit_evidence(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    adapter = build_execution_engine_adapter(output)
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:41:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 110.0,
        "market_price": 110.0,
        "quantity": 2.0,
        "notional": 220.0,
        "sl": 105.0,
        "tp": 115.0,
        "strategy_plan_id": started["plan"]["strategy_plan_id"],
        "strategy_plan_version": started["plan"]["version"],
        "source": "strategy_production_console",
    })
    invalid_market = {
        **market(close=109.0, fresh=False),
        "status": "blocked",
        "source_mode": "unavailable",
        "provider": "",
        "latest_close": None,
        "latest_timestamp": "",
    }

    cancelled = plane.control(
        cycle_id,
        "cancel_all",
        {},
        market=invalid_market,
        now="2026-07-05T01:45:00+00:00",
    )
    after_cancel = adapter.snapshot(cycle_id)
    stopped = plane.control(
        cycle_id,
        "stop",
        {},
        market=invalid_market,
        now="2026-07-05T01:46:00+00:00",
    )
    terminal = adapter.snapshot(cycle_id)

    assert cancelled["cancelled_orders"] > 0
    assert cancelled["safe_action_market_gates"][0]["action_class"] == "cancel"
    assert cancelled["safe_action_market_gates"][0]["market_fresh"] is False
    assert not [row for row in after_cancel["orders"] if row["state"] == "accepted"]
    assert [row for row in after_cancel["positions"] if row["status"] == "open"]
    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["flattened_positions"] == 1
    assert stopped["safe_action_market_gates"][1]["action_class"] == "reduce_only"
    assert stopped["safe_action_market_gates"][1]["pricing_source"] == "last_known_execution_fill"
    assert not [row for row in terminal["positions"] if row["status"] == "open"]
    event_rows = []
    event_root = output / "dualtrack" / "strategy_control" / "control_events"
    for path in sorted(event_root.glob("*.jsonl")):
        event_rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    cancel_event = next(row for row in event_rows if row["action"] == "cancel_all")
    stop_event = next(row for row in event_rows if row["action"] == "stop")
    assert cancel_event["evidence"]["safe_action_market_gates"][0]["market_fresh"] is False
    assert stop_event["evidence"]["safe_action_market_gates"][1]["pricing_source"] == (
        "last_known_execution_fill"
    )


def test_safe_control_api_skips_planning_timeframes_and_account_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("safe controls must not depend on planning or account read models")

    monkeypatch.setattr(dashboard_server, "build_strategy_timeframes_response", forbidden)
    monkeypatch.setattr(dashboard_server, "build_strategy_console_production_history", forbidden)

    result = dashboard_server.build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-05_DAY",
            "action": "cancel_all",
            "as_of": "2026-07-05T01:45:00+00:00",
        },
        output_root=tmp_path / "outputs",
        market={
            "status": "blocked",
            "fresh": False,
            "is_synthetic": False,
            "provider": "",
            "latest_close": None,
            "latest_timestamp": "",
        },
    )

    assert result["action"] == "cancel_all"
    assert result["safe_action_market_gates"][0]["market_status"] == "blocked"
    assert result["safe_action_market_gates"][0]["pricing_required"] is False


def test_stop_api_cancels_without_live_datafeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_market(*_args, **_kwargs):
        raise ConnectionError("datafeed unavailable")

    monkeypatch.setattr(dashboard_server, "build_dualtrack_market_bars_response", unavailable_market)
    result = dashboard_server.build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-05_DAY",
            "action": "stop",
            "as_of": "2026-07-05T01:45:00+00:00",
        },
        output_root=tmp_path / "outputs",
    )

    assert result["runtime"]["actual_state"] == "stopped"
    assert result["safe_action_market_gates"][0]["market_status"] == "blocked"
    assert result["safe_action_market_gates"][0]["pricing_required"] is False


def test_stop_rejects_wrong_provider_mark_and_uses_target_ledger_price(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    adapter = build_execution_engine_adapter(output)
    cycle_id = "2026-07-05_DAY"
    entry = adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:41:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 110.0,
        "quantity": 1.0,
        "notional": 110.0,
        "position_id": "wrong-provider-stop",
        "strategy_plan_id": "plan-safe-stop",
        "strategy_plan_version": 1,
        "source": "test_setup",
    })
    wrong_provider_market = {
        "status": "ready",
        "source_mode": "evil_provider",
        "fresh": True,
        "is_synthetic": False,
        "provider": "evil_provider",
        "latest_timestamp": "2026-07-05T01:45:00+00:00",
        "latest_close": 777.0,
    }

    stopped = StrategyControlPlane(output).control(
        cycle_id,
        "stop",
        {},
        market=wrong_provider_market,
        now="2026-07-05T01:46:00+00:00",
    )
    close = next(
        row
        for row in adapter.snapshot(cycle_id)["fills"]
        if row["event"] == "flatten" and row["trade_id"] == entry["trade_id"]
    )

    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["safe_action_market_gates"][1]["pricing_source"] == (
        "last_known_execution_fill"
    )
    assert stopped["safe_action_market_gates"][1]["pricing_provider"] == (
        "paper_execution_ledger"
    )
    assert close["price"] == pytest.approx(110.0)
    assert close["price"] != 777.0


def test_blocked_stop_prices_each_hedged_position_from_its_own_ledger(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    adapter = build_execution_engine_adapter(output)
    cycle_id = "2026-07-05_DAY"
    entries = []
    for index, (side, price) in enumerate((("buy", 100.0), ("sell", 200.0)), start=1):
        entries.append(adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": f"2026-07-05T01:4{index}:00+00:00",
            "side": side,
            "event": "entry",
            "order_type": "market",
            "price": price,
            "quantity": 1.0,
            "notional": price,
            "position_id": f"hedged-stop-{index}",
            "strategy_plan_id": "plan-hedged-stop",
            "strategy_plan_version": 1,
            "source": "test_setup",
        }))
    blocked_market = {
        "status": "blocked",
        "source_mode": "unavailable",
        "fresh": False,
        "is_synthetic": False,
        "provider": "",
        "latest_timestamp": "",
        "latest_close": None,
    }

    stopped = StrategyControlPlane(output).control(
        cycle_id,
        "stop",
        {},
        market=blocked_market,
        now="2026-07-05T01:46:00+00:00",
    )
    fills = adapter.snapshot(cycle_id)["fills"]
    closes = {
        row["trade_id"]: row
        for row in fills
        if row["event"] == "flatten"
    }

    assert len(stopped["safe_action_market_gates"]) == 3
    assert {
        gate["pricing_price"] for gate in stopped["safe_action_market_gates"][1:]
    } == {100.0, 200.0}
    for entry in entries:
        close = closes[entry["trade_id"]]
        assert close["price"] == pytest.approx(entry["price"])
        assert close["gross_pnl"] == pytest.approx(0.0)


def test_running_adjustment_replaces_pending_grid_without_stopping_runtime(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(cycle_id, "start", safe_grid("neutral", "steady"), market=market(), account=account_context(), now="2026-07-05T01:40:00+00:00")
    original_orders = [order for order in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] if order["state"] == "accepted"]

    adjusted = plane.control(
        cycle_id,
        "adjust_plan",
        {
            "direction": "short",
            "style": "aggressive",
            "range": {"low": 90.0, "high": 130.0},
        },
        market=market(),
        account=account_context(),
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


def test_regrid_stages_every_replacement_before_cancel_and_advances_market_only_after_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class SequencedAdapter:
        # The control plane advances accepted mutations synchronously only for
        # the active Nautilus path; the legacy adapter gives this test a small,
        # deterministic ledger while the name exercises that exact ordering.
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.events: list[str] = []

        def submit_order(self, command: dict) -> dict:
            self.events.append("submit")
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            self.events.append("cancel")
            return real.cancel_orders(requested_cycle, **kwargs)

        def process_market_event(self, event: dict) -> dict:
            self.events.append("process")
            return real.process_market_event(event)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

    adapter = SequencedAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    adapter.events.clear()

    adjusted = plane.control(
        cycle_id,
        "adjust_plan",
        {
            "direction": "short",
            "style": "aggressive",
            "range": {"low": 90.0, "high": 130.0},
        },
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )

    cancel_index = adapter.events.index("cancel")
    process_index = adapter.events.index("process")
    submit_indices = [index for index, event in enumerate(adapter.events) if event == "submit"]
    assert len(submit_indices) == adjusted["created_orders"] > 0
    assert max(submit_indices) < cancel_index < process_index
    assert "process" not in adapter.events[:cancel_index]


def test_edge_adjustment_preserves_internal_orders_positions_and_fixed_sizing(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original_plan = started["plan"]
    adapter = build_execution_engine_adapter(output)
    accepted = [
        row
        for row in adapter.snapshot(cycle_id)["orders"]
        if row["state"] == "accepted" and row.get("event") == "entry"
    ]
    filled_level = max(
        (row for row in accepted if row["side"] == "buy"),
        key=lambda row: row["price"],
    )
    adapter.process_market_event(
        {
            "cycle_id": cycle_id,
            "ts_event": "2026-07-05T01:41:00+00:00",
            "price": filled_level["price"],
            "fresh": True,
            "is_synthetic": False,
            "source": "canonical_test_feed",
        }
    )
    before = adapter.snapshot(cycle_id)
    open_positions_before = [
        deepcopy(row)
        for row in before["positions"]
        if row["status"] == "open"
    ]
    accepted_before = [
        deepcopy(row)
        for row in before["orders"]
        if row["state"] == "accepted" and row.get("event") == "entry"
    ]
    spacing = float(original_plan["grid"]["spacing"])
    requested = {
        "expected_strategy_plan_id": original_plan["strategy_plan_id"],
        "range": {
            "low": float(original_plan["range"]["low"]) - spacing * 0.8,
            "high": float(original_plan["range"]["high"]) - spacing * 0.8,
        },
    }

    adjusted = plane.control(
        cycle_id,
        "extend_range",
        requested,
        market=market(),
        account=account_context(1_000_000.0),
        now="2026-07-05T01:42:00+00:00",
    )
    terminal = adapter.snapshot(cycle_id)
    terminal_by_id = {row["order_id"]: row for row in terminal["orders"]}
    low = adjusted["effective_range"]["low"]
    high = adjusted["effective_range"]["high"]
    retained_before = [row for row in accepted_before if low <= row["price"] <= high]
    outside_before = [row for row in accepted_before if not low <= row["price"] <= high]

    assert adjusted["steps"] == {"low": 1, "high": -1}
    assert adjusted["created_orders"] == 1
    assert adjusted["cancelled_orders"] == len(outside_before) == 1
    for row in retained_before:
        after = terminal_by_id[row["order_id"]]
        assert after["state"] in {"accepted", "filled"}
        assert after["price"] == row["price"]
        assert after["quantity"] == row["quantity"]
        assert after["strategy_plan_id"] == row["strategy_plan_id"]
    assert terminal_by_id[outside_before[0]["order_id"]]["state"] == "cancelled"
    assert [
        {key: row.get(key) for key in ("trade_id", "status", "side", "remaining_units", "entry_price", "sl", "tp", "strategy_plan_id")}
        for row in terminal["positions"]
        if row["status"] == "open"
    ] == [
        {key: row.get(key) for key in ("trade_id", "status", "side", "remaining_units", "entry_price", "sl", "tp", "strategy_plan_id")}
        for row in open_positions_before
    ]
    assert adjusted["positions_preserved"] is True
    assert adjusted["tp_sl_affected"] is False
    assert adjusted["plan"]["grid"]["spacing"] == original_plan["grid"]["spacing"]
    assert adjusted["plan"]["grid"]["notional_per_grid"] == original_plan["grid"]["notional_per_grid"]
    assert adjusted["plan"]["grid"]["notional_mode"] == original_plan["grid"]["notional_mode"]
    projected_leverage = adjusted["risk_decision"]["metrics"]["projected_actual_leverage"]
    assert adjusted["plan"]["grid"]["actual_leverage"] == projected_leverage
    assert adjusted["plan"]["risk_budget"]["actual_leverage"] == projected_leverage
    assert all(
        row["planned_net_profit_usd"] >= 10.0
        for row in adjusted["plan"]["grid"]["orders"]
    )
    assert adjusted["risk_decision"]["outcome"] == "allow"
    assert adjusted["risk_decision"]["request"]["candidate"]["retained_order_ids"] == sorted(
        row["order_id"] for row in retained_before
    )

    retry = plane.control(
        cycle_id,
        "extend_range",
        requested,
        market=market(close=111.0),
        account=account_context(1.0),
        now="2026-07-05T01:43:00+00:00",
    )
    assert retry["idempotent"] is True
    assert retry["created_orders"] == retry["cancelled_orders"] == 0


def test_range_drag_preview_is_read_only_and_preserves_fixed_count_and_notional(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    paths = [
        plane._plans_path(cycle_id),
        plane.root / "runtime.json",
        output / "dualtrack" / "risk_decisions" / f"{cycle_id}.json",
        plane.root / "control_events" / "2026-07-05.jsonl",
    ]
    before = {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    }
    result = plane.control(
        cycle_id,
        "preview_range",
        {
            "expected_strategy_plan_id": plan["strategy_plan_id"],
            "expected_strategy_plan_version": plan["version"],
            "handle": "range",
            "range": {
                "low": float(plan["range"]["low"]) + 1.0,
                "high": float(plan["range"]["high"]) + 1.0,
            },
        },
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]

    assert result["geometry"]["delta"] == {"low": 1.0, "high": 1.0}
    assert result["old"]["range_width"] == result["new"]["range_width"]
    assert result["old"]["grid_count"] == result["new"]["grid_count"]
    assert result["old"]["spacing"] == result["new"]["spacing"]
    assert result["old"]["notional_per_grid"] == result["new"]["notional_per_grid"]
    assert result["side_effects"] == {
        "orders_created": 0,
        "orders_cancelled": 0,
        "positions_changed": 0,
        "strategy_plan_written": False,
        "risk_decision_persisted": False,
    }
    assert {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    } == before


def test_range_boundary_preview_recomputes_spacing_without_resizing_orders(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = safe_grid("neutral", "steady")
    payload["grid"] = {"mode": "geometric"}
    started = plane.control(
        cycle_id,
        "start",
        payload,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    result = plane.control(
        cycle_id,
        "preview_range",
        {
            "expected_strategy_plan_id": plan["strategy_plan_id"],
            "expected_strategy_plan_version": plan["version"],
            "handle": "upper",
            "range": {
                "low": plan["range"]["low"],
                "high": float(plan["range"]["high"]) + 10.0,
            },
        },
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]

    assert result["new"]["range_low"] == result["old"]["range_low"]
    assert result["new"]["grid_count"] == result["old"]["grid_count"]
    assert result["new"]["notional_per_grid"] == result["old"]["notional_per_grid"]
    assert result["new"]["spacing_ratio"] != result["old"]["spacing_ratio"]
    assert result["order_delta"]["cancel_pending_entries"] > 0
    assert result["order_delta"]["submit_new_entries"] > 0
    assert result["positions"]["preview_effect"] == "none"
    assert result["tp_sl"]["existing_orders_affected_by_preview"] is False


def test_legacy_single_side_range_migrates_39_total_levels_to_20_only_on_replace(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    current = deepcopy(started["plan"])
    envelope = dict(current["range"]["source_envelope"])
    current["range"] = {
        **current["range"],
        "low": envelope["low"],
        "high": envelope["high"],
    }
    for key in ("scope", "split_price", "source_envelope"):
        current["range"].pop(key, None)
    current["grid"]["count"] = 39
    plans = load_json(plane._plans_path(cycle_id))
    write_json(
        plane._plans_path(cycle_id),
        [
            current
            if row.get("strategy_plan_id") == current["strategy_plan_id"]
            else row
            for row in plans
        ],
    )

    drifted_market = market(close=109.5)
    request = {
        "expected_strategy_plan_id": current["strategy_plan_id"],
        "expected_strategy_plan_version": current["version"],
        "handle": "lower",
        "range": {
            "low": float(envelope["low"]) + 1.0,
            "high": current["execution_context"]["market"]["price"],
        },
    }
    preview = plane.control(
        cycle_id,
        "preview_range",
        request,
        market=drifted_market,
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]

    assert preview["migration"] == {
        "schema_version": "legacy-single-side-grid-migration-v1",
        "reason": "pre_scope_single_side_plan",
        "direction": "long",
        "legacy_grid_count": 39,
        "executable_grid_count": 20,
        "split_price": 110.0,
        "legacy_range": envelope,
        "executable_range": {
            "low": envelope["low"],
            "high": 110.0,
        },
        "applies_on_final_confirmation_only": True,
    }
    assert preview["old"]["grid_count"] == 39
    assert preview["old"]["range_high"] == envelope["high"]
    assert preview["new"]["grid_count"] == 20
    assert preview["new"]["range_high"] == 110.0
    assert {row["side"] for row in preview["candidate"]["orders"]} == {"buy"}
    assert plane.active_plan(cycle_id)["grid"]["count"] == 39

    snapshot = build_execution_engine_adapter(output).snapshot(cycle_id)
    replacement = {
        **request,
        "expected_preview_id": preview["preview_id"],
        "expected_execution": {
            "accepted_order_ids": sorted(
                row["order_id"]
                for row in snapshot["orders"]
                if row.get("state") == "accepted"
            ),
            "open_position_ids": [],
        },
        "risk_acknowledgements": {
            "schema_version": "grid-range-risk-ack-v1",
            "preview_id": preview["preview_id"],
            "facts_digest": preview["manual_confirmation"]["facts_digest"],
            "risk_snapshot_digest": preview["manual_confirmation"][
                "risk_snapshot_digest"
            ],
            "codes": sorted(
                row["code"]
                for row in preview["manual_confirmation"][
                    "required_acknowledgements"
                ]
            ),
        },
    }
    replaced = plane.control(
        cycle_id,
        "replace_grid",
        replacement,
        market=market(close=109.4),
        account=account_context(),
        now="2026-07-05T01:43:00+00:00",
    )

    assert replaced["plan"]["grid"]["count"] == 20
    assert replaced["plan"]["range"]["scope"] == "long_side"
    assert replaced["plan"]["range"]["high"] == 110.0


def test_range_preview_blocks_over_budget_without_silent_notional_recalculation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    result = plane.control(
        cycle_id,
        "preview_range",
        {
            "expected_strategy_plan_id": plan["strategy_plan_id"],
            "expected_strategy_plan_version": plan["version"],
            "handle": "upper",
            "range": {
                "low": plan["range"]["low"],
                "high": float(plan["range"]["high"]) + 25.0,
            },
        },
        market=market(),
        account=account_context(1_000.0),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]

    assert result["new"]["notional_per_grid"] == plan["grid"]["notional_per_grid"]
    assert result["can_apply"] is False
    assert result["confirm_disabled_reasons"]
    assert result["risk_recalculation"]["available"] is False
    assert result["risk_recalculation"]["reason"] == "profit_target_requires_grid_geometry_or_capital_change"
    assert result["risk_recalculation"]["applied_automatically"] is False
    assert 0 < result["risk_recalculation"]["notional_per_grid"] < result["new"]["notional_per_grid"]

    with pytest.raises(ValueError, match="risk notional recalculation is unavailable"):
        plane.control(
            cycle_id,
            "preview_range",
            {
                "expected_strategy_plan_id": plan["strategy_plan_id"],
                "expected_strategy_plan_version": plan["version"],
                "handle": "upper",
                "range": {
                    "low": plan["range"]["low"],
                    "high": float(plan["range"]["high"]) + 25.0,
                },
                "recalculate_notional_by_risk_budget": True,
            },
            market=market(),
            account=account_context(1_000.0),
            now="2026-07-05T01:42:01+00:00",
        )


def test_range_preview_fails_closed_for_stale_identity_or_market_outside_range(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    with pytest.raises(ValueError, match="strategy_plan_changed"):
        plane.control(
            cycle_id,
            "preview_range",
            {
                "expected_strategy_plan_id": "stale-plan",
                "expected_strategy_plan_version": plan["version"],
                "handle": "upper",
                "range": plan["range"],
            },
            market=market(),
            account=account_context(),
            now="2026-07-05T01:42:00+00:00",
        )

    blocked = plane.control(
        cycle_id,
        "preview_range",
        {
            "expected_strategy_plan_id": plan["strategy_plan_id"],
            "expected_strategy_plan_version": plan["version"],
            "handle": "lower",
            "range": {
                "low": 115.0,
                "high": plan["range"]["high"],
            },
        },
        market=market(close=110.0),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]
    assert blocked["can_apply"] is False
    assert "market_price_outside_range" in blocked["confirm_disabled_reasons"]
    assert blocked["can_apply_with_acknowledgements"] is True
    confirmation = blocked["manual_confirmation"]
    assert confirmation["scope"] == "paper_only"
    assert confirmation["non_overridable_blocker_codes"] == []
    acknowledgement_codes = {
        row["code"] for row in confirmation["required_acknowledgements"]
    }
    assert {
        "specification_change",
        "maximum_loss_scenario",
        "market_outside_range",
    } <= acknowledgement_codes
    max_loss = next(
        row
        for row in confirmation["required_acknowledgements"]
        if row["code"] == "maximum_loss_scenario"
    )
    assert max_loss["facts"]["assumption"] == (
        "old_positions_flatten_then_all_candidate_same_side_entries_fill_"
        "then_planned_stop_beyond_range"
    )


def test_replacement_spec_reports_post_flatten_candidate_max_loss() -> None:
    source = {
        "range": {"low": 100.0, "high": 120.0},
        "grid": {
            "count": 40,
            "mode": "arithmetic",
            "spacing": 0.5,
            "notional_per_grid": 2_500.0,
            "leverage": 10.0,
            "min_net_profit_per_grid_usd": 10.0,
            "orders": [],
        },
        "risk": {"max_loss": 999.0},
    }
    specification = strategy_control_plane_module._range_preview_specification(
        source,
        canonical_metrics={
            "equity": 10_000.0,
            "candidate_notional_by_side": {"buy": 40_000.0, "sell": 30_000.0},
            "candidate_loss_by_side": {"buy": 420.0, "sell": 315.0},
            "existing_stop_loss_by_side": {"buy": 900.0, "sell": 0.0},
            "projected_max_loss": 1_320.0,
            "projected_actual_leverage": 13.0,
            "projected_margin": 13_000.0,
        },
        post_flatten_candidate_only=True,
    )

    assert specification["max_loss"] == 420.0
    assert specification["actual_leverage"] == 4.0
    assert specification["estimated_margin"] == 4_000.0


def test_pre_flatten_position_risk_does_not_create_contradictory_leverage_ack() -> None:
    decision = {
        "request": {"candidate": {"leverage": 10.0}},
        "metrics": {
            "equity": 10_000.0,
            "candidate_notional_by_side": {"buy": 50_000.0, "sell": 40_000.0},
            "existing_notional_by_side": {"buy": 70_000.0, "sell": 0.0},
        },
        "limits": {"max_leverage": 10.0, "margin_budget": 8_000.0},
        "blockers": [
            {"code": "projected_leverage_exceeded"},
            {"code": "projected_margin_exceeded"},
        ],
    }
    effective = strategy_control_plane_module._manual_range_effective_blocker_codes(
        decision
    )
    assert "projected_leverage_exceeded" not in effective
    assert "projected_margin_exceeded" not in effective
    rows = strategy_control_plane_module._manual_range_acknowledgement_contract(
        preview_id="preview-post-flatten",
        old={"range_width": 200.0, "max_loss": 1_000.0},
        new={
            "range_width": 150.0,
            "max_loss": 900.0,
            "actual_leverage": 5.0,
            "estimated_margin": 5_000.0,
        },
        blocker_codes=effective,
        local_profit_target_not_met=False,
        limits=decision["limits"],
    )
    assert "leverage_and_margin_risk" not in {row["code"] for row in rows}

    capital_only_rows = (
        strategy_control_plane_module._manual_range_acknowledgement_contract(
            preview_id="preview-capital-only",
            old={
                "range_width": 200.0,
                "max_loss": 1_000.0,
                "min_net_profit_per_grid_usd": 10.0,
                "actual_leverage": 5.0,
                "estimated_margin": 5_000.0,
            },
            new={
                "range_width": 150.0,
                "max_loss": 900.0,
                "min_net_profit_per_grid_usd": 12.0,
                "actual_leverage": 12.0,
                "estimated_margin": 12_000.0,
            },
            blocker_codes={
                "projected_leverage_exceeded",
                "projected_margin_exceeded",
            },
            local_profit_target_not_met=False,
            limits=decision["limits"],
        )
    )
    capital_codes = {row["code"] for row in capital_only_rows}
    assert "leverage_and_margin_risk" in capital_codes
    assert "profit_target_shortfall" not in capital_codes


@pytest.mark.parametrize("runtime_change", ("id", "version"))
def test_range_preview_rejects_runtime_active_plan_identity_drift(
    tmp_path: Path,
    runtime_change: str,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    runtime_rows = load_json(plane.root / "runtime.json")
    runtime = dict(runtime_rows[-1])
    if runtime_change == "id":
        runtime["strategy_plan_id"] = "runtime-old-plan"
    else:
        runtime["strategy_plan_version"] = int(plan["version"]) - 1
    write_json(plane.root / "runtime.json", [*runtime_rows[:-1], runtime])

    with pytest.raises(ValueError, match="strategy_plan_changed"):
        plane.control(
            cycle_id,
            "preview_range",
            {
                "expected_strategy_plan_id": plan["strategy_plan_id"],
                "expected_strategy_plan_version": plan["version"],
                "handle": "upper",
                "range": {
                    "low": plan["range"]["low"],
                    "high": float(plan["range"]["high"]) + 5.0,
                },
            },
            market=market(),
            account=account_context(),
            now="2026-07-05T01:42:00+00:00",
        )


def test_range_preview_old_and_new_risk_share_current_canonical_accounting(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    adapter = build_execution_engine_adapter(output)
    entry = max(
        (
            row
            for row in adapter.snapshot(cycle_id)["orders"]
            if row.get("state") == "accepted" and row.get("side") == "buy"
        ),
        key=lambda row: row["price"],
    )
    adapter.process_market_event({
        "cycle_id": cycle_id,
        "ts_event": "2026-07-05T01:41:00+00:00",
        "price": entry["price"],
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    result = plane.control(
        cycle_id,
        "preview_range",
        {
            "expected_strategy_plan_id": plan["strategy_plan_id"],
            "expected_strategy_plan_version": plan["version"],
            "handle": "range",
            "range": {
                "low": float(plan["range"]["low"]) + 1.0,
                "high": float(plan["range"]["high"]) + 1.0,
            },
        },
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]

    old_metrics = result["canonical_risk"]["old"]["metrics"]
    new_metrics = result["canonical_risk"]["new"]["metrics"]
    assert result["canonical_risk"]["basis"] == "exact_commands_plus_current_canonical_accounting"
    assert old_metrics["open_position_count"] == new_metrics["open_position_count"] == 1
    assert result["old"]["max_loss"] == old_metrics["projected_max_loss"]
    assert result["old"]["estimated_margin"] == old_metrics["projected_margin"]
    assert result["old"]["actual_leverage"] == old_metrics["projected_actual_leverage"]
    candidate_max_loss = max(new_metrics["candidate_loss_by_side"].values())
    candidate_max_notional = max(
        new_metrics["candidate_notional_by_side"].values()
    )
    assert result["new"]["max_loss"] == candidate_max_loss
    assert result["new"]["estimated_margin"] == pytest.approx(
        candidate_max_notional / float(result["new"]["leverage"])
    )
    assert result["new"]["actual_leverage"] == pytest.approx(
        candidate_max_notional / float(new_metrics["equity"])
    )
    assert result["new"]["max_loss"] <= new_metrics["projected_max_loss"]


def test_range_risk_recalculation_cannot_silently_reduce_profit_target_grid(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = safe_grid("neutral", "steady")
    started = plane.control(
        cycle_id,
        "start",
        payload,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    adapter = build_execution_engine_adapter(output)
    entry = max(
        (
            row
            for row in adapter.snapshot(cycle_id)["orders"]
            if row.get("state") == "accepted" and row.get("side") == "buy"
        ),
        key=lambda row: row["price"],
    )
    adapter.process_market_event({
        "cycle_id": cycle_id,
        "ts_event": "2026-07-05T01:41:00+00:00",
        "price": entry["price"],
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    request = {
        "expected_strategy_plan_id": plan["strategy_plan_id"],
        "expected_strategy_plan_version": plan["version"],
        "handle": "upper",
        "range": {
            "low": plan["range"]["low"],
            "high": float(plan["range"]["high"]) + 5.0,
        },
    }
    blocked = plane.control(
        cycle_id,
        "preview_range",
        request,
        market=market(),
        account=account_context(800.0),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]
    blocker_codes = {
        row["code"] for row in blocked["canonical_risk"]["new"]["blockers"]
    }
    assert {"projected_margin_exceeded", "projected_leverage_exceeded"} <= blocker_codes
    assert blocked["risk_recalculation"]["available"] is False

    with pytest.raises(ValueError, match="risk notional recalculation is unavailable"):
        plane.control(
            cycle_id,
            "preview_range",
            {**request, "recalculate_notional_by_risk_budget": True},
            market=market(),
            account=account_context(800.0),
            now="2026-07-05T01:42:01+00:00",
        )


def test_range_risk_recalculation_is_unavailable_when_open_loss_uses_budget(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    adapter = build_execution_engine_adapter(output)
    entry = max(
        (
            row
            for row in adapter.snapshot(cycle_id)["orders"]
            if row.get("state") == "accepted" and row.get("side") == "buy"
        ),
        key=lambda row: row["price"],
    )
    adapter.process_market_event({
        "cycle_id": cycle_id,
        "ts_event": "2026-07-05T01:41:00+00:00",
        "price": entry["price"],
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    request = {
        "expected_strategy_plan_id": plan["strategy_plan_id"],
        "expected_strategy_plan_version": plan["version"],
        "handle": "upper",
        "range": {
            "low": plan["range"]["low"],
            "high": float(plan["range"]["high"]) + 5.0,
        },
    }
    blocked = plane.control(
        cycle_id,
        "preview_range",
        request,
        market=market(),
        account=account_context(50.0),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]
    assert blocked["canonical_risk"]["new"]["recommendation"]["available"] is False
    assert blocked["risk_recalculation"]["available"] is False
    with pytest.raises(ValueError, match="recalculation is unavailable"):
        plane.control(
            cycle_id,
            "preview_range",
            {**request, "recalculate_notional_by_risk_budget": True},
            market=market(),
            account=account_context(50.0),
            now="2026-07-05T01:42:01+00:00",
        )


def _range_replacement_request(
    plane: StrategyControlPlane,
    cycle_id: str,
    plan: dict,
    *,
    upper_delta: float = 5.0,
) -> tuple[dict, dict]:
    geometry = {
        "expected_strategy_plan_id": plan["strategy_plan_id"],
        "expected_strategy_plan_version": plan["version"],
        "handle": "upper",
        "range": {
            "low": plan["range"]["low"],
            "high": float(plan["range"]["high"]) + upper_delta,
        },
    }
    preview = plane.control(
        cycle_id,
        "preview_range",
        geometry,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]
    snapshot = build_execution_engine_adapter(plane.output_root).snapshot(cycle_id)
    execution = {
        "accepted_order_ids": [
            row["order_id"]
            for row in snapshot["orders"]
            if row.get("state") == "accepted"
        ],
        "open_position_ids": [
            row.get("position_id") or row.get("trade_id")
            for row in snapshot["positions"]
            if row.get("status") == "open"
        ],
    }
    return preview, {
        **geometry,
        "expected_preview_id": preview["preview_id"],
        "expected_execution": execution,
        "risk_acknowledgements": {
            "schema_version": "grid-range-risk-ack-v1",
            "preview_id": preview["preview_id"],
            "facts_digest": preview["manual_confirmation"]["facts_digest"],
            "risk_snapshot_digest": preview["manual_confirmation"][
                "risk_snapshot_digest"
            ],
            "codes": sorted(
                row["code"]
                for row in preview["manual_confirmation"][
                    "required_acknowledgements"
                ]
            ),
        },
    }


def test_risky_manual_range_replacement_requires_every_acknowledgement_and_is_paper_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    real = build_execution_engine_adapter(output)

    class NautilusPaperFacade:
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.authoritative = self

        def submit_order(self, command: dict) -> dict:
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

        def process_market_event(self, event: dict) -> dict:
            return real.process_market_event(event)

        def flush(self, requested_cycle: str) -> dict:
            return {"cycle_id": requested_cycle, "status": "flushed"}

        def flush_commands(self, requested_cycle: str) -> dict:
            return {"cycle_id": requested_cycle, "status": "flushed"}

    adapter = NautilusPaperFacade()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    geometry = {
        "expected_strategy_plan_id": plan["strategy_plan_id"],
        "expected_strategy_plan_version": plan["version"],
        "handle": "draft",
        "range": {"low": 95.0, "high": 99.0},
    }
    preview = plane.control(
        cycle_id,
        "preview_range",
        geometry,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )["preview"]
    assert preview["can_apply"] is False
    assert preview["can_apply_with_acknowledgements"] is True
    required_codes = sorted(
        row["code"]
        for row in preview["manual_confirmation"]["required_acknowledgements"]
    )
    assert {
        "profit_target_shortfall",
        "maximum_loss_scenario",
        "specification_change",
    } <= set(required_codes)
    assert "market_outside_range" not in required_codes
    with pytest.raises(ValueError, match="paper-only"):
        strategy_control_plane_module._require_acknowledged_paper_grid_risk(
            preview["canonical_risk"]["new"],
            {
                "schema_version": "grid-range-risk-ack-v1",
                "scope": "paper_only",
                "preview_id": preview["preview_id"],
                "overridden_blocker_codes": preview["manual_confirmation"][
                    "overridable_blocker_codes"
                ],
            },
            adapter_name="live",
        )
    snapshot = adapter.snapshot(cycle_id)
    request = {
        **geometry,
        "expected_preview_id": preview["preview_id"],
        "expected_execution": {
            "accepted_order_ids": sorted(
                row["order_id"]
                for row in snapshot["orders"]
                if row.get("state") == "accepted"
            ),
            "open_position_ids": [],
        },
        "risk_acknowledgements": {
            "schema_version": "grid-range-risk-ack-v1",
            "preview_id": preview["preview_id"],
            "facts_digest": preview["manual_confirmation"]["facts_digest"],
            "risk_snapshot_digest": preview["manual_confirmation"][
                "risk_snapshot_digest"
            ],
            "codes": required_codes[:-1],
        },
    }
    with pytest.raises(ValueError, match="range_risk_acknowledgements_incomplete"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"
    assert len(plane._accepted_orders(cycle_id, adapter=adapter)) == len(
        request["expected_execution"]["accepted_order_ids"]
    )

    request["risk_acknowledgements"]["codes"] = required_codes
    changed_facts_preview = plane.control(
        cycle_id,
        "preview_range",
        geometry,
        market=market(),
        account=account_context(equity=10_001.0),
        now="2026-07-05T01:43:01+00:00",
    )["preview"]
    assert changed_facts_preview["preview_id"] == preview["preview_id"]
    assert {
        row["code"] for row in changed_facts_preview["risk_decision"]["blockers"]
    } == {row["code"] for row in preview["risk_decision"]["blockers"]}
    assert changed_facts_preview["manual_confirmation"]["facts_digest"] != (
        preview["manual_confirmation"]["facts_digest"]
    )
    with pytest.raises(ValueError, match="range_risk_acknowledgements_incomplete"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(equity=10_001.0),
            now="2026-07-05T01:43:01+00:00",
        )
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"

    replaced = plane.control(
        cycle_id,
        "replace_grid",
        request,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:43:01+00:00",
    )
    assert replaced["runtime"]["actual_state"] == "running"
    assert replaced["plan"]["range"]["low"] == 95.0
    assert replaced["plan"]["range"]["high"] == 99.0
    override = replaced["risk_decision"]["operator_override"]
    assert override["scope"] == "paper_only"
    assert "market_price_outside_range" not in override["overridden_blocker_codes"]
    persisted = replaced["plan"]["replacement_request"]["risk_acknowledgement"]
    assert persisted["acknowledgement_codes"] == required_codes


def test_replace_grid_stages_before_stop_then_activates_once(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    old_plan = started["plan"]
    preview, request = _range_replacement_request(
        plane,
        cycle_id,
        old_plan,
    )

    replaced = plane.control(
        cycle_id,
        "replace_grid",
        request,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:43:00+00:00",
    )

    assert replaced["action"] == "replace_grid"
    assert replaced["idempotent"] is False
    assert replaced["stopped"] is True
    assert replaced["plan"]["version"] == old_plan["version"] + 1
    assert replaced["plan"]["preview_id"] == preview["preview_id"]
    assert replaced["plan"]["replacement_request"]["phase"] == "complete"
    assert replaced["runtime"]["actual_state"] == "running"
    assert replaced["runtime"]["last_action"] == "replace_grid"
    plans = load_json(plane._plans_path(cycle_id))
    assert next(row for row in plans if row["strategy_plan_id"] == old_plan["strategy_plan_id"])["status"] == "superseded"
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == replaced["plan"]["strategy_plan_id"]
    accepted = [
        row
        for row in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
        if row.get("state") == "accepted"
    ]
    assert accepted
    assert {row.get("strategy_plan_id") for row in accepted} == {
        replaced["plan"]["strategy_plan_id"]
    }

    retried = plane.control(
        cycle_id,
        "replace_grid",
        request,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:44:00+00:00",
    )
    assert retried["idempotent"] is True
    assert retried["created_orders"] == 0
    assert len(load_json(plane._plans_path(cycle_id))) == len(plans)


def test_replace_grid_rejects_execution_drift_before_staging_or_stop(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    _preview, request = _range_replacement_request(plane, cycle_id, plan)
    adapter = build_execution_engine_adapter(output)
    removed = request["expected_execution"]["accepted_order_ids"][0]
    adapter.cancel_orders(
        cycle_id,
        order_ids=[removed],
        ts="2026-07-05T01:42:30+00:00",
        reason="test_drift",
    )

    with pytest.raises(ValueError, match="execution_state_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    assert plane.active_plan(cycle_id)["strategy_plan_id"] == plan["strategy_plan_id"]
    assert not [
        row
        for row in load_json(plane._plans_path(cycle_id))
        if isinstance(row.get("replacement_request"), dict)
    ]


def test_replace_grid_persists_request_before_stop_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    _preview, request = _range_replacement_request(plane, cycle_id, plan)

    def fail_stop(*_args, **_kwargs):
        staged = [
            row
            for row in load_json(plane._plans_path(cycle_id))
            if row.get("status") == "staging"
        ]
        assert len(staged) == 1
        assert staged[0]["replacement_request"]["phase"] == "prepared"
        raise RuntimeError("injected stop failure")

    monkeypatch.setattr(plane, "_stop", fail_stop)
    with pytest.raises(RuntimeError, match="injected stop failure"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    staged = [
        row
        for row in load_json(plane._plans_path(cycle_id))
        if isinstance(row.get("replacement_request"), dict)
    ]
    assert len(staged) == 1
    assert staged[0]["status"] == "staging"
    assert staged[0]["replacement_request"]["phase"] == "stop_failed"
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == plan["strategy_plan_id"]


def test_replace_grid_rechecks_risk_and_range_before_any_execution_change(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    plan = started["plan"]
    _preview, request = _range_replacement_request(plane, cycle_id, plan)
    before = build_execution_engine_adapter(output).snapshot(cycle_id)

    with pytest.raises(ValueError, match="strategy_preview_changed|range_replacement_blocked|selected direction has no executable"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(close=90.0),
            account=account_context(100.0),
            now="2026-07-05T01:43:00+00:00",
        )

    after = build_execution_engine_adapter(output).snapshot(cycle_id)
    assert [row["order_id"] for row in after["orders"] if row.get("state") == "accepted"] == [
        row["order_id"] for row in before["orders"] if row.get("state") == "accepted"
    ]
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == plan["strategy_plan_id"]


def test_replace_grid_retry_repairs_final_runtime_write_without_duplicate_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    _preview, request = _range_replacement_request(
        plane,
        cycle_id,
        started["plan"],
    )
    original_write_runtime = plane._write_runtime
    failed_once = False

    def fail_final_runtime(row: dict) -> None:
        nonlocal failed_once
        if (
            not failed_once
            and row.get("last_action") == "replace_grid"
            and row.get("actual_state") == "running"
        ):
            failed_once = True
            raise OSError("injected final runtime write failure")
        original_write_runtime(row)

    monkeypatch.setattr(plane, "_write_runtime", fail_final_runtime)
    with pytest.raises(OSError, match="injected final runtime write failure"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    active = plane.active_plan(cycle_id)
    assert active["replacement_request"]["phase"] == "complete"
    before_retry = build_execution_engine_adapter(output).snapshot(cycle_id)
    order_ids = [row["order_id"] for row in before_retry["orders"]]
    assert len(order_ids) == len(set(order_ids))

    retried = plane.control(
        cycle_id,
        "replace_grid",
        request,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:44:00+00:00",
    )
    after_retry = build_execution_engine_adapter(output).snapshot(cycle_id)
    assert retried["idempotent"] is True
    assert retried["runtime"]["strategy_plan_id"] == active["strategy_plan_id"]
    assert [row["order_id"] for row in after_retry["orders"]] == order_ids


def test_replace_grid_failure_cleans_only_staged_plan_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    old_plan = started["plan"]
    _preview, request = _range_replacement_request(plane, cycle_id, old_plan)
    base_adapter = build_execution_engine_adapter(output)

    class ForeignOrderRaceAdapter:
        name = "legacy_paper"

        def __init__(self) -> None:
            self.injected = False

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return base_adapter.snapshot(requested_cycle, **kwargs)

        def submit_order(self, command: dict) -> dict:
            receipt = base_adapter.submit_order(command)
            if (
                not self.injected
                and command.get("event") == "entry"
                and command.get("strategy_plan_id") != old_plan["strategy_plan_id"]
            ):
                self.injected = True
                base_adapter.submit_order({
                    **command,
                    "source_fill_id": "foreign-plan-order",
                    "strategy_plan_id": "foreign-plan",
                    "strategy_plan_version": 1,
                    "price": 80.0,
                    "requested_price": 80.0,
                })
            return receipt

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return base_adapter.cancel_orders(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return base_adapter.reconcile(requested_cycle)

    adapter = ForeignOrderRaceAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(ValueError, match="unexpected_accepted"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    snapshot = base_adapter.snapshot(cycle_id)
    accepted = [row for row in snapshot["orders"] if row.get("state") == "accepted"]
    assert [row.get("strategy_plan_id") for row in accepted] == ["foreign-plan"]
    failed = max(load_json(plane._plans_path(cycle_id)), key=lambda row: row["version"])
    assert failed["status"] == "failed"
    assert failed["replacement_request"]["phase"] == "failed"


def test_replace_grid_second_preflight_failure_after_stop_is_durable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    _preview, request = _range_replacement_request(
        plane,
        cycle_id,
        started["plan"],
    )
    original_preview = plane.preview

    def changed_preview(*args, **kwargs):
        result = original_preview(*args, **kwargs)
        return {**result, "preview_id": "market-drifted-after-stop"}

    monkeypatch.setattr(plane, "preview", changed_preview)
    with pytest.raises(ValueError, match="strategy_preview_changed"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    failed = max(load_json(plane._plans_path(cycle_id)), key=lambda row: row["version"])
    assert failed["status"] == "failed"
    assert failed["replacement_request"]["phase"] == "failed"
    runtime = plane.runtime_state(cycle_id)
    assert runtime["desired_state"] == "stopped"
    assert runtime["actual_state"] == "error"
    assert runtime["last_action"] == "replace_grid"
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["positions"] == []


def test_replace_grid_rejects_duplicate_ids_in_current_execution_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")

    class DuplicateIdentityAdapter:
        def snapshot(self, _cycle_id: str) -> dict:
            row = {
                "order_id": "duplicate-order",
                "state": "accepted",
                "event": "entry",
                "strategy_plan_id": "plan-1",
            }
            return {"orders": [dict(row), dict(row)], "positions": []}

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: DuplicateIdentityAdapter(),
    )
    with pytest.raises(ValueError, match="execution_state_changed"):
        plane._assert_expected_execution(
            "2026-07-05_DAY",
            {
                "accepted_order_ids": ["duplicate-order"],
                "open_position_ids": [],
            },
            expected_strategy_plan_id="plan-1",
        )


@pytest.mark.parametrize(
    "expected",
    [
        {"accepted_order_ids": ["   "], "open_position_ids": []},
        {"accepted_order_ids": [], "open_position_ids": ["\t"]},
        {"accepted_order_ids": [123], "open_position_ids": []},
        {"accepted_order_ids": [], "open_position_ids": [456]},
    ],
)
def test_replace_grid_rejects_blank_or_non_string_expected_execution_ids(
    expected: dict,
) -> None:
    with pytest.raises(ValueError, match="execution_state_changed"):
        StrategyControlPlane._expected_execution_sets(expected)


@pytest.mark.parametrize("kind", ["order", "position"])
def test_replace_grid_rejects_blank_current_execution_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")

    class BlankIdentityAdapter:
        def snapshot(self, _cycle_id: str) -> dict:
            return {
                "orders": ([{
                    "order_id": "   ",
                    "state": "accepted",
                    "strategy_plan_id": "plan-1",
                }] if kind == "order" else []),
                "positions": ([{
                    "position_id": "\t",
                    "status": "open",
                    "strategy_plan_id": "plan-1",
                }] if kind == "position" else []),
            }

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: BlankIdentityAdapter(),
    )
    with pytest.raises(ValueError, match="execution_state_changed"):
        plane._assert_expected_execution(
            "2026-07-05_DAY",
            {"accepted_order_ids": [], "open_position_ids": []},
            expected_strategy_plan_id="plan-1",
        )


def test_replace_grid_same_fingerprint_recovers_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("long", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    old_plan = started["plan"]
    _preview, request = _range_replacement_request(plane, cycle_id, old_plan)
    original_activate = plane._activate_staged_range_plan

    def hard_crash(*_args, **_kwargs):
        raise SystemExit("injected process crash before activation")

    monkeypatch.setattr(plane, "_activate_staged_range_plan", hard_crash)
    with pytest.raises(SystemExit, match="injected process crash"):
        plane.control(
            cycle_id,
            "replace_grid",
            request,
            market=market(),
            account=account_context(),
            now="2026-07-05T01:43:00+00:00",
        )

    crashed_runtime = plane.runtime_state(cycle_id)
    assert crashed_runtime["actual_state"] == "replanning"
    crashed_plan = max(
        load_json(plane._plans_path(cycle_id)),
        key=lambda row: row["version"],
    )
    assert crashed_plan["status"] == "staging"
    assert crashed_plan["replacement_request"]["phase"] == "launch_authorized"
    staged_orders = [
        row
        for row in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
        if row.get("state") == "accepted"
    ]
    assert staged_orders
    assert {row.get("strategy_plan_id") for row in staged_orders} == {
        crashed_plan["strategy_plan_id"]
    }

    monkeypatch.setattr(plane, "_activate_staged_range_plan", original_activate)
    recovered = plane.control(
        cycle_id,
        "replace_grid",
        request,
        market=market(),
        account=account_context(),
        now="2026-07-05T01:44:00+00:00",
    )

    assert recovered["recovered"] is True
    assert recovered["runtime"]["actual_state"] == "running"
    assert recovered["plan"]["status"] == "active"
    assert recovered["plan"]["replacement_request"]["phase"] == "complete"
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == crashed_plan["strategy_plan_id"]
    terminal_orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
    order_ids = [row["order_id"] for row in terminal_orders]
    assert len(order_ids) == len(set(order_ids))


def test_edge_adjustment_risk_rejects_before_submit_or_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)

    class RecordingAdapter:
        name = real.name

        def __init__(self) -> None:
            self.submissions = 0
            self.cancellations = 0

        def submit_order(self, command: dict) -> dict:
            self.submissions += 1
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            self.cancellations += 1
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

    adapter = RecordingAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(original["grid"]["spacing"])

    with pytest.raises(ValueError, match="projected_leverage_exceeded|projected_margin_exceeded"):
        plane.control(
            cycle_id,
            "extend_range",
            {
                "expected_strategy_plan_id": original["strategy_plan_id"],
                "range": {
                    "low": float(original["range"]["low"]) - spacing,
                    "high": float(original["range"]["high"]) + spacing,
                },
            },
            market=market(),
            account=account_context(10.0),
            now="2026-07-05T01:42:00+00:00",
        )

    assert adapter.submissions == 0
    assert adapter.cancellations == 0
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == original["strategy_plan_id"]


def test_edge_contraction_can_remove_last_pending_entry_without_replacement(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    adapter = build_execution_engine_adapter(output)
    accepted = [
        row for row in adapter.snapshot(cycle_id)["orders"] if row["state"] == "accepted"
    ]
    last = max(accepted, key=lambda row: row["price"])
    adapter.cancel_orders(
        cycle_id,
        order_ids=[row["order_id"] for row in accepted if row["order_id"] != last["order_id"]],
        ts="2026-07-05T01:41:00+00:00",
        reason="test_setup",
    )
    spacing = float(original["grid"]["spacing"])

    result = plane.control(
        cycle_id,
        "extend_range",
        {
            "expected_strategy_plan_id": original["strategy_plan_id"],
            "range": {
                "low": float(original["range"]["low"]),
                "high": float(original["range"]["high"]) - spacing,
            },
        },
        market=market(),
        account=account_context(),
        now="2026-07-05T01:42:00+00:00",
    )

    assert result["created_orders"] == 0
    assert result["cancelled_orders"] == 1
    assert result["risk_decision"]["outcome"] == "allow"
    assert not [
        row for row in adapter.snapshot(cycle_id)["orders"] if row["state"] == "accepted"
    ]


def test_edge_adjustment_stage_failure_removes_only_new_plan_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)
    original_accepted_ids = {
        row["order_id"]
        for row in real.snapshot(cycle_id)["orders"]
        if row["state"] == "accepted"
    }

    class FailingAdapter:
        name = real.name

        def __init__(self) -> None:
            self.submissions = 0

        def submit_order(self, command: dict) -> dict:
            self.submissions += 1
            if self.submissions == 2:
                raise RuntimeError("injected edge stage failure")
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

    adapter = FailingAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(original["grid"]["spacing"])

    with pytest.raises(RuntimeError, match="injected edge stage failure"):
        plane.control(
            cycle_id,
            "extend_range",
            {
                "expected_strategy_plan_id": original["strategy_plan_id"],
                "range": {
                    "low": float(original["range"]["low"]) - spacing,
                    "high": float(original["range"]["high"]) + spacing,
                },
            },
            market=market(),
            account=account_context(1_000_000.0),
            now="2026-07-05T01:42:00+00:00",
        )

    terminal = real.snapshot(cycle_id)
    accepted = {
        row["order_id"]
        for row in terminal["orders"]
        if row["state"] == "accepted"
    }
    assert accepted == original_accepted_ids
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == original["strategy_plan_id"]
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"


def test_edge_adjustment_resumes_same_staged_plan_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)
    class CrashDuringStage:
        name = real.name

        def __init__(self) -> None:
            self.submissions = 0

        def submit_order(self, command: dict) -> dict:
            self.submissions += 1
            if self.submissions == 2:
                raise SimulatedProcessCrash("process exited during edge staging")
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

    crashing = CrashDuringStage()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: crashing,
    )
    spacing = float(original["grid"]["spacing"])
    request = {
        "expected_strategy_plan_id": original["strategy_plan_id"],
        "range": {
            "low": float(original["range"]["low"]) - spacing,
            "high": float(original["range"]["high"]) + spacing,
        },
    }

    with pytest.raises(SimulatedProcessCrash):
        plane.control(
            cycle_id,
            "extend_range",
            request,
            market=market(),
            account=account_context(1_000_000.0),
            now="2026-07-05T01:42:00+00:00",
        )
    staged = [
        row
        for row in load_json(plane._plans_path(cycle_id))
        if row.get("status") == "staging"
    ]
    assert len(staged) == 1
    assert staged[0]["risk_request_id"].startswith("risk-request-")
    assert staged[0]["risk_decision_id"].startswith("risk-decision-")
    assert staged[0]["risk_policy_id"]
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"
    with pytest.raises(ValueError, match="range_adjustment_in_progress"):
        plane.control(
            cycle_id,
            "extend_range",
            {
                "expected_strategy_plan_id": original["strategy_plan_id"],
                "range": dict(original["range"]),
            },
            market=market(),
            account=account_context(),
            now="2026-07-05T01:42:30+00:00",
        )

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: real,
    )
    resumed = plane.control(
        cycle_id,
        "extend_range",
        request,
        market=market(),
        account=account_context(1_000_000.0),
        now="2026-07-05T01:43:00+00:00",
    )

    assert resumed["plan"]["strategy_plan_id"] == staged[0]["strategy_plan_id"]
    assert resumed["plan"]["status"] == "active"
    assert plane.runtime_state(cycle_id)["actual_state"] == "running"
    assert (
        plane.runtime_state(cycle_id)["risk_decision_id"]
        == resumed["plan"]["risk_decision_id"]
    )
    assert not [
        row
        for row in load_json(plane._plans_path(cycle_id))
        if row.get("status") == "staging"
    ]


def test_edge_adjustment_repairs_runtime_after_activation_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    spacing = float(original["grid"]["spacing"])
    request = {
        "expected_strategy_plan_id": original["strategy_plan_id"],
        "range": {
            "low": float(original["range"]["low"]) - spacing,
            "high": float(original["range"]["high"]),
        },
    }
    real_write_runtime = plane._write_runtime

    def crash_after_activation(row: dict) -> None:
        if (
            row.get("last_action") == "extend_range"
            and row.get("strategy_plan_id") != original["strategy_plan_id"]
        ):
            raise SimulatedProcessCrash("process exited before runtime repair")
        real_write_runtime(row)

    monkeypatch.setattr(plane, "_write_runtime", crash_after_activation)
    with pytest.raises(SimulatedProcessCrash):
        plane.control(
            cycle_id,
            "extend_range",
            request,
            market=market(),
            account=account_context(1_000_000.0),
            now="2026-07-05T01:42:00+00:00",
        )

    activated = plane.active_plan(cycle_id)
    assert activated is not None
    assert activated["strategy_plan_id"] != original["strategy_plan_id"]
    assert plane.runtime_state(cycle_id)["strategy_plan_id"] == original["strategy_plan_id"]

    monkeypatch.setattr(plane, "_write_runtime", real_write_runtime)
    repaired = plane.control(
        cycle_id,
        "extend_range",
        request,
        market=market(),
        account=account_context(1_000_000.0),
        now="2026-07-05T01:43:00+00:00",
    )

    assert repaired["idempotent"] is True
    runtime = plane.runtime_state(cycle_id)
    assert runtime["strategy_plan_id"] == activated["strategy_plan_id"]
    assert runtime["risk_decision_id"] == activated["risk_decision_id"]
    assert runtime["risk_policy_id"] == activated["risk_policy_id"]


def test_edge_adjustment_rearms_completed_staged_edge_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)
    real.cancel_orders(
        cycle_id,
        strategy_plan_id=original["strategy_plan_id"],
        ts="2026-07-05T01:41:00+00:00",
        reason="isolate_staged_rearm_test",
    )
    assert not [
        row
        for row in real.snapshot(cycle_id)["orders"]
        if row.get("state") == "accepted"
    ]

    class CrashDuringStage:
        name = real.name

        def __init__(self) -> None:
            self.submissions = 0

        def submit_order(self, command: dict) -> dict:
            self.submissions += 1
            if self.submissions == 2:
                raise SimulatedProcessCrash("process exited during edge staging")
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            return real.reconcile(requested_cycle)

    crashing = CrashDuringStage()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: crashing,
    )
    spacing = float(original["grid"]["spacing"])
    request = {
        "expected_strategy_plan_id": original["strategy_plan_id"],
        "range": {
            "low": float(original["range"]["low"]) - spacing,
            "high": float(original["range"]["high"]) + spacing,
        },
    }
    with pytest.raises(SimulatedProcessCrash):
        plane.control(
            cycle_id,
            "extend_range",
            request,
            market=market(),
            account=account_context(1_000_000.0),
            now="2026-07-05T01:42:00+00:00",
        )
    staged = next(
        row
        for row in load_json(plane._plans_path(cycle_id))
        if row.get("status") == "staging"
    )
    staged_order = next(
        row
        for row in real.snapshot(cycle_id)["orders"]
        if row.get("state") == "accepted"
        and row.get("strategy_plan_id") == staged["strategy_plan_id"]
    )
    real.process_market_event({
        "cycle_id": cycle_id,
        "ts_event": "2026-07-05T01:42:10+00:00",
        "price": staged_order["price"],
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    staged_position = next(
        row
        for row in real.snapshot(cycle_id)["positions"]
        if row.get("status") == "open"
        and row.get("strategy_plan_id") == staged["strategy_plan_id"]
    )
    real.process_market_event({
        "cycle_id": cycle_id,
        "ts_event": "2026-07-05T01:42:20+00:00",
        "price": staged_position["tp"],
        "fresh": True,
        "is_synthetic": False,
        "source": "canonical_test_feed",
    })
    assert not [
        row
        for row in real.snapshot(cycle_id)["positions"]
        if row.get("status") == "open"
        and row.get("strategy_plan_id") == staged["strategy_plan_id"]
    ]

    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: real,
    )
    resumed = plane.control(
        cycle_id,
        "extend_range",
        request,
        market=market(),
        account=account_context(1_000_000.0),
        now="2026-07-05T01:43:00+00:00",
    )

    rearmed = [
        row
        for row in real.snapshot(cycle_id)["orders"]
        if row.get("state") == "accepted"
        and row.get("strategy_plan_id") == staged["strategy_plan_id"]
        and row.get("side") == staged_order["side"]
        and row.get("price") == pytest.approx(staged_order["price"])
    ]
    assert len(rearmed) == 1
    assert rearmed[0]["order_id"] != staged_order["order_id"]
    assert resumed["plan"]["strategy_plan_id"] == staged["strategy_plan_id"]
    assert resumed["plan"]["status"] == "active"


def test_edge_plan_activation_swaps_same_cycle_statuses_in_one_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    current = plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=saved["proposal_id"],
    )
    staged = {
        **deepcopy(current),
        "strategy_plan_id": f"{current['strategy_plan_id']}-edge",
        "version": int(current["version"]) + 1,
        "status": "staging",
    }
    plane._write_plan(staged)
    real_write_json = strategy_control_plane_module.write_json
    plan_writes: list[list[dict]] = []

    def record_write(path: Path, rows: list[dict]) -> None:
        if path == plane._plans_path(cycle_id):
            plan_writes.append(deepcopy(rows))
        real_write_json(path, rows)

    monkeypatch.setattr(strategy_control_plane_module, "write_json", record_write)
    plane._activate_staged_range_plan(
        staged,
        expected_active_plan_id=current["strategy_plan_id"],
    )

    assert len(plan_writes) == 1
    statuses = {
        row["strategy_plan_id"]: row["status"]
        for row in load_json(plane._plans_path(cycle_id))
    }
    assert statuses[current["strategy_plan_id"]] == "superseded"
    assert statuses[staged["strategy_plan_id"]] == "active"


def test_edge_adjustment_post_cancel_failure_keeps_staged_edges_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)

    class DriftAfterCancel:
        name = real.name

        def __init__(self) -> None:
            self.reconciliations = 0

        def submit_order(self, command: dict) -> dict:
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            return real.snapshot(requested_cycle, **kwargs)

        def reconcile(self, requested_cycle: str) -> dict:
            self.reconciliations += 1
            if self.reconciliations >= 3:
                return {"status": "drift", "issues": [{"code": "injected"}]}
            return real.reconcile(requested_cycle)

    adapter = DriftAfterCancel()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(original["grid"]["spacing"])

    with pytest.raises(ValueError, match="paper ledger reconciliation failed"):
        plane.control(
            cycle_id,
            "extend_range",
            {
                "expected_strategy_plan_id": original["strategy_plan_id"],
                "range": {
                    "low": float(original["range"]["low"]) - spacing,
                    "high": float(original["range"]["high"]) - spacing,
                },
            },
            market=market(),
            account=account_context(1_000_000.0),
            now="2026-07-05T01:42:00+00:00",
        )

    partial = next(
        row
        for row in load_json(plane._plans_path(cycle_id))
        if row.get("status") == "partial"
    )
    snapshot = real.snapshot(cycle_id)
    assert any(
        row["state"] == "accepted"
        and row.get("strategy_plan_id") == partial["strategy_plan_id"]
        for row in snapshot["orders"]
    )
    assert plane.runtime_state(cycle_id)["actual_state"] == "error"


def test_nautilus_edge_adjustment_flushes_commands_without_injecting_market_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(
        cycle_id,
        "start",
        operating_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
        now="2026-07-05T01:40:00+00:00",
    )
    original = started["plan"]
    real = build_execution_engine_adapter(output)

    class NautilusCommandFlushAdapter:
        name = "nautilus_paper"

        def __init__(self) -> None:
            self.flushes = 0

        def submit_order(self, command: dict) -> dict:
            return real.submit_order(command)

        def cancel_orders(self, requested_cycle: str, **kwargs) -> dict:
            return real.cancel_orders(requested_cycle, **kwargs)

        def snapshot(self, requested_cycle: str, **kwargs) -> dict:
            snapshot = real.snapshot(requested_cycle, **kwargs)
            snapshot["engine"] = self.name
            return snapshot

        def reconcile(self, requested_cycle: str) -> dict:
            result = real.reconcile(requested_cycle)
            return {**result, "engine": self.name}

        def flush_commands(self, requested_cycle: str) -> dict:
            self.flushes += 1
            return {"status": "ok", "cycle_id": requested_cycle}

        def process_market_event(self, _event: dict) -> dict:
            raise AssertionError("edge adjustment must not inject a market event")

    adapter = NautilusCommandFlushAdapter()
    monkeypatch.setattr(
        strategy_control_plane_module,
        "build_configured_execution_engine_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    spacing = float(original["grid"]["spacing"])
    result = plane.control(
        cycle_id,
        "extend_range",
        {
            "expected_strategy_plan_id": original["strategy_plan_id"],
            "range": {
                "low": float(original["range"]["low"]) - spacing,
                "high": float(original["range"]["high"]),
            },
        },
        market=market(),
        account=account_context(1_000_000.0),
        now="2026-07-05T01:42:00+00:00",
    )

    assert result["runtime"]["actual_state"] == "running"
    assert adapter.flushes == 1


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
        safe_grid("neutral", "steady"),
        market=market(),
        account=account_context(),
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
            safe_grid("short", "aggressive"),
            market=market(),
            account=account_context(),
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
                safe_grid("short", "aggressive"),
                market=market(),
                account=account_context(),
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
