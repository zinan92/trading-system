"""/trade as one text, with GridMind's own files inlined (#1420; the page itself since #1419).

/trade is the desk's template (apps/trading-desk/src/trading_desk/trade.html): a mount point whose
`data-boot` list names the scripts web.js starts, in order, after React draws the skeleton. The skeleton's
rendered markup is apps/web/src/trade/skeleton.reference.html, which a vitest keeps equal to what
Skeleton.tsx renders. Tests that read the page as text use this whole source, so a "never contains" or
"comes before" check still covers every line GridMind ships.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "apps" / "trading-desk" / "src" / "trading_desk" / "trade.html"
MARKUP = ROOT / "apps" / "web" / "src" / "trade" / "skeleton.reference.html"
OWN_CSS = re.compile(r'<link rel="stylesheet" href="(assets/gridmind/[\w.-]+\.css)">')
MOUNT = re.compile(r"""<div id="trade-app"[^>]*data-boot='([^']*)'[^>]*>.*?</div>""", re.S)  # its child is the no-JS fallback line


def _boot(template: str) -> list[str]:
    return json.loads(MOUNT.search(template).group(1))


def gridmind_files(template: str | None = None) -> list[str]:
    """GridMind's own files, in the order /trade loads them: its styles, then its scripts from data-boot."""
    text = PAGE.read_text(encoding="utf-8") if template is None else template
    return OWN_CSS.findall(text) + [path for path in _boot(text) if path.startswith("assets/gridmind/")]


def gridmind_source() -> str:
    template = PAGE.read_text(encoding="utf-8")
    page = OWN_CSS.sub(lambda m: f"<style>{(ROOT / m.group(1)).read_text(encoding='utf-8')}</style>", template)
    scripts = "".join(f"<script>{(ROOT / path).read_text(encoding='utf-8')}</script>\n"
                      for path in _boot(template) if path.startswith("assets/gridmind/"))
    body = f'<div id="trade-app">{MARKUP.read_text(encoding="utf-8")}</div>\n{scripts}'
    return MOUNT.sub(lambda _m: body, page, count=1)
