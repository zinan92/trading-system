"""The conformance contract every Desk Venue Module must meet (ADR 0007, #1379).

Run with every upstream dead: a module must fail closed. It must give no price, return no ready
preview, make no confirm call and start no driver. It must also report its gaps instead of raising
where the page reads. ``tests/test_venue_conformance.py`` runs this over every registered module, so
a new module is covered without anyone listing it. A module that declares ``execute`` and fails
here does not merge.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .base import CAPABILITIES, VenueModule

PLAN_KEYS = ("rungs", "range_pct", "stop_pct", "unit")


class DeadUpstream:
    """A fetch where every call fails, recording what was asked."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, body: Any = None) -> Any:
        # The body too: a confirmation can travel as {"action": "confirm"} to a URL that does not say so.
        self.calls.append(f"{url} {json.dumps(body, default=str, ensure_ascii=False)}")
        raise ConnectionError("conformance: upstream is down")


def problems(module: VenueModule, *, sources: Any, executor: Any, asset: Mapping[str, Any], upstream: DeadUpstream,
             driver_calls: Callable[[], int], build_plan: Callable[..., dict[str, Any]]) -> list[str]:
    """Every way ``module`` breaks the contract with its upstream down; empty means it conforms."""
    found: list[str] = []
    caps = module.capabilities
    if not caps <= CAPABILITIES:
        found.append(f"unknown capabilities {sorted(caps - CAPABILITIES)}")
    try:
        venue, money = module.venue_text(sources, asset)
        if not (isinstance(venue, str) and venue and isinstance(money, str) and money):
            found.append("venue_text must give two non-empty texts")
    except Exception as exc:  # noqa: BLE001 - the page reads this; it must not raise
        found.append(f"venue_text raised {type(exc).__name__}")
    try:
        grid, price = module.grid_and_price(sources, asset)
        if price is not None:
            found.append(f"gave a price ({price}) with its upstream down")
        if not isinstance(grid, dict) or grid.get("ok") is True:
            found.append("grid must be a dict that is not ok with its upstream down")
    except Exception as exc:  # noqa: BLE001
        found.append(f"grid_and_price raised {type(exc).__name__}")
    if "bars" in caps:
        try:
            bars = module.bars(sources, asset, "1h", 5)
            if not isinstance(bars, dict) or bars.get("ok") is not False or not isinstance(bars.get("bars"), list) or not bars.get("reason"):
                found.append("bars must say ok=false with a reason and an empty list when the feed is down")
        except Exception as exc:  # noqa: BLE001
            found.append(f"bars raised {type(exc).__name__}")
    plan: dict[str, Any] = {"kind": "new", "direction": "long", "range": [90.0, 100.0], "rungs": [90.0, 95.0], "hard_stop": 85.0, "max_loss": 1.0}
    if "grid_plan" in caps:
        template = module.plan_template or {}
        missing = [key for key in PLAN_KEYS if not template.get(key)]
        if missing or ("notional" in template) == ("units" in template):
            found.append(f"plan template needs {PLAN_KEYS} and exactly one of notional / units")
        else:
            plan = build_plan(module, "long", 100.0, {})
    before = list(upstream.calls)
    if "preview" in caps:
        try:
            preview = module.preview(executor, asset, plan)
            if preview.get("execution_ready") is True:
                found.append("preview was ready with its upstream down")
        except Exception:  # noqa: BLE001 - refusing or failing are both closed; the caller records why
            pass
    if "execute" in caps:
        try:
            result = module.execute(executor, asset, plan, shown_max_loss=plan.get("max_loss"), judgment_id=0)
            if result.get("started"):
                found.append("execute started with its upstream down")
        except Exception:  # noqa: BLE001
            pass
    asked = upstream.calls[len(before):]
    if any("confirm" in call for call in asked):
        found.append("asked for a confirmation with its upstream down")
    if driver_calls():
        found.append("started a driver with its upstream down")
    return found
