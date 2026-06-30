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
        "# 黄金组合 PM 早盘报告\n\n"
        "## 0. PM 结论\n\n"
        "今天不加风险，不把任何策略升级为已证明盈利。系统可运行但不是可放量状态。\n\n"
        "组合层面仍是研究优先，主动 demo 保守运行。\n\n"
        "## 1. 系统与安全\n\n"
        "- Backend maturity：warn，M0 fail。\n"
        "- Health：warn，runner fresh。\n"
        "- 执行安全：network_call_attempted=false。\n\n"
        "## 2. 组合快照\n\n"
        "- 今日执行：3 笔 paper filled，0 demo，0 live。\n"
        "- 当前 open positions：35 笔 shadow paper。\n"
        "- 样本量：今日 executed=3，低于 5-10 目标区间。\n\n"
        "## 4. 盈利性判断\n\n"
        "- 已证明 edge：无。\n"
        "- 近 promising 但不能加配：Bollinger、Chan buy1。\n\n"
        "## 6. PM 动作，未来 12 小时\n\n"
        "1. 对 MACD 与 5m_v1 做亏损驱动归因，不改参数。\n"
        "2. 跟踪今日新出票策略是否到 TP/SL。\n"
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
