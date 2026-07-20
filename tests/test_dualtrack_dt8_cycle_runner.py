from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipelines.dualtrack_cycle_runner as cycle_runner_module
from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from schemas.market_data import Bar
from services.dualtrack_config import base_rung_notional
from services.dualtrack_clock import parse_utc
from services.dualtrack_grid_core import GridStop, simulate_conditional_grid
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_machine_plan import DualTrackMachinePlanner
from services.dualtrack_store import DualTrackPlanStore, validate_plan
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.strategy_proposal_port import StrategyProposalRequest
from services.strategy_proposal_registry import (
    StrategyProposalPluginRegistry,
    UnknownStrategyProposalPlugin,
)
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _bar(
    ts: datetime,
    open_: float,
    close: float,
    *,
    symbol: str = "GOLD",
    provider: str = "test",
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe="1m",
        timestamp=ts.isoformat(),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1,
        provider=provider,
    )


def _seed_bars(
    store: MarketStore,
    start: datetime,
    closes: list[float],
    *,
    symbol: str = "GOLD",
    provider: str = "test",
) -> list[Bar]:
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(_bar(start + timedelta(minutes=index), previous, close, symbol=symbol, provider=provider))
        previous = close
    store.upsert_bars(rows)
    return rows


def _plan(cycle_id: str, direction: str = "long") -> dict:
    if direction == "flat":
        return {"cycle_id": cycle_id, "direction": "flat", "confidence": 5}
    if direction == "short":
        return {
            "cycle_id": cycle_id,
            "direction": "short",
            "range": {"high": 4040.0},
            "key_levels": [3992.0],
            "invalidation": [{"side": "above", "price": 4040.0, "confirm": "touch"}],
            "confidence": 7,
        }
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 3960.0, "high": None},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3960.0, "confirm": "touch"}],
        "confidence": 7,
    }


def _neutral_machine_plan(cycle_id: str, *, low: float = 4090.0, high: float = 4130.0) -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": "neutral",
        "range": {"low": low, "high": high},
        "key_levels": [4100.0, 4120.0],
        "grid_orders": [
            {"side": "long", "entry": 4100.0, "take_profit": 4110.0, "weight": 0.5},
            {"side": "short", "entry": 4120.0, "take_profit": 4110.0, "weight": 0.5},
        ],
        "invalidation": [
            {"side": "below", "price": low, "confirm": "touch"},
            {"side": "above", "price": high, "confirm": "touch"},
        ],
        "confidence": 5,
        "rationale": "没有单边优势，在区间下沿做多、上沿做空。",
        "sources": [{"kind": "newsletter", "path": "/tmp/newsletter.md", "title": "黄金"}],
        "decision_mode": "ai_newsletter",
        "source": "machine_ai_newsletter",
        "status": "locked",
    }


def _write_market_view(output: Path, date: str = "2026-07-05", *, expire_below: float = 3960.0) -> None:
    write_json(
        output / "market_views" / f"{date}.json",
        [
            {
                "run_date": date,
                "generated_at": f"{date}T00:30:00+00:00",
                "direction_score": 80,
                "direction_bias": "strong_long",
                "key_levels": ["3992"],
                "expiry": {
                    "status": "active",
                    "expires_at": "2026-07-06T01:00:00+00:00",
                    "expire_below": expire_below,
                },
            }
        ],
    )


def _seed_previous_and_day(db: Path) -> MarketStore:
    store = MarketStore(db)
    _seed_bars(store, datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc), [4000.0, 3960.0, 4040.0])
    _seed_bars(store, datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3990.0, 4002.0, 3990.0, 4002.0])
    return store


def _realized_pnl(fills: list[dict]) -> float:
    return round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8)


def test_already_closed_cycle_finalizes_shadow_qualification(tmp_path: Path) -> None:
    class Execution:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def flush_shadow(self, cycle_id: str, *, cycle_complete: bool = False) -> dict:
            self.calls.append((cycle_id, cycle_complete))
            return {"status": "ok", "cycle_complete": cycle_complete}

    cycle_id = "2026-07-05_DAY"
    output = tmp_path / "outputs"
    write_json(output / "dualtrack" / "attribution" / f"{cycle_id}.json", [{"status": "closed"}])
    runner = object.__new__(DualTrackCycleRunner)
    runner.output_root = output
    runner.execution = Execution()

    result = runner.close_cycle(cycle_id, as_of="2026-07-05T13:01:00+00:00")

    assert result["status"] == "already_closed"
    assert result["shadow_finalization"] == {"status": "ok", "cycle_complete": True}
    assert runner.execution.calls == [(cycle_id, True)]


def _comex_config() -> dict:
    config = deepcopy(TEST_CONFIG)
    config["market_session"] = {
        "enabled": True,
        "venue": "comex_futures",
        "timezone": "America/New_York",
    }
    return config


def _human_sync_config(*, refresh: bool = False) -> dict:
    config = deepcopy(TEST_CONFIG)
    config["human_fill_sync"] = {
        "enabled": True,
        "provider": "tiger_openapi",
        "run_before_close": True,
        "refresh_order_sync_before_import": refresh,
        "require_success_before_close": True,
    }
    return config


def _mgc_dualtrack_config(*, refresh: bool = False) -> dict:
    config = _human_sync_config(refresh=refresh)
    config["market_data"] = {
        "symbol": "MGCmain",
        "timeframe": "1m",
        "provider": "tiger_openapi:COMEX",
    }
    config["market_session"] = {
        "enabled": True,
        "venue": "comex_futures",
        "timezone": "America/New_York",
    }
    config["grid"] = {**config["grid"], "spacing_bp": 6.0, "max_rungs": 2}
    config["execution_cost_model"] = {
        "venue": "tiger_mgc",
        "quantity_mode": "integer_contracts",
        "contract_multiplier": 10,
        "contracts_per_rung": 1,
    }
    return config


