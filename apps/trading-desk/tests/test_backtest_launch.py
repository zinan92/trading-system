"""开仓前回测 launcher (#1309): one process per shape and day, its own folder, no secrets passed on."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from trading_desk.backtest import Backtests, shape_key

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)


def plan(price: float = 4000.0) -> dict:
    k = price / 4000.0
    return {"normalized_input": {"strategy_type": "grid", "direction": "long", "lower_price_boundary": 3900 * k, "upper_price_boundary": 4100 * k, "stop_price": 3850 * k},
            "market": {"price": price},
            "risk": {"per_order_quantity": 0.2 / k, "account_equity": 10000, "grid_rungs": [{"rung": 1, "side": "buy", "price": 3950 * k, "take_profit": 3990 * k}]}}


@dataclass
class Cfg:
    db_path: Path
    trading_system_checkout: Path
    nautilus_python: Path
    paper_output: Path
    datafeed_url: str = "http://127.0.0.1:8100"


@pytest.fixture
def launched():
    return []


@pytest.fixture
def tests(tmp_path: Path, launched, monkeypatch) -> Backtests:
    monkeypatch.setenv("HYPERLIQUID_TESTNET_SECRET_FILE", "/secret")
    cfg = Cfg(tmp_path / "desk" / "desk.db", tmp_path / "checkout", tmp_path / "py", tmp_path / "out")
    clock = {"now": NOW}
    runner = Backtests(cfg, launch=lambda args, **kw: launched.append((args, kw)), now=lambda: clock["now"])
    runner.clock = clock
    return runner


def test_the_same_shape_at_another_price_is_the_same_backtest() -> None:
    assert shape_key(plan(4000), day="2026-11-05") == shape_key(plan(4100), day="2026-11-05")
    assert shape_key(plan(4000), day="2026-11-05") != shape_key(plan(4000), day="2026-11-06")
    other = plan(4000)
    other["normalized_input"]["stop_price"] = 3800
    assert shape_key(other, day="2026-11-05") != shape_key(plan(4000), day="2026-11-05")


def test_start_launches_once_with_a_clean_environment(tests: Backtests, launched) -> None:
    key = tests.start(plan())
    args, kw = launched[0]
    assert args[1:3] == ["-m", "pipelines.grid_backtest"] and args[args.index("--output") + 1] == str(tests.path(key))
    assert args[args.index("--preflight") + 1].endswith("park_strategy/paper_preflight_current.json")
    assert "HYPERLIQUID_TESTNET_SECRET_FILE" not in kw["env"] and kw["start_new_session"] is True
    assert json.loads((tests.folder / f"{key}.plan.json").read_text())["market"]["price"] == 4000.0
    tests.path(key).write_text(json.dumps({"status": "running", "started_at": NOW.isoformat()}))
    assert tests.start(plan(4100)) == key and len(launched) == 1


def test_a_finished_result_is_reused_and_a_stuck_run_is_restarted(tests: Backtests, launched) -> None:
    key = tests.start(plan())
    tests.path(key).write_text(json.dumps({"status": "unavailable", "reason": "x"}))
    tests.start(plan())
    assert len(launched) == 1
    tests.path(key).write_text(json.dumps({"status": "running", "started_at": NOW.isoformat()}))
    tests.clock["now"] = NOW + timedelta(minutes=20)
    tests.start(plan())
    assert len(launched) == 2


def test_only_a_grid_with_rungs_is_backtested(tests: Backtests, launched) -> None:
    assert tests.start({"normalized_input": {"strategy_type": "dca"}}) is None
    assert launched == []


def test_read_refuses_anything_but_a_key(tests: Backtests) -> None:
    assert tests.read("../../etc/passwd") is None and tests.read("a" * 24) is None
