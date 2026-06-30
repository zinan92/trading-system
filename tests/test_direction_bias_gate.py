import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelines.daily import run_daily_pipeline
from schemas.market_data import Bar
from schemas.signal import Signal
from services.direction_bias_gate import DirectionBiasGate
from services.market_store import MarketStore
from services.market_view import MarketViewStore
from services.strategy_registry import StrategyRegistry


def _signal(direction: str = "long") -> Signal:
    return Signal(
        signal_id="sig_gold_test",
        asset="GOLD",
        asset_class="commodity",
        direction=direction,
        strength=80,
        confidence=75,
        horizon="intraday",
        thesis="test",
        regime="test_regime",
    )


def _bars(count: int = 180) -> list[Bar]:
    start = datetime(2026, 6, 25, tzinfo=timezone.utc)
    rows = []
    price = 4000.0
    for index in range(count):
        close = round(price + 0.8, 2)
        rows.append(
            Bar(
                "GOLD",
                "5m",
                (start + timedelta(minutes=5 * index)).isoformat(),
                price,
                close + 0.5,
                price - 0.5,
                close,
                10,
                "binance_usdm",
                [],
            )
        )
        price = close
    return rows


def test_direction_bias_gate_blocks_long_on_strong_short(tmp_path: Path):
    root = tmp_path / "outputs"
    MarketViewStore(root).record(
        "2026-06-25",
        10,
        "强空日，只做空。",
        timeframes=["1D", "4H", "15m"],
    )

    signal, decision = DirectionBiasGate(root).apply("2026-06-25", _signal("long"))

    assert decision["action"] == "block"
    assert decision["direction_bias"] == "strong_short"
    assert decision["market_view_expiry"]["status"] == "active"
    assert decision["filter_effect"] == "active_view_blocked_signal"
    assert "已在出票前拦截" in decision["operator_message"]
    assert signal.direction == "watch"
    assert signal.regime == "direction_bias_block"
    rows = json.loads((root / "direction_bias_decisions" / "2026-06-25.json").read_text())
    assert rows[0]["reason"] == "strong_short blocks long signal"


def test_direction_bias_gate_downweights_counter_bias_without_blocking(tmp_path: Path):
    root = tmp_path / "outputs"
    MarketViewStore(root).record(
        "2026-06-25",
        35,
        "偏空，做空优先。",
        timeframes=["1D"],
    )

    signal, decision = DirectionBiasGate(root).apply("2026-06-25", _signal("long"))

    assert decision["action"] == "downweight"
    assert signal.direction == "long"
    assert signal.strength == 65
    assert signal.confidence == 60


