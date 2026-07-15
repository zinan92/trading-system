from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_console_has_one_canvas_and_real_read_model() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    assert "api('/api/strategy-console/current')" in html
    assert 'id="productionChart"' in html
    assert "StandardKline.StandardKlineChart" in html
    assert 'data-track="human"' not in html
    assert 'data-track="machine"' not in html
    assert "Strategy Shadows" in html
    assert "strategy_shadow_separate_from_execution_shadow" not in html  # backend owns this safety contract


def test_console_exposes_five_baseline_modules_and_fail_closed_order_gate() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    for label in ("市场与趋势", "Broker 来源", "策略配置", "运行中调整", "账户总览", "价格 / 网格", "成交记录"):
        assert label in html
    assert "行情陈旧、缺失或 synthetic 时停止新开仓。" in html
    assert "strategy_plan_version" in html
    assert "人工 / AI 提案差异" not in html


def test_console_wires_robot_controls_to_real_backend_actions() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    assert "api('/api/strategy-console/control'" in html
    for action in ("preview", "refresh_recommendation", "start", "stop", "adjust_plan", "reset_statistics"):
        assert action in html
    assert "cancel_all" not in html
    assert "/api/dualtrack/market/bars?symbol=${encodeURIComponent(symbol)}" in html


def test_console_exposes_visible_grid_and_user_configurable_indicators() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    for control in ('id="showEma"', 'id="showMacd"', 'id="emaFast"', 'id="emaSlow"', 'id="gridSummary"'):
        assert control in html
    assert "requestPreview" in html
    assert "Grid ${index}" in html


def test_console_keeps_action_feedback_visible_after_a_refresh() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    assert "setNotice" in html
    assert "state.notice" in html
    assert "正在启动机器人" in html
    assert "正在停止、撤单并平仓" in html


def test_console_uses_authoritative_preview_orders_and_runtime_state() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    for contract in (
        "actual_state",
        "strategy_plan_version",
        "strategy-grid-preview-v1",
        "state.preview",
        "preview.orders",
        "挂买",
        "挂卖",
        "待启动预览",
    ):
        assert contract in html
    assert "control('preview'" in html
    assert "control('start',strategyPayload()" in html
    assert "control('stop'" in html
    assert "await control('cancel_all')" not in html


def test_console_preserves_auto_sizing_until_user_manually_edits_notional() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert 'notionalMode:"auto"' in html
    assert "notional_mode:state.notionalMode" in html
    assert 'state.notionalMode="manual"' in html
    assert "自动风险上限" in html


def test_console_direction_and_style_controls_request_new_grid_geometry() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")
    assert "requestPreview({useInputs:false})" in html
    assert "requestPreview({useInputs:true})" in html
    assert "renderRobotControls" in html
    assert "previewSummary" in html
    assert "robot start is-inactive" in html
    assert "robot stop is-active" in html
    assert 'state.timeframe=event.target.value;await refreshMarket()' in html
    assert 'state.timeframe=event.target.value;state.preview=null' not in html


def test_console_explains_recommendation_and_uses_meaningful_plan_label() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert "renderTrend" in html
    assert "latestRecommendation" in html
    assert "D1 ATR14" in html
    assert "4H ATR14" in html
    assert "未校准" in html
    assert "refresh_recommendation" in html
    assert "生产策略" in html
    assert 'slice(-8)' not in html
    assert "根 K 线 · 更新于" not in html


def test_console_exposes_auditable_ai_input_output_receipt() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    for contract in (
        "AI 评估收据",
        "查看本次 AI Input / Output",
        "15m EMA20",
        "MACD",
        "raw_model_response",
        "evaluation_receipt",
        "本地留档",
    ):
        assert contract in html


def test_public_v5_requires_an_authenticated_access_session_for_remote_mutations() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert "PUBLIC_OBSERVER" in html
    assert "api('/api/auth/session')" in html
    assert "can_control" in html
    assert "state.auth" in html
    assert "ACCESS_CONTROLLED" in html
    assert "公网只读" in html


def test_public_v5_recovers_cleanly_when_cloudflare_access_session_expires() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert 'headers.set("X-Requested-With","XMLHttpRequest")' in html
    assert "response.status===401" in html
    assert "登录会话已过期，正在重新登录" in html
    assert "location.reload()" in html
    assert "response.json()" not in html
    assert "登录后可控制" in html


def test_console_formats_execution_time_and_quantity_for_humans() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert "function beijingDateTime" in html
    assert 'timeZone:"Asia/Shanghai"' in html
    assert "function quantityText" in html
    assert "maximumFractionDigits:6" in html
    assert "时间（北京）" in html
    assert "beijingDateTime(trade.exit_ts||trade.entry_ts||trade.ts)" in html
    assert "quantityText(trade.quantity||trade.units||trade.remaining_units)" in html


def test_console_explains_the_last_runtime_control_action() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert "runtime.last_action" in html
    assert "收到停止指令" in html
    assert "最后动作" in html
    assert "动作时间（北京）" in html


def test_console_reconciles_authoritative_state_after_an_uncertain_control_response() -> None:
    html = (ROOT / "dashboard-dualtrack-split.html").read_text(encoding="utf-8")

    assert "function apiFailure" in html
    assert "function isUncertainControlError" in html
    assert "async function reconcileControlOutcome" in html
    assert "后台响应中断，正在核对真实运行状态" in html
    assert "await reconcileControlOutcome('start',error)" in html
    assert "await reconcileControlOutcome('stop',error)" in html
    assert "后台已确认机器人仍为停止状态，未创建挂单" in html
    assert 'state.timeframe==="1m"&&isFresh(data.market)' in html
    assert "if(!state.busy&&!document.hidden)load()" in html
