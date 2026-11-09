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


def with_gate(view: dict, **market) -> dict:
    """The read model's own market (#1441): the configured instrument, 1m."""
    view["gateMarket"] = {"provider_label": "OKX 1m", "latest_timestamp": "2026-11-05T02:40:00+00:00", "timeframe": "1m", **market}
    return view


def test_the_read_model_feed_counts_for_the_same_asset_and_names_itself() -> None:
    """#1441: never looser than the chart alone; the red body is worded from the feed that failed."""
    view = with_gate(with_run(idle_view(symbol="GOLD")), symbol="GOLD", trusted=False)
    banner = run("banner", view, {"asset": "黄金"})
    assert banner["tone"] == "bad" and banner["title"] == "黄金行情中断，已停止开新仓"
    assert "OKX 1m的数据没有通过可信检查（最后可信数据 11/5 10:40）" in banner["body"]
    assert run("health", view)["text"].startswith("行情没有通过可信检查")
    btc = with_gate(with_run(idle_view(symbol="BTC")), symbol="GOLD", trusted=False)
    assert run("banner", btc, {"asset": "BTC"})["tone"] == "idle"
    assert run("health", btc)["text"] == "行情可信"


def test_a_failed_1m_feed_is_named_even_when_the_chart_shows_a_trusted_4h() -> None:
    """#1443: marketTrusted judges the selected symbol's 1m feed (execMarket); the red body names that feed."""
    view = with_run(idle_view(symbol="GOLD", trusted=False, timeframe="4h"))
    view["execMarket"] = {"symbol": "GOLD", "timeframe": "1m", "trusted": False, "provider_label": "OKX 1m",
                          "latest_timestamp": "2026-11-05T02:40:00+00:00"}
    banner = run("banner", view, {"asset": "黄金"})
    assert banner["tone"] == "bad"
    assert banner["body"].startswith("OKX 1m的数据没有通过可信检查（最后可信数据 11/5 10:40）")
    assert run("health", view)["text"].startswith("行情没有通过可信检查")


def test_a_feed_with_no_provider_says_so_in_plain_words() -> None:
    """Production, weekend: provider "unavailable", label "Unavailable", no timestamp."""
    view = with_gate(with_run(idle_view(symbol="GOLD")), symbol="GOLD", trusted=False,
                     provider="unavailable", provider_label="Unavailable", latest_timestamp="")
    body = run("banner", view, {"asset": "黄金"})["body"]
    assert body.startswith("行情源现在没有返回可用数据（还没有可信数据）。")
    assert "Unavailable" not in body


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
    # #1438: no start button on the banner; it points at 「今天」, the one way in.
    assert banner["action"] is None and banner["startable"] is True
    assert banner["next"] == "今天的计划从「今天」第 1 步开始。"
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
    assert health["text"] == "行情可信"  # whether a plan runs is the banner's to say, once (#1429)
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
    # #1449: range, grids, fills and positions are said once, in 「这一轮」; no unrealized figure here, so no body.
    assert banner["body"] == ""
    assert banner["action"] == {"kind": "stop", "label": "去停止"}
    view = running_view()
    view["data"]["execution"]["pnl"] = {"net_realized": -122.14, "unrealized": 21.4, "total": -100.74}
    assert run("banner", view, {"asset": "黄金"})["body"] == "浮动盈亏 +21.4 USD"


def park_running_view() -> dict:
    """Shaped like tests/test_dashboard_gridmind_order_lifecycle_browser.py's running Park strategy."""
    view = running_view()
    view["data"]["current_strategy"]["specification"] = {
        "strategy_type": "grid", "direction": "short", "lower_price_boundary": 3800.0, "upper_price_boundary": 4000.0,
        "stop_price": 4000.0, "take_profit_price": None, "maximum_leverage": 10.0, "maximum_acceptable_loss": 500.0,
        "maximum_notional": 40000.0, "theoretical_max_loss": 480.0, "order_count": 19, "grid_spacing": 10.0, "grid_rung_count": 19,
    }
    view["data"]["current_strategy"]["execution"] = {"fill_count": 7, "open_position_count": 2}
    view["data"]["execution"]["pnl"] = {"net_realized": -122.14, "unrealized": -8.5, "total": -130.64}
    return view


