from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.journal_store import load_json
from services.lab_evaluator import LabSimulationConfig, equity_curve, metrics_from_trades
from services.lab_r4_promotion import CANDIDATES, FUNDING_FOOTNOTE, _anchor_check, render_r4_report, run_r4_promotion
from services.lab_registry import LabRegistry


def test_r4_consumes_holdout_only_for_phase1_passers(monkeypatch, tmp_path):
    bars = _bars(400)

    class FakeStrategyRegistry:
        def strategies(self):
            return [_Strategy("gold_5m_ema50_position"), _Strategy("gold_5m_psych_level_rejection")]

    def fake_replay(strategy, bars, walkforward):
        return {"status": "valid", "windows": [{"strategy_id": strategy.strategy_id, "bars": bars, "signals": [{"index": 0, "direction": "long"}]}]}

    def fake_window_eval(windows, bp, simulation=None):
        sid = windows[0]["strategy_id"]
        if sid == "gold_5m_ema50_position":
            expectancy = {0.5: 0.9, 0.75: 0.8, 2.0: 0.631297, 3.0: 0.4}.get(float(bp), 1.0)
            return _result(171, expectancy, bp)
        expectancy = {0.5: 0.4, 0.75: -0.1, 2.0: 0.263160, 3.0: -0.2}.get(float(bp), 0.5)
        return _result(128, expectancy, bp)

    monkeypatch.setattr("services.lab_r4_promotion.StrategyRegistry", FakeStrategyRegistry)
    monkeypatch.setattr("services.lab_r4_promotion._pin_to_r3_range", lambda output_root, bars: bars)
    monkeypatch.setattr("services.lab_r1_scan._replay_strategy_windows", fake_replay)
    monkeypatch.setattr("services.lab_r4_promotion._evaluate_replayed_windows", fake_window_eval)
    monkeypatch.setattr("services.lab_r4_promotion._regime_slices", lambda trades, bars: {key: {"trade_count": 10, "net_pnl": 1.0, "expectancy_per_trade": 0.1} for key in ("low", "mid", "high")})
    monkeypatch.setattr("services.lab_r4_promotion.regenerate_strategy_signals", lambda strategy, bars: {"status": "valid", "bars": bars, "signals": [{"index": 0, "direction": "long"}]})
    monkeypatch.setattr("services.lab_r4_promotion.evaluate_signals", lambda bars, signals, config: _result(20, 0.2 if config.cost_bp_per_side == 0.5 else -0.1, config.cost_bp_per_side))

    result = run_r4_promotion(tmp_path, LabRegistry(tmp_path), bars)

    assert result["rows"][0]["paper_eligible"] is True
    assert result["rows"][1]["holdout"]["status"] == "forfeited"
    ema_entry = load_json(tmp_path / "lab" / "experiments" / "R4_gold_5m_ema50_position_swing.json")[0]
    psych_entry = load_json(tmp_path / "lab" / "experiments" / "R4_gold_5m_psych_level_rejection_swing.json")[0]
    assert ema_entry["holdout_consumed"] is True
    assert psych_entry["holdout_consumed"] is False
    assert load_json(tmp_path / "lab" / "promotion" / "gold_5m_ema50_position_swing.json")[0]["paper_eligible"] is True


def test_r4_anchor_check_detects_exact_mismatch():
    candidate = CANDIDATES[0]
    mismatch = _anchor_check(candidate, {"metrics": {"trade_count": candidate.expected_trades, "expectancy_per_trade": 0.0}})

    assert mismatch["maker_2bp_net"]["expected"] == candidate.expected_maker_2bp_net


def test_r4_report_carries_funding_footnote_and_dual_basis():
    report = render_r4_report([
        {
            "candidate": "C1",
            "anchor_status": "matched",
            "phase1": {
                "primary": _phase(True, 0.5),
                "secondary": _phase(True, 2.0),
            },
            "holdout": {
                "status": "consumed",
                "primary": _holdout(True, 10, 0.1),
                "secondary": _holdout(False, 10, -0.2),
            },
            "closing_sentence": "C1 sentence.",
        }
    ])

    assert FUNDING_FOOTNOTE in report
    assert "Primary basis: 0.5 bp/side" in report
    assert "secondary" in report


class _Strategy:
    symbol = "GOLD"
    timeframe = "5m"
    params = {"engine": "fake"}

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id


def _result(count: int, expectancy: float, bp: float) -> dict:
    trades = [
        {
            "net_pnl": expectancy,
            "fixed_notional": 1000.0,
            "entry_timestamp": f"2026-01-01T{index % 24:02d}:00:00+00:00",
            "exit_timestamp": f"2026-01-01T{index % 24:02d}:05:00+00:00",
        }
        for index in range(count)
    ]
    equity = equity_curve(trades, 10_000)
    return {"status": "valid", "reason": "pass", "window_count": 8, "trades": trades, "equity": equity, "metrics": metrics_from_trades(trades, equity, LabSimulationConfig(cost_bp_per_side=bp))}


def _phase(passed: bool, expectancy: float) -> dict:
    return {
        "metrics": {"trade_count": 100, "expectancy_per_trade": expectancy, "max_drawdown_pct": 1.0},
        "stress_expectancy_per_trade": expectancy,
        "regime_slices": {key: {"trade_count": 10, "expectancy_per_trade": expectancy} for key in ("low", "mid", "high")},
        "objective": {"passed": passed, "gates": {"min_oos_trades": {"passed": passed}}},
        "passed": passed,
    }


def _holdout(passed: bool, trades: int, expectancy: float) -> dict:
    return {"metrics": {"trade_count": trades, "expectancy_per_trade": expectancy, "max_drawdown_pct": 1.0}, "bootstrap_ci": {"low": expectancy - 0.1, "high": expectancy + 0.1, "width": 0.2}, "verdict": "pass" if passed else "fail", "passed": passed}


def _bars(count: int) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "1m", (start + timedelta(minutes=index)).isoformat(), 100, 101, 99, 100, 1, "test", [])
        for index in range(count)
    ]
