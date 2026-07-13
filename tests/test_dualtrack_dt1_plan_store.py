from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_clock import cycle_window
from services.dualtrack_store import DualTrackPlanStore, validate_plan
from services.journal_store import load_json, write_json


def _plan(cycle_id: str = "2026-07-05_DAY", direction: str = "long") -> dict:
    if direction == "short":
        return {
            "cycle_id": cycle_id,
            "direction": direction,
            "range": {"low": 4148.0, "high": 4210.0},
            "key_levels": [4168.0, 4180.0, 4200.0],
            "invalidation": [{"side": "above", "price": 4210.0, "confirm": "close_1m"}],
            "confidence": 7,
        }
    return {
        "cycle_id": cycle_id,
        "direction": direction,
        "range": {"low": 4148.0, "high": 4210.0},
        "key_levels": [4168.0, 4180.0, 4200.0],
        "invalidation": [{"side": "below", "price": 4148.0, "confirm": "close_1m"}],
        "confidence": 7,
    }


def test_dt1_cycle_math_uses_utc_internal_and_beijing_cycle_date() -> None:
    assert cycle_window("2026-07-05T00:59:00+00:00").cycle_id == "2026-07-04_NIGHT"
    assert cycle_window("2026-07-05T01:00:00+00:00").cycle_id == "2026-07-05_DAY"
    assert cycle_window("2026-07-05T12:59:00+00:00").cycle_id == "2026-07-05_DAY"
    assert cycle_window("2026-07-05T13:00:00+00:00").cycle_id == "2026-07-05_NIGHT"
    window = cycle_window("2026-07-05T13:00:00+00:00")
    assert window.start.isoformat() == "2026-07-05T13:00:00+00:00"
    assert window.end.isoformat() == "2026-07-06T01:00:00+00:00"


def test_invariant_1_blind_api_response_hides_ai_plan_before_human_lock(tmp_path: Path) -> None:
    store = DualTrackPlanStore(tmp_path / "outputs")
    store.save_ai_plan(_plan(author_cycle := "2026-07-05_DAY") | {"author": "ai"}, now="2026-07-04T23:00:00+00:00")

    response = store.plan_response(author_cycle, as_of="2026-07-04T23:30:00+00:00")

    assert response["human_plan"] is None
    assert response["ai_plan_revealed"] is False
    assert "ai_plan" not in response


