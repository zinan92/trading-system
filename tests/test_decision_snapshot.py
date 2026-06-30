import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelines.daily import run_daily_pipeline
from schemas.market_data import Bar
from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket
from services.decision_snapshot import DecisionSnapshotBuilder
from services.market_store import MarketStore
from services.market_view import MarketViewStore
from services.strategy_registry import StrategyRegistry


def _bars(count: int = 180) -> list[Bar]:
    start = datetime(2026, 6, 25, tzinfo=timezone.utc)
    price = 4000.0
    rows = []
    for index in range(count):
        close = round(price + 0.8, 2)
        rows.append(Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), price, close + 0.5, price - 0.5, close, 10, "binance_usdm", []))
        price = close
    return rows


def _signal() -> Signal:
    return Signal("sig_gold_test", "GOLD", "commodity", "long", 80, 75, "intraday", "test thesis", regime="test_regime")


def _ticket() -> TradeTicket:
    return TradeTicket(
        ticket_id="ticket_gold_test",
        signal_id="sig_gold_test",
        asset="GOLD",
        asset_class="commodity",
        action="prepare_buy",
        entry_zone="4000-4010",
        stop_loss=3990,
        targets=[4040],
        position_size_pct=10,
        max_loss_pct=0.5,
        order_type="limit",
        time_in_force="day",
        paper_only=True,
        trigger="test",
        invalid_if="invalid",
        methods=[],
        backtest={},
        signal_regime="test",
        signal_strength=80,
        signal_confidence=75,
        factor_scores={},
        source_artifacts=[],
        rationale="r",
        counter_rationale="c",
        manual_execution_required=True,
        verdict="approved",
        trade_quality={"reward_to_risk": 2.0, "target_equity_return_pct": 1.2},
    )


def test_decision_snapshot_records_go_with_indicators_and_execution_plan(tmp_path: Path):
    root = tmp_path / "outputs"
    snapshot = DecisionSnapshotBuilder(root).record(
        "2026-06-25",
        "gold_test",
        "5m",
        _bars(),
        _signal(),
        direction_bias={"action": "allow", "direction_bias": "strong_long"},
        ticket=_ticket(),
        final_decision="go",
    )

    assert snapshot["final_decision"] == "go"
    assert snapshot["bar_timestamp"] == _bars()[-1].timestamp
    assert snapshot["indicators"]["ema50"] is not None
    assert snapshot["indicators"]["rsi14"] is not None
    assert snapshot["indicators"]["macd"]["line"] is not None
    assert snapshot["execution_plan"]["take_profit"] == 4040
    assert snapshot["execution_plan"]["stop_loss"] == 3990
    assert snapshot["execution_plan"]["risk_reward"] == 2.0
    saved = json.loads((root / "decision_snapshots" / "2026-06-25.json").read_text())[0]
    assert saved["as_of_contract"]["no_future_bars"] is True


def test_daily_pipeline_records_no_go_snapshot_for_direction_bias_block(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-06-25"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4100.00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-06-25T15:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "isolated-live.env"))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", str(tmp_path / "isolated-obsidian"))
    MarketStore(db).upsert_bars(_bars())
    MarketViewStore(root).record(
        run_date,
        10,
        "强空日，只做空。",
        timeframes=["1D", "4H"],
        reference_price=4100,
        expires_if_price_moves_pct=100.0,
    )
    strategy = StrategyRegistry(
        {
            "snapshot_bias_block": {
                "symbol": "GOLD",
                "timeframe": "5m",
                "position_gate": {"enabled": False},
                "signal": {
                    "long_strength_min": 0,
                    "long_confidence_min": 0,
                    "short_strength_max": -1,
                    "event_block_below": 0,
                },
            }
        }
    ).get("snapshot_bias_block")

    run_daily_pipeline(run_date, strategy=strategy, output_root=root)

    snapshots = json.loads((root / "decision_snapshots" / f"{run_date}.json").read_text())
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["final_decision"] == "no_go"
    assert snap["no_go_reason"] == "direction bias gate blocked trading"
    assert snap["direction_bias"]["action"] == "block"
    assert snap["execution_plan"]["ticket_id"] == ""