def test_this_run_lists_only_the_facts_a_field_backs() -> None:
    """#1449: 「这一轮」 replaces 「今天」's steps while a plan runs."""
    panel = run("thisRun", park_running_view())
    assert panel["title"] == "这一轮 · 网格运行中"
    assert [(f["label"], f["value"]) for f in panel["facts"]] == [
        ("区间", "3,800 – 4,000"), ("方向", "做空"), ("格数", "19 格 · 间距 10"), ("成交", "7 次"), ("持仓", "2 笔"),
        ("止损", "4,000"), ("计划最坏", "−480 / 上限 −500 USD"), ("浮动盈亏", "−8.5 USD")]
    labels = {f["label"] for f in panel["facts"]}
    assert not labels & {"止盈", "已实现", "已用", "买", "卖", "出区间时", "每格"}  # no field backs these (take profit is null)
    assert panel["stale"] is None
    assert run("thisRun", idle_view()) is None


def test_this_run_stays_when_the_feed_fails_or_a_read_fails() -> None:
    untrusted = park_running_view()
    untrusted["marketTrusted"] = False
    assert run("banner", untrusted, {"asset": "黄金"})["tone"] == "bad"  # red banner, and the grid is still shown
    assert run("thisRun", untrusted)["facts"][0] == {"label": "区间", "value": "3,800 – 4,000"}
    failed = park_running_view()
    failed["readError"] = "HTTP 502"
    assert run("thisRun", failed)["stale"] == "最新状态读不到，下面是上次读到的。"


def test_this_run_follows_a_testnet_lifecycle_until_it_ends() -> None:
    view = idle_view()
    view["livePlan"] = {"source": "testnet_grid_lifecycle", "status": "paused_above_range", "direction": "long", "range": {"low": 60000, "high": 70000}, "grid": {"count": 8}}
    panel = run("thisRun", view)
    assert panel["title"] == "这一轮 · 网格价格高于区间，暂停补单"
    assert panel["facts"][:3] == [{"label": "区间", "value": "60,000 – 70,000"}, {"label": "方向", "value": "做多"}, {"label": "格数", "value": "8 格"}]
    view["livePlan"]["status"] = "hard_stop_triggered"
    assert run("thisRun", view)["title"] == "这一轮 · 网格已触发硬止损，正在平仓"
    view["livePlan"]["status"] = "sealed"
    assert run("thisRun", view) is None


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


# ---- #1429: GridMind's run verdict and a failed read reach the banner; start only when all is calm ----

def with_run(view: dict | None, **run) -> dict:
    view = dict(view or {})
    view["runState"] = {"label": "已停止", "runClass": "stopped", "tickLost": False, **run}
    return view


TICK = {"status": "blocked", "reason": "heartbeat_stale", "age_seconds": 181, "max_age_seconds": 180}


def with_tick(view: dict) -> dict:
    view["data"]["runtime"] = {"execution_tick_health": TICK}
    return with_run(view, label="运行降级", runClass="stopped", tickLost=True)


def test_a_failed_read_outranks_everything_and_says_the_page_is_old() -> None:
    first = run("banner", {"readError": "Dashboard 服务暂不可用：服务返回 HTTP 502。"}, {"asset": "黄金"})
    assert first == {"tone": "bad", "title": "读不到交易状态", "body": "Dashboard 服务暂不可用：服务返回 HTTP 502。", "action": None}
    stale = with_run(running_view(), label="运行中", runClass="running")
    stale.update(readError="Cloudflare 隧道不可达。", renderedAt=1762603800000)  # 2025-11-08 12:10 UTC
    banner = run("banner", stale, {"asset": "黄金"})
    assert banner["tone"] == "bad" and banner["title"] == "读不到最新交易状态"
    assert banner["body"] == "Cloudflare 隧道不可达。 页面上的数字停在 11/8 20:10 那次读取，不代表现在。"
    assert "运行中" not in json.dumps(banner, ensure_ascii=False) and banner["action"] is None
    untrusted = idle_view(trusted=False)
    untrusted["readError"] = "x"
    assert run("banner", untrusted, {})["title"] == "读不到最新交易状态"
    assert run("health", stale) == {"tone": "bad", "text": "读不到最新状态", "other": [], "expected": []}