def _write_tiger_order_sync(output: Path, rows: list[dict]) -> None:
    write_json(output / "tiger_order_sync" / "current.json", [{
        "run_date": "2026-07-05",
        "provider": "tiger_openapi",
        "mode": "paper",
        "sync_status": "synced",
        "error": "",
        "open_order_count": 0,
        "filled_order_count": len(rows),
        "exchange_open_orders": [],
        "exchange_filled_orders": rows,
        "checked_at": "2026-07-05T01:03:00+00:00",
    }])


def _tiger_fill(**overrides) -> dict:
    return {
        "symbol": "MGC2608",
        "root_symbol": "MGC",
        "order_id": "T200",
        "parent_id": "",
        "side": "BUY",
        "type": "LMT",
        "status": "FILLED",
        "quantity": 1.0,
        "filled_quantity": 1.0,
        "average_fill_price": 3992.0,
        "currency": "USD",
        "filled_at": "2026-07-05T01:02:03+00:00",
        "source": "tiger_filled_orders",
        **overrides,
    }


def test_d8_1_prefix_replay_matches_batch_runner_on_same_prefix(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    cycle_id = "2026-07-05_DAY"
    output = tmp_path / "outputs"
    expected_output = tmp_path / "expected_outputs"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(expected_output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    DualTrackPlanStore(expected_output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    actual = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    prefix = MarketStore(db).load_bars_between("GOLD", "1m", "2026-07-05T01:00:00+00:00", "2026-07-05T01:04:00+00:00")
    DualTrackMachineRunner(expected_output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        prefix,
        prev_range=runner.previous_cycle_range(cycle_id),
        as_of="2026-07-05T01:04:00+00:00",
    )
    expected = load_json(expected_output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert actual == expected


def test_confirmed_range_break_replans_full_grid_and_preserves_old_fills(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {
        "range_reassessment": {
            "enabled": True,
            "confirm_closes": 3,
            "cooldown_minutes": 60,
            "max_replans_per_cycle": 2,
            "minimum_remaining_minutes": 30,
        }
    }
    old_plan = _neutral_machine_plan(cycle_id)
    DualTrackPlanStore(output, config=config).save_ai_plan(old_plan, now="2026-07-10T12:55:00+00:00")
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text(
        "## 黄金\n盘中突破后重新判断完整区间。\n",
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def decide(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {
            **_neutral_machine_plan(cycle_id, low=4125.0, high=4170.0),
            "key_levels": [4134.0, 4160.0],
            "grid_orders": [
                {"side": "long", "entry": 4134.0, "take_profit": 4144.0, "weight": 0.5},
                {"side": "short", "entry": 4160.0, "take_profit": 4150.0, "weight": 0.5},
            ],
            "rationale": "上破已确认，重新上移并扩展完整震荡区间。",
        }

    runner = DualTrackCycleRunner(
        output_root=output,
        market_db=tmp_path / "market.db",
        config=config,
    )
    runner.machine_planner = DualTrackMachinePlanner(
        output,
        config=config,
        newsletter_root=newsletter_root,
        decision_provider=decide,
    )
    visible_bars = [
        _bar(datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc), 4115.0, 4120.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 1, tzinfo=timezone.utc), 4120.0, 4131.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 2, tzinfo=timezone.utc), 4131.0, 4132.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 3, tzinfo=timezone.utc), 4132.0, 4133.0, provider="binance_usdm"),
    ]
    runner._cycle_bars = lambda _cycle_id, as_of=None: visible_bars  # type: ignore[method-assign]
    runner.previous_cycle_range = lambda _cycle_id: 40.0  # type: ignore[method-assign]
    runner.planning_volatility_context = lambda _cycle_id: {"status": "insufficient_samples"}  # type: ignore[method-assign]

    first = runner.intraday_tick(cycle_id, as_of="2026-07-10T13:03:00+00:00")
    revised = DualTrackPlanStore(output, config=config).machine_plan(cycle_id)
    revisions = load_json(output / "dualtrack" / "plan_revisions" / f"{cycle_id}_ai.json")

    assert first["range_reassessment"]["status"] == "replanned"
    assert revised is not None
    assert revised["range"] == {"low": 4125.0, "high": 4170.0}
    assert revised["execution_start"] == "2026-07-10T13:04:00+00:00"
    assert revised["revision_reason"] == "confirmed_range_breach_above"
    assert "旧 range 已失效" in captured["prompt"]
    assert len(revisions) == 1
    assert revisions[0]["previous_plan"]["range"] == {"low": 4090.0, "high": 4130.0}

    visible_bars = [
        *visible_bars,
        _bar(datetime(2026, 7, 10, 13, 4, tzinfo=timezone.utc), 4133.0, 4135.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 5, tzinfo=timezone.utc), 4135.0, 4145.0, provider="binance_usdm"),
    ]
    runner.intraday_tick(cycle_id, as_of="2026-07-10T13:05:00+00:00")
    fills = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert [(row["event"], row["side"], row["price"]) for row in fills] == [
        ("entry", "sell", 4120.0),
        ("stop", "buy", 4130.0),
        ("entry", "buy", 4134.0),
        ("target", "sell", 4144.0),
    ]


def test_confirmed_range_break_fails_closed_when_replanning_fails(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_NIGHT"
    config = deepcopy(TEST_CONFIG)
    old_plan = DualTrackPlanStore(output, config=config).save_ai_plan(
        _neutral_machine_plan(cycle_id),
        now="2026-07-10T12:55:00+00:00",
    )
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-10-finance-daily-newsletter.md").write_text(
        "## 黄金\n盘中区间复核。\n",
        encoding="utf-8",
    )

    def fail(_prompt: str) -> dict:
        raise RuntimeError("planner unavailable")

    runner = DualTrackCycleRunner(output_root=output, market_db=tmp_path / "market.db", config=config)
    runner.machine_planner = DualTrackMachinePlanner(
        output,
        config=config,
        newsletter_root=newsletter_root,
        decision_provider=fail,
    )
    bars = [
        _bar(datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc), 4110.0, 4131.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 1, tzinfo=timezone.utc), 4131.0, 4132.0, provider="binance_usdm"),
        _bar(datetime(2026, 7, 10, 13, 2, tzinfo=timezone.utc), 4132.0, 4133.0, provider="binance_usdm"),
    ]
    runner._cycle_bars = lambda _cycle_id, as_of=None: bars  # type: ignore[method-assign]
    runner.previous_cycle_range = lambda _cycle_id: 40.0  # type: ignore[method-assign]
    runner.planning_volatility_context = lambda _cycle_id: {"status": "insufficient_samples"}  # type: ignore[method-assign]

    result = runner.intraday_tick(cycle_id, as_of="2026-07-10T13:02:00+00:00")
    preserved = DualTrackPlanStore(output, config=config).machine_plan(cycle_id)
    revisions = load_json(output / "dualtrack" / "plan_revisions" / f"{cycle_id}_ai.json")

    assert result["range_reassessment"]["status"] == "failed"
    assert "planner unavailable" in result["range_reassessment"]["planning_error"]
    assert result["range_reassessment"]["retry_at"] == "2026-07-10T13:07:00+00:00"
    assert result["state"]["machine_stood_down"] is True
    assert preserved is not None
    assert preserved["locked_at"] == old_plan["locked_at"]
    assert revisions == []


