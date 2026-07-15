from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.dualtrack_grid_core import GridStop, simulate_explicit_grid
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_machine_plan import DualTrackMachinePlanner, _apply_range_floor
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore, validate_machine_plan, validate_plan
from services.journal_store import load_json, write_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    ts = datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return Bar(
        symbol="GOLD",
        timeframe="1m",
        timestamp=ts.isoformat(),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        provider="binance_usdm",
    )


def _machine_plan(cycle_id: str = "2026-07-10_NIGHT", direction: str = "long") -> dict:
    if direction == "neutral":
        return {
            "cycle_id": cycle_id,
            "direction": "neutral",
            "range": {"low": 4090.0, "high": 4130.0},
            "key_levels": [4100.0, 4120.0],
            "grid_orders": [
                {"side": "long", "entry": 4100.0, "take_profit": 4110.0, "weight": 0.5},
                {"side": "short", "entry": 4120.0, "take_profit": 4110.0, "weight": 0.5},
            ],
            "invalidation": [
                {"side": "below", "price": 4090.0, "confirm": "touch"},
                {"side": "above", "price": 4130.0, "confirm": "touch"},
            ],
            "confidence": 5,
            "rationale": "没有单边优势，在区间下沿做多、上沿做空。",
            "sources": [{"kind": "newsletter", "path": "/tmp/newsletter.md", "title": "黄金"}],
            "decision_mode": "ai_newsletter",
            "source": "machine_ai_newsletter",
            "status": "locked",
        }
    if direction == "short":
        return {
            "cycle_id": cycle_id,
            "direction": "short",
            "range": {"low": 4090.0, "high": 4130.0},
            "key_levels": [4120.0, 4110.0],
            "grid_orders": [
                {"entry": 4120.0, "take_profit": 4110.0, "weight": 0.5},
                {"entry": 4110.0, "take_profit": 4100.0, "weight": 0.5},
            ],
            "invalidation": [{"side": "above", "price": 4130.0, "confirm": "touch"}],
            "confidence": 7,
            "rationale": "反弹到网格位分批做空。",
            "sources": [{"kind": "newsletter", "path": "/tmp/newsletter.md", "title": "黄金"}],
            "decision_mode": "ai_newsletter",
            "source": "machine_ai_newsletter",
            "status": "locked",
        }
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 4090.0, "high": 4130.0},
        "key_levels": [4100.0, 4110.0],
        "grid_orders": [
            {"entry": 4100.0, "take_profit": 4110.0, "weight": 0.5},
            {"entry": 4110.0, "take_profit": 4120.0, "weight": 0.5},
        ],
        "invalidation": [{"side": "below", "price": 4090.0, "confirm": "touch"}],
        "confidence": 7,
        "rationale": "回踩网格位分批做多。",
        "sources": [{"kind": "newsletter", "path": "/tmp/newsletter.md", "title": "黄金"}],
        "decision_mode": "ai_newsletter",
        "source": "machine_ai_newsletter",
        "status": "locked",
    }


def test_machine_plan_contract_preserves_explicit_ai_decision_fields() -> None:
    normalized = validate_plan(
        _machine_plan(),
        author="ai",
        status="locked",
        now="2026-07-10T12:55:00+00:00",
    )

    assert normalized["direction"] == "long"
    assert normalized["range"] == {"low": 4090.0, "high": 4130.0}
    assert normalized["grid_orders"][0] == {"entry": 4100.0, "take_profit": 4110.0, "weight": 0.5}
    assert normalized["rationale"].startswith("回踩")
    assert normalized["sources"][0]["kind"] == "newsletter"
    assert normalized["decision_mode"] == "ai_newsletter"


def test_neutral_machine_plan_requires_bilateral_grid() -> None:
    plan = _machine_plan(direction="neutral")
    normalized = validate_machine_plan(plan, now="2026-07-10T12:55:00+00:00")

    assert {order["side"] for order in normalized["grid_orders"]} == {"long", "short"}

    invalid = deepcopy(plan)
    invalid["grid_orders"] = [invalid["grid_orders"][0]]
    try:
        validate_machine_plan(invalid, now="2026-07-10T12:55:00+00:00")
    except ValueError as exc:
        assert "both long and short" in str(exc)
    else:
        raise AssertionError("one-sided neutral grid must be rejected")


