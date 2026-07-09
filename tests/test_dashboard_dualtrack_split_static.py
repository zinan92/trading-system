import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPLIT_PAGE = "dashboard-dualtrack-split.html"


WIDGETS = {
    "direction": "方向",
    "levels": "关键位",
    "signal": "信号",
    "kline": "K线图",
    "context": "大级别图",
    "order": "下单",
    "risk": "风险",
    "position": "持仓",
    "fills": "成交",
    "pnl": "收益",
    "data": "数据",
    "status": "状态",
    "review": "复盘",
}


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def read_split_html() -> str:
    path = ROOT / SPLIT_PAGE
    assert path.exists(), f"{SPLIT_PAGE} must exist as the parallel split-canvas page"
    return path.read_text(encoding="utf-8")


def canvas(html: str, track: str) -> str:
    pattern = (
        rf'<section class="canvas {track}"[^>]*data-track="{track}"[^>]*>'
        rf'(?P<body>.*?)</section>\s*<!-- /{track} canvas -->'
    )
    match = re.search(pattern, html, re.S)
    assert match, f"{track} canvas must be present and explicitly closed"
    return match.group("body")


def test_split_page_declares_two_canvases_and_exact_widget_registry():
    html = read_split_html()

    assert '<link rel="stylesheet" href="assets/tokens.css">' in html
    assert '<section class="canvas human" data-track="human"' in html
    assert '<section class="canvas machine" data-track="machine"' in html
    assert html.count('data-widget="phase"') == 2
    assert "Context图" not in html
    assert "balance" not in html

    for track in ("human", "machine"):
        body = canvas(html, track)
        for slug, label in WIDGETS.items():
            assert f'<span class="nm">{label}</span><span class="slug">{slug}</span>' in body
        assert body.count('<span class="nm">大级别图</span><span class="slug">context</span>') == 2
        assert len(re.findall(r'class="[^"]*\brv-sec\b[^"]*"', body)) == 3


def test_split_page_uses_standard_kline_and_09b_read_contracts():
    html = read_split_html()

    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "packages/standard-kline/standard-kline.js" in html
    assert "StandardKline.StandardKlineChart" in html
    assert "data-standard-kline-host" in html
    assert "seedCandles(" not in html
    assert "<svg" not in html

    assert 'api("/api/dualtrack/cycle/current")' in html
    assert 'api(`/api/dualtrack/plan/${cycleId}`)' in html
    assert 'api(`/api/dualtrack/machine/${cycleId}`)' in html
    assert 'api(`/api/dualtrack/human/${cycleId}`)' in html
    assert 'api("/api/dualtrack/ledger")' in html
    assert 'api("/api/dualtrack/runtime/status")' in html
    assert "`/api/dualtrack/market/bars?symbol=GOLD&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`" in html
    assert 'api("/api/dualtrack/plan"' in html
    assert 'api("/api/dualtrack/orders"' in html
    assert 'api("/api/dualtrack/verdict"' in html
    assert 'api("/api/dualtrack/config")' in html
    assert 'api(`/api/dualtrack/trades/${cycleId}?track=human`)' in html
    assert 'api(`/api/dualtrack/trades/${cycleId}?track=machine`)' in html
    assert "loadTradeData" in html
    assert "loadMachineTradesAfterClose" in html


def test_split_page_keeps_machine_mid_blind_in_source_and_render_path():
    html = read_split_html()

    assert "machine_fills_hidden" in html
    assert "盲测中 · 仅显示 PnL" in html
    assert "网格点位收盘后揭示" in html
    assert "renderMachineFillsBlind" in html
    assert "renderMachineOrderRows" in html
    assert "cycleClosed(state.cycle)" in html
    assert "machineTrades = cycleClosed(state.cycle)" in html
    for leaked_mockup_price in ("4,062.1", "4,066.3", "4,098.4"):
        assert leaked_mockup_price not in html


def test_split_page_review_grading_does_not_invent_levels_or_signal_scores():
    html = read_split_html()

    assert "判卷 · direction-only · 自评" in html
    assert 'data-grade-field="direction"' in html
    for field in ("levels", "signal"):
        row = re.search(
            rf'<div class="gr-row" data-grade-field="{field}">(?P<row>.*?)</div>',
            html,
            re.S,
        )
        assert row, f"{field} grading row must exist"
        assert "未建立评分口径" in row.group("row")
        assert "gpass" not in row.group("row")
        assert "gblock" not in row.group("row")


def test_split_page_risk_copy_keeps_simplified_liquidation_and_directional_loss_branch():
    html = read_split_html()

    assert "简化估算" in html
    assert "function renderRiskLossLabel" in html
    assert "function renderRiskWidget" in html
    assert "state.config?.max_leverage" in html
    assert "最大可亏(距SL)" in html
    assert "距失效价" in html
    assert "loss-side" in html
    assert "neutral-side" in html
    assert "Math.abs" not in html


def test_split_page_trade_rows_keep_realized_and_unrealized_mutually_exclusive():
    html = read_split_html()

    assert "function renderTradeRows" in html
    assert 'trade.status === "closed" ? money(trade.realized_pnl) : "--"' in html
    assert 'trade.status === "open" ? money(trade.unrealized_pnl) : "--"' in html
    assert 'class="fst open"' in html
    assert 'class="fst closed"' in html


def test_split_page_is_registered_in_shell_and_static_cache_exemptions():
    html = read("assets/shell.js")
    assert "dashboard-dualtrack-split.html" in html
    assert "双画布" in html

    from pipelines import dashboard_server

    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/dashboard-dualtrack-split.html"

    assert handler._should_disable_static_cache() is True


def test_dualtrack_v5_files_stay_byte_clean_for_task_09():
    result = subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "--",
            "dashboard-dualtrack-v5.html",
            "tests/test_dashboard_dualtrack_static.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
