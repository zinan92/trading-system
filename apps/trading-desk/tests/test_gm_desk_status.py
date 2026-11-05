"""The /trade status line (gm-desk.js DeskStatus) says only what the read model backs (#1294).

Runs the real browser file under node with stub read models, so the running and blocked
states are covered without starting a Paper session.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "trading_desk" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(call: str, *args) -> dict:
    script = (
        f"const S = require({json.dumps(str(STATIC / 'gm-desk.js'))});"
        f"const args = JSON.parse(process.argv[1]);"
        f"process.stdout.write(JSON.stringify(S.{call}(...args)));"
    )
    out = subprocess.run([NODE, "-e", script, json.dumps(list(args))], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)


def idle_view(**market) -> dict:
    """Shaped like production's read model on 2026-11-05: no plan, last session ended 9/14."""
    return {
        "marketTrusted": market.pop("trusted", True),
        "market": {"provider_label": "OKX", "latest_timestamp": "2026-11-05T02:49:00+00:00", "timeframe": "30m", **market},
        "livePlan": None,
        "data": {
            "current_strategy": {
                "active": False,
                "blockers": ["active_strategy_missing", "grid_geometry_invalid"],
                "terminal": {
                    "reason": "lower_boundary_invalidated",
                    "exit_receipts": [{"ts": "2026-09-14T09:19:35+00:00"}] * 3,
                    "closed_count": 10,
                    "ended_at": "2026-09-14T16:21:19+00:00",
                    "ended_flat": True,
                    "open_positions": 0,
                    "sealed_open_positions": 10,
                    "reconciliation": {"status": "ok", "counts": {"open_positions": 10}},
                },
            },
            "completeness": {"status": "degraded", "issues": ["production_plan_missing", "risk_decision_missing"]},
            "execution": {"reconciliation": {"status": "blocked", "reason": "local_execution_authority_unavailable"}, "counts": {}},
        },
    }


def running_view() -> dict:
    view = idle_view()
    view["data"]["current_strategy"] = {
        "active": True,
        "specification": {"strategy_type": "grid", "direction": "neutral", "lower_price_boundary": 4080, "upper_price_boundary": 4180, "grid_rung_count": 10},
        "execution": {"fill_count": 6, "open_position_count": 2},
    }
    view["data"]["completeness"] = {"status": "complete", "issues": []}
    view["data"]["execution"] = {"reconciliation": {"status": "ok"}, "counts": {}}
    return view


def test_idle_is_calm_and_says_how_the_last_run_ended() -> None:
    banner = run("banner", idle_view(), {"asset": "黄金"})
    assert banner["tone"] == "idle"
    assert banner["title"] == "现在没有在跑的策略"
    assert banner["body"] == "上一轮在 9/15 结束：价格跌破区间下沿，平仓 10 笔，现已无持仓（停止时还有 10 笔未平，之后才平完）。"
    assert "对账一致" not in banner["body"]
    assert banner["action"] == {"kind": "start", "label": "开始今天的计划"}
    assert "lower_boundary_invalidated" not in json.dumps(banner, ensure_ascii=False)


def test_clean_stop_says_flat_and_reconciled() -> None:
    view = idle_view()
    view["data"]["current_strategy"]["terminal"].update(closed_count=3, ended_at="2026-09-14T09:19:35+00:00", sealed_open_positions=0)
    assert run("banner", view, {"asset": "黄金"})["body"] == "上一轮在 9/14 结束：价格跌破区间下沿，平仓 3 笔，已无持仓，对账一致。"


def test_open_exposure_is_a_warning_not_an_invitation_to_start() -> None:
    view = idle_view()
    view["data"]["current_strategy"]["terminal"].update(closed_count=3, ended_at=None, ended_flat=False, open_positions=7)
    banner = run("banner", view, {"asset": "黄金"})
    assert banner["tone"] == "warn"
    assert "仍有 7 笔持仓未平" in banner["body"] and "对账一致" not in banner["body"]
    assert banner["action"]["kind"] == "details"


