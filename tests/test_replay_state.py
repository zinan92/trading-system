from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import write_json
from services.market_store import MarketStore
from services.replay_state import ReplayState


def _minute_bars(start: str, count: int) -> list[Bar]:
    base = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc)
    rows = []
    for idx in range(count):
        ts = base + timedelta(minutes=idx)
        price = 4000 + idx
        rows.append(Bar(
            symbol="GOLD",
            timeframe="1m",
            timestamp=ts.replace(microsecond=0).isoformat(),
            open=price,
            high=price + 2,
            low=price - 2,
            close=price + 1,
            volume=idx + 1,
            provider="binance_usdm",
            quality_flags=["public_proxy_feed"],
        ))
    return rows


def test_replay_state_derives_multi_timeframes_without_future_bars(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 61))
    write_json(
        output_root / "market_views" / "2026-06-25.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "summary": "强空，只做空",
            "reference_price": 4000,
            "expiry": {
                "valid_for_hours": 12,
                "expires_at": "2026-06-25T12:00:00+00:00",
                "expires_if_price_moves_pct": 1.0,
                "reference_price": 4000,
            },
        }],
    )
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-25.json",
        [
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:20:00+00:00",
                "final_decision": "go",
                "signal": {"direction": "short", "confidence": 61},
                "execution_plan": {"entry_zone": "4019-4021", "take_profit": 3980, "stop_loss": 4030},
            },
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:40:00+00:00",
                "final_decision": "go",
                "signal": {"direction": "short", "confidence": 70},
            },
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:29:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m", "5m", "15m", "4h", "1d"],
    )

    assert payload["schema_version"] == "replay-v1"
    assert payload["as_of_contract"]["no_future_bars"] is True
    assert payload["as_of_contract"]["higher_timeframes_may_include_partial_bar"] is False
    assert payload["as_of_contract"]["chart_bar_contract"] == "visible bars must have bucket_end <= cursor"
    assert payload["as_of_contract"]["decision_contract"] == "one_go_or_no_go_decision_per_closed_candle"
    assert payload["as_of_contract"]["implicit_no_go_when_no_trigger"] is True
    assert payload["as_of_contract"]["decision_clock_timeframe"] == "1m"
    assert payload["market_view"]["direction_bias"] == "strong_short"
    assert payload["market_view_expiry"]["status"] == "active"
    assert payload["market_view_expiry"]["expired"] is False
    assert payload["decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:29:00+00:00"
    assert payload["decision_snapshot"]["decision_source"] == "implicit_per_bar_no_go"
    assert payload["decision_snapshot"]["final_decision"] == "no_go"
    assert payload["decision_snapshot"]["as_of_contract"]["decision_contract"] == "one_go_or_no_go_decision_per_closed_candle"
    assert payload["decision_snapshot"]["as_of_contract"]["implicit_no_go_when_no_trigger"] is True
    assert payload["decision_snapshot"]["indicators"]["ema20"] is not None
    assert payload["latest_explicit_decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:20:00+00:00"
    assert payload["latest_explicit_decision_snapshot"]["execution_plan"]["take_profit"] == 3980
    assert payload["latest_go_decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:20:00+00:00"
    assert payload["latest_go_decision_snapshot"]["execution_plan"]["stop_loss"] == 4030
    assert len(payload["nearby_decision_snapshots"]) == 1
    assert payload["timeframes"]["1m"]["last_timestamp"] == "2026-06-25T00:28:00+00:00"
    assert payload["timeframes"]["1m"]["future_bar_count"] == 0
    assert payload["timeframes"]["1m"]["bars"][-1]["is_partial"] is False
    assert payload["timeframes"]["5m"]["derived"] is True
    assert payload["timeframes"]["5m"]["bars"][-1]["timestamp"] == "2026-06-25T00:20:00+00:00"
    assert payload["timeframes"]["5m"]["bars"][-1]["bucket_end"] == "2026-06-25T00:25:00+00:00"
    assert payload["timeframes"]["5m"]["bars"][-1]["is_partial"] is False
    assert payload["timeframes"]["15m"]["derived"] is True
    assert payload["timeframes"]["15m"]["bars"][-1]["timestamp"] == "2026-06-25T00:00:00+00:00"
    assert payload["timeframes"]["15m"]["bars"][-1]["is_partial"] is False
    for chart in payload["timeframes"].values():
        assert chart["no_future_bars"] is True
        assert all(item["bucket_end"] <= payload["cursor"] for item in chart["bars"] if item.get("bucket_end"))


def test_replay_state_adapts_window_to_historical_cursor_without_showing_later_days(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    store = MarketStore(db_path)
    store.upsert_bars(_minute_bars("2026-06-23T12:00:00+00:00", 181))
    store.upsert_bars(_minute_bars("2026-06-25T12:00:00+00:00", 60))

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-26",
        cursor="2026-06-23T14:00:00+00:00",
        strategy_id="gold_1m_grid",
        timeframes=["1m", "5m", "15m"],
        limit=120,
    )

    one_minute = payload["timeframes"]["1m"]
    assert payload["run_date"] == "2026-06-26"
    assert payload["cursor_date"] == "2026-06-23"
    assert one_minute["bars"][0]["timestamp"] == "2026-06-23T12:00:00+00:00"
    assert one_minute["last_timestamp"] == "2026-06-23T13:59:00+00:00"
    assert one_minute["future_bar_count"] == 0
    assert all(item["bucket_end"] <= payload["cursor"] for item in one_minute["bars"])
    assert not any(item["timestamp"].startswith("2026-06-25") for item in one_minute["bars"])

    five = payload["timeframes"]["5m"]
    fifteen = payload["timeframes"]["15m"]
    assert five["derived"] is True
    assert fifteen["derived"] is True
    assert five["bars"][-1]["timestamp"] == "2026-06-23T13:55:00+00:00"
    assert five["bars"][-1]["bucket_end"] == "2026-06-23T14:00:00+00:00"
    assert fifteen["bars"][-1]["timestamp"] == "2026-06-23T13:45:00+00:00"
    assert fifteen["bars"][-1]["bucket_end"] == "2026-06-23T14:00:00+00:00"
    assert five["bars"][-1]["is_partial"] is False
    assert fifteen["bars"][-1]["is_partial"] is False


def test_replay_state_exposes_strategy_timeframe_for_replay_clock_copy(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 40))

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:29:00+00:00",
        strategy_id="gold_5m_v1",
        timeframes=["1m", "5m"],
    )

    assert payload["strategy_timeframe"] == "5m"
    assert payload["as_of_contract"]["strategy_timeframe"] == "5m"
    assert payload["as_of_contract"]["decision_clock_timeframe"] == "1m"
    assert "non-1m strategies" in payload["as_of_contract"]["strategy_decision_note"]


def test_replay_state_selects_explicit_snapshot_by_generated_at_cursor(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_1m_grid" / "decision_snapshots" / "2026-06-25.json",
        [
            {
                "strategy_id": "gold_1m_grid",
                "signal_id": "sig_grid_1",
                "bar_timestamp": "2026-06-25T00:12:00+00:00",
                "generated_at": "2026-06-25T00:14:07+00:00",
                "final_decision": "no_go",
                "no_go_reason": "strategy guardrails blocked new paper exposure",
                "signal": {"direction": "short", "strength": 70, "confidence": 62},
                "execution_plan": {"ticket_id": "ticket_1", "entry_zone": "4000-4010", "take_profit": 3920, "stop_loss": 4050},
                "risk_block": {"reason": "strategy guardrails blocked new paper exposure"},
            }
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:14:07+00:00",
        strategy_id="gold_1m_grid",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "explicit_snapshot"
    assert payload["decision_snapshot"]["signal_id"] == "sig_grid_1"
    assert payload["decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:12:00+00:00"
    assert payload["decision_snapshot"]["generated_at"] == "2026-06-25T00:14:07+00:00"
    assert payload["decision_snapshot"]["risk_block"]["reason"] == "strategy guardrails blocked new paper exposure"


def test_replay_state_exposes_latest_go_even_when_latest_explicit_is_no_go(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 90))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-25.json",
        [
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:20:00+00:00",
                "final_decision": "go",
                "signal": {"direction": "short", "confidence": 61},
                "execution_plan": {"entry_zone": "4019-4021", "take_profit": 3980, "stop_loss": 4030},
            },
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:50:00+00:00",
                "final_decision": "no_go",
                "signal": {"direction": "watch", "confidence": 0},
                "no_go_reason": "signal is not directional",
            },
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T01:10:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "implicit_per_bar_no_go"
    assert payload["latest_explicit_decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:50:00+00:00"
    assert payload["latest_explicit_decision_snapshot"]["final_decision"] == "no_go"
    assert payload["latest_go_decision_snapshot"]["bar_timestamp"] == "2026-06-25T00:20:00+00:00"
    assert payload["latest_go_decision_snapshot"]["execution_plan"]["take_profit"] == 3980


def test_replay_state_does_not_leak_native_higher_timeframe_partial_bar(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    store = MarketStore(db_path)
    store.upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 8))
    store.upsert_bars([
        Bar(
            symbol="GOLD",
            timeframe="5m",
            timestamp="2026-06-25T00:05:00+00:00",
            open=9000,
            high=9999,
            low=8990,
            close=9999,
            volume=999,
            provider="native_5m_future_leak",
            quality_flags=["native_complete_bar_should_not_be_used"],
        )
    ])

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:06:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m", "5m"],
    )

    five = payload["timeframes"]["5m"]
    last = five["bars"][-1]
    assert five["derived"] is True
    assert five["source_timeframe"] == "1m"
    assert last["timestamp"] == "2026-06-25T00:00:00+00:00"
    assert last["bucket_end"] == "2026-06-25T00:05:00+00:00"
    assert last["is_partial"] is False
    assert last["provider"] != "native_5m_future_leak"
    assert last["close"] == 4005
    assert last["high"] == 4006
    assert last["low"] == 3998


def test_replay_state_uses_exact_explicit_snapshot_when_cursor_matches(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-25.json",
        [{
            "strategy_id": "gold_1m_chan",
            "bar_timestamp": "2026-06-25T00:20:00+00:00",
            "final_decision": "go",
            "signal": {"direction": "short", "confidence": 61},
            "execution_plan": {"entry_zone": "4019-4021", "take_profit": 3980, "stop_loss": 4030},
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "explicit_snapshot"
    assert payload["decision_snapshot"]["final_decision"] == "go"
    assert payload["decision_snapshot"]["execution_plan"]["stop_loss"] == 4030


def test_replay_state_enriches_legacy_candidate_rejection_with_backtest_gate(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_5m_v1" / "decision_snapshots" / "2026-06-25.json",
        [{
            "strategy_id": "gold_5m_v1",
            "signal_id": "sig_legacy_candidate",
            "bar_timestamp": "2026-06-25T00:20:00+00:00",
            "final_decision": "no_go",
            "no_go_reason": "signal did not meet risk engine thresholds",
            "signal": {"direction": "long", "strength": 63, "confidence": 67},
            "execution_plan": {"ticket_id": "", "entry_zone": ""},
            "risk_block": {},
        }],
    )
    write_json(
        output_root / "strategies" / "gold_5m_v1" / "backtests" / "2026-06-25.json",
        [
            {
                "signal_id": "sig_other_candidate",
                "asset": "GOLD",
                "sample_size": 3,
                "verdict": "supportive",
            },
            {
                "signal_id": "sig_legacy_candidate",
                "asset": "GOLD",
                "sample_size": 211,
                "verdict": "thin",
            },
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:00+00:00",
        strategy_id="gold_5m_v1",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "explicit_snapshot"
    assert payload["decision_snapshot"]["risk_block"] == {}
    context = payload["decision_snapshot"]["backtest_context"]
    assert context["verdict"] == "thin"
    assert context["sample_size"] == 211
    assert context["blocking"] is False
    assert context["diagnostic_source"] == "replay_read_time_enrichment"


def test_replay_state_uses_run_date_artifacts_when_cursor_is_previous_utc_day(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_5m_v1" / "decision_snapshots" / "2026-06-26.json",
        [{
            "run_date": "2026-06-26",
            "strategy_id": "gold_5m_v1",
            "signal_id": "sig_cross_date_candidate",
            "generated_at": "2026-06-25T00:20:33+00:00",
            "bar_timestamp": "2026-06-25T00:20:00+00:00",
            "final_decision": "no_go",
            "no_go_reason": "signal did not meet risk engine thresholds",
            "signal": {"direction": "long", "strength": 63, "confidence": 67},
            "execution_plan": {"ticket_id": "", "entry_zone": ""},
            "risk_block": {},
        }],
    )
    write_json(
        output_root / "strategies" / "gold_5m_v1" / "backtests" / "2026-06-26.json",
        [{"signal_id": "sig_cross_date_candidate", "asset": "GOLD", "sample_size": 208, "verdict": "thin"}],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-26",
        cursor="2026-06-25T00:20:33+00:00",
        strategy_id="gold_5m_v1",
        timeframes=["1m"],
    )

    snap = payload["decision_snapshot"]
    assert payload["run_date"] == "2026-06-26"
    assert payload["cursor_date"] == "2026-06-25"
    assert snap["decision_source"] == "explicit_snapshot"
    assert snap["signal_id"] == "sig_cross_date_candidate"
    assert snap["risk_block"] == {}
    assert snap["backtest_context"]["verdict"] == "thin"
    assert snap["backtest_context"]["blocking"] is False
    assert "2026-06-26" in payload["as_of_contract"]["artifact_dates_considered"]


def test_replay_state_focuses_trade_marker_when_decision_snapshot_missing(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_1m_grid" / "paper_trades" / "closed" / "2026-06-25.json",
        [{
            "trade_id": "trade_1",
            "ticket_id": "ticket_1",
            "strategy_id": "gold_1m_grid",
            "symbol": "GOLD",
            "side": "long",
            "status": "closed",
            "quantity": 0.1,
            "entry_price": 4021,
            "opened_at": "2026-06-25T00:20:35+00:00",
            "stop_loss": 3990,
            "target": 4080,
            "closed_at": "2026-06-25T00:24:00+00:00",
            "exit_price": 3990,
            "exit_reason": "stop_loss",
            "realized_pnl": -3.1,
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:00+00:00",
        strategy_id="gold_1m_grid",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "implicit_per_bar_no_go"
    assert payload["focused_trade_marker"]["trade_id"] == "trade_1"
    assert payload["focused_trade_marker"]["entry_price"] == 4021
    assert payload["focused_trade_marker"]["take_profit"] == 4080
    assert payload["focused_trade_marker"]["stop_loss"] == 3990


def test_replay_state_prefers_requested_trade_marker_over_nearby_guess(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 50))
    write_json(
        output_root / "strategies" / "gold_1m_grid" / "paper_trades" / "closed" / "2026-06-25.json",
        [
            {
                "trade_id": "trade_requested",
                "ticket_id": "ticket_requested",
                "order_id": "order_requested",
                "strategy_id": "gold_1m_grid",
                "symbol": "GOLD",
                "side": "long",
                "status": "closed",
                "quantity": 0.1,
                "entry_price": 4020,
                "opened_at": "2026-06-25T00:10:00+00:00",
                "stop_loss": 3990,
                "target": 4070,
                "closed_at": "2026-06-25T00:40:00+00:00",
                "exit_price": 4060,
                "exit_reason": "take_profit",
                "realized_pnl": 4.0,
            },
            {
                "trade_id": "trade_newer_active",
                "ticket_id": "ticket_newer_active",
                "strategy_id": "gold_1m_grid",
                "symbol": "GOLD",
                "side": "short",
                "status": "closed",
                "quantity": 0.1,
                "entry_price": 4015,
                "opened_at": "2026-06-25T00:24:00+00:00",
                "stop_loss": 4035,
                "target": 3975,
                "closed_at": "2026-06-25T00:38:00+00:00",
                "exit_price": 3985,
                "exit_reason": "take_profit",
                "realized_pnl": 3.0,
            },
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:30:00+00:00",
        strategy_id="gold_1m_grid",
        timeframes=["1m"],
        trade_id="ticket_requested",
    )

    assert payload["requested_trade_id"] == "ticket_requested"
    assert payload["focused_trade_marker"]["trade_id"] == "trade_requested"
    assert payload["focused_trade_marker"]["ticket_id"] == "ticket_requested"
    assert payload["focused_trade_marker"]["order_id"] == "order_requested"


def test_replay_state_links_focused_trade_back_to_entry_go_snapshot(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 40))
    write_json(
        output_root / "strategies" / "gold_1m_chan_ungated" / "decision_snapshots" / "2026-06-26.json",
        [
            {
                "run_date": "2026-06-26",
                "strategy_id": "gold_1m_chan_ungated",
                "signal_id": "sig_trade_linked",
                "bar_timestamp": "2026-06-25T00:20:00+00:00",
                "generated_at": "2026-06-25T00:20:34+00:00",
                "final_decision": "go",
                "signal": {"direction": "short", "confidence": 72},
                "execution_plan": {
                    "ticket_id": "ticket_trade_linked",
                    "entry_zone": "4019-4021",
                    "take_profit": 3980,
                    "stop_loss": 4030,
                },
            }
        ],
    )
    write_json(
        output_root / "strategies" / "gold_1m_chan_ungated" / "paper_trades" / "current.json",
        [{
            "trade_id": "trade_linked",
            "ticket_id": "ticket_trade_linked",
            "signal_id": "sig_trade_linked",
            "strategy_id": "gold_1m_chan_ungated",
            "symbol": "GOLD",
            "side": "short",
            "status": "open",
            "quantity": 0.1,
            "entry_price": 4020,
            "opened_at": "2026-06-25T00:20:35+00:00",
            "stop_loss": 4030,
            "target": 3980,
            "entry_reason": "linked GO decision should be recoverable from replay",
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:35+00:00",
        strategy_id="gold_1m_chan_ungated",
        timeframes=["1m"],
    )

    linked = payload["focused_trade_entry_decision_snapshot"]
    assert payload["decision_snapshot"]["decision_source"] == "implicit_per_bar_no_go"
    assert payload["focused_trade_marker"]["trade_id"] == "trade_linked"
    assert payload["focused_trade_marker"]["signal_id"] == "sig_trade_linked"
    assert linked["decision_source"] == "focused_trade_entry_decision"
    assert linked["final_decision"] == "go"
    assert linked["signal_id"] == "sig_trade_linked"
    assert linked["execution_plan"]["ticket_id"] == "ticket_trade_linked"
    assert linked["execution_plan"]["take_profit"] == 3980


def test_replay_state_prefers_go_when_same_bar_has_conflicting_decisions(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-25.json",
        [
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:20:00+00:00",
                "generated_at": "2026-06-25T00:20:02+00:00",
                "final_decision": "go",
                "signal": {"direction": "short", "confidence": 61},
                "execution_plan": {"entry_zone": "4019-4021", "take_profit": 3980, "stop_loss": 4030},
            },
            {
                "strategy_id": "gold_1m_chan",
                "bar_timestamp": "2026-06-25T00:20:00+00:00",
                "generated_at": "2026-06-25T00:20:03+00:00",
                "final_decision": "no_go",
                "signal": {"direction": "watch", "confidence": 0},
                "execution_plan": {},
            },
        ],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["decision_snapshot"]["decision_source"] == "explicit_snapshot"
    assert payload["decision_snapshot"]["final_decision"] == "go"
    assert payload["decision_snapshot"]["execution_plan"]["take_profit"] == 3980
    assert payload["latest_explicit_decision_snapshot"]["final_decision"] == "go"


def test_replay_state_prefers_cursor_date_artifacts_for_point_in_time_replay(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-24T00:00:00+00:00", 40))
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 10))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-24.json",
        [{
            "strategy_id": "gold_1m_chan",
            "bar_timestamp": "2026-06-24T00:20:00+00:00",
            "final_decision": "go",
            "signal": {"direction": "short", "confidence": 66},
            "execution_plan": {"entry_zone": "4019-4021", "take_profit": 3980, "stop_loss": 4030},
        }],
    )
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "decision_snapshots" / "2026-06-25.json",
        [{
            "strategy_id": "gold_1m_chan",
            "bar_timestamp": "2026-06-25T00:05:00+00:00",
            "final_decision": "no_go",
            "signal": {"direction": "watch", "confidence": 0},
        }],
    )
    write_json(
        output_root / "market_views" / "2026-06-24.json",
        [{
            "run_date": "2026-06-24",
            "generated_at": "2026-06-24T00:10:00+00:00",
            "direction_bias": "strong_short",
            "summary": "cursor-date bearish view",
        }],
    )
    write_json(
        output_root / "market_views" / "2026-06-25.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "direction_bias": "strong_long",
            "summary": "future view must not leak into 06-24 replay",
        }],
    )
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "paper_trades" / "closed" / "2026-06-24.json",
        [{
            "trade_id": "trade_cursor_date",
            "ticket_id": "ticket_cursor_date",
            "strategy_id": "gold_1m_chan",
            "side": "short",
            "status": "closed",
            "opened_at": "2026-06-24T00:19:00+00:00",
            "closed_at": "2026-06-24T00:30:00+00:00",
            "entry_price": 4019,
            "exit_price": 3980,
            "target": 3980,
            "stop_loss": 4030,
            "realized_pnl": 39,
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-24T00:20:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["run_date"] == "2026-06-25"
    assert payload["cursor_date"] == "2026-06-24"
    assert payload["as_of_contract"]["artifact_dates_considered"] == ["2026-06-24", "2026-06-25"]
    assert payload["decision_snapshot"]["decision_source"] == "explicit_snapshot"
    assert payload["decision_snapshot"]["final_decision"] == "go"
    assert payload["decision_snapshot"]["execution_plan"]["take_profit"] == 3980
    assert payload["market_view"]["direction_bias"] == "strong_short"
    assert payload["trade_markers"][0]["trade_id"] == "trade_cursor_date"


def test_replay_state_trade_markers_hide_future_exit(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 20))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "paper_trades" / "closed" / "2026-06-25.json",
        [{
            "trade_id": "trade_1",
            "ticket_id": "ticket_1",
            "strategy_id": "gold_1m_chan",
            "side": "short",
            "status": "closed",
            "quantity": 1,
            "opened_at": "2026-06-25T00:05:00+00:00",
            "closed_at": "2026-06-25T00:18:00+00:00",
            "entry_price": 4005,
            "exit_price": 3990,
            "target": 3990,
            "stop_loss": 4015,
            "realized_pnl": 15,
            "unrealized_pnl": 999,
            "gross_unrealized_pnl": 999,
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:10:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["trade_markers"][0]["opened_at"] == "2026-06-25T00:05:00+00:00"
    assert payload["trade_markers"][0]["closed_at"] == ""
    assert payload["trade_markers"][0]["exit_price"] is None
    assert payload["trade_markers"][0]["take_profit"] == 3990
    assert payload["trade_markers"][0]["stop_loss"] == 4015
    assert payload["trade_markers"][0]["record_card"]["status"] == "open"
    assert payload["trade_markers"][0]["record_card"]["exit"]["reason"] == "holding_open_position"
    assert payload["trade_markers"][0]["record_card"]["exit"]["price"] is None
    assert payload["trade_markers"][0]["record_card"]["pnl"]["realized_pnl"] is None
    assert payload["trade_markers"][0]["record_card"]["pnl"]["unrealized_pnl"] == -5.0
    assert payload["trade_markers"][0]["record_card"]["pnl"]["hand_check"]["status"] == "pass"
    assert payload["trade_markers"][0]["record_card"]["pnl"]["hand_check"]["reported_net_pnl"] == -5.0


def test_replay_state_trade_record_card_flags_protection_missing(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 20))
    write_json(
        output_root / "strategies" / "gold_1m_chan" / "paper_trades" / "current.json",
        [{
            "trade_id": "trade_naked",
            "ticket_id": "ticket_naked",
            "strategy_id": "gold_1m_chan",
            "side": "long",
            "status": "open",
            "opened_at": "2026-06-25T00:05:00+00:00",
            "entry_price": 4005,
            "quantity": 1,
            "target": None,
            "stop_loss": None,
            "protective_order_missing": True,
            "quality_flags": ["protective_order_missing"],
            "entry_reason": "destructive test naked entry",
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:10:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    card = payload["trade_markers"][0]["record_card"]
    assert card["trade_id"] == "trade_naked"
    assert card["protection"]["status"] == "missing"
    assert card["protection"]["alert_required"] is True
    assert card["compliance"]["verdict"] == "fail"
    assert card["audit"]["status"] == "incomplete"
    assert card["audit"]["red_line_passed"] is False


def test_replay_state_marks_market_view_expired_as_of_cursor(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 50))
    write_json(
        output_root / "market_views" / "2026-06-25.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "summary": "只做空，但只对早盘有效。",
            "expiry": {"expires_at": "2026-06-25T00:20:00+00:00", "valid_for_hours": 1},
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:30:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["market_view_expiry"]["status"] == "expired"
    assert payload["market_view_expiry"]["expired"] is True
    assert payload["market_view_expiry"]["filter_effect"] == "expired_direction_filter_disabled"
    assert "不再按这条观点过滤多空信号" in payload["market_view_expiry"]["operator_message"]
    assert "time expired" in payload["market_view_expiry"]["reason"]


def test_replay_state_exposes_default_price_move_expiry_for_legacy_market_view(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 10))
    write_json(
        output_root / "market_views" / "2026-06-25.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:00:00+00:00",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "summary": "只做空，legacy artifact 没写 expiry。",
            "reference_price": 4000,
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:04:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["market_view_expiry"]["expires_if_price_moves_pct"] == 1.0
    assert payload["market_view_expiry"]["reference_price"] == 4000
    assert payload["market_view_expiry"]["price_expiry_ready"] is True
    assert payload["market_view_expiry"]["filter_effect"] == "active_direction_filter_enabled"


def test_replay_state_infers_reference_price_for_legacy_market_view(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars([
        Bar("GOLD", "1m", "2026-06-25T00:00:00+00:00", 3999, 4002, 3998, 4000, 1, "binance_usdm", []),
        Bar("GOLD", "1m", "2026-06-25T00:01:00+00:00", 4000, 4003, 3999, 4001, 1, "binance_usdm", []),
        Bar("GOLD", "1m", "2026-06-25T00:02:00+00:00", 4001, 4002, 3949, 3950, 1, "binance_usdm", []),
    ])
    write_json(
        output_root / "market_views" / "2026-06-25.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:01:30+00:00",
            "direction_score": 10,
            "direction_bias": "strong_short",
            "summary": "4000 附近只做空，跌太多后重评估。legacy artifact 没写 reference_price。",
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:03:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    assert payload["market_view_expiry"]["reference_price"] == 4001
    assert payload["market_view_expiry"]["price_expiry_ready"] is True
    assert payload["market_view_expiry"]["status"] == "expired"
    assert "price moved" in payload["market_view_expiry"]["reason"]


def test_replay_state_exposes_pm_evidence_for_current_strategy(tmp_path: Path):
    output_root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    MarketStore(db_path).upsert_bars(_minute_bars("2026-06-25T00:00:00+00:00", 30))
    write_json(
        output_root / "backend_maturity" / "current.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:20:00+00:00",
            "status": "pass",
            "summary": {"platform_status": "pass", "pm_allocation_status": "locked"},
            "checks": [
                {"name": "M1_trade_record_cards", "status": "pass", "summary": "records complete"},
                {"name": "M2_strategy_books", "status": "pass", "summary": "books reconcile"},
                {
                    "name": "M3_edge_judgment",
                    "status": "pass",
                    "summary": "closed trades only",
                    "evidence": {"labels": {"gold_1m_chan": "insufficient_sample"}},
                },
                {"name": "M4_frequency_attribution", "status": "pass", "summary": "frequency attributed"},
                {"name": "M5_execution_safety", "status": "pass", "summary": "protected and reconciled"},
            ],
        }],
    )
    write_json(
        output_root / "strategy_frequency" / "current.json",
        [{
            "run_date": "2026-06-25",
            "generated_at": "2026-06-25T00:20:00+00:00",
            "status": "within_portfolio_sample_target",
            "strategies": [
                {
                    "strategy_id": "gold_1m_chan",
                    "stage": "low_volume",
                    "stage_label": "成交不足",
                    "reason": "executed 1/2 trades today",
                    "primary_reason": "low_volume",
                    "limiting_reason": "low_volume",
                    "executed_trade_count": 1,
                    "min_daily_executed_trades": 2,
                    "signal_count": 8,
                    "candidate_count": 2,
                    "ticket_count": 1,
                    "recommendation": {"action": "keep_running"},
                }
            ],
        }],
    )

    payload = ReplayState(output_root=output_root, market_db=db_path).snapshot(
        "2026-06-25",
        cursor="2026-06-25T00:20:00+00:00",
        strategy_id="gold_1m_chan",
        timeframes=["1m"],
    )

    evidence = payload["pm_evidence"]
    assert evidence["schema_version"] == "replay-pm-evidence-v1"
    assert evidence["platform_status"] == "pass"
    assert evidence["pm_allocation_status"] == "locked"
    assert evidence["edge_label"] == "insufficient_sample"
    assert evidence["maturity_checks"]["M1_trade_record_cards"]["summary"] == "records complete"
    assert evidence["maturity_checks"]["M5_execution_safety"]["status"] == "pass"
    assert evidence["strategy_frequency"]["stage"] == "low_volume"
    assert evidence["strategy_frequency"]["executed_trade_count"] == 1
    assert evidence["strategy_frequency"]["min_daily_executed_trades"] == 2