def test_d8_2_missing_machine_research_records_error_neutral_and_stands_down(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    pre = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")
    tick = runner.intraday_tick("2026-07-05_DAY", as_of="2026-07-05T01:04:00+00:00")

    assert pre["status"] == "ai_plan_error_neutral"
    assert tick["state"]["machine_stood_down"] is True
    assert load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json") == []
    audit_events = [row["event"] for row in load_json(output / "dualtrack" / "audit" / "2026-07-05_DAY.json")]
    assert "machine_plan_decision_error" in audit_events


def test_cycle_runner_uses_custom_proposal_plugin_through_trusted_planner_core(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    newsletter_root = tmp_path / "newsletter"
    newsletter_root.mkdir()
    (newsletter_root / "2026-07-05-finance-daily-newsletter.md").write_text(
        "## 黄金\n不应暴露给未声明 newsletter context 的插件。\n",
        encoding="utf-8",
    )
    captured: dict[str, StrategyProposalRequest] = {}

    class DeterministicProposal:
        def propose(self, request: StrategyProposalRequest) -> dict:
            captured["request"] = request
            decision = _neutral_machine_plan(request.cycle_id)
            decision.pop("decision_mode")
            decision.pop("source")
            return decision

    registry = StrategyProposalPluginRegistry()
    registry.register(
        "deterministic_grid",
        lambda _params: DeterministicProposal(),
        plan_source="machine_deterministic_grid",
        default_decision_mode="deterministic_grid",
    )
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {
        "plugin": "deterministic_grid",
        "newsletter_root": str(newsletter_root),
    }

    runner = DualTrackCycleRunner(
        output_root=output,
        market_db=db,
        config=config,
        proposal_registry=registry,
    )
    result = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")
    plan = DualTrackPlanStore(output, config=config).machine_plan("2026-07-05_DAY")
    trace = load_json(output / "dualtrack" / "planning" / "2026-07-05_DAY_machine.json")[-1]

    assert result["status"] == "ai_plan_ready"
    assert result["proposal_plugin"]["plugin"]["name"] == "deterministic_grid"
    assert result["proposal_plugin"]["registry_fingerprint"] == registry.fingerprint
    assert plan is not None
    assert plan["source"] == "machine_deterministic_grid"
    assert plan["decision_mode"] == "deterministic_grid"
    assert captured["request"].market["symbol"] == "GOLD"
    assert captured["request"].newsletter_text == ""
    assert trace["proposal_plugin"] == result["proposal_plugin"]


def test_cycle_runner_rejects_unknown_proposal_plugin_before_artifact_creation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {"plugin": "typo"}

    with pytest.raises(UnknownStrategyProposalPlugin, match="typo"):
        DualTrackCycleRunner(
            output_root=output,
            market_db=tmp_path / "market.db",
            config=config,
        )

    assert not output.exists()


def test_invalid_custom_proposal_result_is_persisted_as_no_trade_degraded_plan(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"

    class InvalidProposal:
        def propose(self, _request: StrategyProposalRequest) -> dict:
            return []  # type: ignore[return-value]

    registry = StrategyProposalPluginRegistry()
    registry.register("invalid", lambda _params: InvalidProposal())
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {"plugin": "invalid"}
    runner = DualTrackCycleRunner(
        output_root=output,
        market_db=db,
        config=config,
        proposal_registry=registry,
    )

    result = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")
    plan = DualTrackPlanStore(output, config=config).machine_plan("2026-07-05_DAY")

    assert result["status"] == "ai_plan_error_neutral"
    assert plan is not None
    assert plan["degraded"] is True
    assert plan["grid_orders"] == []
    assert "must return a JSON object" in plan["planning_error"]


def test_proposal_cannot_forge_core_lifecycle_fields_or_bypass_grid_validation(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"

    class ForgedLifecycleProposal:
        def propose(self, request: StrategyProposalRequest) -> dict:
            decision = _neutral_machine_plan(request.cycle_id)
            decision["grid_orders"] = []
            decision["sources"] = []
            decision["degraded"] = True
            decision["planning_error"] = "forged plugin error"
            decision["execution_start"] = "2026-07-05T12:00:00+00:00"
            decision["review_change"] = {
                "change_id": "forged",
                "mode": "paper_challenger",
                "dimension": "range",
                "summary": "forged",
                "expected_metric": "forged",
            }
            return decision

    registry = StrategyProposalPluginRegistry()
    registry.register("forged", lambda _params: ForgedLifecycleProposal())
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {"plugin": "forged"}
    runner = DualTrackCycleRunner(
        output_root=output,
        market_db=db,
        config=config,
        proposal_registry=registry,
    )

    result = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")
    plan = DualTrackPlanStore(output, config=config).machine_plan("2026-07-05_DAY")
    audit = load_json(output / "dualtrack" / "audit" / "2026-07-05_DAY.json")

    assert result["status"] == "ai_plan_error_neutral"
    assert plan is not None
    assert plan["source"] == "machine_ai_decision_error"
    assert plan["degraded"] is True
    assert plan["grid_orders"] == []
    assert "executable machine plans require explicit grid_orders" in plan["planning_error"]
    assert "forged plugin error" not in plan["planning_error"]
    assert "execution_start" not in plan
    assert "review_change" not in plan
    assert audit[-1]["event"] == "machine_plan_decision_error"


def test_explicit_obsidian_plan_sync_keeps_next_cycle_as_draft(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    _write_market_view(output)
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    result = runner.sync_obsidian_human_plans(as_of="2026-07-05T02:30:00+00:00", include_next=True)

    day_plan = load_json(output / "dualtrack" / "plans" / "2026-07-05_DAY_human.json")[0]
    night_plan = load_json(output / "dualtrack" / "plans" / "2026-07-05_NIGHT_human.json")[0]
    assert result["event"] == "sync_obsidian_plan"
    assert [item["cycle_id"] for item in result["results"]] == ["2026-07-05_DAY", "2026-07-05_NIGHT"]
    assert day_plan["status"] == "draft"
    assert day_plan["locked_at"] is None
    assert night_plan["status"] == "draft"
    assert night_plan["locked_at"] is None
    assert day_plan["source"] == night_plan["source"] == "obsidian"
    assert day_plan["range"] == night_plan["range"] == {"low": 3960.0, "high": None}


def test_live_tick_syncs_obsidian_plan_and_runs_intraday(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    _write_market_view(output)
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    result = runner.live_tick(as_of="2026-07-05T02:30:00+00:00")

    assert result["event"] == "live_tick"
    assert result["sync"]["results"][0]["plan_status"] == "draft"
    assert len(result["sync"]["results"]) == 1
    assert not (output / "dualtrack" / "plans" / "2026-07-05_NIGHT_human.json").exists()
    assert result["intraday"]["status"] == "ran"
    runner_rows = load_json(output / "dualtrack" / "runner" / "2026-07-05_DAY.json")
    assert runner_rows[-1]["event"] == "intraday"
    assert [row["event"] for row in runner_rows].count("intraday") == 1


def test_midnight_cutover_closes_legacy_night_and_opens_daily_cycle(tmp_path: Path) -> None:
    runner = DualTrackCycleRunner(
        output_root=tmp_path / "outputs",
        market_db=tmp_path / "market_data.db",
        config=TEST_CONFIG,
    )
    runner.close_cycle = lambda cycle_id, as_of=None: {"event": "close", "cycle_id": cycle_id}
    runner.pre_cycle = lambda cycle_id, as_of=None: {"event": "pre_cycle", "cycle_id": cycle_id}

    results = runner._lifecycle_results(parse_utc("2026-07-13T16:00:00+00:00"))

    assert results == [
        {"event": "close", "cycle_id": "2026-07-13_NIGHT"},
        {"event": "pre_cycle", "cycle_id": "2026-07-14_DAY"},
    ]


def test_live_tick_executes_human_protective_exit_from_fresh_real_bar(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    store = MarketStore(db)
    start = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    _seed_bars(store, start, [100.0, 106.0])
    human = DualTrackHumanEngine(output, config=TEST_CONFIG)
    human.submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    result = runner.live_tick(as_of="2026-07-05T01:01:30+00:00")

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert result["protective_sweep"]["status"] == "triggered"
    assert result["protective_sweep"]["source"] == "market_db:test"
    assert len(fills) == 2
    assert fills[-1]["event"] == "stop"
    assert fills[-1]["price"] == 105.0


def test_live_tick_keeps_prior_cycle_human_tp_sl_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    store = MarketStore(db)
    start = datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc)
    _seed_bars(store, start, [100.0, 89.0])
    human = DualTrackHumanEngine(output, config=TEST_CONFIG)
    human.submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T12:50:00+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)
    monkeypatch.setattr(runner, "_lifecycle_results", lambda _now: [])
    monkeypatch.setattr(runner, "sync_obsidian_human_plans", lambda **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(runner, "intraday_tick", lambda **_kwargs: {"status": "skipped"})

    result = runner.live_tick(as_of="2026-07-05T13:01:30+00:00")

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert result["protective_sweep"]["status"] == "triggered"
    assert result["protective_sweep"]["source_cycle_id"] == "2026-07-05_DAY"
    assert fills[-1]["event"] == "target"
    assert fills[-1]["price"] == 90.0


def test_live_tick_replays_intermediate_bar_wick_after_close_recovers(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    store = MarketStore(db)
    store.upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=100.0,
            high=106.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            provider="test",
        ),
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:02:00+00:00",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            provider="test",
        ),
    ])
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })

    result = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).live_tick(
        as_of="2026-07-05T01:02:30+00:00"
    )

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert result["protective_sweep"]["status"] == "triggered"
    assert result["protective_sweep"]["processed_events"] == 2
    assert fills[-1]["event"] == "stop"
    assert fills[-1]["price"] == 105.0
    assert fills[-1]["trigger_mark_price"] == 100.0


