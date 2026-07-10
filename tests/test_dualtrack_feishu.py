from pathlib import Path

from services.dualtrack_feishu import DualTrackMachineBriefSender, DualTrackTradeRecordNotifier
from services.journal_store import load_json, write_json


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


def test_machine_brief_sends_chinese_plan_to_trade_channel(tmp_path: Path):
    output_root = tmp_path / "outputs"
    cycle_id = "2026-07-10_DAY"
    write_json(
        output_root / "dualtrack" / "plans" / f"{cycle_id}_ai.json",
        [{
            "cycle_id": cycle_id,
            "author": "ai",
            "direction": "long",
            "range": {"low": 4155.0, "high": None},
            "key_levels": [4155.0, 4198.0, 4210.0],
            "grid_orders": [{"entry": 4198.0, "take_profit": 4210.0, "weight": 1.0}],
            "invalidation": [{"side": "below", "price": 4155.0, "confirm": "touch"}],
            "confidence": 6,
            "rationale": "没有额外技术触发器，到 4198 直接补仓。",
            "status": "fallback_active",
        }],
    )
    write_json(
        output_root / "dualtrack" / "cycles" / f"{cycle_id}.json",
        [{"cycle_id": cycle_id, "open_price": 4200.0, "trend_gate_armed": True}],
    )
    sender = _FakeSender()

    result = DualTrackMachineBriefSender(output_root=output_root, sender=sender).send(cycle_id, force=True)

    assert result["status"] == "pass"
    assert len(sender.sent) == 1
    text = sender.sent[0]
    assert "类型：机器轨作战单" in text
    assert "结论：只做多" in text
    assert "关键位：4155.00、4198.00、4210.00" in text
    assert "失效条件：跌破 4155.00" in text
    assert "机器轨明确网格" in text
    assert "入场 4198.00 -> 止盈 4210.00" in text
    assert "不读取或继承人工计划" in text
    assert load_json(output_root / "dualtrack" / "machine_briefs" / f"{cycle_id}.json")[0]["status"] == "ready"


