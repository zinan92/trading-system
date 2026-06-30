from schemas.asset import Asset
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar, MarketEvent
from schemas.signal import Signal
from services.backtest_client import BacktestClient
from services.copilot_client import CopilotClient
from services.risk_engine import RiskEngine
from services.signal_engine import SignalEngine


def _bars(symbol: str, start: float, drift: float) -> list[Bar]:
    price = start
    rows = []
    for index in range(1, 31):
        close = round(price * drift, 2)
        rows.append(Bar(symbol, "1d", f"mock-1d-{index:02d}", price, close * 1.01, close * 0.99, close, 1000 + index, "mock"))
        price = close
    return rows


def test_gold_signal_uses_factor_scores_and_regime():
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    factors = {
        "DXY": {"bars": _bars("DXY", 104, 0.998), "events": []},
        "US10Y_REAL": {"bars": _bars("US10Y_REAL", 2.0, 0.995), "events": []},
        "GLD_FLOW": {"bars": _bars("GLD_FLOW", 1.0, 1.002), "events": []},
    }
    events = [MarketEvent("e1", ["GOLD"], "defensive demand", "mock", "now", "positive", 70)]

    signal = SignalEngine().generate(gold, _bars("GOLD", 2350, 1.006), events, "2026-05-25", factor_context=factors)

    assert signal.asset == "GOLD"
    assert signal.direction == "long"
    assert signal.regime in {"trend_following", "pullback_long"}
    assert signal.factor_scores["macro"] > 55
    assert signal.source_artifacts


def test_event_risk_blocks_ticket_generation():
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    events = [MarketEvent("e1", ["GOLD"], "CPI shock window", "mock", "now", "neutral", 40)]
    signal = SignalEngine().generate(gold, _bars("GOLD", 2350, 1.006), events, "2026-05-25")
    analysis = CopilotClient("http://localhost", True).analyze(signal)
    backtest = BacktestClient("http://localhost", True).evaluate(signal, analysis)

    ticket = RiskEngine(
        {
            "default": {"max_loss_pct": 0.5, "position_size_pct": 8, "min_signal_strength": 60, "min_confidence": 55},
            "asset_class_overrides": {"commodity": {"stop_loss_pct": 2, "target_pct": 4, "position_size_pct": 8}},
        }
    ).generate_ticket(signal, _bars("GOLD", 2350, 1.006), analysis, backtest)

    assert signal.regime == "event_risk_reduction"
    assert ticket is None


def test_thin_backtest_does_not_block_paper_ticket():
    signal = SignalEngine().generate(
        Asset("GOLD", "Gold", "commodity", "high", "UTC"),
        _bars("GOLD", 2350, 1.006),
        [MarketEvent("e1", ["GOLD"], "event", "mock", "now", "positive", 65)],
        "2026-05-25",
    )
    analysis = CopilotClient("http://localhost", True).analyze(signal)
    thin = BacktestEvidence("b1", signal.signal_id, "GOLD", 12, 0.45, 0.1, 8.0, "thin", 0.8)

    engine = RiskEngine(
        {
            "default": {"max_loss_pct": 0.5, "position_size_pct": 8, "min_signal_strength": 60, "min_confidence": 55},
            "asset_class_overrides": {"commodity": {"stop_loss_pct": 2, "target_pct": 4, "position_size_pct": 8}},
        }
    )
    ticket = engine.generate_ticket(signal, _bars("GOLD", 2350, 1.006), analysis, thin)

    assert ticket is not None
    assert ticket.backtest["verdict"] == "thin"
    assert engine.last_rejection == {}


