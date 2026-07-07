from __future__ import annotations

import sys
from pathlib import Path

import pytest

from services.journal_store import load_json
from services.tiger_realtime_validation import TigerRealtimeValidation, run_tiger_realtime_validation


class _FakeRealtimeQuoteClient:
    def __init__(self, *, bars_by_call: list[list[dict]] | None = None, windows_by_date: dict[str, list[dict]] | None = None) -> None:
        self.bars_by_call = bars_by_call or []
        self.windows_by_date = windows_by_date or {}
        self.bar_calls = 0

    def get_quote_permission(self):
        return [{"name": "futuresQuoteLv1", "expire_at": -1}]

    def get_future_contract(self, identifier):
        return [{
            "identifier": identifier,
            "exchange": "COMEX",
            "trade": True,
            "multiplier": 10.0,
            "min_tick": 0.1,
        }]

    def get_future_trading_times(self, identifier, trading_date=None):
        return self.windows_by_date.get(str(trading_date), [])

    def get_future_bars(self, identifiers, period, begin_time, end_time, limit):
        index = min(self.bar_calls, len(self.bars_by_call) - 1)
        self.bar_calls += 1
        return self.bars_by_call[index] if self.bars_by_call else []


def _config() -> dict:
    return {
        "contract": "MGCmain",
        "output_symbol": "MGCmain",
        "timeframe": "1m",
        "period": "1m",
        "provider": "tiger_openapi:COMEX",
    }


def _window(start: str, end: str) -> dict:
    return {"start": start, "end": end, "trading": True, "bidding": False, "zone": "America/New_York"}


def _bar(ts: str, close: float) -> dict:
    return {
        "identifier": "MGCmain",
        "time": ts,
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 12,
        "exchange": "COMEX",
    }


def test_tiger_realtime_validation_waits_for_market_open() -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-06": [_window("2026-07-05T22:00:00+00:00", "2026-07-06T21:00:00+00:00")]}
    )

    result = TigerRealtimeValidation(_config(), quote_client=client, sleeper=lambda _seconds: None).run(
        as_of="2026-07-05T14:46:00+00:00",
        poll_seconds=0,
    )

    assert result["status"] == "pending_market_open"
    assert result["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert result["safety"]["opens_trade_client"] is False
    assert client.bar_calls == 0


def test_tiger_realtime_validation_passes_when_bar_advances_during_open_session() -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-05": [_window("2026-07-05T22:00:00+00:00", "2026-07-05T23:00:00+00:00")]},
        bars_by_call=[
            [_bar("2026-07-05T22:01:00+00:00", 4186.1)],
            [_bar("2026-07-05T22:02:00+00:00", 4186.9)],
        ],
    )

    result = TigerRealtimeValidation(_config(), quote_client=client, sleeper=lambda _seconds: None).run(
        as_of="2026-07-05T22:02:10+00:00",
        trading_date="2026-07-05",
        poll_seconds=0,
        max_lag_seconds=180,
    )

    assert result["status"] == "pass"
    assert result["bar_advanced"] is True
    assert result["fresh"] is True
    assert result["latest_bar_age_seconds"] == 10.0


def test_tiger_realtime_validation_fails_when_open_session_bar_is_stale() -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-05": [_window("2026-07-05T22:00:00+00:00", "2026-07-05T23:00:00+00:00")]},
        bars_by_call=[
            [_bar("2026-07-05T22:00:00+00:00", 4180.0)],
            [_bar("2026-07-05T22:00:00+00:00", 4180.0)],
        ],
    )

    result = TigerRealtimeValidation(_config(), quote_client=client, sleeper=lambda _seconds: None).run(
        as_of="2026-07-05T22:10:00+00:00",
        trading_date="2026-07-05",
        poll_seconds=0,
        max_lag_seconds=180,
    )

    assert result["status"] == "fail"
    assert result["bar_advanced"] is False
    assert result["fresh"] is False
    assert result["latest_bar_age_seconds"] == 600.0


def test_tiger_realtime_validation_pipeline_writes_redacted_artifact(tmp_path: Path) -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-06": [_window("2026-07-05T22:00:00+00:00", "2026-07-06T21:00:00+00:00")]}
    )

    result = run_tiger_realtime_validation(
        "2026-07-05",
        output_root=tmp_path,
        config=_config(),
        quote_client=client,
        as_of="2026-07-05T14:46:00+00:00",
        poll_seconds=0,
        sleeper=lambda _seconds: None,
    )

    assert result["status"] == "pending_market_open"
    current = load_json(tmp_path / "tiger_realtime_validation" / "current.json")[0]
    assert current["schema_version"] == "tiger-realtime-validation-v1"
    assert current["market_hours_gate"]["required"] is False
    assert current["market_hours_gate"]["exit_code"] == 0
    assert "tiger_openapi_config.properties" not in str(current)
    assert "private_key" not in str(current).lower()


def test_tiger_realtime_validation_required_gate_returns_tempfail_before_market_open(tmp_path: Path) -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-06": [_window("2026-07-05T22:00:00+00:00", "2026-07-06T21:00:00+00:00")]}
    )

    result = run_tiger_realtime_validation(
        "2026-07-05",
        output_root=tmp_path,
        config=_config(),
        quote_client=client,
        as_of="2026-07-05T14:46:00+00:00",
        poll_seconds=0,
        require_market_hours_pass=True,
        sleeper=lambda _seconds: None,
    )

    gate = result["market_hours_gate"]
    assert result["status"] == "pending_market_open"
    assert gate["required"] is True
    assert gate["ready_for_price_feed_promotion"] is False
    assert gate["market_hours_observed"] is False
    assert gate["exit_code"] == 75
    assert gate["operator_action"] == "rerun_after_next_trading_window"
    assert gate["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"


def test_tiger_realtime_validation_required_gate_passes_after_market_open_bar_advance(tmp_path: Path) -> None:
    client = _FakeRealtimeQuoteClient(
        windows_by_date={"2026-07-05": [_window("2026-07-05T22:00:00+00:00", "2026-07-05T23:00:00+00:00")]},
        bars_by_call=[
            [_bar("2026-07-05T22:01:00+00:00", 4186.1)],
            [_bar("2026-07-05T22:02:00+00:00", 4186.9)],
        ],
    )

    result = run_tiger_realtime_validation(
        "2026-07-05",
        output_root=tmp_path,
        config=_config(),
        quote_client=client,
        as_of="2026-07-05T22:02:10+00:00",
        trading_date="2026-07-05",
        poll_seconds=0,
        max_lag_seconds=180,
        require_market_hours_pass=True,
        sleeper=lambda _seconds: None,
    )

    gate = result["market_hours_gate"]
    assert result["status"] == "pass"
    assert gate["required"] is True
    assert gate["ready_for_price_feed_promotion"] is True
    assert gate["market_hours_observed"] is True
    assert gate["exit_code"] == 0
    assert gate["operator_action"] == "passed"


def test_tiger_realtime_validation_cli_require_market_hours_pass_exits_with_gate(monkeypatch, capsys) -> None:
    from pipelines import tiger_realtime_validation as cli

    def fake_run(*_args, **kwargs):  # noqa: ANN002, ANN003
        assert kwargs["require_market_hours_pass"] is True
        return {
            "status": "pending_market_open",
            "market_hours_gate": {"required": True, "exit_code": 75},
        }

    monkeypatch.setattr(cli, "run_tiger_realtime_validation", fake_run)
    monkeypatch.setattr(sys, "argv", ["tiger_realtime_validation", "--require-market-hours-pass"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 75
    assert '"pending_market_open"' in capsys.readouterr().out