def test_live_tick_same_bar_stop_and_target_uses_conservative_stop_first(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    MarketStore(db).upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=100.0,
            high=111.0,
            low=89.0,
            close=100.0,
            volume=1.0,
            provider="test",
        )
    ])
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 95.0,
        "tp": 105.0,
    })

    DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).live_tick(
        as_of="2026-07-05T01:01:30+00:00"
    )

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert fills[-1]["event"] == "stop"
    assert fills[-1]["price"] == 95.0


def test_live_tick_does_not_apply_pre_entry_bar_range_or_close_to_new_trade(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    store = MarketStore(db)
    store.upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=100.0,
            high=106.0,
            low=99.0,
            close=106.0,
            volume=1.0,
            provider="test",
        ),
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:03:00+00:00",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            provider="test",
        ),
    ])
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)
    runner.execution.submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 120.0,
        "notional": 1000.0,
        "sl": 125.0,
        "tp": 110.0,
    })
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:02:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })

    result = runner.live_tick(as_of="2026-07-05T01:03:30+00:00")

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert result["protective_sweep"]["status"] == "ok"
    assert len(fills) == 1


def test_live_tick_refuses_synthetic_bar_for_human_protective_exit(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    store = MarketStore(db)
    store.upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=100.0,
            high=106.0,
            low=99.0,
            close=106.0,
            volume=0.0,
            provider="local_synthetic_seed",
            quality_flags=["synthetic_seed"],
        )
    ])
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    result = runner.live_tick(as_of="2026-07-05T01:01:30+00:00")

    fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_human.json")
    assert result["protective_sweep"] == {
        "status": "skipped",
        "reason": "synthetic_market_data",
        "triggered": [],
    }
    assert len(fills) == 1


