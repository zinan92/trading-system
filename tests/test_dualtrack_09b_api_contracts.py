from __future__ import annotations

from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
from services.journal_store import load_json, write_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _entry_fill(*, cycle_id: str, track: str, side: str = "buy", price: float = 100.0, units: float = 2.0) -> dict:
    return {
        "fill_id": f"{cycle_id}_{track}_entry_1",
        "trade_id": f"{cycle_id}_{track}_trade_1",
        "track": track,
        "event": "entry",
        "ts": "2026-07-05T01:10:00+00:00",
        "side": side,
        "price": price,
        "sl": 95.0 if side == "buy" else 105.0,
        "tp": 110.0 if side == "buy" else 90.0,
        "pnl_units": units,
        "remaining_units": units,
        "realized_pnl": -0.1,
        "position_status": "open",
        "position_id": f"{track}-manual",
    }


class FakeFreshMarketFeed:
    def __init__(self, *args, **kwargs):
        pass

    def snapshot(self, *args, **kwargs):
        return {
            "schema_version": "dualtrack-market-bars-v1",
            "status": "ready",
            "symbol": "GOLD",
            "timeframe": "1m",
            "provider": "test",
            "source_mode": "requested_symbol",
            "fresh": True,
            "latest_close": 105.0,
            "bars": [{"timestamp": "2026-07-05T02:00:00+00:00", "close": 105.0}],
        }


def test_dualtrack_mutations_require_same_local_origin() -> None:
    allowed = dashboard_server._dualtrack_mutation_request_allowed

    assert allowed("127.0.0.1:8765", "http://127.0.0.1:8765") is True
    assert allowed("localhost:8765", "http://localhost:8765") is True
    assert allowed("127.0.0.1:8765", "") is True
    assert allowed("127.0.0.1:8765", "https://evil.example") is False
    assert allowed("127.0.0.1:8765", "null") is False
    assert allowed("trading.example:8765", "http://trading.example:8765") is False


def test_network_order_gate_uses_server_clock_and_server_mark_for_market_exit() -> None:
    payload = dashboard_server._prepare_dualtrack_network_order(
        {
            "cycle_id": "2026-07-05_DAY",
            "ts": "1999-01-01T00:00:00+00:00",
            "side": "sell",
            "event": "exit",
            "order_type": "market",
            "price": 9999.0,
        },
        market={
            "status": "ready",
            "source_mode": "requested_symbol",
            "fresh": True,
            "is_synthetic": False,
            "provider": "binance_usdm",
            "latest_timestamp": "2026-07-05T01:59:00+00:00",
            "latest_close": 105.0,
        },
        received_at="2026-07-05T02:00:00+00:00",
        expected_provider="binance_usdm",
    )

    assert payload["ts"] == "2026-07-05T02:00:00+00:00"
    assert payload["price"] == 105.0
    assert payload["market_price"] == 105.0
    assert payload["market_timestamp"] == "2026-07-05T01:59:00+00:00"
    assert payload["market_source"] == "binance_usdm"


def test_network_order_gate_accepts_configured_binance_source_mode() -> None:
    prepared = dashboard_server._prepare_dualtrack_network_order(
        {
            "cycle_id": "2026-07-05_DAY",
            "side": "sell",
            "event": "exit",
            "order_type": "market",
        },
        market={
            "status": "ready",
            "source_mode": "binance_usdm",
            "fresh": True,
            "is_synthetic": False,
            "provider": "binance_usdm",
            "latest_timestamp": "2026-07-05T01:59:00+00:00",
            "latest_close": 105.0,
        },
        received_at="2026-07-05T02:00:00+00:00",
        expected_provider="binance_usdm",
    )

    assert prepared["market_source"] == "binance_usdm"
    assert prepared["price"] == 105.0


