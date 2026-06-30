from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.journal_store import load_json, write_json


FREQUENCY_GOVERNANCE_VERSION = "strategy-frequency-governance-v2"


class StrategyFrequencyGovernance:
    """Daily frequency governance for the multi-strategy portfolio.

    This does not change strategy config. It turns the sample funnel into a
    durable "what should we do about frequency?" artifact for morning/evening
    review.
    """

    def __init__(self, output_root: Path | None = None, rules: dict | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / str(config.get("output_root", "outputs"))
        self.rules = rules or load_risk_rules()
        trade_quality = (self.rules.get("default", {}) or {}).get("trade_quality", {}) or {}
        self.min_per_strategy = int(trade_quality.get("min_daily_executed_trades_per_strategy", 2))
        self.target_low = int(trade_quality.get("daily_target_trade_samples_low", 5))
        self.target_high = int(trade_quality.get("daily_target_trade_samples_high", 10))

    def build(self, run_date: str, *, daily_samples: dict | None = None, leaderboard: dict | None = None) -> dict:
        daily_samples = daily_samples or self._latest(self.output_root / "daily_trade_samples" / f"{run_date}.json")
        leaderboard = leaderboard or self._latest(self.output_root / "strategy_leaderboard" / "current.json")
        sample_rows = daily_samples.get("samples", []) if isinstance(daily_samples, dict) else []
        samples_by_strategy: dict[str, list[dict]] = {}
        for sample in sample_rows:
            sid = str(sample.get("strategy_id", ""))
            if sid and sid != "global":
                samples_by_strategy.setdefault(sid, []).append(sample)
        strategy_rows = leaderboard.get("strategies", []) if isinstance(leaderboard, dict) else []
        strategies = [self._strategy_payload(row, samples_by_strategy.get(str(row.get("strategy_id")), [])) for row in strategy_rows if isinstance(row, dict)]
        counts: dict[str, int] = {}
        for row in strategies:
            counts[row["stage"]] = counts.get(row["stage"], 0) + 1
        attribution_counts = self._portfolio_attribution_counts(strategies)
        portfolio_limiting_reason = self._portfolio_limiting_reason(attribution_counts)
        summary = {
            "portfolio_executed_count": sum(int(row.get("executed_trade_count") or 0) for row in strategies),
            "effective_strategy_count": counts.get("effective", 0),
            "below_min_strategy_count": sum(1 for row in strategies if int(row.get("executed_trade_count") or 0) < self.min_per_strategy),
            "stage_counts": counts,
            "attribution_counts": attribution_counts,
            "portfolio_limiting_reason": portfolio_limiting_reason,
            "portfolio_limiting_reason_label": self._reason_label(portfolio_limiting_reason),
            "portfolio_gap_to_target_low": max(0, self.target_low - sum(int(row.get("executed_trade_count") or 0) for row in strategies)),
            "target_daily_samples": [self.target_low, self.target_high],
            "min_daily_executed_trades_per_strategy": self.min_per_strategy,
        }
        payload = {
            "schema_version": FREQUENCY_GOVERNANCE_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._status(summary),
            "summary": summary,
            "strategies": strategies,
            "next_actions": self._next_actions(strategies, summary),
            "source_artifacts": {
                "daily_trade_samples": str(self.output_root / "daily_trade_samples" / f"{run_date}.json"),
                "strategy_leaderboard": str(self.output_root / "strategy_leaderboard" / "current.json"),
            },
        }
        write_json(self.output_root / "strategy_frequency" / "current.json", [payload])
        write_json(self.output_root / "strategy_frequency" / f"{run_date}.json", [payload])
        return payload

    def _strategy_payload(self, row: dict, samples: list[dict]) -> dict:
        sid = str(row.get("strategy_id", ""))
        daily = row.get("daily_execution", {}) if isinstance(row.get("daily_execution"), dict) else {}
        classification = row.get("classification", {}) if isinstance(row.get("classification"), dict) else {}
        executed = int(daily.get("executed_trade_count") or 0)
        signals = int(daily.get("signal_count") or len(samples) or 0)
        candidates = sum(1 for sample in samples if sample.get("candidate"))
        tickets = int(daily.get("ticket_count") or sum(1 for sample in samples if sample.get("ticket_created")) or 0)
        statuses = [str(sample.get("execution_status", "")) for sample in samples]
        execution_blocker = self._execution_blocker(sid)
        stage = self._stage(executed, signals, candidates, tickets, statuses, execution_blocker)
        attribution = self._attribution(
            stage=stage,
            executed=executed,
            signals=signals,
            candidates=candidates,
            tickets=tickets,
            statuses=statuses,
            samples=samples,
            execution_blocker=execution_blocker,
        )
        return {
            "strategy_id": sid,
            "classification": classification,
            "engine": row.get("engine", ""),
            "timeframe": row.get("timeframe", ""),
            "frequency_bucket": classification.get("frequency_bucket", ""),
            "role": classification.get("role", ""),
            "stage": stage,
            "stage_label": self._stage_label(stage),
            "reason": attribution["summary"],
            "primary_reason": attribution["primary_reason"],
            "limiting_reason": attribution["limiting_reason"],
            "attribution": attribution,
            "executed_trade_count": executed,
            "min_daily_executed_trades": self.min_per_strategy,
            "signal_count": signals,
            "candidate_count": candidates,
            "ticket_count": tickets,
            "sample_statuses": statuses,
            "execution_blocker": execution_blocker,
            "recommendation": self._recommendation(stage, classification, executed, attribution),
        }

    def _stage(self, executed: int, signals: int, candidates: int, tickets: int, statuses: list[str], execution_blocker: dict) -> str:
        if execution_blocker.get("blocked"):
            return "execution_blocked"
        if executed >= self.min_per_strategy:
            return "effective"
        if executed > 0:
            return "low_volume"
        if any(status.startswith(("demo_blocked", "live_blocked")) for status in statuses):
            return "execution_blocked"
        if any(status == "pending_review" for status in statuses):
            return "pending_review"
        if tickets > 0:
            return "ticket_no_execution"
        if candidates > 0:
            return "candidate_without_ticket"
        if signals > 0:
            return "no_signal"
        return "missing_artifacts"

    def _execution_blocker(self, strategy_id: str) -> dict:
        namespace = self.output_root / "strategies" / strategy_id
        reconciliation = self._latest(namespace / "live_reconciliation" / "current.json")
        if reconciliation.get("suspected_naked_position"):
            return {
                "blocked": True,
                "status": "naked_position_suspected",
                "reason": "suspected naked position: " + str(reconciliation.get("escalation_action") or reconciliation.get("reason_code") or ""),
                "artifact": str(namespace / "live_reconciliation" / "current.json"),
            }
        if reconciliation.get("confirmation_status") == "cannot_confirm":
            return {
                "blocked": True,
                "status": "reconciliation_unknown",
                "reason": f"Binance demo reconciliation cannot confirm venue state: {reconciliation.get('error')}",
                "artifact": str(namespace / "live_reconciliation" / "current.json"),
            }
        if reconciliation.get("error"):
            return {
                "blocked": True,
                "status": "reconciliation_error",
                "reason": f"Binance demo reconciliation failed: {reconciliation.get('error')}",
                "artifact": str(namespace / "live_reconciliation" / "current.json"),
            }
        if int(reconciliation.get("drift_count") or 0) > 0:
            reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in reconciliation.get("drifts", [])})
            return {
                "blocked": True,
                "status": "reconciliation_drift",
                "reason": "; ".join(reasons) if reasons else "reconciliation drift",
                "artifact": str(namespace / "live_reconciliation" / "current.json"),
            }
        return {"blocked": False, "status": "clear", "reason": ""}

    def _attribution(
        self,
        *,
        stage: str,
        executed: int,
        signals: int,
        candidates: int,
        tickets: int,
        statuses: list[str],
        samples: list[dict],
        execution_blocker: dict,
    ) -> dict:
        counts: dict[str, int] = {}
        evidence: list[dict] = []

        def add(reason: str, sample: dict | None = None, detail: str = "") -> None:
            counts[reason] = counts.get(reason, 0) + 1
            item = {
                "reason": reason,
                "reason_label": self._reason_label(reason),
                "detail": detail,
            }
            if sample:
                item.update(
                    {
                        "sample_id": sample.get("sample_id", ""),
                        "signal_id": sample.get("signal_id", ""),
                        "signal_generated_at": sample.get("signal_generated_at", ""),
                        "decision_cursor": sample.get("decision_cursor", ""),
                        "ticket_id": sample.get("ticket_id", ""),
                        "execution_status": sample.get("execution_status", ""),
                    }
                )
            if len(evidence) < 8:
                evidence.append(item)
                return
            if reason == "market_no_signal":
                return
            replace_index = next((idx for idx, existing in enumerate(evidence) if existing.get("reason") == "market_no_signal"), None)
            if replace_index is not None:
                evidence[replace_index] = item

        if execution_blocker.get("blocked"):
            add("execution_blocker", None, str(execution_blocker.get("reason") or execution_blocker.get("status") or "execution blocked"))

        for sample in samples:
            if not isinstance(sample, dict):
                continue
            status = str(sample.get("execution_status", "")).lower()
            quality = sample.get("trade_quality") if isinstance(sample.get("trade_quality"), dict) else {}
            block = sample.get("block") if isinstance(sample.get("block"), dict) else {}
            diagnostics = [item for item in sample.get("blocker_diagnostics", []) if isinstance(item, dict)]
            block_reasons = [str(item) for item in sample.get("block_reasons", []) if item]
            detail = "; ".join(block_reasons[:2])
            if sample.get("execution_sample"):
                add("executed", sample, status or "executed")
                continue
            if status.startswith(("demo_blocked", "live_blocked")) or status in {"demo_rejected", "live_rejected"}:
                add("execution_blocker", sample, status)
                continue
            if status == "pending_review":
                add("pending_review", sample, "waiting for manual review")
                continue
            if quality and quality.get("passes") is False:
                reasons = [str(item) for item in quality.get("reasons", []) if item]
                add("quality_gate_failed", sample, "; ".join(reasons[:2]) or "trade quality gate failed")
                continue
            if diagnostics and status == "candidate_without_ticket":
                blocking = [item for item in diagnostics if item.get("blocking")]
                chosen = (blocking or diagnostics)[0]
                add(self._reason_from_diagnostic(chosen), sample, str(chosen.get("summary") or chosen.get("label") or "candidate diagnostic"))
                for extra in diagnostics[1:4]:
                    add(self._reason_from_diagnostic(extra), sample, str(extra.get("summary") or extra.get("label") or "candidate diagnostic"))
                continue
            if block:
                add(self._classify_reason_text(detail or str(block)), sample, detail or "risk block artifact exists")
                continue
            if status == "blocked":
                add(self._classify_reason_text(detail), sample, detail or "blocked before ticket")
                continue
            if status == "no_signal":
                add("market_no_signal", sample, "strategy output watch/no-trade")
                continue
            if sample.get("ticket_created"):
                add("ticket_no_execution", sample, status or "ticket created but not executed")
                continue
            if sample.get("candidate"):
                add(self._classify_reason_text(detail), sample, detail or "candidate did not produce ticket")

        if not counts:
            if signals <= 0:
                add("missing_artifacts", None, "no signal sample artifact found")
            elif candidates <= 0:
                add("market_no_signal", None, "signals existed but none were directional")
            elif tickets <= 0:
                add("candidate_without_ticket", None, "directional candidates did not become tickets")

        primary = self._primary_reason(counts, stage)
        limiting = self._limiting_reason(counts, primary)
        return {
            "schema_version": FREQUENCY_GOVERNANCE_VERSION,
            "primary_reason": primary,
            "primary_reason_label": self._reason_label(primary),
            "limiting_reason": limiting,
            "limiting_reason_label": self._reason_label(limiting),
            "reason_counts": counts,
            "evidence": evidence,
            "summary": self._attribution_summary(primary, limiting, counts, executed),
            "frequency_is_diagnostic_not_sla": True,
        }

    def _classify_reason_text(self, text: str) -> str:
        lower = str(text or "").lower()
        if any(token in lower for token in ("reconciliation", "drift", "orphan", "broker", "exchange", "protective", "order missing", "demo blocked", "live blocked")):
            return "execution_blocker"
        if any(token in lower for token in ("data", "feed", "stale", "bar", "ohlc", "provider")):
            return "data_blocked"
        if "thin backtest" in lower:
            return "backtest_thin_context"
        if any(token in lower for token in ("blocked backtest", "backtest verdict", "sample_size=0", "setup_count=0")):
            return "backtest_blocked"
        if any(token in lower for token in ("quality", "target", "reward/risk", "reward", "1%", "entry price", "stop loss", "stop is", "below required")):
            return "quality_gate_failed"
        if any(token in lower for token in ("risk", "cap", "budget", "drawdown", "daily loss", "loss stop", "leverage", "exposure", "portfolio")):
            return "risk_budget_used"
        if any(token in lower for token in ("strength", "confidence", "minimum")):
            return "signal_threshold_blocked"
        return "candidate_without_ticket"

    def _reason_from_diagnostic(self, diagnostic: dict) -> str:
        code = str(diagnostic.get("code") or "")
        mapping = {
            "data_blocked": "data_blocked",
            "execution_safety": "execution_blocker",
            "trade_quality": "quality_gate_failed",
            "risk_budget": "risk_budget_used",
            "signal_threshold": "signal_threshold_blocked",
            "signal_strength_below_minimum": "signal_threshold_blocked",
            "signal_confidence_below_minimum": "signal_threshold_blocked",
            "position_gate": "position_or_bias_filter",
            "direction_bias": "position_or_bias_filter",
            "backtest_thin_context": "backtest_thin_context",
            "backtest_blocked": "backtest_blocked",
            "unexplained_candidate_without_ticket": "candidate_without_ticket",
        }
        return mapping.get(code, self._classify_reason_text(str(diagnostic.get("summary") or diagnostic.get("label") or "")))

    def _primary_reason(self, counts: dict[str, int], stage: str) -> str:
        if stage == "effective":
            return "executed"
        if stage == "execution_blocked":
            return "execution_blocker"
        if stage == "pending_review":
            return "pending_review"
        if stage == "candidate_without_ticket":
            return self._top_reason(
                counts,
                ["execution_blocker", "data_blocked", "risk_budget_used", "quality_gate_failed", "signal_threshold_blocked", "position_or_bias_filter", "backtest_blocked", "candidate_without_ticket"],
            ) or "candidate_without_ticket"
        if stage == "no_signal":
            return "market_no_signal"
        if stage == "missing_artifacts":
            return "missing_artifacts"
        priority = [
            "execution_blocker",
            "data_blocked",
            "backtest_blocked",
            "risk_budget_used",
            "quality_gate_failed",
            "pending_review",
            "ticket_no_execution",
            "signal_threshold_blocked",
            "position_or_bias_filter",
            "candidate_without_ticket",
            "missing_artifacts",
        ]
        return self._top_reason(counts, priority) or stage

    def _limiting_reason(self, counts: dict[str, int], primary: str) -> str:
        non_success = {key: value for key, value in counts.items() if key != "executed" and value > 0}
        priority = [
            "execution_blocker",
            "data_blocked",
            "backtest_blocked",
            "risk_budget_used",
            "quality_gate_failed",
            "pending_review",
            "ticket_no_execution",
            "signal_threshold_blocked",
            "position_or_bias_filter",
            "candidate_without_ticket",
            "missing_artifacts",
        ]
        return self._top_reason(non_success, priority) or primary

    def _top_reason(self, counts: dict[str, int], priority: list[str]) -> str:
        for reason in priority:
            value = int(counts.get(reason) or 0)
            if value > 0:
                return reason
        return ""

    def _attribution_summary(self, primary: str, limiting: str, counts: dict[str, int], executed: int) -> str:
        if limiting and limiting != primary:
            return f"{executed} executed; limiting factor: {self._reason_label(limiting)} ({counts.get(limiting, 0)})"
        if primary == "executed":
            return f"{executed} executed trades"
        count = int(counts.get(primary) or 0)
        if primary == "candidate_without_ticket" and count == 0:
            count = sum(int(value or 0) for value in counts.values())
        return f"{self._reason_label(primary)} ({count})"

    def _reason_label(self, reason: str) -> str:
        labels = {
            "executed": "已成交",
            "execution_blocker": "执行阻塞",
            "data_blocked": "数据阻塞",
            "backtest_blocked": "回测阻断",
            "backtest_thin_context": "回测样本薄（不挡paper）",
            "risk_budget_used": "风险预算/风控拦截",
            "quality_gate_failed": "质量闸未通过",
            "pending_review": "待人工审批",
            "ticket_no_execution": "有票未执行",
            "signal_threshold_blocked": "信号强度/置信度不足",
            "position_or_bias_filter": "位置/方向过滤",
            "candidate_without_ticket": "有方向信号但未出票",
            "market_no_signal": "市场无合格信号",
            "missing_artifacts": "缺少产物",
        }
        return labels.get(reason, reason or "unknown")

    def _portfolio_attribution_counts(self, strategies: list[dict]) -> dict:
        counts: dict[str, int] = {}
        for row in strategies:
            attribution = row.get("attribution", {}) if isinstance(row, dict) else {}
            reason_counts = attribution.get("reason_counts", {}) if isinstance(attribution, dict) else {}
            for reason, value in reason_counts.items():
                counts[str(reason)] = counts.get(str(reason), 0) + int(value or 0)
        return counts

    def _portfolio_limiting_reason(self, counts: dict[str, int]) -> str:
        reason = self._limiting_reason(counts, "")
        if reason:
            return reason
        if int(counts.get("candidate_without_ticket") or 0) > 0 or int(counts.get("backtest_thin_context") or 0) > 0:
            return "candidate_without_ticket"
        if int(counts.get("market_no_signal") or 0) > 0:
            return "market_no_signal"
        return "executed"

    def _recommendation(self, stage: str, classification: dict, executed: int, attribution: dict | None = None) -> dict:
        family = str(classification.get("family", ""))
        role = str(classification.get("role", ""))
        bucket = str(classification.get("frequency_bucket", ""))
        limiting_reason = str((attribution or {}).get("limiting_reason") or "")
        if stage == "effective":
            if limiting_reason == "risk_budget_used":
                return {"action": "keep_running_risk_budget_capped", "reason": "strategy met minimum, but additional setups were stopped by risk budget"}
            return {"action": "keep_running", "reason": "strategy met today's per-strategy sample minimum"}
        if stage == "execution_blocked":
            return {"action": "resolve_execution_blocker", "reason": "execution lifecycle must be fixed before frequency tuning"}
        if limiting_reason == "quality_gate_failed":
            return {"action": "inspect_trade_quality_gate", "reason": "signals or tickets failed the 1% target / reward-risk quality filter"}
        if limiting_reason == "risk_budget_used":
            return {"action": "inspect_risk_budget", "reason": "risk budget or loss limits, not strategy silence, constrained trade count"}
        if limiting_reason == "data_blocked":
            return {"action": "repair_data_feed", "reason": "data feed or freshness issue blocked valid frequency evaluation"}
        if limiting_reason == "backtest_blocked":
            return {"action": "inspect_backtest_evidence", "reason": "candidate fired, but backtest evidence blocked ticketing; keep out of demo/live until reviewed"}
        if limiting_reason == "backtest_thin_context":
            return {"action": "keep_collecting_paper_samples", "reason": "backtest evidence is thin, but it should not block paper/shadow sample collection"}
        if limiting_reason == "position_or_bias_filter":
            return {"action": "inspect_position_or_direction_filter", "reason": "strategy direction was filtered by position map or market-direction bias"}
        if stage == "low_volume":
            return {"action": "keep_running_collect_more", "reason": f"strategy produced {executed} sample but is below the per-strategy minimum"}
        if family == "chan" and bucket in {"low", "very_low"}:
            return {"action": "keep_as_selective_observer", "reason": "chan structure strategies are expected to be selective; do not force daily volume"}
        if role == "sample_volume_strategy" and stage == "no_signal":
            return {
                "action": "review_market_regime_and_sampling_cadence",
                "reason": "sample-volume strategy did not trigger; check quiet-market regime and runner cadence before changing thresholds",
            }
        if stage in {"candidate_without_ticket", "ticket_no_execution", "pending_review"}:
            return {"action": "inspect_funnel_blocker", "reason": "strategy found a setup but did not produce an executed sample"}
        return {"action": "monitor", "reason": "collect another cycle before changing parameters"}

    def _next_actions(self, strategies: list[dict], summary: dict) -> list[dict]:
        actions = []
        blocked = [row for row in strategies if row["stage"] == "execution_blocked"]
        if blocked:
            actions.append({
                "priority": 1,
                "action": "resolve_execution_blockers",
                "strategy_ids": [row["strategy_id"] for row in blocked],
                "reason": "blocked active/demo strategies cannot contribute valid samples",
            })
        volume_candidates = [
            row for row in strategies
            if row["recommendation"]["action"] == "review_market_regime_and_sampling_cadence"
        ]
        if volume_candidates:
            actions.append({
                "priority": 2,
                "action": "review_market_regime_and_sampling_cadence",
                "strategy_ids": [row["strategy_id"] for row in volume_candidates],
                "reason": "sample-volume strategies did not trigger; verify market regime and runner cadence before loosening thresholds",
            })
        if int(summary.get("portfolio_executed_count") or 0) < self.target_low:
            actions.append({
                "priority": 3,
                "action": "add_or_tune_uncorrelated_paper_strategies",
                "reason": f"portfolio sample count is below target; primary limiter is {summary.get('portfolio_limiting_reason_label')}",
            })
        return actions

    def _status(self, summary: dict) -> str:
        executed = int(summary.get("portfolio_executed_count") or 0)
        if executed < self.target_low:
            return "below_portfolio_sample_target"
        if executed <= self.target_high:
            return "within_portfolio_sample_target"
        return "above_portfolio_sample_target"

    def _stage_label(self, stage: str) -> str:
        labels = {
            "effective": "达标",
            "low_volume": "成交不足",
            "execution_blocked": "执行阻塞",
            "pending_review": "待人工审批",
            "ticket_no_execution": "有票未执行",
            "candidate_without_ticket": "有信号无出票",
            "no_signal": "无交易信号",
            "missing_artifacts": "缺少产物",
        }
        return labels.get(stage, stage)

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        if not rows:
            return {}
        item = rows[-1]
        return item if isinstance(item, dict) else {}


def build_strategy_frequency_governance(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyFrequencyGovernance(output_root).build(run_date)
