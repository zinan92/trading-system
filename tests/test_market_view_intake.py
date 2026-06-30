from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json
from services.market_view import OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR
from services.market_view_intake import MarketViewIntake


ORAL_TEXT = """
今天是十分做空。3D、1D、4H 都偏空，15m/5m 找入场。
关键位 4400 平台，4100/4023 前低，目标位 3950，3877 支撑。
计划只做空，反弹到 EMA50 附近出现顶分型入场，止损放顶分型高点。
观点有效 4 小时，价格偏离 0.8% 失效，上破 4100 后停止使用。
"""


def test_market_view_intake_drafts_oral_view_with_expiry_conditions(tmp_path: Path):
    draft = MarketViewIntake(tmp_path / "outputs").draft("2026-06-26", ORAL_TEXT)

    assert draft.score == 10
    assert draft.expiry_target_price == 3950
    assert draft.expire_above == 4100
    assert draft.expire_below is None
    assert draft.valid_for_hours == 4
    assert draft.expires_if_price_moves_pct == 0.8
    assert {"3D", "1D", "4H", "15m", "5m"}.issubset(set(draft.timeframes))
    assert "4400 平台" in draft.key_levels
    assert "4100 前低" in draft.key_levels
    assert "4023 前低" in draft.key_levels
    assert "3950 目标位" in draft.key_levels
    assert "3877 支撑" in draft.key_levels
    assert "只做空" in draft.trade_plan
    assert "EMA50" in draft.trade_plan


def test_market_view_intake_records_store_artifact_and_obsidian_note(tmp_path: Path):
    output_root = tmp_path / "outputs"
    vault = tmp_path / "vault"

    payload = MarketViewIntake(output_root, obsidian_root=vault).record(
        "2026-06-26",
        ORAL_TEXT,
        write_obsidian=True,
    )

    assert payload["direction_score"] == 10
    assert payload["direction_bias"] == "strong_short"
    assert payload["expiry"]["target_price"] == 3950
    assert payload["expiry"]["expire_below"] == 3950
    assert payload["expiry"]["expire_above"] == 4100
    assert payload["intake"]["parser"] == "market_view_intake_v1"
    assert payload["intake"]["extracted"]["expiry_target_price"] == 3950

    saved = load_json(output_root / "market_views" / "2026-06-26.json")[0]
    current = load_json(output_root / "market_views" / "current.json")[0]
    assert saved["intake"]["extracted"]["expire_above"] == 4100
    assert current["run_date"] == "2026-06-26"

    obsidian = vault / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR / "2026-06-26.md"
    assert obsidian.exists()
    text = obsidian.read_text(encoding="utf-8")
    assert "目标位过期" in text
    assert "3950" in text


def test_market_view_intake_maps_ten_point_long_to_strong_long(tmp_path: Path):
    draft = MarketViewIntake(tmp_path / "outputs").draft(
        "2026-06-26",
        "今天十分做多。目标位 4100，观点有效 2 小时。",
    )

    assert draft.score == 90
    assert draft.expiry_target_price == 4100
    assert draft.valid_for_hours == 2
