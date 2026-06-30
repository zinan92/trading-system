from schemas.analysis import Analysis
from schemas.market_data import Bar
from schemas.signal import Signal
from services.local_backtester import LocalBacktester


def _signal(direction: str = "long", regime: str = "trend_following") -> Signal:
    return Signal(
        signal_id="sig_gold_test",
        asset="GOLD",
        asset_class="commodity",
        direction=direction,
        strength=72,
        confidence=68,
        horizon="intraday_5m",
        thesis="test",
        regime=regime,
    )


def _trend_bars(count: int = 120) -> list[Bar]:
    rows = []
    price = 4500.0
    for index in range(count):
        close = price + 1.2
        rows.append(
            Bar(
                "GOLD",
                "5m",
                f"2026-05-20T{index // 12:02d}:{(index % 12) * 5:02d}:00+00:00",
                price,
                close * 1.003,
                price * 0.999,
                close,
                100,
                "broker_csv",
                ["csv_import"],
            )
        )
        price = close
    return rows


def test_local_backtester_uses_5m_bars_for_metrics():
    evidence = LocalBacktester().evaluate(_signal(), Analysis("a1", "sig_gold_test", "GOLD"), _trend_bars())

    assert evidence.backtest_id.startswith("local5m_")
    assert evidence.sample_size >= 20
    assert evidence.evaluated_bars == 120
    assert evidence.setup_count == evidence.sample_size
    assert evidence.win_rate > 0
    assert evidence.avg_r > 0
    assert evidence.verdict in {"supportive", "mixed"}


def test_local_backtester_no_trade_signal_returns_no_trade():
    evidence = LocalBacktester().evaluate(_signal(direction="watch", regime="no_trade"), Analysis("a1", "sig_gold_test", "GOLD"), _trend_bars())

    assert evidence.sample_size >= 20
    assert evidence.evaluated_bars == 120
    assert evidence.setup_count == evidence.sample_size
    assert evidence.skipped_reason == "signal_direction=watch"
    assert evidence.verdict == "no_trade"