def test_intraday_tick_refuses_synthetic_bars_for_machine_execution(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    MarketStore(db).upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=4000.0,
            high=4002.0,
            low=3990.0,
            close=3992.0,
            volume=0.0,
            provider="local_synthetic_seed",
            quality_flags=["synthetic_seed"],
        )
    ])
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(
        _plan(cycle_id),
        now="2026-07-05T00:59:00+00:00",
    )

    result = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).intraday_tick(
        cycle_id,
        as_of="2026-07-05T01:01:30+00:00",
    )

    assert result == {
        "event": "intraday",
        "cycle_id": cycle_id,
        "status": "skipped",
        "reason": "synthetic_market_data",
    }
    assert load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json") == []


def test_previous_cycle_range_refuses_synthetic_input(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    store.upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-04T13:01:00+00:00",
            open=4000.0,
            high=4010.0,
            low=3990.0,
            close=4005.0,
            volume=0.0,
            provider="local_synthetic_seed",
            quality_flags=["synthetic_seed"],
        )
    ])
    runner = DualTrackCycleRunner(output_root=tmp_path / "outputs", market_db=db, config=TEST_CONFIG)

    with pytest.raises(ValueError, match="synthetic_market_data"):
        runner.previous_cycle_range("2026-07-05_DAY")


def test_live_tick_routes_trusted_market_event_through_execution_adapter(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    _seed_bars(MarketStore(db), datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [100.0, 106.0])
    captured = {}
    flushed = []

    class FakeExecutionAdapter:
        name = "fake"

        def snapshot(self, cycle_id: str, **kwargs) -> dict:
            return {"positions": [{"status": "open", "remaining_units": 1.0}]}

        def process_market_event(self, event: dict) -> dict:
            captured.update(event)
            return {"status": "triggered", "triggered": [{"event": "stop"}]}

        def flush_shadow(self, cycle_id: str) -> dict:
            flushed.append(cycle_id)
            return {"status": "replayed"}

    monkeypatch.setattr(
        cycle_runner_module,
        "build_configured_execution_engine_adapter",
        lambda *args, **kwargs: FakeExecutionAdapter(),
    )

    result = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).live_tick(
        as_of="2026-07-05T01:01:30+00:00"
    )

    assert result["protective_sweep"]["status"] == "triggered"
    assert captured["cycle_id"] == "2026-07-05_DAY"
    assert captured["price"] == 106.0
    assert captured["source"] == "market_db:test"
    assert captured["provider"] == "test"
    assert captured["instrument_id"] == "XAUUSDT"
    assert flushed == ["2026-07-05_DAY"]


def test_live_tick_processes_pending_limit_without_an_open_position(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    _seed_bars(MarketStore(db), datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [100.0, 106.0])
    captured = {}

    class FakeExecutionAdapter:
        name = "fake"

        def snapshot(self, cycle_id: str, **kwargs) -> dict:
            return {
                "positions": [],
                "orders": [{
                    "state": "accepted",
                    "event": "entry",
                    "order_type": "limit",
                    "ts": "2026-07-05T01:00:30+00:00",
                }],
            }

        def process_market_event(self, event: dict) -> dict:
            captured.update(event)
            return {"status": "ok", "triggered": [], "accepted_limit_fill_count": 1}

    monkeypatch.setattr(
        cycle_runner_module,
        "build_configured_execution_engine_adapter",
        lambda *args, **kwargs: FakeExecutionAdapter(),
    )

    result = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).live_tick(
        as_of="2026-07-05T01:01:30+00:00"
    )

    assert result["protective_sweep"]["processed_events"] > 0
    assert captured["cycle_id"] == "2026-07-05_DAY"
    assert captured["price"] == 106.0


def test_live_tick_replays_datafeed_limit_fill_and_target_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    MarketStore(db).upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:01:00+00:00",
            open=101.0,
            high=102.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            provider="binance_usdm_futures",
        ),
        Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp="2026-07-05T01:02:00+00:00",
            open=100.0,
            high=111.0,
            low=100.0,
            close=110.0,
            volume=1.0,
            provider="binance_usdm_futures",
        ),
    ])
    config = deepcopy(TEST_CONFIG)
    config["market_data"] = {
        "symbol": "GOLD",
        "timeframe": "1m",
        "provider": "binance_usdm_futures",
    }
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)
    runner.execution.submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 95.0,
        "tp": 110.0,
        "source": "provider-contract-test",
    })
    monkeypatch.setattr(runner, "_lifecycle_results", lambda _now: [])
    monkeypatch.setattr(runner, "sync_obsidian_human_plans", lambda **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(runner, "intraday_tick", lambda **_kwargs: {"status": "skipped"})

    first = runner.live_tick(as_of="2026-07-05T01:02:30+00:00")
    second = runner.live_tick(as_of="2026-07-05T01:02:30+00:00")

    snapshot = runner.execution.snapshot("2026-07-05_DAY", mark_price=110.0, mark_fresh=True)
    assert first["protective_sweep"]["status"] == "triggered"
    assert first["protective_sweep"]["accepted_limit_fill_count"] == 1
    assert len(first["protective_sweep"]["accepted_limit_fills"]) == 1
    assert [fill["event"] for fill in snapshot["fills"]] == ["entry", "target"]
    assert snapshot["orders"][0]["state"] == "filled"
    assert snapshot["positions"][0]["status"] == "closed"
    assert second["protective_sweep"]["reason"] == "no_open_positions_or_pending_orders"
    assert runner.execution.reconcile("2026-07-05_DAY")["status"] == "ok"


def test_live_tick_rejects_one_minute_protective_mark_older_than_three_minutes(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    output = tmp_path / "outputs"
    _seed_bars(MarketStore(db), datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [100.0, 106.0])
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:00:30+00:00",
        "side": "sell",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
    })

    result = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).live_tick(
        as_of="2026-07-05T01:04:01+00:00"
    )

    assert result["protective_sweep"] == {
        "status": "skipped",
        "reason": "market_data_stale",
        "triggered": [],
    }


