from __future__ import annotations

from services.journal_store import load_json
from services.market_analysis_prompt import build_market_analysis_prompt, send_market_analysis_prompt


class _FakeSender:
    configured = True
    channel = "feishu"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> dict:
        self.messages.append(text)
        return {"ok": True, "channel": self.channel, "code": 0, "message": "success"}


def test_market_analysis_prompt_asks_for_expiry_target_price():
    text = build_market_analysis_prompt("2026-06-26")

    assert "今天你对黄金市场怎么看" in text
    assert "黄金日报 Codex thread" in text
    assert "park 原始输出/每日交易分析" in text
    assert "大方向分数 0-100" in text
    assert "目标位" in text
    assert "强空目标 3950" in text
    assert "打到目标后重新评估" in text
    assert "12 小时过期" in text
    assert "价格相对参考价偏离 1%" in text


def test_send_market_analysis_prompt_uses_feishu_report_receipt(tmp_path):
    sender = _FakeSender()

    result = send_market_analysis_prompt(tmp_path / "outputs", run_date="2026-06-26", sender=sender)

    assert result["delivered"] is True
    assert result["kind"] == "market_analysis_prompt"
    assert result["title"] == "黄金市场分析提醒 - 2026-06-26"
    assert sender.messages
    assert "目标位" in sender.messages[0]
    rows = load_json(tmp_path / "outputs" / "feishu_reports" / "2026-06-26.json")
    assert rows[-1]["delivered"] is True
