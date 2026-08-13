import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.strategy_recommendation import (
    ACCOUNT_CONTEXT_SCHEMA,
    LONG_TERM_D1_BARS,
    RecommendationProviderError,
    StrategyRecommendationService,
    build_recommendation_account_context,
    build_position_first_framework,
)
from services.cloud_ai_provider import CloudAIProviderReadinessGateError


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
            "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", LONG_TERM_D1_BARS + 20, 4100, 20)},
            "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4070, 8)},
            "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 4)},
            "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4045, 2)},
        },
        current_plan={"direction": "neutral", "style": "steady", "version": 3},
        account={
            "equity": 10_000,
            "margin": 0,
            "execution": {
                "open_positions": [
                    {"position_id": "position-1", "side": "long", "status": "open"}
                ],
                "accepted_orders": [],
            },
        },
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
    assert result["prompt_contract"]["version"] == "strategy-recommendation-prompt-v4"
    assert result["framework"]["position"]["lookback_bars"] == LONG_TERM_D1_BARS
    assert result["framework"]["position"]["label"] == "high"
    assert result["strategy_type"] == "grid"
    assert "完整 D1、4H、1H、15m" in prompts[0]
    assert "不得输出概率或胜率" in prompts[0]
    assert "position-1" in prompts[0]
    assert "不得建议自动平仓、反手、对冲或删除既有 TP/SL" in prompts[0]
    receipt = result["evaluation_receipt"]
    assert receipt["schema_version"] == "strategy-ai-evaluation-v2"
    assert receipt["status"] == "success"
    assert receipt["input"]["contexts"]["15m"]["indicators"]["ema20"] is not None
    assert receipt["input"]["position_first_framework"]["strategy"]["recommended_strategy_type"] == "grid"
    assert receipt["input"]["source_manifests"]["1d"]["long_term_position_bar_count"] == LONG_TERM_D1_BARS + 20
    assert receipt["input"]["prompt"] == prompts[0]
    assert receipt["input"]["account"]["schema_version"] == ACCOUNT_CONTEXT_SCHEMA
    assert receipt["input"]["context_observability"]["prompt_char_count"] == len(prompts[0])
    assert receipt["output"]["provider_call_elapsed_ms"] >= 0
    assert '"direction": "short"' in receipt["output"]["raw_model_response"]
    assert receipt["output"]["parsed_decision"]["style"] == "steady"
    archive = tmp_path / "outputs" / receipt["archive"]["relative_path"]
    assert archive.exists()
    assert json.loads(archive.read_text(encoding="utf-8"))[0]["evaluation_id"] == receipt["evaluation_id"]


def test_recommendation_account_context_excludes_history_payloads() -> None:
    context = build_recommendation_account_context(
        {
            "equity": 10_000,
            "execution_account_source": "authoritative_execution_snapshot",
            "accounting_snapshot": {
                "snapshot_id": "accounting-1",
                "source_name": "nautilus_paper",
                "counts": {"order_count": 3, "fill_count": 9},
                "pnl": {"net_realized_pnl": 12.5},
                "fills": [{"fill_id": "must-not-enter-prompt"}],
                "trades": [{"trade_id": "must-not-enter-prompt"}],
                "account": {"equity": 10_000},
            },
            "execution": {
                "engine": "nautilus_paper",
                "cycle_id": "2026-07-05_DAY",
                "open_positions": [
                    {"position_id": "position-1", "side": "short", "quantity": 1}
                ],
                "accepted_orders": [
                    {"order_id": "order-1", "state": "accepted", "side": "sell"}
                ],
                "fills": [{"fill_id": "must-not-enter-prompt"}],
            },
        }
    )

    encoded = json.dumps(context, ensure_ascii=False)
    assert context["schema_version"] == ACCOUNT_CONTEXT_SCHEMA
    assert context["equity"] == 10_000
    assert context["execution"]["open_positions"][0]["position_id"] == "position-1"
    assert "must-not-enter-prompt" not in encoded
    assert "fills" not in context
    assert "trades" not in context


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
        "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", LONG_TERM_D1_BARS + 20, 4100, 20)},
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
    assert receipt["output"]["machine_code"] is None
    assert receipt["output"]["error"] == "provider unavailable"


def test_readiness_refusal_never_calls_recommendation_provider(
    tmp_path: Path,
) -> None:
    provider_calls = 0

    def provider(_: str) -> dict:
        nonlocal provider_calls
        provider_calls += 1
        return {}

    service = StrategyRecommendationService(
        tmp_path / "outputs",
        decision_provider=provider,
        provider_readiness_verifier=lambda: {
            "ok": False,
            "blocker": "cloud_ai_provider_readiness_stale",
        },
    )

    with pytest.raises(CloudAIProviderReadinessGateError) as raised:
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    assert raised.value.code == "cloud_ai_provider_readiness_unavailable"
    assert provider_calls == 0
    assert not (
        tmp_path / "outputs" / "dualtrack" / "strategy_control" / "evaluations"
    ).exists()