@pytest.mark.parametrize(
    ("market", "message"),
    [
        ({"status": "stale", "fresh": False, "is_synthetic": False}, "server market data is stale"),
        ({"status": "seeded", "fresh": False, "is_synthetic": True}, "synthetic market data is forbidden"),
        ({"status": "ready", "fresh": True, "is_synthetic": False, "source_mode": "fallback"}, "server market source is not canonical"),
    ],
)
def test_network_order_gate_fails_closed_on_untrusted_market(market: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dashboard_server._prepare_dualtrack_network_order(
            {
                "cycle_id": "2026-07-05_DAY",
                "side": "buy",
                "event": "entry",
                "order_type": "limit",
                "price": 100.0,
                "notional": 1000.0,
                "sl": 95.0,
                "tp": 110.0,
            },
            market=market,
            received_at="2026-07-05T02:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("market", "expected_provider", "message"),
    [
        (
            {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "source_mode": "requested_symbol",
                "provider": "wrong_provider",
                "latest_timestamp": "2026-07-05T01:59:00+00:00",
                "latest_close": 105.0,
            },
            "binance_usdm",
            "server market provider is not canonical",
        ),
        (
            {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "source_mode": "requested_symbol",
                "provider": "binance_usdm",
                "latest_timestamp": "2026-07-05T02:02:00+00:00",
                "latest_close": 105.0,
            },
            "binance_usdm",
            "server market timestamp is in the future",
        ),
        (
            {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "source_mode": "requested_symbol",
                "provider": "binance_usdm",
                "latest_timestamp": "2026-07-05T00:59:59+00:00",
                "latest_close": 105.0,
            },
            "binance_usdm",
            "server market timestamp is outside the current cycle",
        ),
    ],
)
def test_network_order_gate_rejects_wrong_provider_or_timestamp(
    market: dict,
    expected_provider: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        dashboard_server._prepare_dualtrack_network_order(
            {
                "cycle_id": "2026-07-05_DAY",
                "side": "buy",
                "event": "entry",
                "order_type": "limit",
                "price": 100.0,
                "notional": 1000.0,
                "sl": 95.0,
                "tp": 110.0,
            },
            market=market,
            received_at="2026-07-05T02:00:00+00:00",
            expected_provider=expected_provider,
        )


def test_dualtrack_config_endpoint_exposes_display_config_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_server, "dualtrack_config", lambda: {**TEST_CONFIG, "broker_secret": "do-not-leak"})

    payload = dashboard_server.build_dualtrack_config_response()

    assert payload["schema_version"] == "dualtrack-config-v1"
    assert payload["max_leverage"] == TEST_CONFIG["max_leverage"]
    assert payload["cycle_cadence"]["decision_hours"] == 12
    assert payload["cycle_cadence"]["windows_cst"] == ["09:00-21:00", "21:00-09:00"]
    assert payload["cycle_cadence"]["accounting_day_hours"] == 24
    assert payload["machine_rules"]["range_reassessment"]["confirm_closes"] == 3
    assert payload["machine_rules"]["range_reassessment"]["confirmation_timeframe"] == "1m"
    assert payload["machine_rules"]["untrusted_market_action"] == "stand_down"
    assert payload["safety"]["read_only"] is True
    assert "broker_secret" not in payload
    assert "secret" not in str(payload).lower()


def test_order_post_response_routes_through_execution_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class FakeAdapter:
        def submit_order(self, payload: dict) -> dict:
            captured.update(payload)
            return {"fill_id": "adapter-fill", "event": "entry"}

    monkeypatch.setattr(dashboard_server, "build_execution_engine_adapter", lambda output_root: FakeAdapter())

    response = dashboard_server.build_dualtrack_order_post_response(
        {"cycle_id": "2026-07-05_DAY", "side": "buy"},
        output_root=tmp_path / "outputs",
    )

    assert captured["side"] == "buy"
    assert response == {"status": "filled", "fill": {"fill_id": "adapter-fill", "event": "entry"}}


def test_human_trades_endpoint_returns_order_rows_with_unrealized_when_mark_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_human.json", [_entry_fill(cycle_id=cycle_id, track="human")])
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="human",
        output_root=output,
        as_of="2026-07-05T02:00:00+00:00",
    )

    assert payload["schema_version"] == "dualtrack-trades-v1"
    assert payload["track"] == "human"
    assert payload["blind"] is False
    assert payload["mark_price"] == 105.0
    assert payload["mark_fresh"] is True
    assert len(payload["trades"]) == 1
    assert payload["trades"][0]["status"] == "open"
    assert payload["trades"][0]["unrealized_pnl"] == 10.0
    assert payload["trades"][0]["sl"] == 95.0
    assert payload["trades"][0]["tp"] == 110.0


