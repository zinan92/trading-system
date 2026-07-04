from pathlib import Path

from services.journal_store import load_json, write_json
from services.trade_ticket_notifier import TradeTicketNotifier


class _FakeSender:
    channel = "feishu"
    configured = True

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.cards: list[dict | None] = []

    def send(self, text: str, card: dict | None = None) -> dict:
        self.sent.append(text)
        self.cards.append(card)
        return {"ok": True, "channel": self.channel, "code": 0}


def _card_text(card: dict) -> str:
    chunks = [card["header"]["title"]["content"]]
    for element in card.get("elements", []):
        text = element.get("text")
        if isinstance(text, dict):
            chunks.append(str(text.get("content", "")))
        for field in element.get("fields", []) or []:
            chunks.append(str(field.get("text", {}).get("content", "")))
        for note in element.get("elements", []) or []:
            chunks.append(str(note.get("content", "")))
    return "\n".join(chunks)


def _ticket() -> dict:
    return {
        "ticket_id": "ticket_gold_20260703_manual_review",
        "signal_id": "sig_gold_review",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4175-4180",
        "stop_loss": 4162.5,
        "targets": [4198.0, 4212.0],
        "position_size_pct": 5,
        "max_loss_pct": 0.25,
        "order_type": "market",
        "time_in_force": "day",
        "paper_only": True,
        "trigger": "MACD golden cross with volatility filter",
        "invalid_if": "price closes below 4162.5",
        "methods": ["macd", "volatility_filter"],
        "rationale": "Momentum aligned with the current range break.",
        "counter_rationale": "Market view may be stale.",
        "trade_quality": {"passes": True, "reward_to_risk": 2.1, "target_equity_return_pct": 0.5, "reasons": []},
        "backtest": {"verdict": "insufficient_sample", "win_rate": 0.54, "avg_r": 0.2},
    }


def _mark_executed(strategy_root: Path, run_date: str, ticket: dict) -> None:
    # The 交易记录 channel only receives executed tickets; a paper_order marks execution.
    write_json(
        strategy_root / "paper_orders" / f"{run_date}.json",
        [{"ticket_id": ticket["ticket_id"], "status": "filled", "fill_price": 4180.0, "quantity": 0.1}],
    )


