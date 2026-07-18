from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from schemas.analysis import Analysis
from schemas.market_data import Bar
from schemas.signal import Signal
from services.backtest_local_config import LocalBacktestConfig
from services.backtest_plugin_composition import compose_signal_backtest
from services.backtest_plugin_registry import BacktestPluginRegistry
from services.backtest_port import SignalBacktestRequest
from services.backtest_service import SignalBacktestService
from services.config_loader import ROOT, load_pipeline_config, load_strategy_config
from services.journal_store import load_json, write_json


class StrategyExperimentQueue:
    def __init__(
        self,
        output_root: Path | None = None,
        *,
        backtest_plugin_registry: BacktestPluginRegistry | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.backtest_service = SignalBacktestService(
            compose_signal_backtest(
                config,
                registry=backtest_plugin_registry,
            )
        )

    def build(self, run_date: str) -> dict:
        strategy_config = load_strategy_config()
        strategy_id, active_config = self._active_strategy(strategy_config)
        timeframe = str(active_config.get("timeframe", "5m"))
        current_config = LocalBacktestConfig.from_strategy_config(active_config)
        bars = self._bars(run_date, strategy_id, timeframe)
        signal = self._signal(run_date, strategy_id, timeframe)
        analysis = Analysis(
            analysis_id=f"analysis_experiment_{run_date}",
            signal_id=signal.signal_id,
            asset="GOLD",
            methods=["shadow_backtest", "daily_review"],
            thesis="Evaluate parameter variants without changing live or paper execution config.",
        )
        review = self._latest(self.output_root / "strategy_reviews" / f"{run_date}.json")
        ledger = self._latest(self.output_root / "learning_ledger" / f"{run_date}.json") or self._latest(self.output_root / "learning_ledger" / "current.json")
        proposal = self._latest(self.output_root / "strategy_change_proposals" / f"{run_date}.json") or self._latest(self.output_root / "strategy_change_proposals" / "current.json")
        data_lineage = self._latest(self.output_root / "data_source_lineage" / f"{run_date}.json") or self._latest(self.output_root / "data_source_lineage" / "current.json")
        hypotheses = load_json(self.output_root / "strategy_hypotheses" / f"{run_date}.json") or load_json(self.output_root / "strategy_hypotheses" / "current.json")
        shadow_hypotheses = self._shadow_hypotheses(hypotheses)
        baseline = self._evaluate("baseline", current_config, signal, analysis, bars, "Current configs/strategy.yaml backtest parameters.")
        experiments = [
            self._evaluate("tighter_stop", replace(current_config, stop_pct=max(0.001, round(current_config.stop_pct * 0.85, 6))), signal, analysis, bars, "Tighter stop; tests if losses can be cut sooner."),
            self._evaluate("wider_target", replace(current_config, target_pct=round(current_config.target_pct * 1.15, 6)), signal, analysis, bars, "Wider target; tests whether trend continuation pays for lower hit rate."),
            self._evaluate("shorter_hold", replace(current_config, max_hold_bars=max(6, int(current_config.max_hold_bars * 0.75))), signal, analysis, bars, "Shorter hold; tests reducing overnight/session drift exposure."),
            self._evaluate("stricter_drawdown", replace(current_config, supportive_max_drawdown_pct=max(1.0, round(current_config.supportive_max_drawdown_pct * 0.8, 3))), signal, analysis, bars, "Stricter supportive verdict drawdown gate."),
        ]
        ranked = sorted(experiments, key=lambda item: (item["score"], item["profit_factor"], item["avg_r"]), reverse=True)
        blockers = self._blockers(ledger, data_lineage, bars, proposal)
        status = "blocked" if blockers else ("experiment_ready" if proposal.get("status") == "eligible_for_small_experiment" else "collecting_evidence")
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "paper_only": True,
            "auto_apply": False,
            "sample_bars": len(bars),
            "signal_id": signal.signal_id,
            "signal_direction": signal.direction,
            "signal_regime": signal.regime,
            "backtest_plugin": self.backtest_service.runtime.audit_dict(),
            "baseline": baseline,
            "experiments": ranked,
            "review_hypotheses": hypotheses,
            "shadow_hypotheses": shadow_hypotheses,
            "best_candidate": ranked[0] if ranked else {},
            "blockers": blockers,
            "review_summary": review.get("summary", ""),
            "proposal_status": proposal.get("status", ""),
            "learning_state": ledger.get("learning_state", ""),
            "closed_trade_count": ledger.get("closed_trade_count", 0),
            "data_truth_level": data_lineage.get("truth_level", "unknown"),
            "source_artifacts": {
                "strategy_config": "configs/strategy.yaml",
                "clean_bars": str(self.output_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json"),
                "strategy_review": str(self.output_root / "strategy_reviews" / f"{run_date}.json"),
                "learning_ledger": str(self.output_root / "learning_ledger" / f"{run_date}.json"),
                "strategy_change_proposal": str(self.output_root / "strategy_change_proposals" / f"{run_date}.json"),
                "strategy_hypotheses": str(self.output_root / "strategy_hypotheses" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "strategy_experiments" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_experiments" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _evaluate(self, variant_id: str, config: LocalBacktestConfig, signal: Signal, analysis: Analysis, bars: list[Bar], rationale: str) -> dict:
        variant_signal = replace(signal, signal_id=f"{signal.signal_id}_{variant_id}")
        variant_analysis = replace(analysis, signal_id=variant_signal.signal_id)
        evidence = self.backtest_service.evaluate(
            SignalBacktestRequest.from_domain(
                variant_signal,
                variant_analysis,
                bars,
                backtest_config=config.to_strategy_config(),
                run_context={
                    "strategy_experiment": True,
                    "variant_id": variant_id,
                    "paper_only": True,
                },
            )
        )
        score = self._score(evidence.to_dict())
        return {
            "experiment_id": self._experiment_id(variant_id, config),
            "variant_id": variant_id,
            "rationale": rationale,
            "parameters": {
                "stop_pct": config.stop_pct,
                "target_pct": config.target_pct,
                "max_hold_bars": config.max_hold_bars,
                "supportive_max_drawdown_pct": config.supportive_max_drawdown_pct,
            },
            "verdict": evidence.verdict,
            "sample_size": evidence.sample_size,
            "win_rate": evidence.win_rate,
            "avg_r": evidence.avg_r,
            "profit_factor": evidence.profit_factor,
            "max_drawdown_pct": evidence.max_drawdown_pct,
            "evaluated_bars": evidence.evaluated_bars,
            "setup_count": evidence.setup_count,
            "backtest_plugin": evidence.backtest_plugin,
            "evidence_tier": evidence.evidence_tier,
            "input_hash": evidence.input_hash,
            "promotion_eligible": evidence.promotion_eligible,
            "degraded": evidence.degraded,
            "score": score,
        }

    def _score(self, evidence: dict) -> float:
        verdict_bonus = {"supportive": 2.0, "mixed": 0.75, "no_trade": 0.0, "thin": -1.0}.get(evidence.get("verdict"), -0.5)
        sample_bonus = min(float(evidence.get("sample_size", 0) or 0) / 100.0, 1.0)
        pf = min(float(evidence.get("profit_factor", 0) or 0), 3.0) / 3.0
        avg_r = float(evidence.get("avg_r", 0) or 0)
        drawdown_penalty = min(float(evidence.get("max_drawdown_pct", 0) or 0) / 20.0, 2.0)
        return round(verdict_bonus + sample_bonus + pf + avg_r - drawdown_penalty, 4)

    def _blockers(self, ledger: dict, data_lineage: dict, bars: list[Bar], proposal: dict) -> list[dict]:
        blockers: list[dict] = []
        closed = int(ledger.get("closed_trade_count", 0) or 0)
        provider_groups = data_lineage.get("provider_groups") or {}
        official_rows = int((provider_groups.get("official") or {}).get("rows", 0) or 0)
        execution_venue_rows = int((provider_groups.get("execution_venue") or {}).get("rows", 0) or 0)
        execution_grade_rows = official_rows + execution_venue_rows
        if len(bars) < 200:
            blockers.append({"name": "sample_size", "summary": "Need at least 200 clean GOLD bars before trusting experiments.", "evidence": {"bars": len(bars)}})
        if closed < 20:
            blockers.append({"name": "closed_trade_sample", "summary": "Need at least 20 closed paper trades before applying parameter changes.", "evidence": {"closed_trade_count": closed}})
        if execution_grade_rows == 0:
            blockers.append({"name": "execution_grade_data", "summary": "Execution-grade GOLD 5m rows are required before promoting an experiment.", "evidence": {"truth_level": data_lineage.get("truth_level"), "execution_grade_rows": execution_grade_rows}})
        if proposal.get("status") not in {"eligible_for_small_experiment"}:
            blockers.append({"name": "proposal_gate", "summary": "Strategy change proposal is not eligible for a small experiment.", "evidence": {"proposal_status": proposal.get("status")}})
        return blockers

    def _bars(self, run_date: str, strategy_id: str, timeframe: str) -> list[Bar]:
        candidates = [
            self.output_root / "strategies" / strategy_id / "clean_bars" / run_date / f"GOLD_{timeframe}.json",
            self.output_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json",
        ]
        if timeframe != "5m":
            candidates.extend(
                [
                    self.output_root / "strategies" / strategy_id / "clean_bars" / run_date / "GOLD_5m.json",
                    self.output_root / "clean_bars" / run_date / "GOLD_5m.json",
                ]
            )
        rows = []
        for path in candidates:
            rows = load_json(path)
            if rows:
                break
        bars = []
        for item in rows:
            bars.append(Bar(
                symbol=item.get("symbol", "GOLD"),
                timeframe=item.get("timeframe", timeframe),
                timestamp=item["timestamp"],
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item.get("volume", 0) or 0),
                provider=item.get("provider", ""),
                quality_flags=item.get("quality_flags", []),
            ))
        return bars

    def _signal(self, run_date: str, strategy_id: str, timeframe: str) -> Signal:
        rows = load_json(self.output_root / "strategies" / strategy_id / "signals" / f"{run_date}.json") or load_json(self.output_root / "signals" / f"{run_date}.json")
        item = next((row for row in rows if row.get("asset") == "GOLD"), rows[0] if rows else {})
        if not item:
            item = {"signal_id": f"sig_gold_{run_date}_missing", "asset": "GOLD", "asset_class": "commodity", "direction": "watch", "strength": 0, "confidence": 0, "horizon": timeframe, "thesis": "No signal artifact.", "regime": "no_signal"}
        return Signal(
            signal_id=item.get("signal_id", f"sig_gold_{run_date}"),
            asset=item.get("asset", "GOLD"),
            asset_class=item.get("asset_class", "commodity"),
            direction=item.get("direction", "watch"),
            strength=int(item.get("strength", 0) or 0),
            confidence=int(item.get("confidence", 0) or 0),
            horizon=item.get("horizon", timeframe),
            thesis=item.get("thesis", ""),
            evidence=item.get("evidence", []),
            methods=item.get("methods", []),
            regime=item.get("regime", "unknown"),
            factor_scores=item.get("factor_scores", {}),
            source_artifacts=item.get("source_artifacts", []),
            backtest_verdict=item.get("backtest_verdict", "not_checked"),
            invalid_if=item.get("invalid_if", ""),
            generated_at=item.get("generated_at", ""),
            expires_at=item.get("expires_at", ""),
            status=item.get("status", "new"),
        )

    def _experiment_id(self, variant_id: str, config: LocalBacktestConfig) -> str:
        raw = f"{variant_id}:{config.stop_pct}:{config.target_pct}:{config.max_hold_bars}:{config.supportive_max_drawdown_pct}".encode("utf-8")
        return f"exp_{hashlib.sha256(raw).hexdigest()[:10]}"

    def _active_strategy(self, strategy_config: dict) -> tuple[str, dict]:
        demo = self.config.get("demo_trading", {}) or {}
        strategy_id = str(demo.get("active_strategy_id", "")).strip()
        if strategy_id and isinstance(strategy_config.get(strategy_id), dict):
            return strategy_id, strategy_config[strategy_id]
        for key, value in strategy_config.items():
            if isinstance(value, dict) and value.get("enabled", True):
                return key, value
        return "gold_5m_v1", strategy_config.get("gold_5m_v1", {})

    def _shadow_hypotheses(self, hypotheses: list[dict]) -> list[dict]:
        shadow = []
        for item in hypotheses:
            hypothesis_id = item.get("hypothesis_id", "")
            if not hypothesis_id:
                continue
            raw = f"shadow:{hypothesis_id}:{item.get('category', '')}".encode("utf-8")
            shadow.append(
                {
                    "experiment_id": f"shadow_{hashlib.sha256(raw).hexdigest()[:10]}",
                    "hypothesis_id": hypothesis_id,
                    "strategy_id": item.get("strategy_id", ""),
                    "status": "queued_shadow",
                    "paper_only": True,
                    "auto_apply": False,
                    "thesis": item.get("thesis", ""),
                    "metrics_required": item.get("metrics_required", []),
                    "promotion_rule": item.get("promotion_rule", "explicit promotion gate required"),
                }
            )
        return shadow

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Strategy Experiment Queue - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Paper only: {payload['paper_only']}",
            f"- Auto apply: {payload['auto_apply']}",
            f"- Sample bars: {payload['sample_bars']}",
            f"- Data truth: {payload['data_truth_level']}",
            f"- Closed trades: {payload['closed_trade_count']}",
            f"- Review hypotheses: {len(payload.get('review_hypotheses', []))}",
            "",
            "## Baseline",
            f"- {payload['baseline'].get('variant_id')}: {payload['baseline'].get('verdict')} PF={payload['baseline'].get('profit_factor')} avgR={payload['baseline'].get('avg_r')}",
            "",
            "## Experiments",
        ]
        for item in payload["experiments"]:
            lines.append(f"- {item['variant_id']}: score={item['score']} verdict={item['verdict']} PF={item['profit_factor']} avgR={item['avg_r']} DD={item['max_drawdown_pct']} params={item['parameters']}")
        lines.extend(["", "## Shadow Hypotheses"])
        if payload.get("shadow_hypotheses"):
            for item in payload["shadow_hypotheses"]:
                lines.append(f"- {item['hypothesis_id']}: {item['status']} auto_apply={item['auto_apply']} thesis={item['thesis']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Blockers"])
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        path = self.output_root / "strategy_experiments" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_strategy_experiment_queue(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyExperimentQueue(output_root).build(run_date)
