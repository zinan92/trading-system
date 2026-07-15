import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.strategy_recommendation import StrategyRecommendationService


def _bars(timeframe: str, count: int, close: float, span: float) -> list[dict]:
    rows = []
    step = {"1d": timedelta(days=1), "4h": timedelta(hours=4), "1h": timedelta(hours=1), "15m": timedelta(minutes=15)}[timeframe]
    started = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for index in range(count):
        value = close - 4 + index * 0.2
        rows.append({
            "timestamp": (started + step * index).isoformat(),
            "open": value - 0.2,
            "high": value + span / 2,
            "low": value - span / 2,
            "close": value,
        })
    return rows


def test_strategy_recommendation_uses_fixed_multitimeframe_input_and_rule_score(tmp_path: Path) -> None:
    prompts = []

    def provider(prompt: str) -> dict:
        prompts.append(prompt)
        return {
            "direction": "short",
            "style": "steady",
            "rationale": "日线与四小时共同走弱，建议使用更宽区间等待反弹挂空。",
            "key_levels": [4050, 4100],
            "ai_self_assessment": 7,
            "evidence_used": ["D1", "4H", "current_plan"],
        }

    service = StrategyRecommendationService(tmp_path / "outputs", decision_provider=provider)
    result = service.recommend(
        "2026-07-05_DAY",
        strategy_timeframes={
            "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", 20, 4100, 20)},
            "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4070, 8)},
            "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 4)},
            "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4045, 2)},
        },
        current_plan={"direction": "neutral", "style": "steady", "version": 3},
        account={"equity": 10_000, "margin": 0},
        review={"summary": "上一周期方向失误"},
        now="2026-07-05T02:00:00+00:00",
    )

    assert result["direction"] == "short"
    assert result["style"] == "steady"
    assert result["signal"]["calibration_status"] == "uncalibrated"
    assert 0 <= result["signal"]["rule_score"] <= 100
    assert result["signal"]["ai_self_assessment"] == 7
    assert result["analysis"]["timeframes"] == ["1d", "4h", "1h", "15m"]
    assert result["analysis"]["contexts"]["15m"]["indicators"]["ema20"] is not None
    assert result["analysis"]["contexts"]["15m"]["indicators"]["macd"]["histogram"] is not None
    assert result["prompt_contract"]["version"] == "strategy-recommendation-prompt-v2"
    assert "完整 D1、4H、1H、15m" in prompts[0]
    assert "不得输出概率或胜率" in prompts[0]
    receipt = result["evaluation_receipt"]
    assert receipt["schema_version"] == "strategy-ai-evaluation-v1"
    assert receipt["status"] == "success"
    assert receipt["input"]["contexts"]["15m"]["indicators"]["ema20"] is not None
    assert receipt["input"]["prompt"] == prompts[0]
    assert '"direction": "short"' in receipt["output"]["raw_model_response"]
    assert receipt["output"]["parsed_decision"]["style"] == "steady"
    archive = tmp_path / "outputs" / receipt["archive"]["relative_path"]
    assert archive.exists()
    assert json.loads(archive.read_text(encoding="utf-8"))[0]["evaluation_id"] == receipt["evaluation_id"]


def test_strategy_recommendation_rejects_untrusted_or_insufficient_context(tmp_path: Path) -> None:
    service = StrategyRecommendationService(tmp_path / "outputs", decision_provider=lambda prompt: {})
    invalid = {
        "1d": {"provider": "synthetic_seed", "is_synthetic": True, "bars": _bars("1d", 20, 4100, 20)},
        "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4070, 8)},
        "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 4)},
        "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4045, 2)},
    }

    try:
        service.recommend("2026-07-05_DAY", strategy_timeframes=invalid, current_plan={}, account={}, review={})
    except ValueError as exc:
        assert "synthetic" in str(exc)
    else:
        raise AssertionError("expected fail-closed synthetic rejection")


def test_strategy_recommendation_archives_provider_failure(tmp_path: Path) -> None:
    def unavailable(_: str) -> dict:
        raise RuntimeError("provider unavailable")

    service = StrategyRecommendationService(tmp_path / "outputs", decision_provider=unavailable)
    contexts = {
        "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", 20, 4100, 20)},
        "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4070, 8)},
        "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 4)},
        "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4045, 2)},
    }
    try:
        service.recommend("2026-07-05_DAY", strategy_timeframes=contexts, current_plan={}, account={}, review={})
    except RuntimeError as exc:
        assert "provider unavailable" in str(exc)
    else:
        raise AssertionError("expected provider failure")

    archives = list((tmp_path / "outputs" / "dualtrack" / "strategy_control" / "evaluations" / "2026-07-05_DAY").glob("*.json"))
    assert len(archives) == 1
    receipt = json.loads(archives[0].read_text(encoding="utf-8"))[0]
    assert receipt["status"] == "failed"
    assert receipt["output"]["error"] == "provider unavailable"
