from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json
from services.market_view import OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR
from services.market_view_obsidian import MarketViewObsidianSync


OBSIDIAN_NOTE = """---
title: 2026-06-30 黄金每日交易分析
created: 2026-06-30
asset: XAUUSD
source: Park口述
direction_bias_score: 20
direction_bias: strong_short
timeframes:
  - 3D
  - 1D
  - 4H
  - 15m
  - 5m
tags:
  - trading
  - gold
---

# 2026-06-30 黄金每日交易分析

## 一句话结论

今天偏空，3D/1D/4H 都没有重新转强，15m/5m 只等反弹到压力后找空。

## 关键价位

- 3985 上方压力
- 3950 日内目标位
- 3920 下方支撑

## 今日交易计划

计划只做空，反弹到 15m EMA50 附近出现顶分型入场，止损放 3985 上方。
如果跌破 3959，按新一轮下行处理，但这不是观点过期条件。

## 过期条件

观点有效 6 小时，价格偏离 0.7% 失效；目标位 3920，打到目标后重新评估；上破 3985 后停止使用。
"""


def test_market_view_syncs_from_obsidian_daily_analysis_note(tmp_path: Path):
    output_root = tmp_path / "outputs"
    vault = tmp_path / "vault"
    note_path = vault / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR / "2026-06-30.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(OBSIDIAN_NOTE, encoding="utf-8")
    original_note = note_path.read_text(encoding="utf-8")

    payload = MarketViewObsidianSync(output_root, vault).sync("2026-06-30")

    assert payload["run_date"] == "2026-06-30"
    assert payload["source"] == "Obsidian每日交易分析"
    assert payload["direction_score"] == 20
    assert payload["direction_bias"] == "strong_short"
    assert payload["timeframes"] == ["3D", "1D", "4H", "15m", "5m"]
    assert payload["summary"] == "今天偏空，3D/1D/4H 都没有重新转强，15m/5m 只等反弹到压力后找空。"
    assert "3985 上方压力" in payload["key_levels"]
    assert "计划只做空" in payload["trade_plan"]
    assert payload["expiry"]["valid_for_hours"] == 6
    assert payload["expiry"]["expires_if_price_moves_pct"] == 0.7
    assert payload["expiry"]["target_price"] == 3920
    assert payload["expiry"]["expire_below"] == 3920
    assert payload["expiry"]["expire_below"] != 3959
    assert payload["expiry"]["expire_above"] == 3985
    assert payload["intake"]["parser"] == "market_view_obsidian_v1"
    assert payload["intake"]["source_note"] == "003_park原始输出/每日交易分析/2026-06-30.md"
    assert payload["source_artifacts"]["obsidian"] == str(note_path)
    assert note_path.read_text(encoding="utf-8") == original_note

    current = load_json(output_root / "market_views" / "current.json")[0]
    dated = load_json(output_root / "market_views" / "2026-06-30.json")[0]
    assert current["intake"]["parser"] == "market_view_obsidian_v1"
    assert dated["source_artifacts"]["obsidian"] == str(note_path)
    assert (output_root / "market_views" / "2026-06-30.md").exists()