def test_unknown_flat_state_claims_neither_flat_nor_reconciled() -> None:
    view = idle_view()
    view["data"]["current_strategy"]["terminal"].update(closed_count=None, ended_flat=None, open_positions=None)
    body = run("banner", view, {"asset": "黄金"})["body"]
    assert "持仓情况未知" in body and "对账一致" not in body and "平仓" not in body


def test_idle_health_is_grey_not_green_and_keeps_every_record() -> None:
    health = run("health", idle_view())
    assert health["tone"] == "calm"
    assert health["text"] == "行情可信 · 没有运行中的计划"
    assert "一切" not in health["text"] and "就绪" not in health["text"]
    assert set(health["expected"]) == {"production_plan_missing", "risk_decision_missing", "reconciliation_blocked:local_execution_authority_unavailable"}
    assert health["other"] == []


def test_unknown_record_is_amber_even_when_idle() -> None:
    view = idle_view()
    view["data"]["completeness"]["issues"].append("ledger_digest_mismatch")
    health = run("health", view)
    assert health["tone"] == "warn"
    assert health["other"] == ["ledger_digest_mismatch"]
    assert health["text"].endswith("1 项需要看一下")


def test_no_plan_records_count_when_a_strategy_runs() -> None:
    view = running_view()
    view["data"]["completeness"] = {"status": "degraded", "issues": ["risk_decision_missing"]}
    health = run("health", view)
    assert health["tone"] == "warn" and health["other"] == ["risk_decision_missing"]


def test_untrusted_market_is_the_one_red_state_and_names_no_fallback() -> None:
    banner = run("banner", idle_view(trusted=False, latest_timestamp="2026-11-05T06:02:00+00:00"), {"asset": "黄金"})
    assert banner["tone"] == "bad"
    assert banner["title"] == "黄金行情中断，已停止开新仓"
    assert "最后可信数据 11/5 14:02" in banner["body"]
    assert "不会改用别的数据源" in banner["body"]
    assert banner["action"]["kind"] == "details"
    assert run("health", idle_view(trusted=False))["tone"] == "bad"


def test_running_grid_shows_its_range_and_fills_and_only_points_to_stop() -> None:
    banner = run("banner", running_view(), {"asset": "黄金"})
    assert banner["tone"] == "run"
    assert banner["title"] == "黄金网格运行中"
    assert banner["body"] == "区间 4,080 – 4,180 · 10 格 · 中性 · 已成交 6 笔 · 持仓 2 笔"
    assert banner["action"] == {"kind": "stop", "label": "去停止"}


def test_testnet_lifecycle_pause_is_amber_and_ended_lifecycle_is_idle() -> None:
    view = idle_view()
    view["livePlan"] = {"source": "testnet_grid_lifecycle", "status": "paused_above_range", "direction": "long", "range": {"low": 60000, "high": 70000}, "grid": {"count": 8}}
    banner = run("banner", view, {"asset": "BTC"})
    assert banner["tone"] == "warn"
    assert banner["title"] == "BTC网格价格高于区间，暂停补单"
    view["livePlan"]["status"] = "sealed"
    assert run("banner", view, {"asset": "BTC"})["tone"] == "idle"


def test_unknown_terminal_reason_is_not_printed_raw() -> None:
    view = idle_view()
    view["data"]["current_strategy"]["terminal"]["reason"] = "brand_new_reason_code"
    body = run("banner", view, {})["body"]
    assert "brand_new_reason_code" not in body and "原因见系统详情" in body


def test_no_read_model_yet_says_so() -> None:
    assert run("banner", None, {})["tone"] == "wait"
    assert run("health", None)["tone"] == "wait"


def test_retired_selection_names_old_and_new_venue_only_when_nothing_is_selected() -> None:
    catalog = {"venue_profiles": [{"id": "okx.paper", "label": "OKX Paper", "environment": "paper"}, {"id": "hyperliquid.testnet", "environment": "testnet"}]}
    selection = {"venue_profile_id": None, "retired_selection": {"venue_profile_id": "binance.paper", "instrument_id": "XAUUSDT.BINANCE"}}
    assert run("retired", selection, catalog) == {"old": "Binance Paper", "next": "OKX Paper"}
    assert run("retired", {**selection, "venue_profile_id": "okx.paper"}, catalog) is None
    assert run("retired", {"venue_profile_id": None}, catalog) is None