def test_generated_ticket_keeps_signal_attribution():
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    events = [MarketEvent("e1", ["GOLD"], "defensive demand", "mock", "now", "positive", 70)]
    signal = SignalEngine().generate(gold, _bars("GOLD", 2350, 1.006), events, "2026-05-25")
    analysis = CopilotClient("http://localhost", True).analyze(signal)
    backtest = BacktestEvidence("b1", signal.signal_id, "GOLD", 40, 0.55, 0.2, 4.0, "supportive", 1.4)

    ticket = RiskEngine(
        {
            "default": {"max_loss_pct": 0.5, "position_size_pct": 8, "min_signal_strength": 60, "min_confidence": 55},
            "asset_class_overrides": {"commodity": {"stop_loss_pct": 2, "target_pct": 4, "position_size_pct": 8}},
        }
    ).generate_ticket(signal, _bars("GOLD", 2350, 1.006), analysis, backtest)

    row = ticket.to_dict()

    assert row["signal_regime"] == signal.regime
    assert row["signal_strength"] == signal.strength
    assert row["signal_confidence"] == signal.confidence
    assert row["factor_scores"] == signal.factor_scores
    assert row["source_artifacts"] == signal.source_artifacts
    assert row["trade_quality"]["passes"] is True
    assert row["trade_quality"]["target_equity_return_pct"] >= 1.0


def test_trade_quality_blocks_ticket_below_one_percent_equity_target():
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    events = [MarketEvent("e1", ["GOLD"], "defensive demand", "mock", "now", "positive", 70)]
    signal = SignalEngine().generate(gold, _bars("GOLD", 2350, 1.006), events, "2026-05-25")
    analysis = CopilotClient("http://localhost", True).analyze(signal)
    backtest = BacktestEvidence("b1", signal.signal_id, "GOLD", 40, 0.55, 0.2, 4.0, "supportive", 1.4)
    engine = RiskEngine(
        {
            "default": {
                "max_loss_pct": 0.5,
                "position_size_pct": 8,
                "min_signal_strength": 60,
                "min_confidence": 55,
                "risk_reward_min": 1.8,
                "trade_quality": {
                    "effective_leverage": 5,
                    "min_target_equity_return_pct": 1.0,
                    "min_reward_to_risk": 1.8,
                    "cost_buffer_price_move_pct": 0.02,
                },
            },
            "asset_class_overrides": {"commodity": {"stop_loss_pct": 0.1, "target_pct": 0.1, "position_size_pct": 8}},
        }
    )

    ticket = engine.generate_ticket(signal, _bars("GOLD", 2350, 1.006), analysis, backtest)

    assert ticket is None
    assert engine.last_rejection["reason"] == "trade quality gate blocked trading"
    assert engine.last_rejection["signal_gate"]["passes"] is True
    assert engine.last_rejection["trade_quality"]["passes"] is False
    assert any("target equity return" in item for item in engine.last_rejection["trade_quality"]["reasons"])


def test_signal_threshold_rejection_records_exact_failed_gate():
    signal = Signal(
        "sig_low_strength",
        "GOLD",
        "commodity",
        "long",
        52,
        50,
        "intraday",
        "directional but weak",
    )
    engine = RiskEngine(
        {
            "default": {"max_loss_pct": 0.5, "position_size_pct": 8, "min_signal_strength": 60, "min_confidence": 55},
            "asset_class_overrides": {"commodity": {"stop_loss_pct": 2, "target_pct": 4, "position_size_pct": 8}},
        }
    )

    ticket = engine.generate_ticket(signal, _bars("GOLD", 2350, 1.006))

    assert ticket is None
    assert engine.last_rejection["reason"] == "signal threshold gate blocked trading"
    assert engine.last_rejection["signal_gate"]["strength"] == 52
    assert engine.last_rejection["signal_gate"]["min_signal_strength"] == 60
    assert engine.last_rejection["signal_gate"]["confidence"] == 50
    assert engine.last_rejection["signal_gate"]["min_confidence"] == 55
    assert "signal strength 52 below minimum 60" in engine.last_rejection["signal_gate"]["reasons"]
    assert "signal confidence 50 below minimum 55" in engine.last_rejection["signal_gate"]["reasons"]