def test_invariant_1_ai_plan_reveals_after_human_lock(tmp_path: Path) -> None:
    store = DualTrackPlanStore(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    store.save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-04T23:00:00+00:00")
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")

    response = store.plan_response(cycle_id, as_of="2026-07-05T00:59:01+00:00")

    assert response["ai_plan_revealed"] is True
    assert response["ai_plan"]["author"] == "ai"


def test_invariant_2_locked_plan_is_immutable_and_audit_logged(tmp_path: Path) -> None:
    store = DualTrackPlanStore(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")

    with pytest.raises(ValueError, match="locked"):
        store.save_human_plan(_plan(cycle_id, "short"), now="2026-07-05T00:59:30+00:00")

    audit = load_json(tmp_path / "outputs" / "dualtrack" / "audit" / f"{cycle_id}.json")
    assert audit[-1]["event"] == "locked_mutation_rejected"


def test_invariant_3_precedence_human_then_ai_then_fail_closed(tmp_path: Path) -> None:
    store = DualTrackPlanStore(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    store.save_human_plan(_plan(cycle_id, "long"), now="2026-07-05T00:59:00+00:00")
    store.save_ai_plan(_plan(cycle_id, "short") | {"author": "ai"}, now="2026-07-05T00:50:00+00:00")
    assert store.effective_plan(cycle_id, as_of="2026-07-05T01:00:00+00:00")["effective_author"] == "human"

    ai_only = "2026-07-05_NIGHT"
    store.save_ai_plan(_plan(ai_only, "short") | {"author": "ai"}, now="2026-07-05T12:50:00+00:00")
    assert store.effective_plan(ai_only, as_of="2026-07-05T13:00:00+00:00")["effective_author"] == "ai"

    assert store.effective_plan("2026-07-06_DAY", as_of="2026-07-06T01:00:00+00:00") is None


def test_ai_plan_from_market_view_keeps_long_target_out_of_invalidation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(output / "market_views" / "2026-07-05.json", [{
        "run_date": "2026-07-05",
        "direction_bias": "long_bias",
        "direction_score": 65,
        "key_levels": [
            "4155-4160：回调后重新找多的位置。",
            "4210：当前上方目标位，回调后做多的目标。",
        ],
        "expiry": {
            "status": "active",
            "expires_at": "2026-07-06T01:00:00+00:00",
            "expire_below": 4155.0,
            "expire_above": 4210.0,
            "target_price": 4210.0,
        },
    }])
    store = DualTrackPlanStore(output)

    plan = store.ensure_ai_plan(
        "2026-07-05_DAY",
        cycle_open=4182.93,
        prev_cycle_range=12.26,
        now="2026-07-05T01:00:00+00:00",
    )

    assert plan is not None
    assert plan["range"] == {"low": 4155.0, "high": None}
    assert plan["invalidation"] == [{"side": "below", "price": 4155.0, "confirm": "touch"}]
    assert 4155.0 in plan["key_levels"]
    assert 4210.0 in plan["key_levels"]


def test_human_plan_import_from_market_view_is_draft_after_lock_deadline(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(output / "market_views" / "2026-07-05.json", [{
        "run_date": "2026-07-05",
        "direction_bias": "long_bias",
        "direction_score": 65,
        "key_levels": ["4155-4160", "4210"],
        "expiry": {
            "status": "active",
            "expires_at": "2026-07-06T01:00:00+00:00",
            "expire_below": 4155.0,
            "expire_above": 4210.0,
        },
    }])
    store = DualTrackPlanStore(output)

    plan = store.ensure_human_plan_from_market_view(
        "2026-07-05_DAY",
        cycle_open=4182.93,
        prev_cycle_range=12.26,
        now="2026-07-05T02:00:00+00:00",
    )

    assert plan is not None
    assert plan["author"] == "human"
    assert plan["source"] == "obsidian"
    assert plan["status"] == "draft"
    assert plan["locked_at"] is None
    assert plan["range"] == {"low": 4155.0, "high": None}
    assert plan["source_run_date"] == "2026-07-05"


def test_human_plan_does_not_fall_back_to_stale_current_market_view(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(output / "market_views" / "current.json", [{
        "run_date": "2026-07-04",
        "direction_bias": "long_bias",
        "direction_score": 65,
        "key_levels": ["4155", "4210"],
        "expiry": {"status": "active", "expires_at": "2026-07-06T01:00:00+00:00"},
    }])
    store = DualTrackPlanStore(output)

    plan = store.ensure_human_plan_from_market_view(
        "2026-07-05_DAY",
        cycle_open=4182.93,
        prev_cycle_range=12.26,
        now="2026-07-05T01:00:00+00:00",
    )

    assert plan is None
    assert not (output / "dualtrack" / "plans" / "2026-07-05_DAY_human.json").exists()
    audit = load_json(output / "dualtrack" / "audit" / "2026-07-05_DAY.json")
    assert audit[-1]["event"] == "human_plan_import_failed"
    assert audit[-1]["detail"]["reason"] == "market_view_missing_for_date"


def test_human_plan_rejects_expired_same_day_market_view(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(output / "market_views" / "2026-07-05.json", [{
        "run_date": "2026-07-05",
        "direction_bias": "long_bias",
        "direction_score": 65,
        "key_levels": ["4155", "4210"],
        "expiry": {"status": "active", "expires_at": "2026-07-05T00:30:00+00:00"},
    }])
    store = DualTrackPlanStore(output)

    plan = store.ensure_human_plan_from_market_view(
        "2026-07-05_DAY",
        cycle_open=4182.93,
        prev_cycle_range=12.26,
        now="2026-07-05T01:00:00+00:00",
    )

    assert plan is None
    audit = load_json(output / "dualtrack" / "audit" / "2026-07-05_DAY.json")
    assert audit[-1]["detail"]["reason"] == "market_view_expired"


def test_invariant_8_structured_invalidation_only() -> None:
    with pytest.raises(ValueError, match="invalidation"):
        validate_plan(_plan() | {"invalidation": "breaks 4150"}, author="human", status="locked")
    with pytest.raises(ValueError, match="side"):
        validate_plan(_plan() | {"invalidation": [{"side": "跌穿", "price": 4148, "confirm": "touch"}]}, author="human", status="locked")
    normalized = validate_plan(
        _plan() | {"invalidation": [{"side": "跌破", "price": 4148, "confirm": "触及"}]},
        author="human",
        status="locked",
    )
    assert normalized["invalidation"] == [{"side": "below", "price": 4148.0, "confirm": "touch"}]


def test_invariant_10_plan_store_uses_atomic_write_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def spy(path, rows):
        calls.append(path)
        write_json(path, rows)

    monkeypatch.setattr("services.dualtrack_store.write_json", spy)
    store = DualTrackPlanStore(tmp_path / "outputs")

    store.save_human_plan(_plan(), now="2026-07-05T00:59:00+00:00")

    assert any(path.name == "2026-07-05_DAY_human.json" for path in calls)
    assert any(path.parent.name == "audit" for path in calls)