def test_a_running_grid_names_a_stale_heartbeat_and_a_failed_check() -> None:
    lost = run("banner", with_tick(running_view()), {"asset": "黄金"})
    assert lost["tone"] == "warn" and lost["title"] == "黄金网格运行降级"
    assert lost["body"] == "执行心跳 181 秒没更新（上限 180 秒）。"
    assert lost["action"] == {"kind": "stop", "label": "去停止"}
    error = run("banner", with_run(running_view(), label="运行异常", runClass="error"), {"asset": "黄金"})
    assert error["tone"] == "bad" and error["title"] == "黄金网格运行异常"
    assert "原因见系统详情" in error["body"] and error["action"]["kind"] == "stop"
    calm = run("banner", with_run(running_view(), label="运行中", runClass="running"), {"asset": "黄金"})
    assert calm["tone"] == "run" and calm["title"] == "黄金网格运行中"


def test_nothing_running_but_an_unhealthy_runtime_offers_no_start() -> None:
    lost = run("banner", with_tick(idle_view()), {"asset": "黄金"})
    assert lost["title"] == "执行心跳中断" and lost["tone"] == "warn"
    assert lost["body"] == "执行心跳 181 秒没更新（上限 180 秒）。 恢复之前不要开始新计划。"
    error = run("banner", with_run(idle_view(), label="运行异常", runClass="error"), {"asset": "黄金"})
    assert error["title"] == "运行状态异常" and error["tone"] == "bad" and error["action"]["kind"] == "details"
    working = run("banner", with_run(idle_view(), label="启动中", runClass="working"), {"asset": "黄金"})
    assert working == {"tone": "wait", "title": "黄金启动中", "body": "", "action": None}
    odd = run("banner", with_run(idle_view(), label="运行中", runClass="running"), {"asset": "黄金"})
    assert odd["tone"] == "warn" and "对不上" in odd["body"] and odd["action"]["kind"] == "details"


def test_start_is_offered_only_when_everything_is_calm() -> None:
    """The safety rule of the strip: only a calm idle state is startable and points at 「今天」 (#1429). Since #1438 no
    state puts a start button on the banner, so the banner and 「今天」 never both offer a way in."""
    open_exposure = idle_view()
    open_exposure["data"]["current_strategy"]["terminal"].update(ended_flat=False, open_positions=2)
    states = {
        "calm idle": with_run(idle_view()),
        "calm idle, older page without a verdict": idle_view(),
        "reading": None,
        "read failed": {"readError": "x"},
        "read failed with old idle data": {**with_run(idle_view()), "readError": "x"},
        "untrusted feed": with_run(idle_view(trusted=False)),
        "heartbeat lost": with_tick(idle_view()),
        "runtime error": with_run(idle_view(), label="运行异常", runClass="error"),
        "starting": with_run(idle_view(), label="启动中", runClass="working"),
        "runtime says running": with_run(idle_view(), label="运行中", runClass="running"),
        "open exposure": with_run(open_exposure),
        "running": with_run(running_view(), label="运行中", runClass="running"),
        "running, heartbeat lost": with_tick(running_view()),
        "running, error": with_run(running_view(), label="运行异常", runClass="error"),
        # #1441: the read model's feed counts when it is the chart's instrument.
        "chart trusted, read model untrusted (same asset)": with_gate(with_run(idle_view(symbol="GOLD")), symbol="GOLD", trusted=False),
        "chart untrusted, read model trusted": with_gate(with_run(idle_view(symbol="GOLD", trusted=False)), symbol="GOLD", trusted=True),
        "chart trusted, read model trusted": with_gate(with_run(idle_view(symbol="GOLD")), symbol="GOLD", trusted=True),
        "chart BTC trusted, gold read model untrusted": with_gate(with_run(idle_view(symbol="BTC")), symbol="GOLD", trusted=False),
    }
    banners = {name: run("banner", view, {"asset": "黄金"}) for name, view in states.items()}
    calm = {"calm idle", "calm idle, older page without a verdict", "chart trusted, read model trusted", "chart BTC trusted, gold read model untrusted"}
    assert {name for name, b in banners.items() if b.get("startable")} == calm
    assert {name for name, b in banners.items() if b.get("next")} == calm
    assert all((b["action"] or {}).get("kind") != "start" for b in banners.values())
