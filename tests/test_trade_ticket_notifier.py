from pathlib import Path

from services.journal_store import load_json, write_json
from services.trade_ticket_notifier import TradeTicketNotifier


class _FakeSender:
    channel = "feishu"
    configured = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> dict:
        self.sent.append(text)
        return {"ok": True, "channel": self.channel, "code": 0}


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


def test_trade_ticket_notifier_sends_review_card_and_records_receipt(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        strategy_root / "journal_pending" / f"{run_date}.json",
        [{**ticket, "journal_id": "j1", "decision_status": "pending_manual_decision"}],
    )
    sender = _FakeSender()

    result = TradeTicketNotifier(output_root, sender=sender).notify_namespace(run_date, "gold_1m_macd", strategy_root)

    assert result["status"] == "pass"
    assert result["sent"] == 1
    assert "黄金开单审查卡" in sender.sent[0]
    assert "类型：开单审查卡" in sender.sent[0]
    assert "1. 发现信号" in sender.sent[0]
    assert "2. 策略类别" in sender.sent[0]
    assert "类型：动量 / MACD 金叉/死叉 / 多空都可" in sender.sent[0]
    assert "3. 过滤检查" in sender.sent[0]
    assert "4. 盈亏比检查" in sender.sent[0]
    assert "入场区间：4175-4180" in sender.sent[0]
    assert "估算开单价：4177.5000" in sender.sent[0]
    assert "止盈：4198.0，4212.0" in sender.sent[0]
    assert "止损：4162.5" in sender.sent[0]
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


def test_trade_ticket_notifier_does_not_resend_delivered_ticket(tmp_path: Path):
    output_root = tmp_path / "outputs"
    strategy_root = output_root / "strategies" / "gold_1m_macd"
    run_date = "2026-07-03"
    ticket = _ticket()
    write_json(strategy_root / "trade_tickets" / f"{run_date}.json", [ticket])
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