def test_machine_planner_reads_newsletter_and_persists_independent_ai_plan(tmp_path: Path) -> None:
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    newsletter = newsletter_root / "2026-07-10-finance-daily-newsletter.md"
    newsletter.write_text("## 黄金\n方向观望，日内偏多，区间 4,050-4,150。\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def decide(prompt: str) -> dict:
        captured["prompt"] = prompt
        return _machine_plan()

    planner = DualTrackMachinePlanner(
        tmp_path / "outputs",
        config=TEST_CONFIG,
        newsletter_root=newsletter_root,
        decision_provider=decide,
    )
    plan = planner.ensure_plan(
        "2026-07-10_NIGHT",
        bars=[_bar(0, 4115.0, 4118.0, 4110.0, 4114.0)],
        prev_cycle_range=40.0,
        as_of="2026-07-10T12:55:00+00:00",
    )

    assert "方向观望" in captured["prompt"]
    assert plan["author"] == "ai"
    assert plan["status"] == "locked"
    assert plan["source"] == "machine_ai_newsletter"
    assert plan["sources"][0]["path"] == str(newsletter)
    assert DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG).machine_plan(plan["cycle_id"])["direction"] == "long"


def test_machine_planner_transparently_widens_range_below_active_cycle_floor() -> None:
    decision = _machine_plan(direction="neutral")
    context = {
        "method": "median_completed_non_weekend_12h_range",
        "reference_range": 60.0,
        "minimum_plan_range": 60.0,
        "excluded_samples": [{"reason": "weekend_low_liquidity"}],
    }

    adjusted = _apply_range_floor(decision, context)

    assert adjusted["range"] == {"low": 4080.0, "high": 4140.0}
    assert adjusted["invalidation"] == [
        {"side": "below", "price": 4080.0, "confirm": "touch"},
        {"side": "above", "price": 4140.0, "confirm": "touch"},
    ]
    assert adjusted["range_adjustment"]["original_range"]["width"] == 40.0
    assert adjusted["range_adjustment"]["adjusted_range"]["width"] == 60.0
    assert adjusted["range_adjustment"]["excluded_weekend_sample_count"] == 1


