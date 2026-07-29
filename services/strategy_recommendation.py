"""Auditable AI proposal generation for the single production strategy.

The model recommends direction and style. Numeric range, spacing, sizing and
risk remain deterministic outputs of ``StrategyControlPlane.preview``. A
recommendation is a proposal only; this service never mutates production state.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT
from services.dualtrack_config import dualtrack_config
from services.journal_store import write_json


PROMPT_VERSION = "strategy-recommendation-prompt-v3"
EVALUATION_SCHEMA = "strategy-ai-evaluation-v2"
TIMEFRAMES = ("1d", "4h", "1h", "15m")
LONG_TERM_D1_BARS = 200


class RecommendationProviderError(RuntimeError):
    """Stable operator-facing failure from the external AI provider port."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail).strip()
        message = self.code if not self.detail else f"{self.code}:{self.detail}"
        super().__init__(message)


class StrategyRecommendationService:
    def __init__(
        self,
        output_root: Path,
        *,
        decision_provider: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.config = dualtrack_config()
        self.decision_provider = decision_provider or self._codex_decision
        self._last_raw_model_response: str | None = None

    def recommend(
        self,
        cycle_id: str,
        *,
        strategy_timeframes: dict[str, Any],
        current_plan: dict[str, Any] | None,
        account: dict[str, Any] | None,
        review: dict[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        created_at = str(now or datetime.now(timezone.utc).isoformat())
        evaluation_id = f"ai-eval-{uuid.uuid4().hex[:16]}"
        receipt: dict[str, Any] = {
            "schema_version": EVALUATION_SCHEMA,
            "evaluation_id": evaluation_id,
            "cycle_id": cycle_id,
            "evaluated_at": created_at,
            "status": "started",
            "input": {
                "timeframes_requested": list(TIMEFRAMES),
                "completed_bars_only": True,
                "synthetic_allowed": False,
                "chart_timeframe_used": False,
                "source_manifests": {
                    timeframe: {
                        "provider": (strategy_timeframes.get(timeframe) or {}).get("provider"),
                        "is_synthetic": (strategy_timeframes.get(timeframe) or {}).get("is_synthetic"),
                        "bar_count": len((strategy_timeframes.get(timeframe) or {}).get("bars") or []),
                        "long_term_position_bar_count": len((strategy_timeframes.get(timeframe) or {}).get("long_term_position_bars") or (strategy_timeframes.get(timeframe) or {}).get("bars") or []),
                        "latest_timestamp": (((strategy_timeframes.get(timeframe) or {}).get("bars") or [{}])[-1]).get("timestamp"),
                    }
                    for timeframe in TIMEFRAMES
                },
                "current_plan": dict(current_plan or {}),
                "account": dict(account or {}),
                "review": dict(review or {}),
            },
            "output": {},
            "effects": {
                "mode": "proposal_only",
                "production_plan_mutation_allowed": False,
                "orders_allowed": False,
            },
        }
        prompt = ""
        try:
            contexts = {
                timeframe: self._context_summary(strategy_timeframes, timeframe)
                for timeframe in TIMEFRAMES
            }
            framework = build_position_first_framework(contexts)
            prompt = self._prompt(
                cycle_id,
                contexts=contexts,
                framework=framework,
                current_plan=current_plan or {},
                account=account or {},
                review=review or {},
            )
            prompt_contract = {
                "version": PROMPT_VERSION,
                "model": str((self.config.get("machine_planner") or {}).get("model") or "gpt-5.4"),
                "system_prompt": None,
                "system_prompt_note": "No hidden repository system prompt; the complete task prompt is stored with this proposal.",
                "prompt": prompt,
            }
            receipt["input"].update({"contexts": contexts, "position_first_framework": framework, "prompt": prompt, "prompt_contract": prompt_contract})
            self._last_raw_model_response = None
            decision = self.decision_provider(prompt)
            raw_model_response = self._last_raw_model_response or json.dumps(
                decision, ensure_ascii=False, sort_keys=True, indent=2
            )
            if not isinstance(decision, dict):
                raise ValueError("AI recommendation must be a JSON object")
            direction = str(decision.get("direction") or "").lower()
            style = str(decision.get("style") or "").lower()
            if direction not in {"neutral", "long", "short"}:
                raise ValueError("AI recommendation direction must be neutral, long, or short")
            if style not in {"steady", "aggressive"}:
                raise ValueError("AI recommendation style must be steady or aggressive")
            rationale = str(decision.get("rationale") or "").strip()
            if not rationale:
                raise ValueError("AI recommendation rationale is required")
            self_assessment = _bounded_number(decision.get("ai_self_assessment"), 1, 10, "ai_self_assessment")
            components = self._rule_score(contexts, direction=direction, style=style)
            rule_score = round(sum(components.values()), 1)
            signal = {
                "rule_score": rule_score,
                "rule_score_components": components,
                "calibration_status": "uncalibrated",
                "ai_self_assessment": self_assessment,
                "ai_self_assessment_scale": "1-10 self-rated reasoning adequacy; not a probability",
            }
            receipt["status"] = "success"
            receipt["output"] = {
                "raw_model_response": raw_model_response,
                "parsed_decision": dict(decision),
                "deterministic_validation": {
                    "accepted": True,
                    "rule_score": rule_score,
                    "rule_score_components": components,
                    "calibration_status": "uncalibrated",
                    "position_first_framework": framework,
                },
                "final_recommendation": {
                    "direction": direction,
                    "style": style,
                    "rationale": rationale,
                    "key_levels": [float(value) for value in decision.get("key_levels") or []],
                },
            }
            receipt = self.persist_receipt(receipt)
            return {
                "schema_version": "strategy-recommendation-v2",
                "cycle_id": cycle_id,
                "created_at": created_at,
                "direction": direction,
                "style": style,
                "rationale": rationale,
                "key_levels": [float(value) for value in decision.get("key_levels") or []],
                "evidence_used": [str(value) for value in decision.get("evidence_used") or []],
                "signal": signal,
                "strategy_type": framework["strategy"]["recommended_strategy_type"],
                "framework": framework,
                "analysis": {
                    "timeframes": list(TIMEFRAMES),
                    "contexts": contexts,
                    "framework": framework,
                    "chart_timeframe_used": False,
                },
                "prompt_contract": prompt_contract,
                "evaluation_receipt": receipt,
            }
        except Exception as exc:
            receipt["status"] = "failed"
            if prompt:
                receipt["input"].setdefault("prompt", prompt)
            receipt["output"] = {
                "raw_model_response": self._last_raw_model_response,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.persist_receipt(receipt)
            raise

    def persist_receipt(self, receipt: dict[str, Any], *, effects: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically archive one immutable-by-id evaluation receipt locally."""
        saved = json.loads(json.dumps(receipt, ensure_ascii=False))
        if effects:
            saved.setdefault("effects", {}).update(effects)
        archive = dict(saved.get("archive") or {})
        if archive.get("relative_path"):
            path = self.output_root / str(archive["relative_path"])
        else:
            stamp = str(saved.get("evaluated_at") or "unknown").replace(":", "-").replace("+", "_")
            path = (
                self.output_root / "dualtrack" / "strategy_control" / "evaluations"
                / str(saved.get("cycle_id") or "unknown")
                / f"{stamp}_{saved.get('evaluation_id')}.json"
            )
        relative_path = str(path.relative_to(self.output_root))
        checksum_payload = {key: value for key, value in saved.items() if key != "archive"}
        checksum = hashlib.sha256(
            json.dumps(checksum_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        saved["archive"] = {
            "relative_path": relative_path,
            "sha256": checksum,
            "format": "json",
            "record_count": 1,
        }
        write_json(path, [saved])
        return saved

    def _context_summary(self, contexts: dict[str, Any], timeframe: str) -> dict[str, Any]:
        context = contexts.get(timeframe) if isinstance(contexts, dict) else None
        if not isinstance(context, dict):
            raise ValueError(f"strategy timeframe {timeframe} is unavailable")
        if context.get("is_synthetic") is not False:
            raise ValueError(f"strategy timeframe {timeframe} is synthetic")
        provider = str(context.get("provider") or "").strip()
        if not provider:
            raise ValueError(f"strategy timeframe {timeframe} provider is missing")
        bars = list(context.get("bars") or [])
        long_term_bars = list(context.get("long_term_position_bars") or bars)
        minimum = LONG_TERM_D1_BARS if timeframe == "1d" else 15
        available = len(long_term_bars) if timeframe == "1d" else len(bars)
        if available < minimum:
            raise ValueError(f"strategy timeframe {timeframe} history is insufficient")
        closes = [_positive(bar.get("close"), f"{timeframe} close") for bar in bars]
        atr = _atr(bars)
        change = (closes[-1] / closes[0] - 1.0) if closes[0] else 0.0
        ema_fast = _ema(closes, min(20, len(closes)))
        ema_slow = _ema(closes, min(50, len(closes)))
        net_move = abs(closes[-1] - closes[0])
        path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
        efficiency = net_move / path if path else 0.0
        recent = bars[-20:]
        recent_closes = closes[-20:]
        recent_move = abs(recent_closes[-1] - recent_closes[0])
        recent_path = sum(abs(right - left) for left, right in zip(recent_closes, recent_closes[1:]))
        recent_efficiency = recent_move / recent_path if recent_path else 0.0
        long_term = long_term_bars[-LONG_TERM_D1_BARS:] if timeframe == "1d" else []
        long_low = min((_positive(bar.get("low"), f"{timeframe} low") for bar in long_term), default=None)
        long_high = max((_positive(bar.get("high"), f"{timeframe} high") for bar in long_term), default=None)
        position_close = _positive(long_term[-1].get("close"), f"{timeframe} position close") if long_term else closes[-1]
        position_rank = (
            (position_close - long_low) / (long_high - long_low)
            if long_low is not None and long_high is not None and long_high > long_low
            else None
        )
        return {
            "timeframe": timeframe,
            "provider": provider,
            "bar_count": len(bars),
            "start": bars[0].get("timestamp"),
            "end": bars[-1].get("timestamp"),
            "close": round(closes[-1], 6),
            "change_pct": round(change * 100.0, 4),
            "atr14": round(atr, 6),
            "atr_pct": round(atr / closes[-1] * 100.0, 4),
            "ema_fast": round(ema_fast, 6),
            "ema_slow": round(ema_slow, 6),
            "indicators": {
                "ema20": round(_ema(closes, 20), 6) if len(closes) >= 20 else None,
                "ema50": round(_ema(closes, 50), 6) if len(closes) >= 50 else None,
                "macd": _macd(closes),
            },
            "trend": "up" if ema_fast > ema_slow else "down" if ema_fast < ema_slow else "flat",
            "directional_efficiency": round(efficiency, 4),
            "recent_directional_efficiency": round(recent_efficiency, 4),
            "recent_change_pct": round((recent_closes[-1] / recent_closes[0] - 1.0) * 100.0, 4) if recent_closes[0] else 0.0,
            "recent_closes": [round(value, 6) for value in recent_closes],
            "long_term_position": {
                "lookback_bars": len(long_term),
                "window_low": round(long_low, 6) if long_low is not None else None,
                "window_high": round(long_high, 6) if long_high is not None else None,
                "close": round(position_close, 6),
                "rank": round(position_rank, 4) if position_rank is not None else None,
            } if timeframe == "1d" else None,
            "last_completed_bar": {
                key: bars[-1].get(key) for key in ("timestamp", "open", "high", "low", "close", "volume")
            },
        }

    def _prompt(
        self,
        cycle_id: str,
        *,
        contexts: dict[str, Any],
        framework: dict[str, Any],
        current_plan: dict[str, Any],
        account: dict[str, Any],
        review: dict[str, Any],
    ) -> str:
        return f"""你是黄金 paper trading 单一生产策略的市场评估员。你的输出只会创建 AI 提案，不会自动修改生产计划或下单。

周期: {cycle_id}
完整 D1、4H、1H、15m 可信行情与指标: {json.dumps(contexts, ensure_ascii=False, sort_keys=True)}
确定性评估框架（必须按此顺序解释，不可绕过）: {json.dumps(framework, ensure_ascii=False, sort_keys=True)}
当前生产计划: {json.dumps(current_plan, ensure_ascii=False, sort_keys=True)}
账户与权威执行状态（包括 open_positions / accepted_orders）: {json.dumps(account, ensure_ascii=False, sort_keys=True)}
上一周期复盘: {json.dumps(review, ensure_ascii=False, sort_keys=True)}

只输出 JSON 对象，字段必须为：
- direction: neutral、long 或 short
- style: steady 或 aggressive
- rationale: 直白中文，严格按「长期位置 → 趋势阶段 → Grid/DCA 与参数职责」说明 D1、4H、1H 证据和建议。若方向与长期位置倾向冲突，必须明确说明冲突。
- key_levels: number[]，只列从输入行情中可解释的关键位
- ai_self_assessment: 1-10，只代表你认为本次推理材料是否充分，不是置信概率
- evidence_used: string[]，列出实际使用的输入，例如 D1、4H、1H、current_plan、review

约束：
1. 不得生成 range、网格间距、网格数量、每格资金或杠杆；这些由确定性风险引擎计算。
2. 不得输出概率或胜率，不得把 ai_self_assessment 称为 confidence。
3. 图表展示周期不是策略输入。只使用上面固定的 D1、4H、1H、15m 数据；15m 只用于短周期确认，不得改变 D1 Range 与 4H spacing 合同。
4. 材料冲突时必须写明冲突；信息不足时必须明确说明，禁止静默补全。
5. 只输出 JSON，不要 markdown。
6. 长期位置是默认方向倾向；趋势阶段只决定更适合 Grid 还是 DCA。不得把 DCA 写成自动启用，也不得绕过确定性 Range、格子、杠杆和风险计算。
7. 必须先检查账户里的 open_positions。若建议方向与已有持仓相反，明确标注冲突；不得建议自动平仓、反手、对冲或删除既有 TP/SL。
"""

    def _rule_score(self, contexts: dict[str, Any], *, direction: str, style: str) -> dict[str, float]:
        wanted = {"long": "up", "short": "down"}.get(direction)
        trends = [str(contexts[timeframe]["trend"]) for timeframe in TIMEFRAMES]
        if direction == "neutral":
            alignment_ratio = 1.0 - max(trends.count("up"), trends.count("down")) / len(trends)
        else:
            alignment_ratio = trends.count(wanted) / len(trends)
        strength = min(1.0, sum(abs(float(contexts[tf]["change_pct"])) / max(float(contexts[tf]["atr_pct"]), 0.01) for tf in TIMEFRAMES) / 9.0)
        efficiency = sum(float(contexts[tf]["directional_efficiency"]) for tf in TIMEFRAMES) / len(TIMEFRAMES)
        regime_fit = (1.0 - efficiency) if direction == "neutral" else efficiency
        if style == "aggressive":
            regime_fit *= min(1.0, strength * 1.25)
        # Component weights sum to 90. The reserved 10-point calibration
        # component is zero until historical shadow outcomes exist.
        return {
            "timeframe_alignment": round(35.0 * max(0.0, min(1.0, alignment_ratio)), 1),
            "signal_strength": round(25.0 * max(0.0, min(1.0, strength)), 1),
            "regime_fit": round(20.0 * max(0.0, min(1.0, regime_fit)), 1),
            "data_health": 10.0,
            "historical_calibration": 0.0,
        }

    def _codex_decision(self, prompt: str) -> dict[str, Any]:
        if os.getenv("PYTEST_CURRENT_TEST"):
            raise RuntimeError("external recommendation provider disabled under pytest")
        planner = self.config.get("machine_planner") if isinstance(self.config.get("machine_planner"), dict) else {}
        command = str(planner.get("command") or "codex")
        model = str(planner.get("model") or "gpt-5.4")
        timeout = int(planner.get("timeout_seconds") or 240)
        command_args = shlex.split(command)
        if not command_args:
            raise RecommendationProviderError(
                "strategy_recommendation_provider_command_invalid",
                "configured command is empty",
            )
        executable = command_args[0]
        executable_path = Path(executable)
        if executable_path.is_absolute() and not executable_path.exists():
            raise RecommendationProviderError(
                "strategy_recommendation_provider_missing",
                executable,
            )
        if executable_path.is_absolute() and not os.access(executable, os.X_OK):
            raise RecommendationProviderError(
                "strategy_recommendation_provider_not_executable",
                executable,
            )
        resolved = executable if executable_path.is_absolute() else shutil.which(executable)
        if not resolved:
            raise RecommendationProviderError(
                "strategy_recommendation_provider_missing",
                executable,
            )
        command_args[0] = resolved
        with tempfile.NamedTemporaryFile(prefix="strategy-recommendation-", suffix=".json", delete=False) as handle:
            result_path = Path(handle.name)
        try:
            args = [
                *command_args, "--ask-for-approval", "never", "exec",
                "--ignore-user-config", "--ephemeral", "--model", model,
                "--sandbox", "read-only", "--cd", str(ROOT),
                "--output-last-message", str(result_path), "-",
            ]
            env = dict(os.environ)
            try:
                result = subprocess.run(
                    args,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise RecommendationProviderError(
                    "strategy_recommendation_provider_timeout",
                    f"{timeout}s",
                ) from exc
            except OSError as exc:
                raise RecommendationProviderError(
                    "strategy_recommendation_provider_unavailable",
                    str(exc),
                ) from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown recommendation error").strip()
                raise RecommendationProviderError(
                    "strategy_recommendation_provider_failed",
                    detail[-1000:],
                )
            try:
                self._last_raw_model_response = result_path.read_text(encoding="utf-8")
                return _parse_json_object(self._last_raw_model_response)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RecommendationProviderError(
                    "strategy_recommendation_provider_invalid_output",
                    str(exc),
                ) from exc
        finally:
            result_path.unlink(missing_ok=True)


def build_position_first_framework(contexts: dict[str, Any]) -> dict[str, Any]:
    """Classify long-term position before choosing a Grid or DCA archetype.

    This is an auditable advisory frame, not a risk override or execution
    decision. The D1 position supplies the directional prior. D1/4H structure
    then determines whether a trend is established enough to *recommend* DCA;
    every resulting range, spacing, size and order still comes from the
    deterministic preview/control plane.
    """
    d1 = dict(contexts.get("1d") or {})
    h4 = dict(contexts.get("4h") or {})
    long_term = dict(d1.get("long_term_position") or {})
    rank = long_term.get("rank")
    if not isinstance(rank, (int, float)) or not 0.0 <= float(rank) <= 1.0:
        raise ValueError("D1 long-term position is unavailable")
    lookback = int(long_term.get("lookback_bars") or 0)
    if lookback < LONG_TERM_D1_BARS:
        raise ValueError("D1 long-term position history is insufficient")

    if rank <= 1.0 / 3.0:
        position_label, directional_prior = "low", "long"
    elif rank >= 2.0 / 3.0:
        position_label, directional_prior = "high", "short"
    else:
        position_label, directional_prior = "middle", "neutral"

    d1_trend = str(d1.get("trend") or "flat")
    h4_trend = str(h4.get("trend") or "flat")
    d1_efficiency = _unit_interval(d1.get("recent_directional_efficiency"))
    h4_efficiency = _unit_interval(h4.get("recent_directional_efficiency"))
    aligned_direction = d1_trend if d1_trend in {"up", "down"} and d1_trend == h4_trend else "flat"
    if aligned_direction != "flat" and d1_efficiency >= 0.35 and h4_efficiency >= 0.35:
        trend_stage = "established"
        trend_direction = aligned_direction
    elif aligned_direction != "flat" or (
        d1_trend in {"up", "down"} and (d1_efficiency >= 0.2 or h4_efficiency >= 0.25)
    ):
        trend_stage = "forming"
        trend_direction = aligned_direction if aligned_direction != "flat" else d1_trend
    else:
        trend_stage = "range"
        trend_direction = "flat"

    position_conflicts_with_trend = (
        directional_prior != "neutral"
        and trend_direction in {"up", "down"}
        and {directional_prior, trend_direction} in ({"long", "down"}, {"short", "up"})
    )
    dca_eligible = trend_stage == "established" and not position_conflicts_with_trend
    strategy_type = "dca" if dca_eligible else "grid"
    strategy_reason = (
        "established_trend_aligned_with_position"
        if dca_eligible
        else "position_trend_conflict"
        if position_conflicts_with_trend
        else "trend_not_established"
    )
    return {
        "schema_version": "position-first-market-framework-v1",
        "decision_order": ["long_term_position", "trend_stage", "strategy_and_parameters"],
        "position": {
            "timeframe": "1d",
            "lookback_bars": lookback,
            "window_low": long_term.get("window_low"),
            "window_high": long_term.get("window_high"),
            "rank": round(float(rank), 4),
            "label": position_label,
            "directional_prior": directional_prior,
        },
        "trend": {
            "timeframes": ["1d", "4h"],
            "stage": trend_stage,
            "direction": trend_direction,
            "d1_trend": d1_trend,
            "h4_trend": h4_trend,
            "d1_recent_efficiency": round(d1_efficiency, 4),
            "h4_recent_efficiency": round(h4_efficiency, 4),
            "position_conflicts_with_trend": position_conflicts_with_trend,
        },
        "strategy": {
            "recommended_strategy_type": strategy_type,
            "reason": strategy_reason,
            "parameter_owner": "deterministic_preview",
            "range_input_timeframe": "1d",
            "spacing_input_timeframe": "4h",
            "sizing_inputs": ["leverage", "profit_target", "risk_confirmation"],
            "automatic_execution": False,
        },
    }


def _unit_interval(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    previous: float | None = None
    values: list[float] = []
    for bar in bars[-(period + 1):]:
        high = _positive(bar.get("high"), "bar high")
        low = _positive(bar.get("low"), "bar low")
        close = _positive(bar.get("close"), "bar close")
        if previous is not None:
            values.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    if len(values) < period:
        raise ValueError("ATR history is insufficient")
    return sum(values[-period:]) / period


def _ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    rows = [result]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
        rows.append(result)
    return rows


def _macd(values: list[float]) -> dict[str, Any]:
    """Return the standard 12/26/9 MACD snapshot with an explicit warmup flag."""
    if len(values) < 35:
        return {
            "parameters": {"fast": 12, "slow": 26, "signal": 9},
            "line": None,
            "signal": None,
            "histogram": None,
            "warmup_complete": False,
        }
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)
    line_series = [left - right for left, right in zip(fast, slow)]
    signal_series = _ema_series(line_series, 9)
    line = line_series[-1]
    signal = signal_series[-1]
    return {
        "parameters": {"fast": 12, "slow": 26, "signal": 9},
        "line": round(line, 6),
        "signal": round(signal, 6),
        "histogram": round(line - signal, 6),
        "warmup_complete": True,
    }


def _positive(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be positive") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _bounded_number(value: Any, low: float, high: float, label: str) -> float:
    parsed = _positive(value, label)
    if parsed < low or parsed > high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}")
    return parsed


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI recommendation returned no JSON object")
        payload = json.loads(stripped[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("AI recommendation JSON must be an object")
    return payload
