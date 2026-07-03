from pathlib import Path

import pytest

from services.feishu_report_sender import FeishuReportSender, resolve_report_sender
from services.journal_store import load_json


class _FakeSender:
    channel = "feishu"

    def __init__(self, configured: bool = True, ok: bool = True) -> None:
        self._configured = configured
        self.ok = ok
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def send(self, text: str) -> dict:
        self.sent.append(text)
        return {"ok": self.ok, "channel": self.channel, "code": 0 if self.ok else 999}


def test_resolve_trade_sender_uses_trade_channel_then_falls_back(monkeypatch):
    import services.alert_notifier as an
    import services.feishu_report_sender as frs

    monkeypatch.setattr(frs, "apply_live_env", lambda *a, **k: {})
    monkeypatch.setattr(an, "apply_live_env", lambda *a, **k: {})
    for key in (
        "TRADING_ORCHESTRATOR_TRADE_FEISHU_WEBHOOK_URL",
        "TRADING_ORCHESTRATOR_TRADE_FEISHU_SECRET",
        "TRADING_ORCHESTRATOR_REPORT_FEISHU_WEBHOOK_URL",
        "TRADING_ORCHESTRATOR_FEISHU_REPORT_WEBHOOK_URL",
        "TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL",
        "TRADING_ORCHESTRATOR_LARK_WEBHOOK_URL",
        "FEISHU_WEBHOOK_URL",
        "LARK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("TRADING_ORCHESTRATOR_TRADE_FEISHU_WEBHOOK_URL", "https://trade.example/hook")
    assert frs.resolve_trade_sender().webhook_url == "https://trade.example/hook"

    monkeypatch.delenv("TRADING_ORCHESTRATOR_TRADE_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_REPORT_FEISHU_WEBHOOK_URL", "https://report.example/hook")
    assert frs.resolve_trade_sender().webhook_url == "https://report.example/hook"


def test_sends_file_report_and_records_receipt(tmp_path: Path):
    output_root = tmp_path / "outputs"
    artifact = tmp_path / "report.md"
    artifact.write_text(
        "# 晚间复盘\n\n"
        "## PM 结论\n\n"
        "今晚不加风险，暂无 proven edge。\n\n"
        "## 组合快照\n\n"
        "- 今日执行：0 demo，0 live。\n\n"
        "## 盈利性判断\n\n"
        "- 已证明 edge：无。\n\n"
        "## PM 动作\n\n"
        "1. 继续只读观察。\n",
        encoding="utf-8",
    )
    sender = _FakeSender()

    result = FeishuReportSender(output_root, sender=sender).run(
        "2026-06-25",
        "pm_evening",
        "黄金组合 PM 晚间复盘",
        source_path=artifact,
    )

    assert result["delivered"] is True
    assert result["status"] == "pass"
    assert result["summarized"] is True
    assert result["truncated"] is False
    assert len(sender.sent) == 1
    assert "黄金组合 PM 晚间复盘" in sender.sent[0]
    assert "结论" in sender.sent[0]
    assert "暂无 proven edge" in sender.sent[0]
    assert "完整晚报已保存：report.md" in sender.sent[0]
    rows = load_json(output_root / "feishu_reports" / "2026-06-25.json")
    assert rows[0]["kind"] == "pm_evening"
    assert rows[0]["source_path"] == str(artifact.resolve())
    assert load_json(output_root / "feishu_reports" / "current.json")[0]["delivered"] is True


def test_records_unconfigured_delivery_without_sending(tmp_path: Path):
    sender = _FakeSender(configured=False)

    result = FeishuReportSender(tmp_path / "outputs", sender=sender).run(
        "2026-06-25",
        "strategy_research",
        "策略研究",
        message="研究结论",
    )

    assert result["delivered"] is False
    assert result["status"] == "fail"
    assert result["channel"] == "log"
    assert sender.sent == []


def test_truncates_unknown_long_report_but_keeps_artifact_path(tmp_path: Path):
    artifact = tmp_path / "long.md"
    artifact.write_text("A" * 5000, encoding="utf-8")
    sender = _FakeSender()

    result = FeishuReportSender(tmp_path / "outputs", sender=sender).run(
        "2026-06-25",
        "custom_report",
        "自定义报告",
        source_path=artifact,
        max_chars=800,
    )

    assert result["truncated"] is True
    assert result["summarized"] is False
    assert result["sent_chars"] <= 800
    assert "完整 artifact:" in sender.sent[0]
    assert "已截断" in sender.sent[0]


def test_long_pm_report_sends_digest_without_truncating(tmp_path: Path):
    artifact = tmp_path / "morning.md"
    artifact.write_text(
        "# 黄金交易早盘复盘\n\n"
        "## 一句话\n\n"
        "过去 12 小时黄金 +1.20%；`gold_1m_macd` 开仓/请求 3 单，TP 2、SL 1，已实现 PnL +18.50。\n\n"
        "## 行情\n\n"
        "- 过去 12 小时：上涨 +1.20%，从 4000.00 到 4048.00。\n"
        "- 区间：高点 4055.00，低点 3988.00；数据源 binance_usdm，周期 5m。\n\n"
        "## 策略表现\n\n"
        "- 当前只复盘 active 策略：`gold_1m_macd`。\n"
        "- 过去 12 小时：paper filled 2，demo requests 1，demo blocked 0。\n"
        "- 平仓结果：TP 2，SL 1，其他 0；窗口已实现 PnL +18.50。\n\n"
        "## 为什么\n\n"
        "- 策略在这段行情里赚钱，主要看它是否站在黄金上涨的同侧，以及 TP 是否真实落袋。\n\n"
        "## 现在看什么\n\n"
        "- 下一步只看这些 open 单最终是 TP 还是 SL；不要提前按浮盈浮亏评价策略。\n"
        + "\n".join(f"- 噪音行 {i}" for i in range(200)),
        encoding="utf-8",
    )
    sender = _FakeSender()

    result = FeishuReportSender(tmp_path / "outputs", sender=sender).run(
        "2026-06-25",
        "pm_morning",
        "早盘计划",
        source_path=artifact,
    )

    assert result["summarized"] is True
    assert result["truncated"] is False
    assert result["sent_chars"] < 1600
    assert "已截断" not in sender.sent[0]
    assert "行情" in sender.sent[0]
    assert "策略" in sender.sent[0]
    assert "Backend maturity" not in sender.sent[0]
    assert "完整早报已保存：morning.md" in sender.sent[0]


def test_strategy_research_sends_decision_digest(tmp_path: Path):
    artifact = tmp_path / "research.md"
    artifact.write_text(
        "# 黄金策略研究\n\n"
        "## PM summary\n\n"
        "今天新增一个 shadow/paper-only 策略，解决 5m VWAP 延伸后的回归问题。\n\n"
        "## Proposed class\n\n"
        "Strategy id: `gold_5m_vwap_extension_reversion`\n\n"
        "## Implementation\n\n"
        "- `services/technical_rule_signal_engine.py`: added vwap engine.\n"
        "- `configs/strategy.yaml`: added shadow candidate.\n"
        "- `tests/test_technical_rule_signal_engine.py`: added tests.\n\n"
        "## Verification\n\n"
        "- `python3 -m pytest`: 24 passed.\n\n"
        "## Next validation step\n\n"
        "Run normal paper/shadow collection and compare closed-trade R.\n\n"
        "## CEO-facing recommendation\n\n"
        "Add to shadow observation only; do not promote any strategy today.\n",
        encoding="utf-8",
    )
    sender = _FakeSender()

    result = FeishuReportSender(tmp_path / "outputs", sender=sender).run(
        "2026-06-25",
        "strategy_research",
        "黄金策略研究",
        source_path=artifact,
    )

    assert result["summarized"] is True
    assert result["truncated"] is False
    assert "新方案：gold_5m_vwap_extension_reversion" in sender.sent[0]
    assert "24 passed" in sender.sent[0]
    assert "完整研究已保存：research.md" in sender.sent[0]


def test_requires_content(tmp_path: Path):
    with pytest.raises(ValueError, match="either source_path or message"):
        FeishuReportSender(tmp_path / "outputs", sender=_FakeSender()).run(
            "2026-06-25",
            "pm_morning",
            "早盘计划",
        )


def test_default_report_sender_prefers_report_specific_feishu(monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL", "https://alert.example/webhook")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_FEISHU_SECRET", "alert-secret")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_REPORT_FEISHU_WEBHOOK_URL", "https://report.example/webhook")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_REPORT_FEISHU_SECRET", "report-secret")

    sender = resolve_report_sender()

    assert sender.webhook_url == "https://report.example/webhook"
    assert sender.secret == "report-secret"
