from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.dualtrack_grid_core import GridStop, simulate_explicit_grid
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_machine_plan import DualTrackMachinePlanner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore, validate_plan
from services.journal_store import load_json
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
            "grid_orders": [],
            "invalidation": [],
            "confidence": 5,
            "rationale": "区间内没有足够方向优势，本周期观察不下单。",
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


def test_machine_review_is_written_for_neutral_zero_fill_cycle(tmp_path: Path) -> None:
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
    assert review["no_trade_reason"] == "neutral_decision"
    assert review["completed"] is True
    assert attribution["machine_review"] == review


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