def test_trade_record_notifier_sends_entry_and_exit_once(tmp_path: Path):
    output_root = tmp_path / "outputs"
    cycle_id = "2026-07-10_DAY"
    write_json(
        output_root / "dualtrack" / "plans" / f"{cycle_id}_ai.json",
        [{
            "cycle_id": cycle_id,
            "author": "ai",
            "direction": "long",
            "range": {"low": 98.0, "high": None},
            "key_levels": [98.0, 100.0, 101.0],
            "invalidation": [{"side": "below", "price": 98.0, "confirm": "touch"}],
            "confidence": 7,
            "source": "obsidian",
        }],
    )
    fills = [
        {
            "fill_id": f"{cycle_id}_grid_0001",
            "ts": "2026-07-10T01:00:00+00:00",
            "side": "buy",
            "price": 100.0,
            "sl": 98.0,
            "tp": 101.0,
            "layer": "grid",
            "order_type": "limit",
            "out_of_plan": False,
            "realized_pnl": -0.1,
            "event": "entry",
            "rung": 0,
            "notional": 100.0,
            "cost": 0.1,
            "track": "machine",
        },
        {
            "fill_id": f"{cycle_id}_grid_0002",
            "ts": "2026-07-10T01:05:00+00:00",
            "side": "sell",
            "price": 101.0,
            "sl": 98.0,
            "tp": 101.0,
            "layer": "grid",
            "order_type": "limit",
            "out_of_plan": False,
            "realized_pnl": 0.9,
            "event": "target",
            "rung": 0,
            "notional": 101.0,
            "cost": 0.1,
            "matched_entries": [{
                "trade_id": f"{cycle_id}_grid_0001",
                "units": 1.0,
                "gross_pnl": 1.0,
                "realized_pnl": 0.9,
            }],
            "track": "machine",
        },
    ]
    write_json(output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json", fills)
    sender = _FakeSender()
    notifier = DualTrackTradeRecordNotifier(output_root=output_root, sender=sender)

    result = notifier.notify_cycle(cycle_id, backfill_existing=True)
    duplicate = notifier.notify_cycle(cycle_id)

    assert result["sent"] == 2
    assert duplicate["sent"] == 0
    assert duplicate["skipped"] == 2
    assert len(sender.sent) == 2
    assert "黄金开单 · 自动成交" in sender.sent[0]
    assert "GOLD 准备做多 · 机器轨网格 · 本周期第 1 张" in sender.sent[0]
    assert "决策路径" in sender.sent[0]
    assert "作战单方向 — 只做多 · 置信度 7/10" in sender.sent[0]
    assert "风险闭环 — TP 101.00 / SL 98.00" in sender.sent[0]
    assert "仓位（美元名义）" in sender.sent[0]
    assert "止损预估" in sender.sent[0]
    assert "旧 ticket 回测不适用" in sender.sent[0]
    assert sender.cards[0]["header"]["template"] == "green"
    assert sender.cards[0]["header"]["title"]["content"] == "黄金开单 · 自动成交"
    assert "机器轨网格" in str(sender.cards[0])
    assert sender.cards[1] is None
    assert "事件：止盈平仓" in sender.sent[1]
    assert "策略：机器轨网格" in sender.sent[1]
    rows = load_json(output_root / "dualtrack_trade_notifications" / "2026-07-10.json")
    assert len(rows) == 2
    assert all(row["delivered"] is True for row in rows)


def test_trade_record_notifier_baselines_existing_cycle_before_sending_new_fills(tmp_path: Path):
    output_root = tmp_path / "outputs"
    cycle_id = "2026-07-10_DAY"
    first_fill = {
        "fill_id": f"{cycle_id}_grid_0001",
        "ts": "2026-07-10T01:00:00+00:00",
        "side": "buy",
        "price": 100.0,
        "sl": 98.0,
        "tp": 101.0,
        "layer": "grid",
        "order_type": "limit",
        "out_of_plan": False,
        "realized_pnl": -0.1,
        "event": "entry",
        "rung": 0,
        "notional": 100.0,
        "cost": 0.1,
        "track": "machine",
    }
    write_json(output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [first_fill])
    sender = _FakeSender()
    notifier = DualTrackTradeRecordNotifier(output_root=output_root, sender=sender)

    baseline = notifier.notify_cycle(cycle_id)
    second_fill = {**first_fill, "fill_id": f"{cycle_id}_grid_0002", "ts": "2026-07-10T01:02:00+00:00", "price": 99.8}
    write_json(output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [first_fill, second_fill])
    sent = notifier.notify_cycle(cycle_id)

    assert baseline["status"] == "baseline"
    assert baseline["sent"] == 0
    assert sent["sent"] == 1
    assert len(sender.sent) == 1
    rows = load_json(output_root / "dualtrack_trade_notifications" / "2026-07-10.json")
    assert rows[0]["suppressed"] is True
    assert rows[1]["delivered"] is True


def test_trade_record_notifier_ignores_intraday_flatten_marks(tmp_path: Path):
    output_root = tmp_path / "outputs"
    cycle_id = "2026-07-10_DAY"
    write_json(
        output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json",
        [{
            "fill_id": f"{cycle_id}_grid_0009",
            "ts": "2026-07-10T05:00:00+00:00",
            "side": "sell",
            "price": 100.0,
            "sl": 98.0,
            "tp": 101.0,
            "layer": "grid",
            "order_type": "limit",
            "out_of_plan": False,
            "realized_pnl": 0.0,
            "event": "flatten",
            "rung": 0,
            "notional": 100.0,
            "cost": 0.1,
            "track": "machine",
        }],
    )
    sender = _FakeSender()

    result = DualTrackTradeRecordNotifier(output_root=output_root, sender=sender).notify_cycle(cycle_id)

    assert result["status"] == "no_events"
    assert sender.sent == []
