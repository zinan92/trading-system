from pathlib import Path

import pytest

from services.market_view import OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR, MarketViewStore, direction_bias_from_score
from services.journal_store import load_json


@pytest.mark.parametrize(
    ("score", "bias", "blocked"),
    [
        (10, "strong_short", ["long"]),
        (35, "short_bias", []),
        (50, "neutral", []),
        (65, "long_bias", []),
        (90, "strong_long", ["short"]),
    ],
)
def test_direction_bias_buckets(score: int, bias: str, blocked: list[str]):
    result = direction_bias_from_score(score)

    assert result["bias"] == bias
    assert result["blocked_directions"] == blocked


def test_market_view_records_json_markdown_and_obsidian(tmp_path: Path):
    root = tmp_path / "outputs"
    vault = tmp_path / "vault"

    result = MarketViewStore(root, obsidian_root=vault).record(
        run_date="2026-06-25",
        score=10,
        summary="三日线、日线、4H 都偏空。",
        raw_text="今天强烈做空。",
        key_levels=["4400 平台", "4100 前低", "3877 支撑"],
        timeframes=["3D", "1D", "4H", "15m", "5m"],
        trade_plan="反弹到 15m EMA50 附近找做空。",
        write_obsidian=True,
    )

    assert result["direction_bias"] == "strong_short"
    assert result["blocked_directions"] == ["long"]
    assert result["expiry"]["valid_for_hours"] == 12
    assert result["expiry"]["expires_if_price_moves_pct"] == 1.0
    saved = load_json(root / "market_views" / "2026-06-25.json")[0]
    assert saved["direction_score"] == 10
    assert (root / "market_views" / "2026-06-25.md").exists()
    obsidian = vault / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR / "2026-06-25.md"
    assert obsidian.exists()
    assert "反弹到 15m EMA50" in obsidian.read_text(encoding="utf-8")


def test_market_view_requires_summary_and_timeframe(tmp_path: Path):
    with pytest.raises(ValueError, match="summary"):
        MarketViewStore(tmp_path / "outputs").record("2026-06-25", 50, "", timeframes=["1D"])
    with pytest.raises(ValueError, match="timeframe"):
        MarketViewStore(tmp_path / "outputs").record("2026-06-25", 50, "neutral")
    with pytest.raises(ValueError, match="0 and 100"):
        direction_bias_from_score(120)


def test_market_view_records_expiry_conditions(tmp_path: Path):
    root = tmp_path / "outputs"

    result = MarketViewStore(root).record(
        run_date="2026-06-25",
        score=10,
        summary="4000 附近强空，但跌太多后重新评估。",
        timeframes=["1D", "4H"],
        reference_price=4000,
        valid_for_hours=4,
        expires_if_price_moves_pct=0.8,
        expire_below=3950,
    )

    assert result["reference_price"] == 4000
    assert result["expiry"]["valid_for_hours"] == 4
    assert result["expiry"]["expires_if_price_moves_pct"] == 0.8
    assert result["expiry"]["expire_below"] == 3950


def test_market_view_maps_target_price_to_directional_expiry(tmp_path: Path):
    root = tmp_path / "outputs"

    short_view = MarketViewStore(root).record(
        run_date="2026-06-25",
        score=10,
        summary="强空，打到 3950 后重新评估。",
        timeframes=["1D", "4H"],
        expiry_target_price=3950,
    )
    assert short_view["expiry"]["target_price"] == 3950
    assert short_view["expiry"]["expire_below"] == 3950
    assert short_view["expiry"]["expire_above"] is None
    assert "target_price" in (root / "market_views" / "2026-06-25.md").read_text(encoding="utf-8") or "目标位过期" in (root / "market_views" / "2026-06-25.md").read_text(encoding="utf-8")

    long_view = MarketViewStore(root).record(
        run_date="2026-06-26",
        score=90,
        summary="强多，打到 4100 后重新评估。",
        timeframes=["1D", "4H"],
        expiry_target_price=4100,
    )
    assert long_view["expiry"]["target_price"] == 4100
    assert long_view["expiry"]["expire_above"] == 4100
    assert long_view["expiry"]["expire_below"] is None
