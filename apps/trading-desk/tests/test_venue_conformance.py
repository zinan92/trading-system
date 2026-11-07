"""Every registered Desk Venue Module meets the fail-closed contract (ADR 0007, #1379).

Parametrized over the registry itself, so a module added there is checked here without being listed.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fake_venue import FakeVenue
from trading_desk import plans
from trading_desk.executor import Executor
from trading_desk.sources import Sources
from trading_desk.store import Store
from trading_desk.venues import DEFAULT, build
from trading_desk.venues.conformance import DeadUpstream, problems

REGISTRY = build(*DEFAULT.modules.values(), FakeVenue())  # the shipped modules plus the acceptance fake


def _check(module, config, tmp_path: Path) -> list[str]:
    upstream, driven = DeadUpstream(), []
    config = replace(config, paper_output=tmp_path / "empty-output")
    sources = Sources(config, fetch=upstream, venues=REGISTRY)
    executor = Executor(config, upstream, run=lambda *a, **k: driven.append(a), venues=REGISTRY)
    seeded = next((a for a in Store(tmp_path / "c.db").assets() if a["kind"] == module.kind), None)
    asset = seeded or {"key": "FAKE", "label": "假", "kind": module.kind, "venue_profile_id": "", "instrument_id": "FAKE-1", "coin": "FAKE", "news_queries": [], "kline_key": None}
    return problems(module, sources=sources, executor=executor, asset=asset, upstream=upstream, driver_calls=lambda: len(driven), build_plan=plans.build_plan)


@pytest.mark.parametrize("kind", REGISTRY.kinds())
def test_every_venue_fails_closed_with_its_upstream_down(kind: str, config, tmp_path: Path) -> None:
    assert _check(REGISTRY.get(kind), config, tmp_path) == []


def test_the_shipped_registry_is_all_covered() -> None:
    assert set(DEFAULT.kinds()) <= set(REGISTRY.kinds())


def test_a_module_that_keeps_a_price_when_the_feed_is_down_fails(config, tmp_path: Path) -> None:
    class Stale(FakeVenue):
        kind = "stale"
        def grid_and_price(self, sources, asset):
            return {"ok": False}, 76253.0  # a remembered price is not a live one (#1372)
    assert any("gave a price" in p for p in _check(build(Stale()).get("stale"), config, tmp_path))


def test_a_module_that_confirms_without_a_ready_preview_fails(config, tmp_path: Path) -> None:
    class Eager(FakeVenue):
        kind = "eager"
        def execute(self, executor, asset, plan, *, shown_max_loss, judgment_id):
            try:
                executor.fetch("http://fake-venue.test/confirm", {})
            except ConnectionError:
                pass
            return {"started": False}
    assert any("confirmation" in p for p in _check(build(Eager()).get("eager"), config, tmp_path))


def test_a_confirmation_in_the_body_is_caught_too(config, tmp_path: Path) -> None:
    """Paper gold confirms with {"action": "confirm"} to a chat URL; a URL-only check would miss that."""
    class Quiet(FakeVenue):
        kind = "quiet"
        def execute(self, executor, asset, plan, *, shown_max_loss, judgment_id):
            try:
                executor.fetch("http://fake-venue.test/chat", {"action": "confirm", "draft": "x"})
            except ConnectionError:
                pass
            return {"started": False}
    assert any("confirmation" in p for p in _check(build(Quiet()).get("quiet"), config, tmp_path))
