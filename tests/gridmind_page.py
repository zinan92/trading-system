"""dashboard-gridmind.html with its own files inlined back (#1420).

The page loads its styles and scripts from assets/gridmind/. Tests that read the page as text use
this whole source, so a "never contains" or "comes before" check still covers every line it did.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "dashboard-gridmind.html"
OWN_FILE = re.compile(r'<link rel="stylesheet" href="(assets/gridmind/[\w.-]+\.css)">|<script src="(assets/gridmind/[\w.-]+\.js)"></script>')


def gridmind_files(shell: str | None = None) -> list[str]:
    """The page's own files, in the order the page loads them."""
    return [css or js for css, js in OWN_FILE.findall(PAGE.read_text(encoding="utf-8") if shell is None else shell)]


def gridmind_source() -> str:
    def inline(match: re.Match[str]) -> str:
        path = match.group(1) or match.group(2)
        tag = "style" if match.group(1) else "script"
        return f"<{tag}>{(ROOT / path).read_text(encoding='utf-8')}</{tag}>"

    return OWN_FILE.sub(inline, PAGE.read_text(encoding="utf-8"))
