import json
from pathlib import Path

import pytest

import pipelines.daily as daily_pipeline
from pipelines.daily import run_daily_pipeline
from services.backtest_plugin_registry import UnknownBacktestPlugin
from services.market_view import OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR


def test_daily_pipeline_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(tmp_path / "market_data.db"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4567.89")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-05-07T00:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", str(tmp_path / "vault"))
    note_path = tmp_path / "vault" / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR / "2026-05-07.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        """---
direction_bias_score: 35
timeframes:
  - 1D
  - 4H
---

# 2026-05-07 黄金每日交易分析

## 一句话结论

今天偏空，反弹优先找空。

## 过期条件

观点有效 4 小时，价格偏离 1% 失效。
""",
        encoding="utf-8",
    )
    paths = run_daily_pipeline("2026-05-07")
    for path in paths.values():
        assert Path(path).exists()
    assert "analyses" in paths
    assert "backtests" in paths
    assert "report" in paths
    assert "journal" in paths
    assert "review_notes" in paths
    assert (tmp_path / "outputs" / "journals" / "2026-05-07.md").exists()
    market_view = json.loads((tmp_path / "outputs" / "market_views" / "current.json").read_text(encoding="utf-8"))[0]
    assert market_view["intake"]["parser"] == "market_view_obsidian_v1"
    assert market_view["direction_score"] == 35
    backtests = json.loads(Path(paths["backtests"]).read_text(encoding="utf-8"))
    assert backtests
    assert backtests[0]["backtest_plugin"] == "local_signal"
    assert backtests[0]["evidence_tier"] == "local_historical_signal"
    assert len(backtests[0]["input_hash"]) == 64
    assert backtests[0]["degraded"] is False


def test_unknown_daily_backtest_plugin_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setattr(
        daily_pipeline,
        "load_pipeline_config",
        lambda: {"backtest_plugins": {"signal": "typo"}},
    )

    with pytest.raises(UnknownBacktestPlugin, match="typo"):
        run_daily_pipeline("2026-07-18", output_root=output)

    assert not output.exists()