def test_d8_3_intraday_tick_is_idempotent_for_same_bar_set(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    first = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    second = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert len(second) == len(first)
    assert _realized_pnl(second) == _realized_pnl(first)
    assert second == first


def test_d8_3_auto_event_replaces_machine_fills_for_same_bar_set(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    monkeypatch.setattr(cycle_runner_module, "dualtrack_config", lambda: TEST_CONFIG)
    argv = [
        "--event", "auto",
        "--as-of", "2026-07-05T01:04:00+00:00",
        "--market-db", str(db),
        "--output-root", str(output),
    ]

    assert cycle_runner_module.main(argv) == 0
    first = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    assert cycle_runner_module.main(argv) == 0
    second = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert first
    assert len(second) == len(first)
    assert _realized_pnl(second) == _realized_pnl(first)
    assert second == first


def test_d8_3_run_close_run_keeps_frozen_trend_gate_and_fills(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    pre = runner.pre_cycle(cycle_id, as_of="2026-07-05T01:00:00+00:00")
    assert pre["trend_gate_armed"] is False
    runner.intraday_tick(cycle_id, as_of="2026-07-05T13:00:00+00:00")
    first = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    assert first
    assert {fill["layer"] for fill in first} == {"grid"}

    close = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")
    assert close["status"] == "closed"
    assert load_json(output / "dualtrack" / "scoreboard.json")[-1]["trend_leg_gate"]["armed"] is True
    assert load_json(output / "dualtrack" / "cycles" / f"{cycle_id}.json")[-1]["trend_gate_armed"] is False

    runner.intraday_tick(cycle_id, as_of="2026-07-05T13:00:00+00:00")
    second = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert len(second) == len(first)
    assert _realized_pnl(second) == _realized_pnl(first)
    assert second == first
    assert {fill["layer"] for fill in second} == {"grid"}


def test_d8_3_frozen_armed_gate_runs_trend_leg_on_first_pass(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "scoreboard.json", [{
        "history": {"human": [], "ai": []},
        "trend_leg_gate": {"threshold": 0.60, "armed": True, "basis": {"hit_rate": 1.0}},
    }])
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(output, config=TEST_CONFIG).save_ai_plan(_plan(cycle_id) | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    pre = runner.pre_cycle(cycle_id, as_of="2026-07-05T01:00:00+00:00")
    state = runner.intraday_tick(cycle_id, as_of="2026-07-05T13:00:00+00:00")["state"]
    fills = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert pre["trend_gate_armed"] is True
    assert state["trend_gate_armed"] is True
    assert any(fill["layer"] == "trend" for fill in fills)


def test_d8_4_close_cycle_is_single_shot(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    first = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")
    second = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")

    assert first["status"] == "closed"
    assert second["status"] == "already_closed"
    assert second["attribution"] == first["attribution"]
    scoreboard = load_json(output / "dualtrack" / "scoreboard.json")[-1]
    assert [row["cycle_id"] for row in scoreboard["history"]["human"]] == [cycle_id]
    daily = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[-1]
    assert list(daily["cycles"]) == [cycle_id]


def test_tiger_human_fill_sync_runs_before_close_and_scoring(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    config = _human_sync_config()
    DualTrackPlanStore(output, config=config).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    _write_tiger_order_sync(output, [_tiger_fill()])
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)

    result = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")

    assert result["status"] == "closed"
    assert result["human_fill_sync"]["status"] == "synced"
    assert result["human_fill_sync"]["imported_count"] == 1
    attribution = result["attribution"]
    assert attribution["tracks"]["human"]["fill_count"] == 1
    assert attribution["tracks"]["human"]["realized_pnl"] == -2.7
    assert attribution["fills"]["human"][0]["external_order_id"] == "T200"
    runner_events = [row["event"] for row in load_json(output / "dualtrack" / "runner" / f"{cycle_id}.json")]
    assert "human_fill_sync" in runner_events
    assert runner_events.index("human_fill_sync") < runner_events.index("close")


def test_mgc_dualtrack_close_scores_machine_and_human_with_same_tiger_contract_cost_model(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    _seed_bars(
        store,
        datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc),
        [4185.0, 4170.0, 4190.0],
        symbol="MGCmain",
        provider="tiger_openapi:COMEX",
    )
    _seed_bars(
        store,
        datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc),
        [4183.0, 4180.0, 4183.5, 4180.5, 4183.5],
        symbol="MGCmain",
        provider="tiger_openapi:COMEX",
    )
    output = tmp_path / "outputs"
    cycle_id = "2026-07-06_DAY"
    config = _mgc_dualtrack_config()
    DualTrackPlanStore(output, config=config).save_human_plan(
        _plan(cycle_id),
        now="2026-07-06T00:59:00+00:00",
    )
    DualTrackPlanStore(output, config=config).save_ai_plan(
        _plan(cycle_id) | {"author": "ai"},
        now="2026-07-06T00:58:00+00:00",
    )
    _write_tiger_order_sync(output, [_tiger_fill(average_fill_price=4182.0, filled_at="2026-07-06T01:02:03+00:00")])
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)

    runner.intraday_tick(cycle_id, as_of="2026-07-06T12:59:00+00:00")
    result = runner.close_cycle(cycle_id, as_of="2026-07-06T13:00:00+00:00")

    assert result["status"] == "closed"
    assert runner.symbol == "MGCmain"
    assert result["human_fill_sync"]["status"] == "synced"
    attribution = result["attribution"]
    machine_fills = attribution["fills"]["machine"]
    human_fills = attribution["fills"]["human"]
    assert machine_fills
    assert human_fills
    assert {fill["cost_model"]["venue"] for fill in machine_fills + human_fills} == {"tiger_mgc"}
    assert {fill["cost_model"]["quantity_mode"] for fill in machine_fills + human_fills} == {"integer_contracts"}
    assert {fill["contracts"] for fill in machine_fills + human_fills} == {1}
    assert {fill["quantity"] for fill in machine_fills + human_fills} == {1}
    assert all(abs(float(fill["notional"]) - (float(fill["price"]) * 10)) < 0.000001 for fill in machine_fills + human_fills)
    assert all(abs(float(fill["cost"]) - 2.7) < 0.000001 for fill in machine_fills + human_fills)
    assert attribution["tracks"]["machine"]["realized_pnl"] == _realized_pnl(machine_fills)
    assert attribution["tracks"]["human"]["realized_pnl"] == _realized_pnl(human_fills)
    assert attribution["ledger"]["daily"]["tracks"]["machine"]["realized_pnl"] == _realized_pnl(machine_fills)
    assert attribution["ledger"]["daily"]["tracks"]["human"]["realized_pnl"] == _realized_pnl(human_fills)


def test_cycle_runner_uses_configured_market_data_symbol_by_default(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    db = tmp_path / "market.db"
    store = MarketStore(db)
    _seed_bars(
        store,
        datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc),
        [4180.0, 4175.0, 4185.0],
        symbol="MGCmain",
        provider="tiger_openapi:COMEX",
    )
    _seed_bars(
        store,
        datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc),
        [4183.0, 4182.0, 4184.0],
        symbol="MGCmain",
        provider="tiger_openapi:COMEX",
    )
    _write_market_view(output)
    config = deepcopy(TEST_CONFIG)
    config["market_data"] = {"symbol": "MGCmain", "timeframe": "1m", "provider": "tiger_openapi:COMEX"}
    DualTrackPlanStore(output, config=config).save_ai_plan(
        _plan("2026-07-05_DAY") | {"author": "ai"},
        now="2026-07-05T00:58:00+00:00",
    )

    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)
    result = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")

    assert runner.symbol == "MGCmain"
    assert runner.timeframe == "1m"
    assert result["status"] == "ai_plan_ready"


def test_tiger_human_fill_sync_blocks_close_when_artifact_missing(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    config = _human_sync_config()
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)

    result = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")

    assert result["status"] == "skipped"
    assert result["reason"] == "human_fill_sync_blocked"
    assert result["human_fill_sync"]["status"] == "blocked"
    assert result["human_fill_sync"]["reason"] == "order_sync_missing"
    assert not (output / "dualtrack" / "attribution" / f"{cycle_id}.json").exists()
    assert not (output / "dualtrack" / "fills" / f"{cycle_id}_machine.json").exists()


def test_tiger_human_fill_sync_can_refresh_order_sync_before_import(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    config = _human_sync_config(refresh=True)
    DualTrackPlanStore(output, config=config).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")

    class _FakeOrderSync:
        def __init__(self, output_root):
            self.output_root = output_root

        def run(self, run_date: str) -> dict:
            _write_tiger_order_sync(Path(self.output_root), [_tiger_fill(order_id="T201")])
            return {
                "run_date": run_date,
                "sync_status": "synced",
                "filled_order_count": 1,
                "open_order_count": 0,
            }

    monkeypatch.setattr(cycle_runner_module, "TigerOpenApiOrderSync", _FakeOrderSync)
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=config)

    result = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")

    assert result["status"] == "closed"
    assert result["human_fill_sync"]["refreshed_order_sync"] is True
    assert result["human_fill_sync"]["order_sync_refresh"]["sync_status"] == "synced"
    assert result["attribution"]["fills"]["human"][0]["external_order_id"] == "T201"


def test_d8_5_orchestrated_intraday_machine_payload_stays_pnl_only(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")

    payload = DualTrackMachineRunner(output, config=TEST_CONFIG).machine_payload(cycle_id, as_of="2026-07-05T01:04:00+00:00")

    assert set(payload) == {"realized_pnl", "unrealized_pnl", "layers"}
    assert {"fills", "orders", "entries", "inventory", "rungs", "price", "notional", "sl", "tp"}.isdisjoint(payload)


def test_planning_volatility_context_excludes_weekends_and_uses_active_cycle_median(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    _seed_bars(store, datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc), [100.0, 102.0])
    _seed_bars(store, datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc), [100.0, 130.0])
    _seed_bars(store, datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc), [100.0, 140.0])
    config = deepcopy(TEST_CONFIG)
    config["machine_planner"] = {
        "volatility_lookback_cycles": 2,
        "minimum_active_cycle_samples": 2,
        "minimum_sample_coverage_pct": 0.001,
        "minimum_range_multiplier": 1.0,
        "exclude_weekends_from_range_reference": True,
    }

    context = DualTrackCycleRunner(output_root=tmp_path / "outputs", market_db=db, config=config).planning_volatility_context(
        "2026-07-13_DAY"
    )

    assert context["status"] == "ready"
    assert context["reference_range"] == 35.0
    assert context["minimum_plan_range"] == 35.0
    assert [sample["range"] for sample in context["selected_samples"]] == [30.0, 40.0]
    assert any(
        sample["cycle_id"] == "2026-07-12_NIGHT" and sample["reason"] == "weekend_low_liquidity"
        for sample in context["excluded_samples"]
    )


