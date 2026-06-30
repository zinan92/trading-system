from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_risk_rules
from services.execution_accounting import (
    BLOCKING_EXECUTION_STATUSES,
    SUCCESS_EXECUTION_STATUSES,
    counts_as_executed_record,
    execution_record_id,
    execution_record_status,
)
from services.journal_store import load_json, write_json


def _round(value: float | None, digits: int = 4):
    if value is None:
        return None
    return round(float(value), digits)


def entry_midpoint(entry_zone: str, fallback: float | None = None) -> float | None:
    text = str(entry_zone or "").strip()
    if not text:
        return fallback
    parts = text.replace("–", "-").split("-")
    try:
        values = [float(item.strip()) for item in parts if item.strip()]
    except ValueError:
        return fallback
    if not values:
        return fallback
    return sum(values) / len(values)


@dataclass(frozen=True)
class TradeQualityConfig:
    effective_leverage: float = 5.0
    min_target_equity_return_pct: float = 1.0
    min_reward_to_risk: float = 1.8
    cost_buffer_price_move_pct: float = 0.02
    daily_min_trade_samples: int = 3
    daily_target_trade_samples_low: int = 5
    daily_target_trade_samples_high: int = 10

    @classmethod
    def from_rules(cls, rules: dict | None = None) -> "TradeQualityConfig":
        rules = rules or load_risk_rules()
        default = rules.get("default", {}) or {}
        raw = default.get("trade_quality", {}) or {}
        return cls(
            effective_leverage=float(raw.get("effective_leverage", 5.0)),
            min_target_equity_return_pct=float(raw.get("min_target_equity_return_pct", 1.0)),
            min_reward_to_risk=float(raw.get("min_reward_to_risk", default.get("risk_reward_min", 1.8))),
            cost_buffer_price_move_pct=float(raw.get("cost_buffer_price_move_pct", 0.0)),
            daily_min_trade_samples=int(raw.get("daily_min_trade_samples", 3)),
            daily_target_trade_samples_low=int(raw.get("daily_target_trade_samples_low", 5)),
            daily_target_trade_samples_high=int(raw.get("daily_target_trade_samples_high", 10)),
        )

    def to_dict(self) -> dict:
        min_price_move = self.min_target_equity_return_pct / self.effective_leverage if self.effective_leverage else 0
        return {
            "effective_leverage": self.effective_leverage,
            "min_target_equity_return_pct": self.min_target_equity_return_pct,
            "min_target_price_move_pct": round(min_price_move + self.cost_buffer_price_move_pct, 4),
            "min_reward_to_risk": self.min_reward_to_risk,
            "cost_buffer_price_move_pct": self.cost_buffer_price_move_pct,
            "daily_min_trade_samples": self.daily_min_trade_samples,
            "daily_target_trade_samples_low": self.daily_target_trade_samples_low,
            "daily_target_trade_samples_high": self.daily_target_trade_samples_high,
        }