@pytest.mark.parametrize(
    "detail",
    [
        "socket timed out while waiting for bytes",
        "operator wording changed completely",
    ],
)
def test_typed_provider_failure_receipt_keeps_code_separate_from_text(
    tmp_path: Path,
    detail: str,
) -> None:
    def unavailable(_: str) -> dict:
        raise RecommendationProviderError(
            "strategy_recommendation_provider_timeout",
            detail,
        )

    service = StrategyRecommendationService(
        tmp_path / "outputs",
        decision_provider=unavailable,
    )
    with pytest.raises(RecommendationProviderError) as raised:
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    assert raised.value.code == (
        "strategy_recommendation_provider_timeout"
    )
    receipt_path = next(
        (
            tmp_path
            / "outputs"
            / "dualtrack"
            / "strategy_control"
            / "evaluations"
            / "2026-07-05_DAY"
        ).glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))[0]
    assert receipt["status"] == "failed"
    assert receipt["output"]["machine_code"] == (
        "strategy_recommendation_provider_timeout"
    )
    assert receipt["output"]["error"].endswith(detail)


def _external_provider_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "provider.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + body,
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _external_service(
    tmp_path: Path,
    script: Path,
    *,
    timeout_seconds: int = 5,
) -> StrategyRecommendationService:
    service = StrategyRecommendationService(
        tmp_path / "outputs",
        provider_timeout_seconds=timeout_seconds,
    )
    service.config["machine_planner"] = {
        "command": str(script),
        "model": "test-model",
        "timeout_seconds": timeout_seconds,
    }
    return service


def test_external_provider_receipt_persists_deadline_phases_return_code_and_redacted_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    script = _external_provider_script(
        tmp_path,
        """
import json
import sys
output = sys.argv[sys.argv.index('--output-last-message') + 1]
print('Authorization: Bearer redact-me', flush=True)
print('api_key=redact-me-too', flush=True)
open(output, 'w', encoding='utf-8').write(json.dumps({
    'direction': 'short',
    'style': 'steady',
    'rationale': 'external provider test',
    'key_levels': [],
    'ai_self_assessment': 5,
    'evidence_used': ['D1'],
}))
""",
    )
    receipt = _external_service(tmp_path, script).recommend(
        "2026-07-05_DAY",
        strategy_timeframes=_trusted_contexts(),
        current_plan={},
        account={},
        review={},
    )["evaluation_receipt"]

    trace = receipt["output"]["provider_call"]
    assert trace["deadline_seconds"] == 5
    assert trace["deadline_at"]
    assert trace["finished_at"]
    assert trace["elapsed_ms"] >= 0
    assert trace["return_code"] == 0
    assert trace["timed_out"] is False
    assert trace["phase_timings_ms"]["command_resolution"] >= 0
    assert trace["phase_timings_ms"]["subprocess"] >= 0
    assert "redact-me" not in trace["stdout"]
    assert "[REDACTED]" in trace["stdout"]


def test_external_provider_failure_receipt_persists_return_code_and_bounded_redacted_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    script = _external_provider_script(
        tmp_path,
        """
import sys
sys.stderr.write('Authorization: Bearer redact-me\\n' + 'x' * 6000)
sys.stderr.flush()
raise SystemExit(7)
""",
    )
    service = _external_service(tmp_path, script)
    with pytest.raises(RecommendationProviderError) as raised:
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    assert raised.value.code == "strategy_recommendation_provider_failed"
    receipt_path = next(
        (
            tmp_path
            / "outputs"
            / "dualtrack"
            / "strategy_control"
            / "evaluations"
            / "2026-07-05_DAY"
        ).glob("*.json")
    )
    trace = json.loads(receipt_path.read_text(encoding="utf-8"))[0]["output"][
        "provider_call"
    ]
    assert trace["return_code"] == 7
    assert len(trace["stderr"]) <= 4110
    assert "redact-me" not in trace["stderr"]
    assert trace["stderr"].endswith("...[truncated]")


def test_external_provider_timeout_receipt_keeps_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    script = _external_provider_script(
        tmp_path,
        """
import sys
import time
print('partial stdout', flush=True)
print('partial stderr', file=sys.stderr, flush=True)
time.sleep(2)
""",
    )
    service = _external_service(tmp_path, script, timeout_seconds=1)
    with pytest.raises(RecommendationProviderError) as raised:
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    assert raised.value.code == "strategy_recommendation_provider_timeout"
    receipt_path = next(
        (
            tmp_path
            / "outputs"
            / "dualtrack"
            / "strategy_control"
            / "evaluations"
            / "2026-07-05_DAY"
        ).glob("*.json")
    )
    trace = json.loads(receipt_path.read_text(encoding="utf-8"))[0]["output"][
        "provider_call"
    ]
    assert trace["deadline_seconds"] == 1
    assert trace["timed_out"] is True
    assert trace["return_code"] is None
    assert trace["partial_output"] is True
    assert "partial stdout" in trace["stdout"]
    assert "partial stderr" in trace["stderr"]


def _trusted_contexts() -> dict:
    return {
        "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", LONG_TERM_D1_BARS + 20, 4100, 20)},
        "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4070, 8)},
        "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 4)},
        "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4045, 2)},
    }


