"""Isolated NautilusTrader execution-semantics fixture.

This is not the GOLD adapter. It proves bracket accounting with a packaged test
instrument while the canonical datafeed instrument-definition contract is
still missing.
"""

from __future__ import annotations

import json
from decimal import Decimal


def run_fixture() -> dict:
    import nautilus_trader
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.objects import Money
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.trading.strategy import Strategy

    class BracketFixtureConfig(StrategyConfig, frozen=True):
        instrument_id: object
        bar_type: BarType

    class BracketFixture(Strategy):
        def __init__(self, config: BracketFixtureConfig) -> None:
            super().__init__(config)
            self.submitted = False

        def on_start(self) -> None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)

        def on_bar(self, _bar: Bar) -> None:
            if self.submitted:
                return
            self.submitted = True
            orders = self.order_factory.bracket(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.instrument.make_qty(1),
                sl_trigger_price=self.instrument.make_price(95),
                tp_price=self.instrument.make_price(105),
            )
            self.submit_order_list(orders)

    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")

    def bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
        ts = (1_800_000_000 + minute * 60) * 1_000_000_000
        return Bar(
            bar_type=bar_type,
            open=instrument.make_price(open_),
            high=instrument.make_price(high),
            low=instrument.make_price(low),
            close=instrument.make_price(close),
            volume=instrument.make_qty(100),
            ts_event=ts,
            ts_init=ts,
        )

    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")),
    )
    engine.add_venue(
        venue=instrument.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
        base_currency=USDT,
        default_leverage=Decimal(10),
        reject_stop_orders=False,
        use_position_ids=False,
    )
    engine.add_instrument(instrument)
    engine.add_data([
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 101, 94, 100),
        bar(2, 100, 101, 99, 100),
    ])
    engine.add_strategy(
        BracketFixture(BracketFixtureConfig(instrument_id=instrument.id, bar_type=bar_type)),
    )
    engine.run()

    fills_report = engine.trader.generate_order_fills_report()
    positions_report = engine.trader.generate_positions_report()
    fills = [
        {
            "type": str(row["type"]),
            "side": str(row["side"]),
            "quantity": str(row["quantity"]),
            "avg_px": float(row["avg_px"]),
            "status": str(row["status"]),
        }
        for row in fills_report.to_dict(orient="records")
    ]
    position = positions_report.to_dict(orient="records")[0]
    result = {
        "nautilus_version": nautilus_trader.__version__,
        "fixture_scope": "execution_semantics_only_not_gold_adapter",
        "instrument": str(instrument.id),
        "bars": [
            {"open": 100, "high": 100, "low": 100, "close": 100},
            {"open": 100, "high": 101, "low": 94, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
        ],
        "fills": fills,
        "position": {
            "side": str(position["side"]),
            "quantity": str(position["quantity"]),
            "avg_px_open": float(position["avg_px_open"]),
            "avg_px_close": float(position["avg_px_close"]),
            "realized_pnl": str(position["realized_pnl"]),
        },
    }
    engine.dispose()

    assert [fill["type"] for fill in fills] == ["MARKET", "STOP_MARKET"]
    assert [fill["avg_px"] for fill in fills] == [100.0, 95.0]
    assert result["position"] == {
        "side": "FLAT",
        "quantity": "0.000",
        "avg_px_open": 100.0,
        "avg_px_close": 95.0,
        "realized_pnl": "-5.03510000 USDT",
    }
    return result


if __name__ == "__main__":
    print(json.dumps(run_fixture(), indent=2, ensure_ascii=False))
