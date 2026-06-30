from schemas.market_data import Bar
from services.strategy_backtester import BacktestConfig, StrategyBacktester

_ZERO_COSTS = {"spread_pct": 0, "slippage_pct": 0, "commission_pct_notional": 0, "commission_per_order": 0, "min_commission": 0}


def _bar(close: float, high: float, low: float) -> Bar:
    return Bar("GOLD", "1m", "2026-05-01T00:00:00+00:00", close, high, low, close, 10, "binance_usdm", [])


def _flat(close: float) -> Bar:
    return _bar(close, close, close)


def test_long_trade_hits_target_with_expected_pnl():
    # entry at bar0 close=100; stop 1%, target 2%; bar1 high reaches target.
    bars = [_flat(100)] + [_bar(101, 103, 100.5)] + [_flat(102)] * 5
    cfg = BacktestConfig(stop_pct=1.0, target_pct=2.0, max_hold_bars=5, starting_equity=10_000, position_size_pct=8.0)
    bt = StrategyBacktester(cfg, cost_rules=_ZERO_COSTS)

    result = bt.run(bars, [{"index": 0, "direction": "long"}])

    assert result["trades"] == 1
    assert result["wins"] == 1
    # qty = 10000*8% / 100 = 8 ; pnl = (102-100)*8 = 16 ; risk = (100-99)*8 = 8 -> R = 2
    assert result["net_pnl"] == 16.0
    assert result["avg_r"] == 2.0
    assert result["return_pct"] == 0.16  # 16 / 10000


def test_long_trade_hits_stop_first_when_both_touched():
    # bar1 touches BOTH stop and target -> conservative: stop wins.
    bars = [_flat(100)] + [_bar(100, 103, 98)] + [_flat(100)] * 5
    cfg = BacktestConfig(stop_pct=1.0, target_pct=2.0, max_hold_bars=5, starting_equity=10_000, position_size_pct=8.0)
    bt = StrategyBacktester(cfg, cost_rules=_ZERO_COSTS)

    trades = bt._simulate(bars, [{"index": 0, "direction": "long"}])
    assert trades[0]["exit_reason"] == "stop"
    assert trades[0]["net_pnl"] < 0


def test_timeout_exit_when_no_level_hit():
    bars = [_flat(100)] + [_flat(100.2)] * 10
    cfg = BacktestConfig(stop_pct=5.0, target_pct=5.0, max_hold_bars=3, starting_equity=10_000, position_size_pct=8.0)
    bt = StrategyBacktester(cfg, cost_rules=_ZERO_COSTS)

    trades = bt._simulate(bars, [{"index": 0, "direction": "long"}])
    assert trades[0]["exit_reason"] == "timeout"
    assert trades[0]["bars_held"] == 3


def test_no_overlapping_positions():
    # two signals one bar apart; the second must be skipped while the first is open.
    bars = [_flat(100)] + [_flat(100.1)] * 10
    cfg = BacktestConfig(stop_pct=5.0, target_pct=5.0, max_hold_bars=5, starting_equity=10_000)
    bt = StrategyBacktester(cfg, cost_rules=_ZERO_COSTS)

    trades = bt._simulate(bars, [{"index": 0, "direction": "long"}, {"index": 1, "direction": "long"}])
    assert len(trades) == 1


def test_costs_reduce_net_pnl_below_gross():
    bars = [_flat(100)] + [_bar(101, 103, 100.5)] + [_flat(102)] * 5
    cfg = BacktestConfig(stop_pct=1.0, target_pct=2.0, max_hold_bars=5)
    real = StrategyBacktester(cfg)  # real paper costs
    trades = real._simulate(bars, [{"index": 0, "direction": "long"}])
    assert trades[0]["costs"] > 0
    assert trades[0]["net_pnl"] < trades[0]["gross_pnl"]


def test_metrics_profit_factor_and_drawdown():
    # one winner then one loser, sequential (non-overlapping).
    bars = [_flat(100)] + [_bar(102, 103, 101)] + [_flat(102)] * 3 + [_bar(102, 102, 97)] + [_flat(98)] * 5
    cfg = BacktestConfig(stop_pct=1.0, target_pct=2.0, max_hold_bars=3, starting_equity=10_000, position_size_pct=8.0)
    bt = StrategyBacktester(cfg, cost_rules=_ZERO_COSTS)

    result = bt.run(bars, [{"index": 0, "direction": "long"}, {"index": 5, "direction": "long"}])
    assert result["trades"] == 2
    assert result["wins"] == 1 and result["losses"] == 1
    assert result["max_drawdown_pct"] > 0  # the loser creates a drawdown
    assert result["profit_factor"] > 0