def test_human_manual_close_payload_can_exit_open_position_without_notional(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    entry = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:10:00+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "market",
            "price": 4108.8,
            "notional": 2000.0,
            "sl": 4111.9,
            "tp": 4104.1,
            "position_id": "manual-short",
            "source": "split_canvas",
        },
        output_root=output,
    )

    exit_fill = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:20:00+00:00",
            "side": "buy",
            "event": "exit",
            "order_type": "market",
            "price": 4107.8,
            "trade_id": entry["fill"]["trade_id"],
            "position_id": "manual-short",
            "source": "split_canvas_manual_close",
        },
        output_root=output,
    )

    assert exit_fill["status"] == "filled"
    assert exit_fill["fill"]["event"] == "exit"
    assert exit_fill["fill"]["notional"] > 0
    assert exit_fill["fill"]["source"] == "split_canvas_manual_close"
    trades = dashboard_server.build_dualtrack_trades_response(cycle_id, track="human", output_root=output)
    assert trades["trades"][0]["status"] == "closed"
    assert trades["trades"][0]["remaining_units"] == 0.0
    ledger = dashboard_server.build_dualtrack_ledger_response(output_root=output)
    daily = next(row for row in ledger["daily"] if row["date"] == "2026-07-05")
    recorded = sum(float(fill.get("realized_pnl") or 0.0) for fill in load_json(output / "dualtrack" / "fills" / f"{cycle_id}_human.json"))
    assert daily["cycles"][cycle_id]["human"] == pytest.approx(recorded)


def test_human_manual_close_targets_the_position_origin_cycle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    day_cycle = "2026-07-05_DAY"
    night_cycle = "2026-07-05_NIGHT"
    entry = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": day_cycle,
            "ts": "2026-07-05T12:50:00+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "market",
            "price": 100.0,
            "notional": 1000.0,
            "sl": 105.0,
            "tp": 90.0,
            "position_id": "carried-short",
        },
        output_root=output,
    )

    closed = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": night_cycle,
            "position_cycle_id": day_cycle,
            "ts": "2026-07-05T13:10:00+00:00",
            "side": "buy",
            "event": "exit",
            "order_type": "market",
            "price": 99.0,
            "trade_id": entry["fill"]["trade_id"],
            "position_id": "carried-short",
        },
        output_root=output,
    )

    assert closed["status"] == "filled"
    assert closed["fill"]["cycle_id"] == day_cycle
    assert closed["fill"]["request_cycle_id"] == night_cycle
    assert closed["fill"]["event"] == "exit"
    ledger = dashboard_server.build_dualtrack_ledger_response(output_root=output)
    daily = next(row for row in ledger["daily"] if row["date"] == "2026-07-05")
    assert daily["cycles"][day_cycle]["human"] > 0


def test_human_trades_endpoint_is_read_only_and_ignores_client_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    entry = dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:10:00+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "market",
            "price": 100.0,
            "notional": 1000.0,
            "sl": 105.0,
            "tp": 90.0,
            "position_id": "manual-short",
            "source": "split_canvas",
        },
        output_root=output,
    )

    fills_path = output / "dualtrack" / "fills" / f"{cycle_id}_human.json"
    before = load_json(fills_path)
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="human",
        output_root=output,
        mark_price=106.0,
        mark_source="binance_websocket:test",
    )

    assert entry["fill"]["trade_id"]
    assert payload["mark_price"] == 105.0
    assert payload["mark_source"] == "requested_symbol"
    assert "protective_sweep" not in payload
    assert payload["trades"][0]["status"] == "open"
    assert payload["summary"]["open_trade_count"] == 1
    assert load_json(fills_path) == before