def test_d8_6_open_ended_directional_schema_and_flat_stand_down(tmp_path: Path) -> None:
    normalized = validate_plan(
        {
            "cycle_id": "2026-07-05_DAY",
            "direction": "long",
            "range": {},
            "key_levels": [4210.0],
            "invalidation": [{"side": "below", "price": 4160.0, "confirm": "close_1m"}],
        },
        author="human",
        status="locked",
    )
    assert normalized["range"] == {"low": 4160.0, "high": None}

    flat = validate_plan({"cycle_id": "2026-07-05_DAY", "direction": "flat"}, author="human", status="locked")
    assert flat["range"] == {"low": None, "high": None}
    assert flat["key_levels"] == []

    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id, "flat"), now="2026-07-05T00:59:00+00:00")

    state = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).intraday_tick(
        cycle_id,
        as_of="2026-07-05T01:04:00+00:00",
    )["state"]

    assert state["machine_stood_down"] is True
    assert state["layers"] == ["grid:stand_down:no_effective_plan", "trend:stand_down:no_effective_plan"]


def test_dt8_acceptance_fast_forward_day_produces_two_unattended_cycles(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = _seed_previous_and_day(db)
    _seed_bars(store, datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc), [4010.0, 4000.0, 4012.0, 4000.0, 4012.0])
    output = tmp_path / "outputs"
    tight_floor = 3980.0
    _write_market_view(output, expire_below=tight_floor)
    plan_store = DualTrackPlanStore(output, config=TEST_CONFIG)
    plan_store.save_ai_plan(_plan("2026-07-05_DAY") | {"author": "ai"}, now="2026-07-05T00:58:00+00:00")
    plan_store.save_ai_plan(_plan("2026-07-05_NIGHT") | {"author": "ai"}, now="2026-07-05T12:58:00+00:00")

    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)
    result = runner.fast_forward_day("2026-07-05")

    closed = [row for row in result["results"] if row["event"] == "close" and row["status"] == "closed"]
    assert [row["cycle_id"] for row in closed] == ["2026-07-05_DAY", "2026-07-05_NIGHT"]
    assert load_json(output / "dualtrack" / "plans" / "2026-07-05_DAY_ai.json")
    assert load_json(output / "dualtrack" / "plans" / "2026-07-05_NIGHT_ai.json")
    assert load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json")
    ledger = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[-1]
    assert set(ledger["cycles"]) == {"2026-07-05_DAY", "2026-07-05_NIGHT"}
    assert abs(float(ledger["tracks"]["machine"]["realized_pnl"])) < 100.0
    day_fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json")
    day_pnl = sum(float(fill["realized_pnl"]) for fill in day_fills)
    day_budgeted = simulate_conditional_grid(
        cycle_id="2026-07-05_DAY",
        bars=store.load_bars_between("GOLD", "1m", "2026-07-05T01:00:00+00:00", "2026-07-05T01:04:00+00:00"),
        direction=1,
        prev_range=runner.previous_cycle_range("2026-07-05_DAY"),
        spacing_bp=float(TEST_CONFIG["grid"]["spacing_bp"]),
        range_k=float(TEST_CONFIG["grid"]["range_k"]),
        rung_notional=base_rung_notional(TEST_CONFIG),
        max_rungs=10,
        cost_per_side_bp=float(TEST_CONFIG["cost_per_side_bp"]),
        tp_mult=float(TEST_CONFIG["grid"]["tp_mult_base"]),
        re_arm_max=int(TEST_CONFIG["grid"]["re_arm_max"]),
        budget_sizing=True,
        stop=GridStop(side="below", price=tight_floor),
    )
    day_budgeted_pnl = sum(float(fill["realized_pnl"]) for fill in day_budgeted.fills)
    assert abs(day_pnl) < abs(day_budgeted_pnl)
    stop_fills = [
        fill
        for cycle_id in ("2026-07-05_DAY", "2026-07-05_NIGHT")
        for fill in load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
        if fill["event"] == "stop"
    ]
    assert all(float(fill["realized_pnl"]) <= 0 for fill in stop_fills)