class TradeQualityGate:
    def __init__(self, rules: dict | None = None) -> None:
        self.config = TradeQualityConfig.from_rules(rules)

    def evaluate_ticket(self, ticket: dict, latest_price: float | None = None) -> dict:
        entry = entry_midpoint(str(ticket.get("entry_zone", "")), fallback=latest_price)
        targets = ticket.get("targets") or []
        target = float(targets[0]) if targets else None
        stop = float(ticket.get("stop_loss")) if ticket.get("stop_loss") is not None else None
        action = str(ticket.get("action", ""))
        side = "long" if "buy" in action else ("short" if "sell" in action else "")
        return self.evaluate_levels(entry, target, stop, side)

    def evaluate_levels(self, entry: float | None, target: float | None, stop: float | None, side: str) -> dict:
        reasons: list[str] = []
        cfg = self.config
        if entry is None or entry <= 0:
            reasons.append("entry price missing")
        if target is None:
            reasons.append("target missing")
        if stop is None:
            reasons.append("stop loss missing")
        if side not in {"long", "short"}:
            reasons.append("side missing")
        if reasons:
            return self._payload(False, reasons, entry, target, stop, side, None, None, None, None)

        assert entry is not None and target is not None and stop is not None
        if side == "long":
            target_move_pct = (target - entry) / entry * 100
            stop_move_pct = (entry - stop) / entry * 100
        else:
            target_move_pct = (entry - target) / entry * 100
            stop_move_pct = (stop - entry) / entry * 100

        target_equity_return_pct = target_move_pct * cfg.effective_leverage
        stop_equity_risk_pct = stop_move_pct * cfg.effective_leverage
        reward_to_risk = target_move_pct / stop_move_pct if stop_move_pct > 0 else None
        min_target_price_move_pct = cfg.min_target_equity_return_pct / cfg.effective_leverage + cfg.cost_buffer_price_move_pct

        if target_move_pct <= 0:
            reasons.append("target is not profitable from entry")
        if stop_move_pct <= 0:
            reasons.append("stop loss is not protective from entry")
        if target_move_pct < min_target_price_move_pct:
            reasons.append(
                f"target price move {target_move_pct:.2f}% below required {min_target_price_move_pct:.2f}% for {cfg.min_target_equity_return_pct:.2f}% equity target"
            )
        if target_equity_return_pct < cfg.min_target_equity_return_pct:
            reasons.append(
                f"target equity return {target_equity_return_pct:.2f}% below required {cfg.min_target_equity_return_pct:.2f}%"
            )
        if reward_to_risk is None or reward_to_risk < cfg.min_reward_to_risk:
            actual = "n/a" if reward_to_risk is None else f"{reward_to_risk:.2f}"
            reasons.append(f"reward/risk {actual} below required {cfg.min_reward_to_risk:.2f}")

        return self._payload(
            not reasons,
            reasons,
            entry,
            target,
            stop,
            side,
            target_move_pct,
            stop_move_pct,
            target_equity_return_pct,
            reward_to_risk,
            stop_equity_risk_pct=stop_equity_risk_pct,
        )

    def _payload(
        self,
        passes: bool,
        reasons: list[str],
        entry: float | None,
        target: float | None,
        stop: float | None,
        side: str,
        target_move_pct: float | None,
        stop_move_pct: float | None,
        target_equity_return_pct: float | None,
        reward_to_risk: float | None,
        *,
        stop_equity_risk_pct: float | None = None,
    ) -> dict:
        return {
            "passes": bool(passes),
            "reasons": reasons,
            "side": side,
            "entry_price": _round(entry, 4),
            "target_price": _round(target, 4),
            "stop_loss": _round(stop, 4),
            "target_price_move_pct": _round(target_move_pct, 4),
            "stop_price_move_pct": _round(stop_move_pct, 4),
            "target_equity_return_pct": _round(target_equity_return_pct, 4),
            "stop_equity_risk_pct": _round(stop_equity_risk_pct, 4),
            "reward_to_risk": _round(reward_to_risk, 4),
            "requirements": self.config.to_dict(),
        }