def test_notifier_pushes_only_executed_tickets(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    # pending (not executed) -> NOT pushed to Feishu
    sender = _FakeSender()
    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_1m_macd", strategy_root)
    assert result["sent"] == 0 and sender.sent == []
    # once executed -> pushed
    _mark_executed(strategy_root, run_date, ticket)
    sender2 = _FakeSender()
    result2 = TradeTicketNotifier(output_root, sender=sender2).notify_namespace(run_date, "gold_1m_macd", strategy_root)
    assert result2["sent"] == 1 and len(sender2.cards) == 1


def test_trade_ticket_notifier_sends_review_card_and_records_receipt(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()

    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_1m_macd", strategy_root)

    assert result["status"] == "pass"
    assert result["sent"] == 1
    assert "黄金开单审查卡" in sender.sent[0]
    assert "类型：开单审查卡" in sender.sent[0]
    assert "审批结论：纸面已成交" in sender.sent[0]
    assert "1. 信号与开单逻辑" in sender.sent[0]
    assert "2. 策略类别" in sender.sent[0]
    assert "类型：动量 / MACD 金叉/死叉 / 多空都可" in sender.sent[0]
    assert "3. 过滤检查" in sender.sent[0]
    assert "4. 盈亏比与价格计划" in sender.sent[0]
    assert "入场区间：4175-4180" in sender.sent[0]
    assert "估算开单价：4177.5000" in sender.sent[0]
    assert "止盈：4198.0，4212.0" in sender.sent[0]
    assert "止损：4162.5" in sender.sent[0]
    assert "5. 仓位计算" in sender.sent[0]
    assert "6. 执行与出场闭环" in sender.sent[0]
    assert "保护单状态：计划止盈 4198.0，4212.0，计划止损 4162.5；尚未提交交易所保护单。" in sender.sent[0]
    assert "8. 人工核对项" in sender.sent[0]
    assert "Entry zone" not in sender.sent[0]
    assert "Rationale:" not in sender.sent[0]
    assert "MACD golden cross" not in sender.sent[0]
    assert "price closes below" not in sender.sent[0]
    rows = load_json(output_root / "trade_ticket_notifications" / f"{run_date}.json")
    assert rows[0]["delivered"] is True
    assert rows[0]["ticket_id"] == ticket["ticket_id"]
    receipt = load_json(output_root / "feishu_reports" / f"{run_date}.json")[0]
    assert receipt["kind"] == "trade_ticket_open"


def _rich_ticket() -> dict:
    return {
        **_ticket(),
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "signal_regime": "macd_golden_cross",
        "signal_strength": 7,
        "signal_confidence": 8,
        "latest_price": 4185.7,
        "generated_at": "2026-07-03T08:15:00+00:00",
        "trade_quality": {
            "passes": True,
            "reward_to_risk": 2.0,
            "target_price_move_pct": 4.0,
            "stop_price_move_pct": 2.0,
            "target_equity_return_pct": 20.0,
            "stop_equity_risk_pct": 10.0,
            "estimated_account_stop_risk_pct": 0.8,
            "requirements": {"effective_leverage": 5.0},
            "reasons": [],
        },
        "backtest": {"verdict": "supportive", "win_rate": 0.621, "avg_r": 0.39, "sample_size": 58},
    }


def test_trade_ticket_notifier_delivers_interactive_card(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _rich_ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()

    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_1m_macd", strategy_root)

    assert result["sent"] == 1
    card = sender.cards[0]
    assert card is not None
    assert card["header"]["template"] in {"blue", "wathet", "turquoise", "green", "yellow", "orange", "red", "carmine", "violet", "purple", "indigo", "grey"}
    text = _card_text(card)
    # account badge is present (label derived from routing), not the retired paper_only line
    assert any(label in text for label in ("纸面 PAPER", "模拟盘 DEMO", "实盘 REAL"))
    assert "只允许纸面交易" not in text
    assert "paper_only" not in text
    # flow-structured decision path + nominal dollars + no local path leak
    assert "决策路径" in text
    assert "✅ 1 · 信号闸门" in text
    assert "名义价值" in text and "$" in text
    assert "权益 8%" not in text
    assert "/Users/" not in text
    # translated regime, not raw english / raw enum
    assert "MACD 金叉" in text
    assert "macd_golden_cross" not in text


def test_executed_ticket_renders_green_auto_filled_card(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_x"
    run_date = "2026-07-03"
    ticket = _rich_ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        strategy_root / "paper_orders" / f"{run_date}.json",
        [{"ticket_id": ticket["ticket_id"], "status": "filled", "fill_price": 4183.27, "quantity": 0.19}],
    )
    sender = _FakeSender()

    TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_x", strategy_root)

    card = sender.cards[0]
    assert card["header"]["template"] == "green"
    text = _card_text(card)
    assert "自动成交 · 已建仓" in text
    assert "✅ 4 · 自动批准闸门" in text


def test_pending_limit_ticket_renders_waiting_for_entry_not_manual_review(tmp_path: Path):
    output_root = tmp_path / "outputs"
    ticket = {
        **_rich_ticket(),
        "order_type": "limit",
        "entry_order_limit_price": 4170.0,
        "entry_order_ttl_bars": 10,
    }
    context = {
        **_ctx(output_root),
        "pending": {
            "ticket_id": ticket["ticket_id"],
            "decision_status": "pending_entry_order",
            "entry_order_limit_price": 4170.0,
            "entry_order_ttl_bars": 10,
        },
    }
    notifier = TradeTicketNotifier(output_root, sender=_FakeSender())

    message = notifier._format_message("2026-07-03", "gold_1m_macd", ticket, context)
    card = notifier._build_card("2026-07-03", "gold_1m_macd", ticket, context, 1, 1)
    text = _card_text(card)

    assert "审批结论：限价单等待触价" in message
    assert "执行状态：限价单等待触价" in message
    assert "等待人工确认" not in message
    assert "限价单等待触价" in text


def _ctx(output_root):
    return {"pending": {}, "decision": {}, "paper_order": {}, "demo_request": {}, "strategy_root": str(output_root)}


def test_trade_ticket_card_flags_risk_breach_when_stop_move_exceeds_cap(tmp_path: Path):
    # Card rendering for a non-executed (rejected) breach -> built directly (not pushed).
    output_root = tmp_path / "outputs"
    ticket = _rich_ticket()
    # executor口径 stop = pos 8% * stop_move 8% / 100 = 0.64% > cap 0.5% -> breach
    ticket["trade_quality"]["stop_price_move_pct"] = 8.0
    notifier = TradeTicketNotifier(output_root, sender=_FakeSender())

    card = notifier._build_card("2026-07-03", "gold_1m_macd", ticket, _ctx(output_root), 1, 1)

    assert card["header"]["template"] == "red"
    text = _card_text(card)
    assert "❌ 3 · 账户风险闸门" in text
    assert "自动拒绝" in text


def test_card_translates_non_macd_regimes_without_english_leak(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_pullback"
    run_date = "2026-07-03"
    ticket = {**_rich_ticket(), "signal_regime": "pullback_long"}
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()

    TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_pullback", strategy_root)

    text = _card_text(sender.cards[0])
    assert "回调做多" in text
    assert "pullback_long" not in text
    assert "pullback long" not in text


def test_card_translates_breakout_invalid_if(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_break"
    run_date = "2026-07-03"
    ticket = {**_rich_ticket(), "invalid_if": "Price closes back below the latest breakout/reference level."}
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()

    TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_break", strategy_root)

    # invalid_if lives in the text fallback (_format_message), not on the lean flow card
    text = sender.sent[0]
    assert "价格重新收回到最近的突破/参考位下方。" in text
    assert "Price closes back below" not in text


def test_missing_risk_data_renders_pending_not_false_breach(tmp_path: Path):
    output_root = tmp_path / "outputs"
    ticket = _rich_ticket()
    ticket["trade_quality"].pop("stop_price_move_pct")  # cannot recompute executor stop
    notifier = TradeTicketNotifier(output_root, sender=_FakeSender())

    card = notifier._build_card("2026-07-03", "gold_x", ticket, _ctx(output_root), 1, 1)

    text = _card_text(card)
    # missing risk data -> node 3 is neither ✅ nor ❌ (⬜), not a false red breach
    assert "⬜ 3 · 账户风险闸门" in text
    assert "❌" not in text
    assert card["header"]["template"] != "red"


def test_account_badge_reflects_routing(tmp_path: Path):
    notifier = TradeTicketNotifier(tmp_path / "outputs", sender=_FakeSender())

    demo_cfg = {"demo_trading": {"enabled": True, "active_strategy_id": "gold_1m_macd"}}
    demo = notifier._account_badge("gold_1m_macd", {}, demo_cfg)
    assert demo["label"] == "模拟盘 DEMO" and demo["mode"] == "binance_futures_demo"

    paper = notifier._account_badge("gold_1m_macd", {}, {"execution_mode": "paper"})
    assert paper["label"] == "纸面 PAPER" and paper["mode"] == "paper_sim"

    live_cfg = {"execution_mode": "live", "live_trading_enabled": True, "broker": {"dry_run": False}}
    live = notifier._account_badge("top_level", {}, live_cfg)
    assert live["label"] == "实盘 REAL" and live["mode"] == "live_adapter_gated" and live["armed"] is True

    # a per-ticket demo request marks the account demo even without global demo config
    via_request = notifier._account_badge("gold_x", {"status": "requested"}, {"execution_mode": "paper"})
    assert via_request["label"] == "模拟盘 DEMO"


def test_trade_ticket_notifier_sends_top_level_ticket(tmp_path: Path):
    output_root = tmp_path / "outputs"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(output_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(output_root, run_date, ticket)
    sender = _FakeSender()

    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "top_level", output_root)

    assert result["status"] == "pass"
    assert result["sent"] == 1
    assert "黄金开单审查卡｜主流程" in sender.sent[0]
    assert "策略：主流程" in sender.sent[0]
    rows = load_json(output_root / "trade_ticket_notifications" / f"{run_date}.json")
    assert rows[0]["strategy_id"] == "top_level"
    assert rows[0]["ticket_id"] == ticket["ticket_id"]
    assert rows[0]["delivered"] is True


def test_trade_ticket_notifier_shows_demo_block_reason(tmp_path: Path):
    # A blocked demo ticket is not executed (not pushed); its text rendering is built directly.
    output_root = tmp_path / "outputs"
    run_date = "2026-07-03"
    ticket = _ticket()
    demo_request = {
        "ticket_id": ticket["ticket_id"],
        "status": "blocked",
        "guard": {
            "block_reason": "Binance demo reconciliation cannot confirm venue state: timeout: The read operation timed out"
        },
    }
    notifier = TradeTicketNotifier(output_root, sender=_FakeSender())
    ctx = {"pending": {}, "decision": {}, "paper_order": {}, "demo_request": demo_request, "strategy_root": str(output_root)}

    text = notifier._format_message(run_date, "gold_1m_macd", ticket, ctx)

    assert "审批结论：禁止开单：demo 请求已阻断" in text
    assert "阻断/风险原因：Binance demo 对账无法确认交易所状态：读取交易所状态超时" in text
    assert "原因：Binance demo 对账无法确认交易所状态：读取交易所状态超时" in text


def test_trade_ticket_notifier_does_not_resend_delivered_ticket(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()
    notifier = TradeTicketNotifier(output_root, sender=sender)

    first = notifier.notify_namespace(run_date, "gold_1m_macd", strategy_root)
    second = notifier.notify_namespace(run_date, "gold_1m_macd", strategy_root)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["skipped"] == 1
    assert len(sender.sent) == 1


def test_trade_ticket_notifier_can_force_resend_delivered_ticket(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()
    notifier = TradeTicketNotifier(output_root, sender=sender)

    first = notifier.notify_namespace(run_date, "gold_1m_macd", strategy_root)
    second = notifier.notify_namespace(run_date, "gold_1m_macd", strategy_root, force=True)

    assert first["sent"] == 1
    assert second["sent"] == 1
    assert second["skipped"] == 0
    assert len(sender.sent) == 2


def test_trade_ticket_notifier_translates_system_english_phrases(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd_trend_vol_filter"
    run_date = "2026-07-03"
    ticket = {
        **_ticket(),
        "ticket_id": "ticket_gold_20260703_trend_vol",
        "trigger": "Review GOLD if signal remains above strength/confidence thresholds.",
        "rationale": "GOLD is best interpreted through no-trade, risk-management; current regime is macd_trend_volatility_filter.",
        "counter_rationale": "Signal should be ignored if price confirmation fails, macro pressure reverses, or event risk dominates.",
    }
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    _mark_executed(strategy_root, run_date, ticket)
    sender = _FakeSender()

    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_1m_macd_trend_vol_filter", strategy_root)

    assert result["sent"] == 1
    text = sender.sent[0]
    assert "如果信号强度和置信度仍高于阈值，则审查黄金。" in text
    assert "当前用不开仓过滤和风险管理框架解读黄金；当前信号状态为 MACD 趋势/波动过滤。" in text
    assert "如果价格确认失败、宏观压力反转，或事件风险主导，则忽略该信号。" in text
    assert "Review GOLD" not in text
    assert "GOLD is best interpreted" not in text
    assert "Signal should be ignored" not in text
    assert "macd_trend_volatility_filter" not in text


def test_card_translates_chan_regime_without_english_leak(tmp_path: Path):
    output_root = tmp_path / "outputs"
    ticket = {**_rich_ticket(), "signal_regime": "chan_second_buy"}
    notifier = TradeTicketNotifier(output_root, sender=_FakeSender())

    card = notifier._build_card("2026-07-03", "gold_1m_chan", ticket, _ctx(output_root), 1, 1)

    text = _card_text(card)
    assert "缠论二买" in text
    assert "chan second buy" not in text and "chan_second_buy" not in text