def test_machine_planner_consumes_previous_four_dimension_review(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text("## 黄金\n区间震荡。\n", encoding="utf-8")
    write_json(output / "dualtrack" / "attribution" / "2026-07-10_DAY.json", [{
        "cycle_id": "2026-07-10_DAY",
        "tracks": {"machine": {"trade_count": 2, "realized_pnl": -12.0}},
        "plan_grades": {"ai": {"graded": True, "hit": False}},
        "machine_review": {
            "decision": "long",
            "realized_direction": "short",
            "key_level_review": {"summary": "1/4 个关键位被触及"},
            "signal_review": {"summary": "关键位到价直接成交"},
            "tpsl_review": {"summary": "平均 R 0.80"},
            "recorded_realized_pnl": -12.0,
            "summary": "方向错误且止盈过窄。",
        },
    }])
    captured: dict[str, str] = {}

    def decide(prompt: str) -> dict:
        captured["prompt"] = prompt
        return _machine_plan() | {"review_adjustment": "降低方向置信度，扩大止盈宽度。"}

    plan = DualTrackMachinePlanner(
        output,
        config=TEST_CONFIG,
        newsletter_root=newsletter_root,
        decision_provider=decide,
    ).ensure_plan(
        "2026-07-10_NIGHT",
        bars=[_bar(0, 4115.0, 4118.0, 4110.0, 4114.0)],
        prev_cycle_range=40.0,
        as_of="2026-07-10T12:55:00+00:00",
    )

    assert "方向错误且止盈过窄" in captured["prompt"]
    assert "平均 R 0.80" in captured["prompt"]
    assert plan["previous_review_cycle_id"] == "2026-07-10_DAY"
    assert plan["review_adjustment"] == "降低方向置信度，扩大止盈宽度。"
    trace = load_json(output / "dualtrack" / "planning" / "2026-07-10_NIGHT_machine.json")[-1]
    assert trace["previous_review"]["status"] == "available"


def test_machine_planner_carries_one_structured_review_change_into_next_cycle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text("## 黄金\n区间震荡。\n", encoding="utf-8")
    change_id = "2026-07-10_DAY:range:01"
    write_json(output / "dualtrack" / "attribution" / "2026-07-10_DAY.json", [{
        "cycle_id": "2026-07-10_DAY",
        "tracks": {"machine": {"trade_count": 2, "realized_pnl": -12.0}},
        "machine_review": {
            "schema_version": "dualtrack-machine-review-v3",
            "decision": "long",
            "realized_regime": "short",
            "key_level_review": {"summary": "1/4 个关键位被触及"},
            "signal_review": {"summary": "关键位到价直接成交"},
            "tpsl_review": {"summary": "平均 R 0.80"},
            "recorded_realized_pnl": -12.0,
            "summary": "区间上边界被突破。",
            "next_iteration": {
                "status": "proposed",
                "change_id": change_id,
                "mode": "paper_challenger",
                "dimension": "range",
                "keep": "保留双边网格逻辑",
                "change": "只测试更稳健的区间宽度",
                "expected_metric": "range_breach_rate",
                "validation_rule": "至少 10 个完整周期且 30 笔交易",
                "promotion_gate": {"minimum_cycles": 10, "minimum_trades": 30, "preferred_trades": 100},
            },
        },
    }])

    def decide(_prompt: str) -> dict:
        return _machine_plan() | {
            "review_adjustment": "保留双边网格，本周期只测试更稳健的区间宽度。",
            "review_change": {
                "change_id": change_id,
                "mode": "paper_challenger",
                "dimension": "range",
                "summary": "只测试更稳健的区间宽度",
                "expected_metric": "range_breach_rate",
            },
        }

    plan = DualTrackMachinePlanner(
        output,
        config=TEST_CONFIG,
        newsletter_root=newsletter_root,
        decision_provider=decide,
    ).ensure_plan(
        "2026-07-10_NIGHT",
        bars=[_bar(0, 4115.0, 4118.0, 4110.0, 4114.0)],
        prev_cycle_range=40.0,
        as_of="2026-07-10T12:55:00+00:00",
    )

    assert plan["previous_review_cycle_id"] == "2026-07-10_DAY"
    assert plan["review_change"] == {
        "change_id": change_id,
        "mode": "paper_challenger",
        "dimension": "range",
        "summary": "只测试更稳健的区间宽度",
        "expected_metric": "range_breach_rate",
    }


def test_legacy_neutral_plan_repair_starts_at_revision_time(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text("## 黄金\n区间震荡。\n", encoding="utf-8")
    legacy = _machine_plan(direction="neutral")
    legacy["grid_orders"] = []
    legacy["invalidation"] = []
    write_json(output / "dualtrack" / "plans" / "2026-07-10_NIGHT_ai.json", [legacy])

    planner = DualTrackMachinePlanner(
        output,
        config=TEST_CONFIG,
        newsletter_root=newsletter_root,
        decision_provider=lambda _prompt: _machine_plan(direction="neutral"),
    )
    repaired = planner.repair_legacy_neutral_plan(
        "2026-07-10_NIGHT",
        bars=[_bar(5, 4110.0, 4112.0, 4108.0, 4111.0)],
        prev_cycle_range=40.0,
        as_of="2026-07-10T13:05:00+00:00",
    )

    assert repaired["execution_start"] == "2026-07-10T13:05:00+00:00"
    assert repaired["revision_reason"] == "neutral_bilateral_grid_contract_fix"
    assert {order["side"] for order in repaired["grid_orders"]} == {"long", "short"}
    audit = load_json(output / "dualtrack" / "audit" / "2026-07-10_NIGHT.json")
    assert audit[-1]["event"] == "machine_plan_relocked"


def test_failed_legacy_neutral_repair_preserves_locked_plan(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text("## 黄金\n区间震荡。\n", encoding="utf-8")
    legacy = _machine_plan(direction="neutral")
    legacy["grid_orders"] = []
    legacy["invalidation"] = []
    plan_path = output / "dualtrack" / "plans" / "2026-07-10_NIGHT_ai.json"
    write_json(plan_path, [legacy])

    def fail(_prompt: str) -> dict:
        raise RuntimeError("planner unavailable")

    planner = DualTrackMachinePlanner(
        output,
        config=TEST_CONFIG,
        newsletter_root=newsletter_root,
        decision_provider=fail,
    )
    preserved = planner.repair_legacy_neutral_plan(
        "2026-07-10_NIGHT",
        bars=[_bar(5, 4110.0, 4112.0, 4108.0, 4111.0)],
        prev_cycle_range=40.0,
        as_of="2026-07-10T13:05:00+00:00",
    )

    assert preserved["grid_orders"] == []
    assert load_json(plan_path)[-1] == legacy
    audit = load_json(output / "dualtrack" / "audit" / "2026-07-10_NIGHT.json")
    assert audit[-1]["event"] == "machine_plan_revision_failed_preserved"


def test_machine_runner_never_inherits_locked_human_plan(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_human_plan(
        {
            "cycle_id": "2026-07-10_NIGHT",
            "direction": "long",
            "range": {"low": 4090.0, "high": 4130.0},
            "key_levels": [4100.0],
            "invalidation": [{"side": "below", "price": 4090.0, "confirm": "touch"}],
            "confidence": 9,
        },
        now="2026-07-10T12:55:00+00:00",
    )
    store.save_ai_plan(_machine_plan(direction="short"), now="2026-07-10T12:56:00+00:00")
    bars = [
        _bar(0, 4115.0, 4121.0, 4114.0, 4120.0),
        _bar(1, 4120.0, 4121.0, 4109.0, 4110.0),
    ]

    state = DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        "2026-07-10_NIGHT",
        bars,
        prev_range=40.0,
        as_of="2026-07-10T13:02:00+00:00",
        finalize=False,
    )
    fills = load_json(output / "dualtrack" / "fills" / "2026-07-10_NIGHT_machine.json")

    assert state["effective_plan_author"] == "ai"
    assert {fill["side"] for fill in fills if fill["event"] == "entry"} == {"sell"}
    assert any(fill["side"] == "buy" and fill["event"] == "target" for fill in fills)


def test_explicit_grid_keeps_open_position_intraday_and_flattens_only_at_close() -> None:
    bars = [
        _bar(0, 4110.0, 4111.0, 4099.0, 4101.0),
        _bar(1, 4101.0, 4105.0, 4100.0, 4104.0),
    ]
    kwargs = {
        "cycle_id": "2026-07-10_NIGHT",
        "bars": bars,
        "direction": 1,
        "orders": [{"entry": 4100.0, "take_profit": 4120.0, "weight": 1.0}],
        "stop": GridStop(side="below", price=4090.0, confirm="touch"),
        "rung_notional": 10_000.0,
        "cost_per_side_bp": 0.5,
    }

    intraday = simulate_explicit_grid(**kwargs, finalize=False)
    closed = simulate_explicit_grid(**kwargs, finalize=True)

    assert [fill["event"] for fill in intraday.fills] == ["entry"]
    assert intraday.fills[0]["position_status"] == "open"
    assert [fill["event"] for fill in closed.fills] == ["entry", "flatten"]
    assert closed.fills[-1]["price"] == 4104.0


def test_explicit_grid_stop_closes_open_rung_at_declared_invalidation() -> None:
    result = simulate_explicit_grid(
        cycle_id="2026-07-10_NIGHT",
        bars=[
            _bar(0, 4110.0, 4111.0, 4099.0, 4101.0),
            _bar(1, 4101.0, 4102.0, 4088.0, 4089.0),
        ],
        direction=1,
        orders=[{"entry": 4100.0, "take_profit": 4120.0, "weight": 1.0}],
        stop=GridStop(side="below", price=4090.0, confirm="touch"),
        rung_notional=10_000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )

    assert [fill["event"] for fill in result.fills] == ["entry", "stop"]
    assert result.fills[-1]["price"] == 4090.0
    assert result.stop_hit is True


def test_neutral_machine_grid_executes_both_sides(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_ai_plan(_machine_plan(direction="neutral"), now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4110.0, 4111.0, 4099.0, 4101.0),
        _bar(1, 4101.0, 4111.0, 4100.0, 4110.0),
        _bar(2, 4110.0, 4121.0, 4109.0, 4120.0),
        _bar(3, 4120.0, 4121.0, 4109.0, 4110.0),
    ]

    state = DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=False,
    )
    fills = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert state["layers"] == ["decision:ai_independent", "grid:neutral_bilateral_traded"]
    assert {(fill["event"], fill["side"]) for fill in fills} == {
        ("entry", "buy"),
        ("target", "sell"),
        ("entry", "sell"),
        ("target", "buy"),
    }
    assert {fill["layer"] for fill in fills} == {"ai_grid_long", "ai_grid_short"}


def test_neutral_range_touch_is_recorded_once_and_pauses_both_entry_sides(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_ai_plan(_machine_plan(direction="neutral"), now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4110.0, 4111.0, 4105.0, 4110.0),
        _bar(1, 4110.0, 4131.0, 4110.0, 4130.0),
    ]
    runner = DualTrackMachineRunner(output, config=TEST_CONFIG)

    first = runner.run_effective_plan(cycle_id, bars, prev_range=40.0, finalize=False)
    second = runner.run_effective_plan(cycle_id, bars, prev_range=40.0, finalize=False)
    audit = load_json(output / "dualtrack" / "audit" / f"{cycle_id}.json")
    breach_events = [row for row in audit if row.get("event") == "machine_plan_range_breached"]

    assert first["range_observation"]["status"] == "high_breached"
    assert first["range_observation"]["eligible_sides"] == []
    assert first["range_observation"]["policy"] == "pause_new_entries_and_reassess"
    assert second["range_observation"] == first["range_observation"]
    assert len(breach_events) == 1
    assert breach_events[0]["detail"]["boundary"] == 4130.0


def test_neutral_range_touch_blocks_later_entries_from_the_old_range(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_ai_plan(_machine_plan(direction="neutral"), now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4115.0, 4121.0, 4114.0, 4120.0),
        _bar(1, 4129.0, 4131.0, 4128.0, 4130.0),
        _bar(2, 4130.0, 4130.0, 4099.0, 4100.0),
    ]

    DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=False,
    )
    fills = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert [(fill["event"], fill["side"], fill["price"]) for fill in fills] == [
        ("entry", "sell", 4120.0),
        ("stop", "buy", 4130.0),
    ]


def test_live_prefix_refresh_preserves_namespaced_recovery_replay(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_ai_plan(_machine_plan(direction="neutral"), now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4110.0, 4111.0, 4099.0, 4101.0),
        _bar(1, 4101.0, 4111.0, 4100.0, 4110.0),
        _bar(2, 4110.0, 4121.0, 4109.0, 4120.0),
        _bar(3, 4120.0, 4121.0, 4109.0, 4110.0),
    ]
    runner = DualTrackMachineRunner(output, config=TEST_CONFIG)
    runner.run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=False,
        execution_provenance={
            "origin": "recovery_replay",
            "classified_at": "2026-07-10T13:04:00+00:00",
            "live_observed_until": None,
            "reason": "test_recovery",
            "recovery_id": "neutral-contract-fix",
        },
    )

    fills_path = output / "dualtrack" / "fills" / f"{cycle_id}_machine.json"
    recovery = load_json(fills_path)
    assert recovery
    assert all(fill["fill_id"].startswith("recovery_replay:neutral-contract-fix:") for fill in recovery)

    runner._persist_fills(cycle_id, [])
    assert load_json(fills_path) == recovery

    runner.run_effective_plan(cycle_id, bars, prev_range=40.0, finalize=False)
    combined = load_json(fills_path)
    assert {fill["execution_origin"] for fill in combined} == {"live_observed", "recovery_replay"}
    assert len({fill["fill_id"] for fill in combined}) == len(combined)

    runner.run_effective_plan(cycle_id, bars, prev_range=40.0, finalize=False)
    assert load_json(fills_path) == combined