def _configure_external_provider(
    service: StrategyRecommendationService,
    command: str,
    *,
    timeout_seconds: int = 10,
) -> None:
    service.config = {
        **service.config,
        "machine_planner": {
            **dict(service.config.get("machine_planner") or {}),
            "command": command,
            "timeout_seconds": timeout_seconds,
        },
    }


def test_configured_portable_provider_command_returns_valid_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = tmp_path / "provider.py"
    provider.write_text(
        """#!/usr/bin/env python3
import json, sys
output = sys.argv[sys.argv.index("--output-last-message") + 1]
decision = {
    "direction": "neutral",
    "style": "steady",
    "rationale": "长期位置偏低但趋势尚未建立，维持中性网格。",
    "key_levels": [4050.0],
    "ai_self_assessment": 7,
    "evidence_used": ["D1", "4H", "1H"],
}
open(output, "w", encoding="utf-8").write(json.dumps(decision, ensure_ascii=False))
""",
        encoding="utf-8",
    )
    provider.chmod(0o700)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    service = StrategyRecommendationService(tmp_path / "outputs")
    _configure_external_provider(service, str(provider))

    result = service.recommend(
        "2026-07-05_DAY",
        strategy_timeframes=_trusted_contexts(),
        current_plan={},
        account={},
        review={},
    )

    assert result["direction"] == "neutral"
    assert result["evaluation_receipt"]["status"] == "success"


@pytest.mark.parametrize(
    ("provider_setup", "expected_code"),
    [
        ("missing", "strategy_recommendation_provider_missing"),
        ("not_executable", "strategy_recommendation_provider_not_executable"),
        ("invalid_output", "strategy_recommendation_provider_invalid_output"),
    ],
)
def test_external_provider_failures_have_stable_codes_and_failed_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_setup: str,
    expected_code: str,
) -> None:
    provider = tmp_path / "provider"
    if provider_setup == "missing":
        command = str(provider)
    else:
        provider.write_text(
            "#!/bin/sh\n"
            + (
                "exit 0\n"
                if provider_setup == "invalid_output"
                else "exit 0\n"
            ),
            encoding="utf-8",
        )
        provider.chmod(0o600 if provider_setup == "not_executable" else 0o700)
        command = str(provider)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    service = StrategyRecommendationService(tmp_path / "outputs")
    _configure_external_provider(service, command)

    with pytest.raises(RuntimeError, match=expected_code):
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    receipt_path = next(
        (tmp_path / "outputs" / "dualtrack" / "strategy_control" / "evaluations" / "2026-07-05_DAY").glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))[0]
    assert receipt["status"] == "failed"
    assert receipt["output"]["machine_code"] == expected_code
    assert expected_code in receipt["output"]["error"]


def test_external_provider_timeout_has_stable_code_and_failed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], timeout=1)
        ),
    )
    service = StrategyRecommendationService(tmp_path / "outputs")
    _configure_external_provider(service, os.environ.get("PYTHON", "/usr/bin/python3"), timeout_seconds=1)

    with pytest.raises(RuntimeError, match="strategy_recommendation_provider_timeout"):
        service.recommend(
            "2026-07-05_DAY",
            strategy_timeframes=_trusted_contexts(),
            current_plan={},
            account={},
            review={},
        )

    receipt_path = next(
        (tmp_path / "outputs" / "dualtrack" / "strategy_control" / "evaluations" / "2026-07-05_DAY").glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))[0]
    assert receipt["status"] == "failed"
    assert receipt["output"]["machine_code"] == (
        "strategy_recommendation_provider_timeout"
    )
    assert "strategy_recommendation_provider_timeout" in receipt["output"]["error"]


def test_position_first_framework_classifies_position_before_trend_archetype() -> None:
    def contexts(rank: float, d1_trend: str, h4_trend: str, d1_efficiency: float, h4_efficiency: float) -> dict:
        return {
            "1d": {
                "trend": d1_trend,
                "recent_directional_efficiency": d1_efficiency,
                "long_term_position": {"lookback_bars": LONG_TERM_D1_BARS, "window_low": 3900, "window_high": 4300, "rank": rank},
            },
            "4h": {"trend": h4_trend, "recent_directional_efficiency": h4_efficiency},
        }

    low_established = build_position_first_framework(contexts(0.2, "up", "up", 0.6, 0.5))
    assert low_established["position"]["directional_prior"] == "long"
    assert low_established["trend"]["stage"] == "established"
    assert low_established["strategy"]["recommended_strategy_type"] == "dca"

    high_conflict = build_position_first_framework(contexts(0.8, "up", "up", 0.6, 0.5))
    assert high_conflict["position"]["directional_prior"] == "short"
    assert high_conflict["trend"]["position_conflicts_with_trend"] is True
    assert high_conflict["strategy"]["recommended_strategy_type"] == "grid"

    forming = build_position_first_framework(contexts(0.5, "up", "up", 0.25, 0.2))
    assert forming["trend"]["stage"] == "forming"
    assert forming["strategy"]["recommended_strategy_type"] == "grid"