def test_dt8_empty_market_db_skip_paths_and_auto_are_safe(tmp_path: Path) -> None:
    runner = DualTrackCycleRunner(output_root=tmp_path / "outputs", market_db=tmp_path / "empty.db", config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"

    assert runner.previous_cycle_range(cycle_id) == 0.0
    assert runner.pre_cycle(cycle_id, as_of="2026-07-05T01:00:00+00:00")["status"] == "skipped"
    assert runner.intraday_tick(cycle_id, as_of="2026-07-04T23:00:00+00:00")["status"] == "skipped"
    assert runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")["status"] == "skipped"

    auto = runner.auto(as_of="2026-07-05T01:00:00+00:00")

    assert auto["event"] == "auto"
    assert [row["status"] for row in auto["results"]] == ["skipped", "skipped", "skipped"]


def test_comex_session_auto_skips_during_daily_break(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    runner = DualTrackCycleRunner(
        output_root=output,
        market_db=tmp_path / "market_data.db",
        config=_comex_config(),
    )

    result = runner.auto(as_of="2026-07-06T21:30:00+00:00")

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"
    assert result["market_session"]["reason"] == "daily_break"
    assert result["market_session"]["next_open"] == "2026-07-06T22:00:00+00:00"
    assert not (output / "dualtrack" / "fills").exists()


def test_comex_session_intraday_skips_during_weekend_close(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_NIGHT"
    DualTrackPlanStore(output, config=_comex_config()).save_human_plan(_plan(cycle_id), now="2026-07-05T12:59:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=_comex_config())

    result = runner.intraday_tick(cycle_id, as_of="2026-07-05T21:30:00+00:00")

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"
    assert result["market_session"]["reason"] == "weekend_closed"
    assert load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json") == []


def test_comex_session_mask_filters_closed_break_bars_from_cycle_and_previous_range(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = MarketStore(db)
    cycle_id = "2026-07-06_NIGHT"
    rows = [
        _bar(datetime(2026, 7, 6, 20, 59, tzinfo=timezone.utc), 4180.0, 4181.0),
        _bar(datetime(2026, 7, 6, 21, 30, tzinfo=timezone.utc), 4181.0, 5000.0),
        _bar(datetime(2026, 7, 6, 22, 0, tzinfo=timezone.utc), 4181.0, 4182.0),
    ]
    store.upsert_bars(rows)
    runner = DualTrackCycleRunner(output_root=tmp_path / "outputs", market_db=db, config=_comex_config())

    cycle_bars = runner._cycle_bars(cycle_id, as_of="2026-07-06T22:01:00+00:00")
    previous_range = runner.previous_cycle_range("2026-07-07_DAY")

    assert [bar.timestamp for bar in cycle_bars] == [
        "2026-07-06T20:59:00+00:00",
        "2026-07-06T22:00:00+00:00",
    ]
    assert previous_range == 2.0