def test_revised_neutral_plan_does_not_backfill_pre_revision_bars(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    plan = _machine_plan(direction="neutral")
    plan["execution_start"] = "2026-07-10T13:02:00+00:00"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(plan, now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4110.0, 4111.0, 4099.0, 4101.0),
        _bar(1, 4101.0, 4111.0, 4100.0, 4110.0),
        _bar(2, 4110.0, 4115.0, 4105.0, 4112.0),
    ]

    DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=False,
    )

    assert load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json") == []


def test_machine_review_is_written_for_neutral_untouched_grid(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    config = deepcopy(TEST_CONFIG)
    store = DualTrackPlanStore(output, config=config)
    cycle_id = "2026-07-10_NIGHT"
    store.save_ai_plan(_machine_plan(direction="neutral"), now="2026-07-10T12:55:00+00:00")
    bars = [
        _bar(0, 4110.0, 4114.0, 4108.0, 4112.0),
        _bar(1, 4112.0, 4116.0, 4109.0, 4111.0),
    ]
    DualTrackMachineRunner(output, config=config).run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=True,
    )

    attribution = DualTrackScorer(output, config=config).close_cycle(cycle_id, bars)
    review = load_json(output / "dualtrack" / "reviews" / f"{cycle_id}_machine.json")[-1]

    assert review["decision"] == "neutral"
    assert review["fill_count"] == 0
    assert review["no_trade_reason"] == "planned_levels_not_touched"
    assert review["completed"] is True
    assert review["signal_review"]["logic"] == "price_touch"
    assert review["key_level_review"]["planned_count"] > 0
    assert review["tpsl_review"]["valid_geometry_count"] > 0
    assert review["schema_version"] == "dualtrack-machine-review-v3"
    assert review["market_review"]["realized_regime"] == "neutral"
    assert review["direction_review"]["verdict"] == "keep"
    assert review["evidence"]["status"] == "complete"
    assert review["next_iteration"]["max_changes"] == 1
    assert review["next_iteration"]["promotion_gate"]["minimum_cycles"] == 10
    assert review["next_iteration"]["promotion_gate"]["minimum_trades"] == 30
    assert review["next_iteration"]["promotion_gate"]["auto_promote"] is False
    assert attribution["machine_review"] == review
    assert attribution["human_review"]["completed"] is True


