from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_strategy_config
from services.journal_store import load_json, write_json


class StrategyReviewer:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def build(self, run_date: str) -> dict:
        strategy_snapshot = self.build_strategy_snapshot(run_date)
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        manifest = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        paper_orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        performance = (load_json(self.output_root / "performance" / f"{run_date}.json") or [{}])[-1]
        review = {
            "run_date": run_date,
            "summary": self._summary(signals, backtests, decisions, paper_orders, open_trades, closed_trades, risk_blocks, manifest, performance),
            "observations": self._observations(signals, backtests, open_trades, closed_trades, risk_blocks, manifest, performance),
            "suggestions": self._suggestions(signals, backtests, open_trades, closed_trades, risk_blocks, manifest, performance),
            "metrics": self._metrics(signals, backtests, decisions, paper_orders, open_trades, closed_trades, risk_blocks, manifest, performance),
            "strategy_snapshot": strategy_snapshot,
        }
        write_json(self.output_root / "strategy_reviews" / f"{run_date}.json", [review])
        self.build_cumulative_ledger(run_date)
        return review

    def build_cumulative_ledger(self, run_date: str) -> dict:
        reviews = self._load_strategy_reviews()
        metrics_rows = [item.get("metrics", {}) for item in reviews]
        closed_trades = sum(int(item.get("closed_trade_count", 0)) for item in metrics_rows)
        realized_pnl = round(sum(float(item.get("closed_realized_pnl", 0)) for item in metrics_rows), 4)
        executed_paper = sum(int(item.get("executed_paper_count", 0)) for item in metrics_rows)
        paper_orders = sum(int(item.get("paper_order_count", 0)) for item in metrics_rows)
        risk_blocks = sum(int(item.get("risk_block_count", 0)) for item in metrics_rows)
        avg_strength = round(sum(float(item.get("signal_strength", 0)) for item in metrics_rows) / len(metrics_rows), 2) if metrics_rows else 0
        verdict_counts: dict[str, int] = {}
        regime_counts: dict[str, int] = {}
        for item in metrics_rows:
            verdict = str(item.get("backtest_verdict", "n/a"))
            regime = str(item.get("signal_regime", "no_signal"))
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
        recent_suggestions = []
        for review in reviews[-5:]:
            for suggestion in review.get("suggestions", []):
                if suggestion not in recent_suggestions:
                    recent_suggestions.append(suggestion)
        ledger = {
            "run_date": run_date,
            "strategy_id": (reviews[-1].get("strategy_snapshot") or {}).get("strategy_id", "gold_5m_v1") if reviews else "gold_5m_v1",
            "strategy_config_hash": (reviews[-1].get("strategy_snapshot") or {}).get("config_hash", "") if reviews else "",
            "review_days": len(reviews),
            "executed_paper_count": executed_paper,
            "paper_order_count": paper_orders,
            "closed_trade_count": closed_trades,
            "closed_realized_pnl": realized_pnl,
            "risk_block_count": risk_blocks,
            "avg_signal_strength": avg_strength,
            "backtest_verdict_counts": verdict_counts,
            "regime_counts": regime_counts,
            "recent_suggestions": recent_suggestions[:8],
            "learning_state": self._learning_state(reviews, realized_pnl, closed_trades, risk_blocks),
        }
        write_json(self.output_root / "learning_ledger" / "current.json", [ledger])
        write_json(self.output_root / "learning_ledger" / f"{run_date}.json", [ledger])
        self.build_strategy_change_proposal(run_date, ledger)
        return ledger

    def build_strategy_change_proposal(self, run_date: str, ledger: dict | None = None) -> dict:
        ledger = ledger or (load_json(self.output_root / "learning_ledger" / "current.json") or [{}])[-1]
        closed_trades = int(ledger.get("closed_trade_count", 0))
        realized_pnl = float(ledger.get("closed_realized_pnl", 0))
        review_days = int(ledger.get("review_days", 0))
        risk_blocks = int(ledger.get("risk_block_count", 0))
        state = str(ledger.get("learning_state", "no_review_history"))
        if closed_trades < 20:
            proposal = {
                "run_date": run_date,
                "status": "hold_parameters",
                "reason": "Need at least 20 closed paper trades before changing strategy parameters.",
                "proposed_changes": [],
                "guardrails": ["Keep MA windows, signal thresholds, stop/target, and risk caps unchanged.", "Continue collecting 5m broker-quality paper trade evidence."],
                "evidence": ledger,
                "strategy_config_hash": ledger.get("strategy_config_hash", ""),
            }
        elif realized_pnl < 0:
            proposal = {
                "run_date": run_date,
                "status": "review_required",
                "reason": "Closed paper trade evidence is negative; review losing regimes before parameter changes.",
                "proposed_changes": [],
                "guardrails": ["Do not loosen entries after negative realized paper PnL.", "Segment losses by regime and exit reason first."],
                "evidence": ledger,
                "strategy_config_hash": ledger.get("strategy_config_hash", ""),
            }
        elif risk_blocks > max(3, review_days // 2):
            proposal = {
                "run_date": run_date,
                "status": "review_risk_gate",
                "reason": "Risk gate blocks candidates frequently; inspect false blocks before changing strategy thresholds.",
                "proposed_changes": [],
                "guardrails": ["Keep daily loss cap unchanged until blocked candidates are manually reviewed."],
                "evidence": ledger,
                "strategy_config_hash": ledger.get("strategy_config_hash", ""),
            }
        else:
            proposal = {
                "run_date": run_date,
                "status": "eligible_for_small_experiment",
                "reason": f"Learning state is {state}; sufficient closed paper trades and non-negative realized PnL.",
                "proposed_changes": [
                    {"field": "position_size_pct", "change": "keep_or_increase_by_max_10pct_in_paper_only", "requires_manual_approval": True},
                    {"field": "entry_thresholds", "change": "test_one_small_variant_in_shadow_backtest_only", "requires_manual_approval": True},
                ],
                "guardrails": ["Paper-only experiment first.", "Re-run local 5m backtest before changing live config.", "Stop experiment after daily loss cap or three consecutive losing closes."],
                "evidence": ledger,
                "strategy_config_hash": ledger.get("strategy_config_hash", ""),
            }
        write_json(self.output_root / "strategy_change_proposals" / "current.json", [proposal])
        write_json(self.output_root / "strategy_change_proposals" / f"{run_date}.json", [proposal])
        return proposal

    def build_strategy_snapshot(self, run_date: str) -> dict:
        config = load_strategy_config()
        strategy_id = "gold_5m_v1"
        strategy = config.get(strategy_id, {})
        canonical = json.dumps(strategy, sort_keys=True, separators=(",", ":"))
        config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        signal = strategy.get("signal", {})
        backtest = strategy.get("backtest", {})
        snapshot = {
            "run_date": run_date,
            "strategy_id": strategy_id,
            "config_hash": config_hash,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source_path": "configs/strategy.yaml",
            "parameters": {
                "ma_short_bars": signal.get("ma_short_bars"),
                "ma_long_bars": signal.get("ma_long_bars"),
                "long_strength_min": signal.get("long_strength_min"),
                "long_confidence_min": signal.get("long_confidence_min"),
                "event_block_below": signal.get("event_block_below"),
                "stop_pct": backtest.get("stop_pct"),
                "target_pct": backtest.get("target_pct"),
                "max_hold_bars": backtest.get("max_hold_bars"),
            },
            "config": strategy,
        }
        write_json(self.output_root / "strategy_snapshots" / "current.json", [snapshot])
        write_json(self.output_root / "strategy_snapshots" / f"{run_date}.json", [snapshot])
        return snapshot

    def _summary(
        self,
        signals: list[dict],
        backtests: list[dict],
        decisions: list[dict],
        paper_orders: list[dict],
        open_trades: list[dict],
        closed_trades: list[dict],
        risk_blocks: list[dict],
        manifest: list[dict],
        performance: dict | None = None,
    ) -> str:
        signal = self._gold_signal(signals)
        backtest = self._gold_backtest(backtests, signal)
        gold_manifest = self._gold_manifest(manifest)
        realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
        perf = (performance or {}).get("summary", {})
        return (
            f"{signal.get('regime', 'no_signal')} / {signal.get('direction', 'watch')} with "
            f"strength {signal.get('strength', 0)}; backtest {backtest.get('verdict', 'n/a')} "
            f"on {backtest.get('sample_size', 0)} samples; {len(open_trades)} open, "
            f"{len(closed_trades)} closed, realized PnL {realized:.4f}; "
            f"net marked {perf.get('net_pnl_marked', realized):.4f}; "
            f"{gold_manifest.get('missing_bars', 0)} missing 5m gaps."
        )

    def _observations(
        self,
        signals: list[dict],
        backtests: list[dict],
        open_trades: list[dict],
        closed_trades: list[dict],
        risk_blocks: list[dict],
        manifest: list[dict],
        performance: dict | None = None,
    ) -> list[str]:
        observations = []
        signal = self._gold_signal(signals)
        backtest = self._gold_backtest(backtests, signal)
        gold_manifest = self._gold_manifest(manifest)
        if signal:
            observations.append(f"Signal regime is {signal.get('regime')} with direction {signal.get('direction')} and strength {signal.get('strength')}.")
        if backtest:
            observations.append(f"Local 5m backtest verdict is {backtest.get('verdict')} with sample size {backtest.get('sample_size')}.")
        if open_trades:
            observations.append(f"{len(open_trades)} paper trade(s) remain open and need mark-to-market review before adding exposure.")
        perf = (performance or {}).get("summary", {})
        if perf:
            observations.append(f"Paper performance is marked at net PnL {perf.get('net_pnl_marked')} with open unrealized R {perf.get('open_unrealized_r')}.")
        if closed_trades:
            realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
            observations.append(f"{len(closed_trades)} trade(s) closed today with realized PnL {realized:.4f}.")
        if risk_blocks:
            observations.append(f"Risk system blocked {len(risk_blocks)} candidate(s); latest reason: {risk_blocks[-1].get('reason')}.")
        if gold_manifest.get("missing_bars", 0):
            observations.append(f"Data quality warning: {gold_manifest.get('missing_bars')} missing 5m gaps in clean GOLD bars.")
        return observations or ["No material strategy observations generated today."]

    def _suggestions(
        self,
        signals: list[dict],
        backtests: list[dict],
        open_trades: list[dict],
        closed_trades: list[dict],
        risk_blocks: list[dict],
        manifest: list[dict],
        performance: dict | None = None,
    ) -> list[str]:
        suggestions = []
        signal = self._gold_signal(signals)
        backtest = self._gold_backtest(backtests, signal)
        gold_manifest = self._gold_manifest(manifest)
        if gold_manifest.get("missing_bars", 0) > 100:
            suggestions.append("Prioritize importing broker/MT5 XAUUSD 5m history to reduce missing-bar gaps before trusting parameter changes.")
        if backtest.get("verdict") in {"thin", "no_trade"}:
            suggestions.append("Do not loosen entry thresholds yet; collect more clean 5m samples and wait for a valid long/short candidate.")
        if backtest.get("verdict") == "supportive" and signal.get("direction") in {"long", "short"}:
            suggestions.append("Allow paper execution only if portfolio daily risk budget remains available and event score stays above the block threshold.")
        if open_trades:
            suggestions.append("Review open paper trades against stop/target before approving additional exposure.")
        perf = (performance or {}).get("summary", {})
        if float(perf.get("open_unrealized_r", 0) or 0) < -0.5:
            suggestions.append("Open paper exposure is below -0.5R; do not add another paper position until stop distance and invalidation are reviewed.")
        if closed_trades:
            realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
            if realized < 0:
                suggestions.append("After a losing close, compare exit reason with signal regime before changing stop/target parameters.")
            elif realized > 0:
                suggestions.append("Preserve current stop/target settings until at least 20 comparable closed paper trades are available.")
        if risk_blocks:
            suggestions.append("Risk block is working; keep daily loss cap unchanged unless repeated false blocks appear in the ledger.")
        return suggestions or ["Keep strategy parameters unchanged; no evidence-backed adjustment today."]

    def _metrics(
        self,
        signals: list[dict],
        backtests: list[dict],
        decisions: list[dict],
        paper_orders: list[dict],
        open_trades: list[dict],
        closed_trades: list[dict],
        risk_blocks: list[dict],
        manifest: list[dict],
        performance: dict | None = None,
    ) -> dict:
        signal = self._gold_signal(signals)
        backtest = self._gold_backtest(backtests, signal)
        gold_manifest = self._gold_manifest(manifest)
        perf = (performance or {}).get("summary", {})
        return {
            "signal_strength": signal.get("strength", 0),
            "signal_confidence": signal.get("confidence", 0),
            "signal_direction": signal.get("direction", "watch"),
            "signal_regime": signal.get("regime", "no_signal"),
            "backtest_verdict": backtest.get("verdict", "n/a"),
            "backtest_sample_size": backtest.get("sample_size", 0),
            "backtest_evaluated_bars": backtest.get("evaluated_bars", 0),
            "backtest_setup_count": backtest.get("setup_count", backtest.get("sample_size", 0)),
            "executed_paper_count": sum(1 for item in decisions if item.get("decision_status") == "executed_paper"),
            "paper_order_count": len(paper_orders),
            "open_trade_count": len(open_trades),
            "closed_trade_count": len(closed_trades),
            "closed_realized_pnl": round(sum(float(item.get("realized_pnl", 0)) for item in closed_trades), 4),
            "risk_block_count": len(risk_blocks),
            "missing_5m_bars": gold_manifest.get("missing_bars", 0),
            "spike_flags": gold_manifest.get("spike_flags", 0),
            "net_pnl_marked": perf.get("net_pnl_marked", round(sum(float(item.get("realized_pnl", 0)) for item in closed_trades), 4)),
            "open_unrealized_r": perf.get("open_unrealized_r", 0),
            "expectancy_r": perf.get("expectancy_r", 0),
            "profit_factor": perf.get("profit_factor", 0),
        }

    def _gold_signal(self, signals: list[dict]) -> dict:
        return next((item for item in signals if item.get("asset") == "GOLD"), signals[0] if signals else {})

    def _gold_backtest(self, backtests: list[dict], signal: dict) -> dict:
        return next((item for item in backtests if item.get("signal_id") == signal.get("signal_id")), backtests[0] if backtests else {})

    def _gold_manifest(self, manifest: list[dict]) -> dict:
        return next((item for item in manifest if item.get("symbol") == "GOLD" and item.get("timeframe") == "5m"), {})

    def _load_strategy_reviews(self) -> list[dict]:
        root = self.output_root / "strategy_reviews"
        if not root.exists():
            return []
        reviews = []
        for path in sorted(root.glob("*.json")):
            rows = load_json(path)
            if rows:
                reviews.append(rows[-1])
        return reviews

    def _learning_state(self, reviews: list[dict], realized_pnl: float, closed_trades: int, risk_blocks: int) -> str:
        if not reviews:
            return "no_review_history"
        if closed_trades < 20:
            return "collect_more_paper_trades"
        if realized_pnl < 0:
            return "review_losing_regimes_before_parameter_changes"
        if risk_blocks > len(reviews) * 0.5:
            return "risk_gate_frequently_blocks_candidates"
        return "stable_keep_parameters"
