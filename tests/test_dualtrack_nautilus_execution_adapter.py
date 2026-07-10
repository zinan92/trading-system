from __future__ import annotations

from pathlib import Path

from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.journal_store import load_json, write_json


CYCLE_ID = "2026-07-10_DAY"


def _preflight(path: Path) -> None:
    write_json(path, [{
        "status": "ready_for_paper_shadow",
        "fee_model": {
            "mode": "account_observed",
            "maker_fee_rate": "0.00005",
            "taker_fee_rate": "0.00005",
            "funding_rate": "0.0001",
            "funding_time": 1,
            "observed_at": "2026-07-10T10:00:00+00:00",
            "real_money_eligible": False,
        },
    }])


def _candidate(cycle_id: str) -> dict:
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_shadow",
        "cycle_id": cycle_id,
        "orders": [{"order_id": "n-1", "state": "filled"}],
        "fills": [{"fill_id": "n-fill-1", "side": "buy", "price": 100.0, "quantity": 1.0}],
        "positions": [{"status": "open", "side": "long", "remaining_units": 1.0}],
        "account": {"margin": 10.0, "exposure": 100.0, "slippage": 0.0},
        "pnl": {"realized": -0.005, "unrealized": 0.0},
        "mark": {"price": 100.0, "fresh": True, "source": "test"},
        "capabilities": {"native_order_lifecycle": True},
        "reconciliation": {"status": "ok", "issues": []},
    }


def test_persists_orders_fills_positions_and_restarts_idempotently(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)

    def replay(_preflight: Path, _input: Path, output_path: Path) -> dict:
        snapshot = _candidate(CYCLE_ID)
        snapshot["orders"][0]["order_id"] = first["order_id"]
        write_json(output_path, [snapshot])
        return snapshot

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    command = {
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-10T01:00:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 100.0,
        "quantity": 1.0,
        "source_fill_id": "external-1",
    }
    first = adapter.submit_order(command)
    second = adapter.submit_order(command)
    assert first == second
    assert len(load_json(adapter.root / "commands" / f"{CYCLE_ID}.json")) == 1

    event = {
        "cycle_id": CYCLE_ID,
        "ts_event": "2026-07-10T01:01:00+00:00",
        "event_started_at": "2026-07-10T01:00:00+00:00",
        "price": 100.0,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "instrument_id": "XAUUSDT",
    }
    result = adapter.process_market_event(event)
    assert result["status"] == "replayed"
    assert len(load_json(adapter.root / "orders" / f"{CYCLE_ID}.json")) == 1
    assert len(load_json(adapter.root / "fills" / f"{CYCLE_ID}.json")) == 1
    assert len(load_json(adapter.root / "positions" / f"{CYCLE_ID}.json")) == 1
    assert adapter.reconcile(CYCLE_ID)["status"] == "ok"

    pending = adapter.submit_order({
        **command,
        "ts": "2026-07-10T01:02:00+00:00",
        "source_fill_id": "external-2",
    })
    assert pending["state"] == "accepted"
    assert len(adapter.snapshot(CYCLE_ID)["orders"]) == 2
    assert adapter.reconcile(CYCLE_ID)["status"] == "ok"

    restarted = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    assert restarted.snapshot(CYCLE_ID) == adapter.snapshot(CYCLE_ID)
    duplicate = restarted.process_market_event(event)
    assert duplicate["status"] == "idempotent"
    assert len(load_json(adapter.root / "events" / f"{CYCLE_ID}.json")) == 1


def test_retries_persisted_event_until_replay_is_acknowledged(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    attempts = []

    def replay(_preflight: Path, _input: Path, _output: Path) -> dict:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("injected replay failure")
        return _candidate(CYCLE_ID)

    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=replay,
    )
    event = {
        "cycle_id": CYCLE_ID,
        "ts_event": "2026-07-10T01:01:00+00:00",
        "price": 100.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm",
        "provider": "binance_usdm",
        "instrument_id": "XAUUSDT",
    }

    try:
        adapter.process_market_event(event)
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("first replay should fail")
    assert len(load_json(adapter.root / "events" / f"{CYCLE_ID}.json")) == 1
    assert load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json") == []

    assert adapter.process_market_event(event)["status"] == "replayed"
    assert adapter.process_market_event(event)["status"] == "idempotent"
    assert len(attempts) == 2
    assert len(load_json(adapter.root / "processed_events" / f"{CYCLE_ID}.json")) == 1


def test_rejects_market_event_without_execution_identity(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    adapter = NautilusExecutionAdapter(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=lambda *_args: _candidate(CYCLE_ID),
    )
    try:
        adapter.process_market_event({
            "cycle_id": CYCLE_ID,
            "ts_event": "2026-07-10T01:01:00+00:00",
            "price": 100.0,
            "fresh": True,
            "is_synthetic": False,
            "source": "test",
        })
    except ValueError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("missing provider should fail closed")


def test_rejects_persistent_adapter_with_assumed_fees(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    rows = load_json(preflight)
    rows[-1]["fee_model"]["mode"] = "paper_assumption"
    write_json(preflight, rows)

    try:
        NautilusExecutionAdapter(
            output,
            nautilus_python=tmp_path / "unused-python",
            preflight_path=preflight,
            replay_executor=lambda *_args: _candidate(CYCLE_ID),
        )
    except RuntimeError as exc:
        assert "account-observed" in str(exc)
    else:
        raise AssertionError("assumed fees must not enable the persistent adapter")