def test_direction_bias_gate_expires_view_after_large_price_move(tmp_path: Path):
    root = tmp_path / "outputs"
    MarketViewStore(root).record(
        "2026-06-25",
        10,
        "4000 附近强空，但跌太多后要重评估。",
        timeframes=["1D"],
        reference_price=4000,
        expires_if_price_moves_pct=1.0,
    )

    signal, decision = DirectionBiasGate(root).apply(
        "2026-06-25",
        _signal("long"),
        current_price=3950,
        as_of="2026-06-25T04:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["expired"] is True
    assert decision["market_view_expiry"]["status"] == "expired"
    assert decision["filter_effect"] == "expired_direction_filter_disabled"
    assert "不再按这条观点过滤多空信号" in decision["operator_message"]
    assert "price moved 1.25%" in decision["market_view_expiry"]["reason"]
    assert signal.direction == "long"


def test_direction_bias_gate_expires_view_after_target_price_reached(tmp_path: Path):
    root = tmp_path / "outputs"
    MarketViewStore(root).record(
        "2026-06-25",
        10,
        "强空，打到 3950 后重新评估，不再继续按只做空过滤。",
        timeframes=["1D"],
        reference_price=4000,
        expires_if_price_moves_pct=10,
        expiry_target_price=3950,
    )

    signal, decision = DirectionBiasGate(root).apply(
        "2026-06-25",
        _signal("long"),
        current_price=3949,
        as_of="2026-06-25T04:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["expired"] is True
    assert decision["market_view_expiry"]["target_price"] == 3950
    assert decision["market_view_expiry"]["expire_below"] == 3950
    assert "price reached expire_below 3950.00" in decision["market_view_expiry"]["reason"]
    assert decision["filter_effect"] == "expired_direction_filter_disabled"
    assert signal.direction == "long"


def test_direction_bias_gate_expires_view_after_explicit_time(tmp_path: Path):
    root = tmp_path / "outputs"
    MarketViewStore(root).record(
        "2026-06-25",
        90,
        "早盘强多，只在上午有效。",
        timeframes=["1D"],
        expires_at="2026-06-25T03:00:00+00:00",
    )

    signal, decision = DirectionBiasGate(root).apply(
        "2026-06-25",
        _signal("short"),
        current_price=4010,
        as_of="2026-06-25T04:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["expired"] is True
    assert decision["market_view_expiry"]["status"] == "expired"
    assert decision["filter_effect"] == "expired_direction_filter_disabled"
    assert "time expired" in decision["market_view_expiry"]["reason"]
    assert signal.direction == "short"


def test_direction_bias_gate_expires_legacy_view_after_default_ttl(tmp_path: Path):
    root = tmp_path / "outputs"
    path = root / "market_views" / "2026-06-25.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "asset": "XAUUSD",
            "source": "legacy",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "stance": "只做空",
            "allowed_directions": ["short"],
            "blocked_directions": ["long"],
            "summary": "legacy strong short",
            "key_levels": [],
            "timeframes": ["1D"],
            "trade_plan": "",
            "raw_text": "",
        }
    ]), encoding="utf-8")

    signal, decision = DirectionBiasGate(root).apply(
        "2026-06-25",
        _signal("long"),
        current_price=3900,
        as_of="2026-06-25T13:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["expired"] is True
    assert "time expired" in decision["market_view_expiry"]["reason"]
    assert signal.direction == "long"


def test_direction_bias_gate_expires_legacy_view_after_default_price_move(tmp_path: Path):
    root = tmp_path / "outputs"
    path = root / "market_views" / "2026-06-25.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "asset": "XAUUSD",
            "source": "legacy",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "stance": "只做空",
            "allowed_directions": ["short"],
            "blocked_directions": ["long"],
            "summary": "legacy strong short near 4000",
            "reference_price": 4000,
            "key_levels": [],
            "timeframes": ["1D"],
            "trade_plan": "",
            "raw_text": "",
        }
    ]), encoding="utf-8")

    signal, decision = DirectionBiasGate(root).apply(
        "2026-06-25",
        _signal("long"),
        current_price=3950,
        as_of="2026-06-25T04:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["expired"] is True
    assert decision["market_view_expiry"]["threshold_move_pct"] == 1.0
    assert "price moved 1.25%" in decision["market_view_expiry"]["reason"]
    assert signal.direction == "long"


def test_direction_bias_gate_infers_reference_price_for_legacy_oral_view(tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    MarketStore(db).upsert_bars([
        Bar("GOLD", "1m", "2026-06-25T00:00:00+00:00", 3998, 4001, 3997, 4000, 1, "binance_usdm", []),
        Bar("GOLD", "1m", "2026-06-25T00:01:00+00:00", 4000, 4002, 3999, 4001, 1, "binance_usdm", []),
    ])
    path = root / "market_views" / "2026-06-25.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:01:30+00:00",
            "asset": "XAUUSD",
            "source": "legacy_oral_import",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "stance": "只做空",
            "allowed_directions": ["short"],
            "blocked_directions": ["long"],
            "summary": "4000 附近强空，跌太多后重评估。",
            "key_levels": [],
            "timeframes": ["1D"],
            "trade_plan": "",
            "raw_text": "",
        }
    ]), encoding="utf-8")

    signal, decision = DirectionBiasGate(root, market_db=db).apply(
        "2026-06-25",
        _signal("long"),
        current_price=3950,
        as_of="2026-06-25T02:00:00+00:00",
    )

    assert decision["action"] == "allow"
    assert decision["market_view_expiry"]["reference_price"] == 4001
    assert decision["market_view_expiry"]["expired"] is True
    assert "price moved" in decision["market_view_expiry"]["reason"]
    assert signal.direction == "long"


def test_daily_pipeline_direction_bias_blocks_ticket_before_risk(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    run_date = "2026-06-25"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_PRICE", "4100.00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP", "2026-06-25T15:00:00+00:00")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE", "1")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", str(tmp_path / "empty_vault"))
    MarketStore(db).upsert_bars(_bars())
    MarketViewStore(root).record(
        run_date,
        10,
        "强空日，只做空。",
        timeframes=["1D", "4H", "15m"],
        reference_price=4100,
        expires_if_price_moves_pct=100.0,
    )
    strategy = StrategyRegistry(
        {
            "bias_block_test": {
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
    ).get("bias_block_test")

    run_daily_pipeline(run_date, strategy=strategy, output_root=root)

    signal = json.loads((root / "signals" / f"{run_date}.json").read_text())[0]
    tickets = json.loads((root / "trade_tickets" / f"{run_date}.json").read_text())
    risk_blocks = json.loads((root / "risk_blocks" / f"{run_date}.json").read_text())
    decisions = json.loads((root / "direction_bias_decisions" / f"{run_date}.json").read_text())

    assert signal["direction"] == "watch"
    assert signal["regime"] == "direction_bias_block"
    assert tickets == []
    assert decisions[0]["action"] == "block"
    assert any(item["reason"] == "direction bias gate blocked trading" for item in risk_blocks)
