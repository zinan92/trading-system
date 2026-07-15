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
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT
from services.dualtrack_config import dualtrack_config
from services.journal_store import write_json


PROMPT_VERSION = "strategy-recommendation-prompt-v2"
EVALUATION_SCHEMA = "strategy-ai-evaluation-v1"
TIMEFRAMES = ("1d", "4h", "1h", "15m")


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
            prompt = self._prompt(
                cycle_id,
                contexts=contexts,
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
            receipt["input"].update({"contexts": contexts, "prompt": prompt, "prompt_contract": prompt_contract})
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
                "schema_version": "strategy-recommendation-v1",
                "cycle_id": cycle_id,
                "created_at": created_at,
                "direction": direction,
                "style": style,
                "rationale": rationale,
                "key_levels": [float(value) for value in decision.get("key_levels") or []],
                "evidence_used": [str(value) for value in decision.get("evidence_used") or []],
                "signal": signal,
                "analysis": {
                    "timeframes": list(TIMEFRAMES),
                    "contexts": contexts,
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
        if len(bars) < 15:
            raise ValueError(f"strategy timeframe {timeframe} history is insufficient")
        closes = [_positive(bar.get("close"), f"{timeframe} close") for bar in bars]
        atr = _atr(bars)
        change = (closes[-1] / closes[0] - 1.0) if closes[0] else 0.0
        ema_fast = _ema(closes, min(20, len(closes)))
        ema_slow = _ema(closes, min(50, len(closes)))
        net_move = abs(closes[-1] - closes[0])
        path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
        efficiency = net_move / path if path else 0.0
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
            "recent_closes": [round(value, 6) for value in closes[-20:]],
            "last_completed_bar": {
                key: bars[-1].get(key) for key in ("timestamp", "open", "high", "low", "close", "volume")
            },
        }

    def _prompt(
        self,
        cycle_id: str,
        *,
        contexts: dict[str, Any],
        current_plan: dict[str, Any],
        account: dict[str, Any],
        review: dict[str, Any],
    ) -> str:
        return f"""你是黄金 paper trading 单一生产策略的市场评估员。你的输出只会创建 AI 提案，不会自动修改生产计划或下单。

周期: {cycle_id}
完整 D1、4H、1H、15m 可信行情与指标: {json.dumps(contexts, ensure_ascii=False, sort_keys=True)}
当前生产计划: {json.dumps(current_plan, ensure_ascii=False, sort_keys=True)}
账户状态: {json.dumps(account, ensure_ascii=False, sort_keys=True)}
上一周期复盘: {json.dumps(review, ensure_ascii=False, sort_keys=True)}

只输出 JSON 对象，字段必须为：
- direction: neutral、long 或 short
- style: steady 或 aggressive
- rationale: 直白中文，分别说明 D1、4H、1H 给出的证据以及为何选择该方向和风格
- key_levels: number[]，只列从输入行情中可解释的关键位
- ai_self_assessment: 1-10，只代表你认为本次推理材料是否充分，不是置信概率
- evidence_used: string[]，列出实际使用的输入，例如 D1、4H、1H、current_plan、review

约束：
1. 不得生成 range、网格间距、网格数量、每格资金或杠杆；这些由确定性风险引擎计算。
2. 不得输出概率或胜率，不得把 ai_self_assessment 称为 confidence。
3. 图表展示周期不是策略输入。只使用上面固定的 D1、4H、1H、15m 数据；15m 只用于短周期确认，不得改变 D1 Range 与 4H spacing 合同。
4. 材料冲突时必须写明冲突；信息不足时必须明确说明，禁止静默补全。
5. 只输出 JSON，不要 markdown。
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
        with tempfile.NamedTemporaryFile(prefix="strategy-recommendation-", suffix=".json", delete=False) as handle:
            result_path = Path(handle.name)
        try:
            args = [
                *shlex.split(command), "--ask-for-approval", "never", "exec",
                "--ignore-user-config", "--ephemeral", "--model", model,
                "--sandbox", "read-only", "--cd", str(ROOT),
                "--output-last-message", str(result_path), "-",
            ]
            env = dict(os.environ)
            path_parts = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", str(env.get("PATH") or "")]
            env["PATH"] = ":".join(part for part in path_parts if part)
            result = subprocess.run(
                args,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown recommendation error").strip()
                raise RuntimeError(f"strategy_recommendation_failed:{detail[-1000:]}")
            self._last_raw_model_response = result_path.read_text(encoding="utf-8")
            return _parse_json_object(self._last_raw_model_response)
        finally:
            result_path.unlink(missing_ok=True)


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
