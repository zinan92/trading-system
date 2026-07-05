from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
from schemas.market_data import Bar
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_store import DualTrackPlanStore
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _plan(cycle_id: str = "2026-07-05_DAY", direction: str = "long") -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": direction,
        "range": {"low": 3950.0, "high": 4050.0},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3940.0, "confirm": "touch"}],
        "confidence": 7,
    }


def _bars() -> list[Bar]:
    start = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    closes = [4000.0, 3990.0, 4001.0]
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        rows.append(Bar("GOLD", "1m", (start + timedelta(minutes=i)).isoformat(), prev, max(prev, close), min(prev, close), close, 1, "test"))
        prev = close
    return rows


def test_invariant_1_api_does_not_serve_ai_plan_before_lock(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output)
    cycle_id = "2026-07-05_DAY"
    store.save_ai_plan(_plan(cycle_id, "short") | {"author": "ai"}, now="2026-07-04T23:00:00+00:00")

    response = dashboard_server.build_dualtrack_plan_response(cycle_id, output_root=output, as_of="2026-07-04T23:30:00+00:00")

    assert response["ai_plan_revealed"] is False
    assert "ai_plan" not in response


def test_fix_1_get_plan_ignores_network_as_of_inside_blind_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    store = DualTrackPlanStore(output)
    store.save_ai_plan(_plan(cycle_id, "short") | {"author": "ai"}, now="2026-07-04T23:00:00+00:00")
    original = dashboard_server.build_dualtrack_plan_response

    def wrapped(requested_cycle_id, *, output_root=None, as_of=None):
        assert requested_cycle_id == cycle_id
        assert as_of is None
        return original(requested_cycle_id, output_root=output, as_of="2026-07-04T23:30:00+00:00")

    monkeypatch.setattr(dashboard_server, "build_dualtrack_plan_response", wrapped)
    handler = object.__new__(dashboard_server.DashboardHandler)
    captured = {}
    handler._write_json = lambda status, payload: captured.update({"status": status, "payload": payload})
    handler._write_error = lambda status, code, message: captured.update({"status": status, "error": code, "message": message})

    handler._handle_dualtrack_plan_get(f"/api/dualtrack/plan/{cycle_id}", "as_of=2026-07-06T00%3A00%3A00%2B00%3A00")

    assert captured["status"] == 200
    assert captured["payload"]["ai_plan_revealed"] is False
    assert "ai_plan" not in captured["payload"]


def test_invariant_2_api_rejects_locked_plan_mutation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    dashboard_server.build_dualtrack_plan_post_response(_plan(cycle_id) | {"as_of": "2026-07-05T00:59:00+00:00"}, output_root=output)

    with pytest.raises(ValueError, match="locked"):
        dashboard_server.build_dualtrack_plan_post_response(_plan(cycle_id, "short") | {"as_of": "2026-07-05T00:59:30+00:00"}, output_root=output)


def test_invariant_3_api_current_reports_fail_closed_without_effective_plan(tmp_path: Path) -> None:
    response = dashboard_server.build_dualtrack_cycle_current_response(output_root=tmp_path / "outputs", as_of="2026-07-05T01:00:00+00:00")

    assert response["cycle_id"] == "2026-07-05_DAY"
    assert response["effective_plan_status"]["has_effective_plan"] is False
    assert response["effective_plan_status"]["machine_stands_down"] is True


def test_fix_2_cycle_current_hides_author_until_reveal_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    config = {**TEST_CONFIG, "plan_lock_deadline_min_before_cycle": -60}
    store = DualTrackPlanStore(output, config=config)
    store.save_ai_plan(_plan(cycle_id, "short") | {"author": "ai"}, now="2026-07-04T23:00:00+00:00")
    monkeypatch.setattr(dashboard_server, "dualtrack_config", lambda: config)

    blind = dashboard_server.build_dualtrack_cycle_current_response(output_root=output, as_of="2026-07-05T01:30:00+00:00")
    assert "author" not in blind["effective_plan_status"]
    assert blind["effective_plan_status"]["has_effective_plan"] is False
    assert blind["effective_plan_status"]["machine_stands_down"] is True

    store.save_human_plan(_plan(cycle_id, "long"), now="2026-07-05T01:45:00+00:00")
    revealed = dashboard_server.build_dualtrack_cycle_current_response(output_root=output, as_of="2026-07-05T01:45:01+00:00")
    assert revealed["effective_plan_status"]["author"] == "human"
    assert revealed["effective_plan_status"]["has_effective_plan"] is True


def test_invariant_4_api_intraday_machine_payload_is_pnl_only(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    runner = DualTrackMachineRunner(output, config=TEST_CONFIG)
    runner.run_plan(cycle_id, _plan(cycle_id), _bars(), prev_range=40.0, trend_gate_armed=False)

    response = dashboard_server.build_dualtrack_machine_response(cycle_id, output_root=output, as_of="2026-07-05T02:00:00+00:00")

    assert set(response) == {"realized_pnl", "unrealized_pnl", "layers"}
    assert {"fills", "orders", "entries", "inventory", "rungs", "price", "notional", "sl", "tp"}.isdisjoint(response)


def test_fix_1_get_machine_ignores_network_as_of_inside_intraday_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    runner = DualTrackMachineRunner(output, config=TEST_CONFIG)
    runner.run_plan(cycle_id, _plan(cycle_id), _bars(), prev_range=40.0, trend_gate_armed=False)
    original = dashboard_server.build_dualtrack_machine_response

    def wrapped(requested_cycle_id, *, output_root=None, as_of=None):
        assert requested_cycle_id == cycle_id
        assert as_of is None
        return original(requested_cycle_id, output_root=output, as_of="2026-07-05T02:00:00+00:00")

    monkeypatch.setattr(dashboard_server, "build_dualtrack_machine_response", wrapped)
    handler = object.__new__(dashboard_server.DashboardHandler)
    captured = {}
    handler._write_json = lambda status, payload: captured.update({"status": status, "payload": payload})
    handler._write_error = lambda status, code, message: captured.update({"status": status, "error": code, "message": message})

    handler._handle_dualtrack_machine_get(f"/api/dualtrack/machine/{cycle_id}", "as_of=2026-07-06T01%3A00%3A00%2B00%3A00")

    assert captured["status"] == 200
    assert set(captured["payload"]) == {"realized_pnl", "unrealized_pnl", "layers"}
    assert {"fills", "orders", "entries", "inventory", "rungs", "price", "notional", "sl", "tp"}.isdisjoint(captured["payload"])


def test_invariant_5_no_machine_intervention_endpoint_is_registered() -> None:
    forbidden = {"pause", "flatten", "override", "halt"}

    assert dashboard_server._DUALTRACK_POST_ENDPOINTS == {
        "/api/dualtrack/plan",
        "/api/dualtrack/orders",
        "/api/dualtrack/verdict",
    }
    assert all(not any(word in path for word in forbidden) for path in dashboard_server._DUALTRACK_POST_ENDPOINTS)


def test_invariant_11_no_production_runner_or_executor_imports_dualtrack() -> None:
    root = Path(__file__).resolve().parents[1]
    production_files = [
        root / "pipelines" / "bot.py",
        root / "services" / "multi_strategy_runner.py",
        root / "services" / "paper_executor.py",
        root / "services" / "broker_adapter.py",
        root / "services" / "direction_bias_gate.py",
    ]

    for path in production_files:
        assert "dualtrack" not in path.read_text(encoding="utf-8").lower()
