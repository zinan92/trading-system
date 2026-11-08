"""Serving /trade to a browser test, and acting on it the way the page is built (#1419).

/trade is the only page that runs GridMind since dashboard-gridmind.html was retired. Two things differ from the
old page, and tests that drove the old page meet both:
- gm-desk.css hides GridMind's raw start/stop buttons and its old status badges (the desk's 「今天」 flow and the
  header island show those instead), and folds the rest of the control rail into 「手动配置与 AI 助手」.
- the desk's own scripts read a few endpoints of their own (assets, watch, the desk state, news).
`press` clicks a control the way the intent router sees a press: a bubbling click on the element, after it is
enabled, whether or not the desk shows it. `text` waits for an element's text, shown or not.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "apps" / "trading-desk" / "src" / "trading_desk" / "static"
if str(ROOT / "apps" / "trading-desk" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "apps" / "trading-desk" / "src"))

DESK_ASSETS = {"assets": [
    {"key": "BTC", "label": "BTC", "kind": "hl_testnet", "instrument_id": "BTC-USD-PERP"},
    {"key": "XAU", "label": "黄金", "kind": "xau_paper", "instrument_id": "XAU-USDT-SWAP"},
]}
DESK_STATE = {"kline": {"ok": True, "structure": "日线偏多", "synthesis": "金价修复。"}, "today_judgment": None, "executions": [],
              "ai_today": None, "ai_running": False, "control": {"can_stop": False},
              "meta": {"kind": "xau_paper", "capabilities": ["bars", "execute", "grid_plan", "live_price", "preview"]}}


def trade_html() -> str:
    """Exactly what the desk's /trade sends."""
    from trading_desk.gridmind import render_trade_page

    return render_trade_page()


def desk_answer(path: str) -> dict | None:
    """The desk's own reads, answered quietly; None for anything else."""
    if path == "/api/assets":
        return DESK_ASSETS
    if path == "/api/watch":
        return {"items": []}
    if path.startswith("/api/desk/"):
        return DESK_STATE
    if path.startswith("/api/news/"):
        return {"ok": True, "items": []}
    if path == "/api/review/runs":  # the desk's 交易回看 tab: no past runs
        from trading_desk import review_timeline

        return review_timeline.filtered([], [], asset=None, days=0)
    return None


def static_file(path: str) -> Path | None:
    """A /static/... path of the desk, from disk."""
    if not path.startswith("/static/"):
        return None
    local = STATIC / path.removeprefix("/static/")
    return local if local.is_file() else None


def press(page, selector: str) -> None:
    page.locator(f"{selector}:not([disabled])").first.wait_for(state="attached")
    page.locator(selector).first.dispatch_event("click")


def text(page, selector: str, words: str, timeout: float = 10000) -> None:
    page.wait_for_function(
        "([s, w]) => [...document.querySelectorAll(s)].some(el => el.textContent.includes(w))", arg=[selector, words], timeout=timeout)


def open_system(page) -> None:
    """Unfold 「系统详情」, where /trade keeps GridMind's current-strategy and production-status cards."""
    page.locator("#deskSystem").wait_for(state="attached")
    page.evaluate("() => { document.getElementById('deskSystem').open = true; }")


def open_manual(page) -> None:
    """Unfold 「自己调」 (under step 2 of 「今天」 since #1438), where /trade keeps GridMind's own form."""
    page.locator("#deskManual").wait_for(state="attached")
    page.evaluate("() => { document.getElementById('deskManual').open = true; }")
