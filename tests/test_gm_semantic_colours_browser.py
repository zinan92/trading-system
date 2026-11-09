"""#1459: /trade gives each colour one meaning. Cyan is the action and selection colour, green and red are
up/down and profit/loss, amber asks for a look, and a solid red panel only appears when new orders are blocked.
Mocks only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server
from tests.trade_page import open_manual, open_system

ROOT = Path(__file__).resolve().parents[1]
HUES = {"accent": (63, 208, 224), "green": (47, 201, 138), "red": (242, 73, 95), "warn": (240, 165, 46)}

COLOURS = """() => {
  const found = [];
  const parse = value => {
    const m = value.match(/[\\d.]+/g);
    if (!m) return null;
    const [r, g, b, a = 1] = m.map(Number);
    if (+a === 0 || Math.max(r, g, b) - Math.min(r, g, b) < 24) return null;  // transparent or neutral grey
    return [Math.round(r), Math.round(g), Math.round(b)];
  };
  for (const element of document.querySelectorAll('body *')) {
    if (element.closest('script,style,svg,canvas,.tv-lightweight-charts')) continue;
    const box = element.getBoundingClientRect(), style = getComputedStyle(element);
    if (!box.width || !box.height || style.visibility === 'hidden') continue;
    const id = element.id ? '#' + element.id : element.tagName.toLowerCase() + '.' + [...element.classList].slice(0, 2).join('.');
    const own = [...element.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    const checks = [['background', style.backgroundColor]];
    if (own) checks.push(['text', style.color]);
    if (parseFloat(style.borderLeftWidth) > 0) checks.push(['border', style.borderLeftColor]);
    if (parseFloat(style.borderTopWidth) > 0) checks.push(['border', style.borderTopColor]);
    for (const [kind, value] of checks) {
      const rgb = parse(value);
      if (rgb) found.push({id, kind, rgb, solid: kind === 'background' && box.width >= 40 && box.height >= 20 && !/rgba\\(.*,\\s*0?\\.\\d+\\)$/.test(value)});
    }
  }
  return found;
}"""


def _hue(rgb: list[int]) -> str | None:
    for name, base in HUES.items():
        if max(abs(a - b) for a, b in zip(rgb, base)) <= 3:
            return name
    return None


def _open(runtime, origin: str, width: int, model: dict):
    browser = runtime.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": width, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/trading-system/read-model",
               lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
    page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(model["market"], ensure_ascii=False)))
    page.goto(f"{origin}/trade", wait_until="load")
    page.wait_for_function("() => window.gridmindView && window.gridmindView.data")
    open_manual(page)
    open_system(page)
    return browser, page, errors


@pytest.mark.parametrize("width", [1680, 375])
def test_every_coloured_element_uses_one_of_the_four_meanings(width: int) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser, page, errors = _open(runtime, origin, width, model)
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page.add_style_tag(content="*,*::before,*::after{transition:none!important;animation:none!important}")
        page.wait_for_timeout(50)  # measure end colours, not a hover/selection transition caught mid-way
        found = page.evaluate(COLOURS)
        strays = [item for item in found if _hue(item["rgb"]) is None]
        assert strays == [], strays[:12]
        solid_red = [item["id"] for item in found if item["solid"] and _hue(item["rgb"]) == "red"]
        assert solid_red == [], solid_red  # an accepted plan with live data blocks nothing; small buy/sell dots are not panels
        reject = page.evaluate("() => getComputedStyle(document.querySelector('#parkAiReject')).color")
        assert _hue([int(v) for v in reject[4:-1].split(",")[:3]]) is None, reject  # rejecting a draft is not a loss or a block
        assert errors == []
        browser.close()


def test_chart_candles_take_the_desk_green_and_red() -> None:
    gridmind = (ROOT / "assets/gridmind/gridmind.js").read_text()
    assert 'upColor:deskColour("--gm-green"' in gridmind and 'downColor:deskColour("--gm-red"' in gridmind


def test_the_desk_stylesheet_has_no_off_palette_literals() -> None:
    css = (ROOT / "apps/trading-desk/src/trading_desk/static/gm-desk.css").read_text().lower()
    for literal in ("#f0a24b", "#d9a35b", "#7fb2ff", "#5a4526", "240,162,75"):
        assert literal not in css, literal
