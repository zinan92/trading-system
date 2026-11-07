"""The venue template in docs/venues stays a working, conforming module (#1381)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from test_venue_conformance import _check
from trading_desk.venues import build

TEMPLATE = Path(__file__).resolve().parents[3] / "docs" / "venues" / "template_venue.py"


def _template_class():
    spec = importlib.util.spec_from_file_location("template_venue", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TemplateVenue


def test_the_template_builds_with_everything_it_declares() -> None:
    venue = build(_template_class()()).get("template_venue")
    assert venue.capabilities == {"live_price", "bars", "grid_plan"}


def test_the_template_fails_closed(config, tmp_path: Path) -> None:
    assert _check(build(_template_class()()).get("template_venue"), config, tmp_path) == []
