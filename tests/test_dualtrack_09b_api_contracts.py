from __future__ import annotations

from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
from services.journal_store import write_json
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


def test_dualtrack_config_endpoint_exposes_display_config_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_server, "dualtrack_config", lambda: {**TEST_CONFIG, "broker_secret": "do-not-leak"})

    payload = dashboard_server.build_dualtrack_config_response()

    assert payload["schema_version"] == "dualtrack-config-v1"
    assert payload["max_leverage"] == TEST_CONFIG["max_leverage"]
    assert payload["safety"]["read_only"] is True
    assert "broker_secret" not in payload
    assert "secret" not in str(payload).lower()


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


def test_machine_trades_endpoint_rejects_mid_cycle_order_level_leak(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [_entry_fill(cycle_id=cycle_id, track="machine")])

    with pytest.raises(PermissionError, match="machine trades are hidden"):
        dashboard_server.build_dualtrack_trades_response(
            cycle_id,
            track="machine",
            output_root=output,
            as_of="2026-07-05T02:00:00+00:00",
        )


def test_trades_handler_maps_machine_mid_leak_to_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [_entry_fill(cycle_id=cycle_id, track="machine")])
    monkeypatch.setattr(
        dashboard_server,
        "build_dualtrack_trades_response",
        lambda requested_cycle_id, **kwargs: (_ for _ in ()).throw(PermissionError("machine trades are hidden until cycle close")),
    )

    handler = object.__new__(dashboard_server.DashboardHandler)
    captured = {}
    handler._write_json = lambda status, payload: captured.update({"status": status, "payload": payload})
    handler._write_error = lambda status, code, message: captured.update({"status": status, "error": code, "message": message})

    handler._handle_dualtrack_trades_get(f"/api/dualtrack/trades/{cycle_id}", "track=machine")

    assert captured["status"] == 403
    assert captured["error"] == "dualtrack_trades_hidden"