class DailyTradeSampler:
    def __init__(self, output_root: Path, rules: dict | None = None) -> None:
        self.output_root = output_root
        self.rules = rules or load_risk_rules()
        self.quality = TradeQualityGate(self.rules)
        self.config = self.quality.config
        self.risk_default = self.rules.get("default", {}) or {}

    def build(self, run_date: str, active_strategy_id: str = "") -> dict:
        strategy_root = self.output_root / "strategies"
        samples: list[dict] = []
        if strategy_root.exists():
            for root in sorted(path for path in strategy_root.iterdir() if path.is_dir()):
                samples.extend(self._strategy_samples(root.name, root, run_date, active_strategy_id))
        samples.extend(self._strategy_samples("global", self.output_root, run_date, active_strategy_id))
        summary = self._summary(samples)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._status(summary),
            "active_strategy_id": active_strategy_id,
            "sample_requirements": self.config.to_dict(),
            "summary": summary,
            "samples": samples,
        }
        write_json(self.output_root / "daily_trade_samples" / "current.json", [payload])
        write_json(self.output_root / "daily_trade_samples" / f"{run_date}.json", [payload])
        return payload

    def _strategy_samples(self, strategy_id: str, root: Path, run_date: str, active_strategy_id: str) -> list[dict]:
        signals = load_json(root / "signals" / f"{run_date}.json")
        tickets = load_json(root / "trade_tickets" / f"{run_date}.json")
        risk_blocks = load_json(root / "risk_blocks" / f"{run_date}.json")
        decisions = load_json(root / "journal_decisions" / f"{run_date}.json")
        pending = load_json(root / "journal_pending" / f"{run_date}.json")
        paper_orders = load_json(root / "paper_orders" / f"{run_date}.json")
        demo_orders = load_json(root / "demo_order_requests" / f"{run_date}.json")
        live_orders = load_json(root / "live_order_requests" / f"{run_date}.json")
        backtests = load_json(root / "backtests" / f"{run_date}.json")
        by_signal = {item.get("signal_id"): item for item in tickets if item.get("signal_id")}
        block_by_signal = {item.get("signal_id"): item for item in risk_blocks if item.get("signal_id")}
        backtest_by_signal = {item.get("signal_id"): item for item in backtests if item.get("signal_id")}
        decision_by_ticket = {item.get("ticket_id"): item for item in decisions if item.get("ticket_id")}
        pending_by_ticket = {item.get("ticket_id"): item for item in pending if item.get("ticket_id")}
        paper_by_ticket = {execution_record_id(item): item for item in paper_orders if execution_record_id(item)}
        demo_by_ticket = {execution_record_id(item): item for item in demo_orders if execution_record_id(item)}

        rows = []
        for index, signal in enumerate(signals, start=1):
            ticket = by_signal.get(signal.get("signal_id"), {})
            ticket_id = ticket.get("ticket_id", "")
            quality = ticket.get("trade_quality") or (self.quality.evaluate_ticket(ticket) if ticket else {})
            block = block_by_signal.get(signal.get("signal_id"), {})
            backtest = backtest_by_signal.get(signal.get("signal_id"), {})
            status = self._sample_status(signal, ticket, block, ticket_id, decision_by_ticket, pending_by_ticket, paper_by_ticket, demo_by_ticket)
            blocker_diagnostics = self._inferred_block_diagnostics(signal, ticket, block, backtest)
            block_reasons = [item["summary"] for item in blocker_diagnostics if item.get("summary")]
            rows.append(
                {
                    "sample_id": f"{run_date}_{strategy_id}_{index}",
                    "run_date": run_date,
                    "strategy_id": strategy_id,
                    "is_active_strategy": strategy_id == active_strategy_id,
                    "signal_id": signal.get("signal_id", ""),
                    "signal_generated_at": signal.get("generated_at") or signal.get("timestamp") or "",
                    "signal_expires_at": signal.get("expires_at", ""),
                    "decision_cursor": signal.get("generated_at") or signal.get("timestamp") or "",
                    "ticket_id": ticket_id,
                    "direction": signal.get("direction", ""),
                    "signal_status": signal.get("status", ""),
                    "signal_regime": signal.get("regime", ""),
                    "signal_strength": signal.get("strength", 0),
                    "signal_confidence": signal.get("confidence", 0),
                    "candidate": signal.get("direction") in {"long", "short"},
                    "ticket_created": bool(ticket),
                    "execution_status": status,
                    "execution_sample": self._is_executed_status(status),
                    "entry_zone": ticket.get("entry_zone", ""),
                    "stop_loss": ticket.get("stop_loss"),
                    "targets": ticket.get("targets", []),
                    "trade_quality": quality,
                    "backtest": self._compact_backtest(backtest),
                    "block": block,
                    "block_reasons": block_reasons,
                    "blocker_diagnostics": blocker_diagnostics,
                    "block_reason_codes": [item["code"] for item in blocker_diagnostics if item.get("code")],
                    "blocking_reason_count": sum(1 for item in blocker_diagnostics if item.get("blocking")),
                    "diagnostic_reason_count": sum(1 for item in blocker_diagnostics if not item.get("blocking")),
                    "review_required": bool(ticket or signal.get("direction") in {"long", "short"}),
                    "source_artifacts": signal.get("source_artifacts", []),
                }
            )
        rows.extend(
            self._execution_only_samples(
                strategy_id,
                run_date,
                active_strategy_id,
                rows,
                paper_orders=paper_orders,
                demo_orders=demo_orders,
                live_orders=live_orders,
                decisions=decisions,
            )
        )
        return rows

    def _execution_only_samples(
        self,
        strategy_id: str,
        run_date: str,
        active_strategy_id: str,
        existing_rows: list[dict],
        *,
        paper_orders: list[dict],
        demo_orders: list[dict],
        live_orders: list[dict],
        decisions: list[dict],
    ) -> list[dict]:
        seen_ticket_ids = {str(row.get("ticket_id", "")) for row in existing_rows if row.get("ticket_id")}
        rows: list[dict] = []
        groups = [
            ("paper", "paper_orders", paper_orders, True),
            ("demo", "demo_order_requests", demo_orders, False),
            ("live", "live_order_requests", live_orders, False),
            ("decision", "journal_decisions", decisions, True),
        ]
        for prefix, source_dir, items, legacy in groups:
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if not counts_as_executed_record(item, legacy_count_missing_status=legacy):
                    continue
                ticket_id = execution_record_id(item)
                if not ticket_id or ticket_id in seen_ticket_ids:
                    continue
                seen_ticket_ids.add(ticket_id)
                status = execution_record_status(item) or ("executed_paper" if prefix == "decision" else "filled")
                execution_status = status if prefix == "decision" else f"{prefix}_{status}"
                rows.append(
                    {
                        "sample_id": f"{run_date}_{strategy_id}_execution_{len(rows) + 1}",
                        "run_date": run_date,
                        "strategy_id": strategy_id,
                        "is_active_strategy": strategy_id == active_strategy_id,
                        "signal_id": item.get("signal_id", ""),
                        "signal_generated_at": item.get("generated_at") or item.get("timestamp") or "",
                        "signal_expires_at": item.get("expires_at", ""),
                        "decision_cursor": item.get("generated_at") or item.get("timestamp") or item.get("filled_at") or item.get("created_at") or "",
                        "ticket_id": ticket_id,
                        "direction": item.get("side") or item.get("direction") or "",
                        "signal_status": "execution_only",
                        "signal_regime": item.get("signal_regime", ""),
                        "signal_strength": 0,
                        "signal_confidence": 0,
                        "candidate": True,
                        "ticket_created": True,
                        "execution_status": execution_status,
                        "execution_sample": True,
                        "entry_zone": item.get("entry_zone", ""),
                        "stop_loss": item.get("stop_loss"),
                        "targets": item.get("targets", []),
                        "trade_quality": item.get("trade_quality", {}),
                        "backtest": {},
                        "block": {},
                        "block_reasons": [],
                        "blocker_diagnostics": [],
                        "block_reason_codes": [],
                        "blocking_reason_count": 0,
                        "diagnostic_reason_count": 0,
                        "review_required": True,
                        "source_artifacts": [f"{source_dir}/{run_date}.json"],
                    }
                )
        return rows

    def _sample_status(self, signal: dict, ticket: dict, block: dict, ticket_id: str, decisions: dict, pending: dict, paper_orders: dict, demo_orders: dict) -> str:
        if ticket_id and ticket_id in demo_orders:
            return f"demo_{execution_record_status(demo_orders[ticket_id]) or 'requested'}"
        if ticket_id and ticket_id in paper_orders:
            return f"paper_{execution_record_status(paper_orders[ticket_id]) or 'ordered'}"
        if ticket_id and ticket_id in decisions:
            return str(decisions[ticket_id].get("decision_status", "decided"))
        if ticket_id and ticket_id in pending:
            return "pending_review"
        if ticket:
            return "ticket_created"
        if block:
            return "blocked"
        if signal.get("direction") in {"long", "short"}:
            return "candidate_without_ticket"
        return "no_signal"

    def _is_executed_status(self, status: str) -> bool:
        text = str(status or "").lower()
        if text in {"executed", "executed_paper"}:
            return True
        for prefix in ("paper_", "demo_", "live_"):
            if text.startswith(prefix):
                raw_status = text[len(prefix):]
                if raw_status in BLOCKING_EXECUTION_STATUSES:
                    return False
                return raw_status in SUCCESS_EXECUTION_STATUSES
        return False

    def _compact_backtest(self, backtest: dict) -> dict:
        if not isinstance(backtest, dict) or not backtest:
            return {}
        keys = (
            "verdict",
            "sample_size",
            "setup_count",
            "win_rate",
            "profit_factor",
            "max_drawdown_pct",
            "adapter_name",
            "engine_version",
        )
        return {key: backtest.get(key) for key in keys if key in backtest}

    def _inferred_block_reasons(self, signal: dict, ticket: dict, block: dict, backtest: dict | None = None) -> list[str]:
        return [item["summary"] for item in self._inferred_block_diagnostics(signal, ticket, block, backtest) if item.get("summary")]

    def _inferred_block_diagnostics(self, signal: dict, ticket: dict, block: dict, backtest: dict | None = None) -> list[dict]:
        if ticket:
            return []
        diagnostics: list[dict] = []

        def add(code: str, label: str, summary: str, *, blocking: bool, evidence: dict | None = None) -> None:
            if not summary:
                return
            diagnostics.append(
                {
                    "code": code,
                    "label": label,
                    "summary": summary,
                    "blocking": bool(blocking),
                    "evidence": evidence or {},
                }
            )

        if block:
            reason_texts: list[str] = []
            for key in ("reason", "message", "block_reason"):
                value = block.get(key)
                if value:
                    reason_texts.append(str(value))
            quality = block.get("trade_quality") or {}
            reason_texts.extend(str(item) for item in quality.get("reasons", []) if item)
            for reason in reason_texts or ["explicit risk block artifact exists"]:
                code, label, blocking = self._diagnostic_code_for_text(reason, block=block)
                add(code, label, reason, blocking=blocking, evidence=block)
            return diagnostics
        if signal.get("direction") not in {"long", "short"}:
            add("no_actionable_direction", "No actionable direction / 无可执行方向", "no actionable direction", blocking=False)
            return diagnostics

        min_strength = float(self.risk_default.get("min_signal_strength", 0))
        min_confidence = float(self.risk_default.get("min_confidence", 0))
        strength = float(signal.get("strength") or 0)
        confidence = float(signal.get("confidence") or 0)
        if strength < min_strength:
            add(
                "signal_strength_below_minimum",
                "Signal strength too low / 信号强度不足",
                f"signal strength {strength:.0f} below minimum {min_strength:.0f}",
                blocking=True,
                evidence={"strength": strength, "min_signal_strength": min_strength},
            )
        if confidence < min_confidence:
            add(
                "signal_confidence_below_minimum",
                "Signal confidence too low / 信号置信度不足",
                f"signal confidence {confidence:.0f} below minimum {min_confidence:.0f}",
                blocking=True,
                evidence={"confidence": confidence, "min_confidence": min_confidence},
            )
        if isinstance(backtest, dict) and backtest:
            verdict = str(backtest.get("verdict", "")).lower()
            sample_size = backtest.get("sample_size")
            setup_count = backtest.get("setup_count")
            if verdict == "thin":
                add(
                    "backtest_thin_context",
                    "Thin backtest context / 回测样本薄",
                    f"thin backtest verdict sample_size={sample_size if sample_size is not None else 'unknown'} setup_count={setup_count if setup_count is not None else 'unknown'}",
                    blocking=False,
                    evidence={"verdict": verdict, "sample_size": sample_size, "setup_count": setup_count},
                )
            elif verdict == "blocked":
                add(
                    "backtest_blocked",
                    "Backtest blocked / 回测阻断",
                    f"blocked backtest verdict sample_size={sample_size if sample_size is not None else 'unknown'} setup_count={setup_count if setup_count is not None else 'unknown'}",
                    blocking=True,
                    evidence={"verdict": verdict, "sample_size": sample_size, "setup_count": setup_count},
                )
        if not diagnostics:
            add(
                "unexplained_candidate_without_ticket",
                "Unexplained ticket gap / 候选未出票待查",
                "candidate did not produce ticket; no explicit block artifact",
                blocking=True,
            )
        return diagnostics

    def _diagnostic_code_for_text(self, text: str, *, block: dict) -> tuple[str, str, bool]:
        lower = str(text or "").lower()
        if block.get("position_gate") or "position map" in lower:
            return "position_gate", "Position gate / 位置过滤", True
        if block.get("direction_bias") or "direction bias" in lower:
            return "direction_bias", "Direction bias filter / 方向过滤", True
        if block.get("data_quality") or any(token in lower for token in ("data", "feed", "stale", "ohlc", "provider")):
            return "data_blocked", "Data blocked / 数据阻塞", True
        if any(token in lower for token in ("reconciliation", "drift", "orphan", "broker", "exchange", "protective", "order missing", "demo blocked", "live blocked")):
            return "execution_safety", "Execution safety / 执行安全", True
        if "thin backtest" in lower:
            return "backtest_thin_context", "Thin backtest context / 回测样本薄", False
        if any(token in lower for token in ("blocked backtest", "backtest verdict", "sample_size=0", "setup_count=0")):
            return "backtest_blocked", "Backtest blocked / 回测阻断", True
        if any(token in lower for token in ("quality", "target", "reward/risk", "reward", "1%", "entry price", "stop loss", "stop is", "below required")):
            return "trade_quality", "Trade quality / 交易质量", True
        if block.get("portfolio_risk") or any(token in lower for token in ("risk", "cap", "budget", "drawdown", "daily loss", "loss stop", "leverage", "exposure", "portfolio")):
            return "risk_budget", "Risk budget / 风险预算", True
        if any(token in lower for token in ("strength", "confidence", "minimum")):
            return "signal_threshold", "Signal threshold / 信号阈值", True
        return "candidate_without_ticket", "Ticket gap / 有方向未出票", True

    def _summary(self, samples: list[dict]) -> dict:
        observation_count = len(samples)
        candidate_count = sum(1 for item in samples if item.get("candidate"))
        ticket_count = sum(1 for item in samples if item.get("ticket_created"))
        quality_pass_count = sum(1 for item in samples if (item.get("trade_quality") or {}).get("passes"))
        executed_count = sum(1 for item in samples if item.get("execution_sample"))
        active_samples = [item for item in samples if item.get("is_active_strategy")]
        no_signal_count = sum(1 for item in samples if item.get("execution_status") == "no_signal")
        blocked_count = sum(1 for item in samples if item.get("execution_status") == "blocked")
        candidate_without_ticket_count = sum(1 for item in samples if item.get("execution_status") == "candidate_without_ticket")
        pending_review_count = sum(1 for item in samples if item.get("execution_status") == "pending_review")
        paper_order_count = sum(1 for item in samples if str(item.get("execution_status", "")).startswith("paper_"))
        demo_order_count = sum(1 for item in samples if str(item.get("execution_status", "")).startswith("demo_"))
        live_order_count = sum(1 for item in samples if str(item.get("execution_status", "")).startswith("live_"))
        return {
            "total_samples": observation_count,
            "observation_count": observation_count,
            "candidate_count": candidate_count,
            "ticket_count": ticket_count,
            "quality_pass_count": quality_pass_count,
            "executed_count": executed_count,
            "executed_trade_sample_count": executed_count,
            "no_signal_count": no_signal_count,
            "blocked_count": blocked_count,
            "candidate_without_ticket_count": candidate_without_ticket_count,
            "pending_review_count": pending_review_count,
            "paper_order_count": paper_order_count,
            "demo_order_count": demo_order_count,
            "live_order_count": live_order_count,
            "active_strategy_samples": len(active_samples),
            "active_strategy_candidates": sum(1 for item in active_samples if item.get("candidate")),
            "active_strategy_executed": sum(1 for item in active_samples if item.get("execution_sample")),
            "below_minimum": executed_count < self.config.daily_min_trade_samples,
            "target_range_met": self.config.daily_target_trade_samples_low <= executed_count <= self.config.daily_target_trade_samples_high,
            "observation_target_range_met": self.config.daily_target_trade_samples_low <= observation_count <= self.config.daily_target_trade_samples_high,
            "target_range": [self.config.daily_target_trade_samples_low, self.config.daily_target_trade_samples_high],
            "minimum_executed_trades": self.config.daily_min_trade_samples,
            "funnel": {
                "observations": observation_count,
                "no_signal": no_signal_count,
                "directional_candidates": candidate_count,
                "blocked_before_ticket": blocked_count,
                "candidate_without_ticket": candidate_without_ticket_count,
                "tickets": ticket_count,
                "quality_passed_tickets": quality_pass_count,
                "pending_review": pending_review_count,
                "paper_orders": paper_order_count,
                "demo_orders": demo_order_count,
                "live_orders": live_order_count,
                "executed_or_requested": executed_count,
            },
        }

    def _status(self, summary: dict) -> str:
        executed = int(summary.get("executed_count") or 0)
        if executed < self.config.daily_min_trade_samples:
            return "below_minimum_executed_trades"
        if executed < self.config.daily_target_trade_samples_low:
            return "below_target_executed_trades"
        if summary.get("target_range_met"):
            return "target_met"
        return "above_target_executed_trades"
