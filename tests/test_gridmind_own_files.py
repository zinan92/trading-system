"""#1420: GridMind's styles and scripts are files; since #1419 /trade is the only page that loads them.

Every file the page loads must exist and be served by the desk, which reads them from disk (#1414). 8765 and the
cloud gateway still list them (never cached / allowed) until those paths are retired with the redesign.
"""
from __future__ import annotations

import re
import sys

from tests.gridmind_page import PAGE, ROOT, gridmind_files

sys.path.insert(0, str(ROOT / "apps" / "trading-desk" / "src"))


def test_the_page_has_no_inline_style_or_script_left() -> None:
    shell = PAGE.read_text(encoding="utf-8")
    shell = re.sub(r"<!--.*?-->", "", shell, flags=re.S)
    assert re.findall(r"<style[^>]*>", shell) == []
    assert [tag for tag in re.findall(r"<script[^>]*>", shell) if "src=" not in tag] == []
    assert len(PAGE.read_bytes()) < 5_000


def test_the_page_loads_its_files_in_the_old_order() -> None:
    assert gridmind_files() == [
        "assets/gridmind/base.css", "assets/gridmind/skin.css",
        "assets/gridmind/control-intents.js", "assets/gridmind/read.js", "assets/gridmind/chart.js", "assets/gridmind/control.js", "assets/gridmind/gridmind.js",
        "assets/gridmind/park-ai-chat.js", "assets/gridmind/dashboard-control.js",
    ]
    for path in gridmind_files():
        assert (ROOT / path).is_file() and (ROOT / path).stat().st_size > 0


def test_the_old_single_file_page_is_gone() -> None:
    """#1419: /trade replaced dashboard-gridmind.html; 8765's old addresses send the browser to /trade."""
    assert not (ROOT / "dashboard-gridmind.html").exists()


def test_every_server_that_carries_the_page_forwards_its_files() -> None:
    from pipelines.dashboard_server import GRIDMIND_OWN_FILES, DashboardHandler
    from services.cloud_access_gateway import _is_allowed
    from trading_desk.gridmind import STATIC_FILES

    paths = ["/" + path for path in gridmind_files()]
    assert list(GRIDMIND_OWN_FILES) == paths
    for path in paths:
        assert STATIC_FILES[path] in {"text/css; charset=utf-8", "text/javascript; charset=utf-8"}
        assert _is_allowed(path)
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = path
        assert handler._should_disable_static_cache()
