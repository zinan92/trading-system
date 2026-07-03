from services.trade_ticket_card import build_ticket_card


def _all_text(card: dict) -> str:
    chunks = [card["header"]["title"]["content"]]
    for element in card["elements"]:
        text = element.get("text")
        if isinstance(text, dict):
            chunks.append(str(text.get("content", "")))
        for field in element.get("fields", []) or []:
            chunks.append(str(field.get("text", {}).get("content", "")))
        for note in element.get("elements", []) or []:
            chunks.append(str(note.get("content", "")))
        for action in element.get("actions", []) or []:
            chunks.append(str(action.get("text", {}).get("content", "")))
    return "\n".join(chunks)


def _sample_view(**overrides) -> dict:
    view = {
        "title": "黄金开单 · 自动成交",
        "header_template": "green",
        "subtitle": "GOLD 做多　·　MACD 金叉　·　今日第 1/1 张",
        "approval": "",
        "account": {"label": "纸面 PAPER", "mode": "paper_sim", "armed": None},
        "flow": [
            {"step": 1, "name": "信号闸门", "passed": True, "detail": "强度 66 ≥ 60　·　置信 69 ≥ 55　·　方向 做多"},
            {"step": 2, "name": "盈亏比 / 质量闸门", "passed": True, "detail": "盈亏比 2 ≥ 1.5　·　目标涨幅 +4%"},
            {"step": 3, "name": "账户风险闸门", "passed": True, "detail": "账户止损 $16 ≤ 单笔上限 $50"},
            {"step": 4, "name": "自动批准闸门", "passed": True, "detail": "风控 · 护栏 · 对账　全绿"},
        ],
        "flow_terminal": {"icon": "⚡", "text": "自动成交 · 已建仓"},
        "risk_divergence": "",
        "position_usd": {"nominal": "$800", "margin": "$160", "leverage": "5", "stop": "−$16", "target": "+$32", "equity": "$10,000"},
        "price": {"entry_zone": "4162.35 – 4204.19", "entry_price": "4183.27", "targets": "4350.6", "stop_loss": "4099.6"},
        "backtest": {"verdict": "支持性", "win_rate": "58.4%", "avg_r": "0.39", "sample": "305"},
        "dashboard_url": "https://dash.example/tickets/2026-07-03",
        "meta": {"generated_at": "2026-07-03T08:15:00Z", "price_line": "开单时价 4183.27"},
    }
    view.update(overrides)
    return view


def test_card_header_template_and_title():
    card = build_ticket_card(_sample_view())
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "黄金开单 · 自动成交"
    assert card["config"]["wide_screen_mode"] is True


def test_invalid_template_falls_back_to_grey():
    assert build_ticket_card(_sample_view(header_template="not-a-color"))["header"]["template"] == "grey"


def test_card_shows_account_badge_not_paper_only():
    text = _all_text(build_ticket_card(_sample_view()))
    assert "纸面 PAPER" in text and "paper_sim" in text
    assert "只允许纸面交易" not in text and "paper_only" not in text


def test_flow_renders_numbered_checklist_with_values():
    text = _all_text(build_ticket_card(_sample_view()))
    assert "决策路径" in text
    assert "✅ 1 · 信号闸门" in text
    assert "强度 66 ≥ 60" in text
    assert "✅ 3 · 账户风险闸门" in text
    assert "自动成交 · 已建仓" in text


def test_failed_flow_node_shows_cross():
    view = _sample_view()
    view["flow"][2] = {"step": 3, "name": "账户风险闸门", "passed": False, "detail": "账户止损 $80 > 单笔上限 $50"}
    view["flow"] = view["flow"][:3]
    view["flow_terminal"] = {"icon": "⛔", "text": "自动拒绝"}
    text = _all_text(build_ticket_card(view))
    assert "❌ 3 · 账户风险闸门" in text
    assert "自动拒绝" in text
    assert "✅ 4" not in text


def test_position_shown_in_nominal_dollars():
    text = _all_text(build_ticket_card(_sample_view()))
    assert "名义价值 **$800**" in text
    assert "保证金 $160" in text
    assert "−$16" in text and "+$32" in text
    assert "权益 8%" not in text and "权益的" not in text


def test_divergence_note_rendered_when_present():
    text = _all_text(build_ticket_card(_sample_view(risk_divergence="风控引擎按保证金口径记为账户止损 $80，差 5× 杠杆")))
    assert "⚠️" in text and "5× 杠杆" in text


def test_dashboard_url_is_a_button_no_local_path():
    card = build_ticket_card(_sample_view())
    buttons = [a for e in card["elements"] for a in e.get("actions", []) or [] if a.get("tag") == "button"]
    assert buttons and buttons[0]["url"].startswith("https://")
    assert "/Users/" not in _all_text(card)


def test_evidence_note_when_no_dashboard_url():
    card = build_ticket_card(_sample_view(dashboard_url="", evidence_note="trade_tickets/2026-07-03.json"))
    assert "trade_tickets/2026-07-03.json" in _all_text(card)
    assert not any(a.get("tag") == "button" for e in card["elements"] for a in e.get("actions", []) or [])


def test_missing_meta_shows_pending_placeholder():
    text = _all_text(build_ticket_card(_sample_view(meta={})))
    assert "生成时间 待补" in text and "开单时价 待补" in text