def test_human_payload_endpoint_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    dashboard_server.build_dualtrack_order_post_response(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:10:00+00:00",
            "side": "sell",
            "event": "entry",
            "order_type": "limit",
            "price": 100.0,
            "notional": 1000.0,
            "sl": 105.0,
            "tp": 90.0,
        },
        output_root=output,
    )
    fills_path = output / "dualtrack" / "fills" / f"{cycle_id}_human.json"
    before = load_json(fills_path)
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_human_response(
        cycle_id,
        output_root=output,
        mark_price=106.0,
        mark_source="browser_supplied",
    )

    assert "protective_sweep" not in payload
    assert load_json(fills_path) == before


def test_machine_trades_endpoint_returns_mid_cycle_order_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [_entry_fill(cycle_id=cycle_id, track="machine")])
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="machine",
        output_root=output,
        as_of="2026-07-05T02:00:00+00:00",
    )

    assert payload["track"] == "machine"
    assert payload["blind"] is False
    assert payload["safety"]["machine_mid_order_rows_hidden"] is False
    assert payload["trades"][0]["status"] == "open"
    assert payload["trades"][0]["unrealized_pnl"] == 10.0


def test_machine_trades_endpoint_excludes_recovery_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    replay_fill = _entry_fill(cycle_id=cycle_id, track="machine") | {
        "execution_origin": "recovery_replay",
        "realized_pnl": 12.5,
    }
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [replay_fill])
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="machine",
        output_root=output,
        as_of="2026-07-05T02:00:00+00:00",
    )

    assert payload["trades"] == []
    assert len(payload["recovery_replay_trades"]) == 1
    assert payload["recovery_replay_trades"][0]["execution_origin"] == "recovery_replay"
    assert payload["recovery_replay_summary"]["trade_count"] == 1
    assert payload["safety"]["recovery_replay_fill_count"] == 1
    assert payload["safety"]["recovery_replay_realized_pnl"] == 12.5
    assert payload["safety"]["recovery_replay_excluded_from_paper_pnl"] is True


def test_machine_trade_rows_infer_units_and_match_layer_rung_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(
        output / "dualtrack" / "fills" / f"{cycle_id}_machine.json",
        [
            {
                "fill_id": "grid_entry",
                "event": "entry",
                "track": "machine",
                "ts": "2026-07-05T01:10:00+00:00",
                "side": "buy",
                "price": 100.0,
                "notional": 1000.0,
                "layer": "grid",
                "rung": 0,
                "realized_pnl": -0.1,
            },
            {
                "fill_id": "grid_exit",
                "event": "target",
                "track": "machine",
                "ts": "2026-07-05T01:20:00+00:00",
                "side": "sell",
                "price": 105.0,
                "notional": 1050.0,
                "layer": "grid",
                "rung": 0,
                "realized_pnl": 49.9,
            },
        ],
    )
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="machine",
        output_root=output,
        as_of="2026-07-05T02:00:00+00:00",
    )

    trade = payload["trades"][0]
    assert trade["units"] == 10.0
    assert trade["remaining_units"] == 0.0
    assert trade["status"] == "closed"
    assert trade["exit_ts"] == "2026-07-05T01:20:00+00:00"
    assert trade["realized_pnl"] == 49.8
    assert payload["summary"]["realized_pnl"] == 49.8


def test_trade_summary_counts_realized_on_partially_open_trade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(
        output / "dualtrack" / "fills" / f"{cycle_id}_machine.json",
        [
            {
                "fill_id": "grid_entry",
                "event": "entry",
                "track": "machine",
                "ts": "2026-07-05T01:10:00+00:00",
                "side": "buy",
                "price": 100.0,
                "notional": 1000.0,
                "layer": "grid",
                "rung": 0,
                "remaining_units": 10.0,
                "realized_pnl": -0.1,
            },
            {
                "fill_id": "grid_exit",
                "event": "target",
                "track": "machine",
                "ts": "2026-07-05T01:20:00+00:00",
                "side": "sell",
                "price": 105.0,
                "notional": 945.0,
                "layer": "grid",
                "rung": 0,
                "matched_entries": [{
                    "trade_id": "grid_entry",
                    "units": 9.0,
                    "gross_pnl": 45.0,
                    "realized_pnl": 44.5,
                }],
                "realized_pnl": 44.5,
            },
        ],
    )
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="machine",
        output_root=output,
        as_of="2026-07-05T02:00:00+00:00",
    )

    assert payload["trades"][0]["status"] == "open"
    assert payload["summary"]["realized_pnl"] == 44.4


