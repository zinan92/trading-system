"""#1451: the account strip is three cards (equity, cumulative PnL, positions) for both sources. Mocks only."""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server


def _cards(model: dict, width: int) -> tuple[list[list[str]], int, list[str]]:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/trading-system/read-model",
                   lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
        page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []})))
        page.goto(f"{origin}/trade", wait_until="load")
        page.locator("#account .metric").first.wait_for(state="attached")
        cards = page.evaluate("() => [...document.querySelectorAll('#account .metric')].map(m => [...m.children].map(c => c.textContent))")
        width_now = page.evaluate("() => document.documentElement.scrollWidth")
        browser.close()
        return cards, width_now, errors


@pytest.mark.parametrize("width", [1680, 375])
def test_the_paper_ledger_is_three_cards(width: int) -> None:
    model = _read_model("accepted")
    model["current_strategy"]["ledger"] = {"equity": 9877.86, "starting_cash": 10000, "active": False,
                                           "open_positions": 0, "fees": 4.51, "realized": -122.14, "unrealized": 0}
    cards, scroll, errors = _cards(model, width)
    assert cards == [
        ["纸面盘权益", "9,877.86", "初始 10,000 USD · 最近一次会话"],
        ["累计盈亏", "-122.14", "已实现 -122.14 · 浮动 0 · 收益率 -1.22%"],
        ["持仓", "0 笔", "手续费 4.51 USD"],
    ]
    assert scroll <= width and errors == []


def test_the_execution_account_is_three_cards_and_unknown_stays_unknown() -> None:
    model = _read_model("accepted")
    model["current_strategy"].pop("ledger", None)
    cards, _, errors = _cards(model, 1680)
    assert [c[0] for c in cards] == ["账户权益", "累计盈亏", "持仓"]
    assert all("每格计划净利目标" not in " ".join(c) for c in cards)
    assert errors == []
