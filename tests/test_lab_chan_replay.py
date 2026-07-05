from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.journal_store import write_json
from services.lab_chan_replay import MAX_CAUSAL_REPLAY_BARS, _evaluate_chan, causal_chan_signals, chan_live_parity, run_chan_addendum
from services.lab_registry import LabRegistry


def test_causal_chan_signals_maps_detected_points_to_bars():
    bars = _bars(5)
    strategy = _Strategy()

    replay = causal_chan_signals(strategy, bars)

    assert replay["status"] == "valid"
    assert replay["signals"] == [{"index": 2, "direction": "long", "timestamp": bars[2].timestamp, "close": bars[2].close}]


def test_causal_chan_signals_fails_without_detector_and_respects_filters():
    missing = causal_chan_signals(_NoDetectorStrategy(), _bars(3))
    filtered = causal_chan_signals(_FilteredStrategy(), _bars(3))

    assert missing["status"] == "not_replayable"
    assert missing["reason"] == "missing_detect_points"
    assert filtered["status"] == "valid"
    assert filtered["signals"] == []


def test_chan_parity_reports_no_directional_records(tmp_path):
    path = tmp_path / "strategies" / "gold_1m_chan" / "signals" / "2026-01-01.json"
    write_json(path, [{"direction": "watch", "generated_at": "2026-01-01T00:02:00+00:00"}])

    parity = chan_live_parity(tmp_path, _bars(5))

    assert parity["status"] == "no_directional_live_records"
    assert parity["records_checked"] == 1


def test_chan_parity_reports_missing_source_artifacts(tmp_path):
    path = tmp_path / "strategies" / "gold_1m_chan" / "signals" / "2026-01-01.json"
    write_json(path, [{"direction": "long", "generated_at": "2026-01-01T00:02:00+00:00", "source_artifacts": ["clean_bars/2026-01-01/GOLD_1m.json"]}])

    parity = chan_live_parity(tmp_path, _bars(5))

    assert parity["status"] == "source_artifacts_missing"
    assert parity["mismatches"] == 1


def test_chan_parity_can_match_bounded_live_record(monkeypatch, tmp_path):
    source = tmp_path / "clean_bars" / "2026-01-01" / "GOLD_1m.json"
    write_json(source, [{"placeholder": True}])
    path = tmp_path / "strategies" / "gold_1m_chan" / "signals" / "2026-01-01.json"
    write_json(path, [{"direction": "long", "generated_at": "2026-01-01T00:02:00+00:00", "source_artifacts": ["clean_bars/2026-01-01/GOLD_1m.json"]}])

    class FakeStrategyRegistry:
        def strategies(self):
            return [_Strategy("gold_1m_chan")]

    monkeypatch.setattr("services.lab_chan_replay.StrategyRegistry", FakeStrategyRegistry)
    monkeypatch.setattr("services.lab_chan_replay.causal_chan_signals", lambda strategy, bars: {"signals": [{"direction": "long"}]})

    parity = chan_live_parity(tmp_path, _bars(5))

    assert parity["status"] == "match"
    assert parity["matches"] == 1


def test_evaluate_chan_fails_closed_when_full_replay_exceeds_bound():
    payload = _evaluate_chan(_Strategy(), _bars(MAX_CAUSAL_REPLAY_BARS + 1), (0.0,))

    assert payload["row"]["status"] == "not_replayable"
    assert "bounded lab replay limit" in payload["row"]["reason"]


def test_evaluate_chan_aggregates_small_window(monkeypatch):
    bars = _bars(5)

    class FakeQuarantine:
        def __init__(self, bars, config):
            self.bars = bars

        def public_bars_between(self, start, end):
            return self.bars

    monkeypatch.setattr("services.lab_chan_replay.HoldoutQuarantine", FakeQuarantine)
    monkeypatch.setattr("services.lab_chan_replay.build_windows", lambda bars, config: [{"validate_start": bars[0].timestamp, "validate_end": bars[-1].timestamp}])
    monkeypatch.setattr("services.lab_chan_replay.causal_chan_signals", lambda strategy, bars: {"status": "valid", "bars": bars, "signals": [{"index": 0, "direction": "long"}]})
    monkeypatch.setattr("services.lab_chan_replay.evaluate_signals", lambda bars, signals, config: {"status": "valid", "trades": [{"net_pnl": 1.0}], "reason": "pass"})
    monkeypatch.setattr("services.lab_evaluator.equity_curve", lambda trades, start: [{"equity": start + 1.0}])
    monkeypatch.setattr("services.lab_evaluator.metrics_from_trades", lambda trades, equity, config: {"trade_count": 150, "expectancy_bp_on_notional": 1.0, "expectancy_per_trade": 0.1})

    payload = _evaluate_chan(_Strategy(), bars, (0.0, 2.0))

    assert payload["row"]["status"] == "valid"
    assert payload["row"]["oos_trades"] == 150


def test_run_chan_addendum_registers_all_chan_rows(monkeypatch, tmp_path):
    strategies = [_Strategy(strategy_id) for strategy_id in ("gold_1m_chan", "gold_1m_chan_ungated", "gold_1m_chan_buy1", "gold_1m_chan_macdfilter")]

    class FakeStrategyRegistry:
        def strategies(self):
            return strategies

    def fake_evaluate(strategy, bars, cost_grid):
        row = {
            "strategy": strategy.strategy_id,
            "oos_trades": None,
            "gross_bp_per_trip": None,
            "break_even_bp": None,
            "maker_2bp_expectancy_per_trade": None,
            "status": "not_replayable",
            "reason": "test",
        }
        return {"row": row, "cost_grid_results": {}, "notes": ["test"]}

    monkeypatch.setattr("services.lab_chan_replay.StrategyRegistry", FakeStrategyRegistry)
    monkeypatch.setattr("services.lab_chan_replay.chan_live_parity", lambda output_root, bars: {"status": "no_directional_live_records", "records_checked": 0, "matches": 0, "mismatches": 0, "details": []})
    monkeypatch.setattr("services.lab_chan_replay._evaluate_chan", fake_evaluate)

    result = run_chan_addendum(tmp_path, LabRegistry(tmp_path), _bars(5))

    assert len(result["rows"]) == 4
    assert len(list((tmp_path / "lab" / "experiments").glob("R1_CHAN_CAUSAL_*.json"))) == 4


class _Strategy:
    def __init__(self, strategy_id: str = "fake_chan") -> None:
        self.strategy_id = strategy_id

    timeframe = "1m"
    params = {"engine": "chan"}

    def signal_engine(self):
        return _Engine()


class _Engine:
    def detect_points(self, bars):
        return [{"time": "2026/01/01 00:02", "is_buy": True}]


class _NoDetectorStrategy(_Strategy):
    def signal_engine(self):
        return object()


class _FilteredStrategy(_Strategy):
    def signal_engine(self):
        engine = _Engine()
        engine.filters = [_RejectFilter()]
        return engine


class _RejectFilter:
    def accepts(self, signal, bars):
        return False, "reject"


def _bars(count: int) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "1m", (start + timedelta(minutes=index)).isoformat(), 100 + index, 100 + index, 100 + index, 100 + index, 1, "test", [])
        for index in range(count)
    ]
