import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_OBJECTS = [
    "command-center.html",
    "dashboard-dualtrack-split.html",
    "dashboard-dualtrack-v5.html",
    "dashboard-dualtrack-replay.html",
    "ops-dashboard.html",
    "dashboard-replay-v4.html",
]
OBJECTS = HTML_OBJECTS + ["assets/shell.css"]

CORE_TOKENS = [
    "--bg",
    "--panel",
    "--panel2",
    "--ink",
    "--muted",
    "--faint",
    "--gold",
    "--run",
    "--warn",
    "--block",
    "--unknown",
    "--human",
    "--machine",
    "--rule",
    "--rule-strong",
    "--r",
    "--mono",
    "--sans",
    "--fs-0",
    "--fs-1",
    "--fs-2",
    "--fs-3",
    "--fs-4",
    "--fs-5",
    "--fs-6",
    "--sp-1",
    "--sp-2",
    "--sp-3",
    "--sp-4",
    "--sp-5",
]

TOKEN_COLOR_WHITELIST = {
    "#0a0b0c",
    "#101216",
    "#15181e",
    "#e8eaed",
    "#9aa3ad",
    "#5d666f",
    "#d8aa3f",
    "#35d07f",
    "#e8a33d",
    "#ef5f5f",
    "#7d8590",
    "#7aa2ff",
    "#b894ff",
    "rgba(255,255,255,.08)",
    "rgba(255,255,255,.16)",
}

COLOR_RE = re.compile(r"#[0-9a-fA-F]{6,8}|rgba\([^)]*\)")
STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
FORBIDDEN_WEIGHT_RE = re.compile(
    r"(?:font-weight\s*:\s*|font\s*:[^;{}]*\b)(?:640|660|720|740|760|800|820|900)\b"
)
FORBIDDEN_FRACTIONAL_SIZE_RE = re.compile(
    r"(?:font-size\s*:\s*|font\s*:[^;{}]*\b)(?:10\.5|11\.5|12\.5)px"
)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def normalize_color(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def style_blocks(html: str) -> str:
    return "\n".join(STYLE_RE.findall(html))


def test_tokens_css_exists_and_contains_core_tokens():
    css_path = ROOT / "assets/tokens.css"

    assert css_path.exists()
    css = css_path.read_text(encoding="utf-8")
    for token in CORE_TOKENS:
        assert token in css


def test_active_pages_link_tokens_and_shell_uses_only_token_vars():
    for path in HTML_OBJECTS:
        html = read(path)
        assert '<link rel="stylesheet" href="assets/tokens.css">' in html
        assert html.index("assets/tokens.css") < html.index("<style>")

    shell = read("assets/shell.css")
    assert "var(--" in shell
    assert not COLOR_RE.findall(shell)


def test_page_style_blocks_do_not_define_unauthorized_color_literals():
    allowed = {normalize_color(color) for color in TOKEN_COLOR_WHITELIST}
    violations = {}

    for path in HTML_OBJECTS:
        colors = {normalize_color(color) for color in COLOR_RE.findall(style_blocks(read(path)))}
        unexpected = sorted(colors - allowed)
        if unexpected:
            violations[path] = unexpected

    assert not violations


def test_no_forbidden_font_weights_remain():
    violations = {}
    for path in OBJECTS:
        matches = sorted(set(match.group(0) for match in FORBIDDEN_WEIGHT_RE.finditer(read(path))))
        if matches:
            violations[path] = matches

    assert not violations


def test_no_forbidden_fractional_font_sizes_remain():
    violations = {}
    for path in OBJECTS:
        matches = sorted(set(match.group(0) for match in FORBIDDEN_FRACTIONAL_SIZE_RE.finditer(read(path))))
        if matches:
            violations[path] = matches

    assert not violations


def test_ops_dashboard_uses_dark_token_palette():
    html = read("ops-dashboard.html").lower()

    assert "#f6efe4" not in html
    assert "#fffaf1" not in html
    assert "#17130c" not in html
    assert "var(--bg)" in html
    assert "var(--ink)" in html
    assert "var(--panel)" in html


def test_blind_answer_regression_assertions_stay_present():
    test_source = read("tests/test_dashboard_dualtrack_static.py")

    assert "test_dualtrack_v5_keeps_machine_track_blind_and_without_intervention_surface" in test_source
    assert "盲测中 · 仅显示 PnL" in test_source
    assert "进出场点位、库存、网格状态收盘后揭示" in test_source
    assert "本界面不提供机器轨干预操作" in test_source
    assert "test_dualtrack_v5_does_not_fetch_attribution_before_cycle_close" in test_source