def test_trades_endpoint_uses_same_day_previous_cycle_for_display_when_current_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    day_cycle = "2026-07-05_DAY"
    night_cycle = "2026-07-05_NIGHT"
    write_json(
        output / "dualtrack" / "fills" / f"{day_cycle}_human.json",
        [_entry_fill(cycle_id=day_cycle, track="human")],
    )
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        night_cycle,
        track="human",
        output_root=output,
        as_of="2026-07-05T14:00:00+00:00",
    )

    assert payload["cycle_id"] == night_cycle
    assert payload["trades"] == []
    assert payload["display_cycle_id"] == day_cycle
    assert payload["display_reason"] == "latest_same_day"
    assert payload["display_summary"]["trade_count"] == 1
    assert payload["display_trades"][0]["source_cycle_id"] == day_cycle


def test_trades_endpoint_keeps_prior_open_position_when_current_cycle_has_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "outputs"
    day_cycle = "2026-07-05_DAY"
    night_cycle = "2026-07-05_NIGHT"
    write_json(
        output / "dualtrack" / "fills" / f"{day_cycle}_human.json",
        [_entry_fill(cycle_id=day_cycle, track="human")],
    )
    closed_entry = _entry_fill(cycle_id=night_cycle, track="human")
    closed_entry["trade_id"] = "night_closed_trade"
    closed_entry["position_id"] = "night_closed"
    write_json(
        output / "dualtrack" / "fills" / f"{night_cycle}_human.json",
        [
            closed_entry,
            {
                "fill_id": "night_exit",
                "cycle_id": night_cycle,
                "track": "human",
                "event": "exit",
                "side": "sell",
                "price": 101.0,
                "pnl_units": 2.0,
                "realized_pnl": 1.0,
                "matched_entries": [{"trade_id": "night_closed_trade", "units": 2.0, "realized_pnl": 1.0}],
                "ts": "2026-07-05T13:10:00+00:00",
            },
        ],
    )
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        night_cycle,
        track="human",
        output_root=output,
        as_of="2026-07-05T14:00:00+00:00",
    )

    open_rows = [row for row in payload["display_trades"] if row["status"] == "open"]
    assert len(open_rows) == 1
    assert open_rows[0]["source_cycle_id"] == day_cycle
    assert payload["display_reason"] == "current_plus_carried_open"


def test_machine_trade_rows_filter_invalid_stop_geometry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_NIGHT"
    write_json(
        output / "dualtrack" / "fills" / f"{cycle_id}_machine.json",
        [
            {
                "fill_id": "bad_grid_entry",
                "event": "entry",
                "track": "machine",
                "ts": "2026-07-05T13:00:00+00:00",
                "side": "buy",
                "price": 4155.0,
                "sl": 4155.0,
                "tp": 4163.0,
                "notional": 10000.0,
                "layer": "grid",
                "rung": 0,
                "realized_pnl": -0.5,
            },
            {
                "fill_id": "bad_grid_stop",
                "event": "stop",
                "track": "machine",
                "ts": "2026-07-05T13:00:00+00:00",
                "side": "sell",
                "price": 4121.3,
                "sl": 4155.0,
                "notional": 10000.0,
                "layer": "grid",
                "rung": 0,
                "realized_pnl": -81.7,
            },
        ],
    )
    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeFreshMarketFeed)

    payload = dashboard_server.build_dualtrack_trades_response(
        cycle_id,
        track="machine",
        output_root=output,
        as_of="2026-07-05T14:00:00+00:00",
    )

    assert payload["trades"] == []
    assert payload["summary"]["realized_pnl"] == 0
    assert payload["safety"]["invalid_machine_fill_count"] == 2
    assert payload["display_safety"]["invalid_machine_fill_count"] == 2
