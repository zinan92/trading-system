from schemas.asset import Asset
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar, CleanDatasetManifest, MarketEvent, PaperOrder, PaperPosition
from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket


def test_asset_from_dict():
    asset = Asset.from_dict(
        {
            "symbol": "BTC",
            "name": "Bitcoin",
            "asset_class": "crypto",
            "priority": "high",
            "timezone": "UTC",
        }
    )
    assert asset.symbol == "BTC"
    assert asset.enabled is True


def test_signal_candidate_thresholds():
    signal = Signal(
        signal_id="sig_test",
        asset="BTC",
        asset_class="crypto",
        direction="long",
        strength=80,
        confidence=70,
        horizon="swing",
        thesis="test",
    )
    assert signal.approved_candidate(60, 55) is True


def test_gold_system_schema_to_dict_fields():
    bar = Bar("GOLD", "1d", "t1", 1, 2, 0.5, 1.5, 100, "mock", ["mock"])
    event = MarketEvent("e1", ["GOLD"], "event", "mock", "now", "positive", 60, "mock://event")
    manifest = CleanDatasetManifest("GOLD", "1d", ["raw.json"], 2, 1, 0, 1, 1, "UTC", "now")
    order = PaperOrder("o1", "t1", "filled", 2350, 2350, 1, "now")
    position = PaperPosition("GOLD", "long", 1, 2350, 0, 0, 0.5)
    ticket = TradeTicket("t1", "s1", "GOLD", "commodity", "prepare_buy", "2340-2360", 2300)
    backtest = BacktestEvidence("b1", "s1", "GOLD", 24, 0.5, 0.1, 1.0, "no_trade", 1.1, 200, 24, "signal_direction=watch")

    assert bar.to_dict()["provider"] == "mock"
    assert event.to_dict()["impact_score"] == 60
    assert manifest.to_dict()["duplicate_rows"] == 1
    assert order.to_dict()["status"] == "filled"
    assert position.to_dict()["risk_used_pct"] == 0.5
    assert ticket.to_dict()["paper_only"] is True
    assert backtest.to_dict()["evaluated_bars"] == 200
    assert backtest.to_dict()["setup_count"] == 24