def test_machine_review_proposes_only_one_shadow_change_after_range_breach(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(
        _machine_plan(cycle_id, direction="neutral"),
        now="2026-07-10T12:55:00+00:00",
    )
    bars = [
        _bar(0, 4110.0, 4120.0, 4105.0, 4115.0),
        _bar(1, 4115.0, 4145.0, 4110.0, 4140.0),
    ]

    review = DualTrackScorer(output, config=TEST_CONFIG).close_cycle(cycle_id, bars)["machine_review"]

    assert review["market_review"]["realized_regime"] == "long"
    assert review["range_review"]["verdict"] == "adjust"
    assert review["next_iteration"]["status"] == "proposed"
    assert review["next_iteration"]["mode"] == "paper_challenger"
    assert review["next_iteration"]["dimension"] == "range"
    assert review["next_iteration"]["expected_metric"] == "range_breach_rate"
    assert review["next_iteration"]["change_id"] == f"{cycle_id}:range:01"


def test_human_review_is_advisory_and_never_auto_applies_a_change(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(
        _machine_plan(cycle_id),
        now="2026-07-10T12:55:00+00:00",
    )
    bars = [
        _bar(0, 4110.0, 4114.0, 4108.0, 4112.0),
        _bar(1, 4112.0, 4116.0, 4109.0, 4111.0),
    ]

    review = DualTrackScorer(output, config=TEST_CONFIG).close_cycle(cycle_id, bars)["human_review"]

    assert review["schema_version"] == "dualtrack-human-review-v3"
    assert review["next_iteration"]["mode"] == "human_advisory"
    assert review["next_iteration"]["auto_apply"] is False
    assert review["next_iteration"]["max_changes"] == 1


def test_machine_review_explains_directional_plan_with_untouched_levels(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(
        _machine_plan(cycle_id),
        now="2026-07-10T12:55:00+00:00",
    )
    bars = [
        _bar(0, 4125.0, 4128.0, 4122.0, 4126.0),
        _bar(1, 4126.0, 4129.0, 4121.0, 4124.0),
    ]
    DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=40.0,
        finalize=True,
    )

    DualTrackScorer(output, config=TEST_CONFIG).close_cycle(cycle_id, bars)
    review = load_json(output / "dualtrack" / "reviews" / f"{cycle_id}_machine.json")[-1]

    assert review["decision"] == "long"
    assert review["fill_count"] == 0
    assert review["no_trade_reason"] == "planned_levels_not_touched"
    assert review["completed"] is True
