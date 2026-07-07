from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.tiger_price_feed_acceptance import TigerPriceFeedAcceptance


RUN_DATE = "2026-07-05"


class _FakeQuoteClient:
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


def _config(props_env: str = "TIGER_OPENAPI_CONFIG_PATH") -> dict:
    return {
        "output_root": "outputs",
        "local_market_db": "data/market_data.db",
        "broker": {"profile": "tiger_openapi_paper"},
        "binance_usdm_1m_feed": {"symbol": "XAUUSDT", "output_symbol": "GOLD", "interval": "1m"},
        "gold_5m_backfill": {"yahoo_symbol": "GC=F", "range": "5d"},
        "market_data_sources": {
            "mgcmain_1m": {
                "public_providers": [],
                "execution_venue_providers": ["tiger_openapi:COMEX"],
                "official_broker_providers": [],
                "allow_execution_venue_for_live": True,
                "require_tiger_price_feed_readiness": True,
                "max_live_bar_lag_minutes": 15,
                "max_public_quote_age_minutes": 15,
                "price_sanity": {"enabled": True, "min_price": 3000, "max_price": 6000, "max_quote_bar_deviation_pct": 3},
            }
        },
        "tiger_futures_feed": {
            "props_path_env": props_env,
            "contract": "MGCmain",
            "output_symbol": "MGCmain",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "props_path_env": props_env,
                "allowed_symbols": ["MGCmain", "MGC2608"],
            }
        },
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


def _write_feed_artifact(root: Path, *, imported_rows: int = 500) -> None:
    write_json(
        root / "tiger_futures_feed" / "current.json",
        [
            {
                "status": "pass",
                "ready": True,
                "message": "imported Tiger OpenAPI futures bars",
                "contract": "MGCmain",
                "output_symbol": "MGCmain",
                "timeframe": "1m",
                "provider": "tiger_openapi:COMEX",
                "imported_rows": imported_rows,
                "latest_timestamp": "2026-07-03T16:59:00+00:00",
                "latest_price": 4186.9,
                "checked_at": "2026-07-05T14:44:11+00:00",
            }
        ],
    )


def _write_market_bar(db_path: Path, *, timestamp: str = "2026-07-05T22:02:00+00:00") -> None:
    MarketStore(db_path).upsert_bars(
        [
            Bar(
                "MGCmain",
                "1m",
                timestamp,
                4186.0,
                4187.0,
                4185.5,
                4186.9,
                12,
                "tiger_openapi:COMEX",
                ["official_broker_feed", "execution_venue_feed", "exchange_futures", "tiger_openapi"],
            )
        ]
    )


def _write_props(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\nprivate_key_pk1=redacted\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))


def test_tiger_price_feed_acceptance_accepts_after_market_hours_gate_and_catalog_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    market_db = tmp_path / "market_data.db"
    _write_feed_artifact(root)
    _write_market_bar(market_db)
    client = _FakeQuoteClient(
        windows_by_date={"2026-07-05": [_window("2026-07-05T22:00:00+00:00", "2026-07-05T23:00:00+00:00")]},
        bars_by_call=[
            [_bar("2026-07-05T22:01:00+00:00", 4186.1)],
            [_bar("2026-07-05T22:02:00+00:00", 4186.9)],
        ],
    )

    result = TigerPriceFeedAcceptance(_config(), root, quote_client=client, sleeper=lambda _seconds: None, market_db=market_db).run(
        RUN_DATE,
        as_of="2026-07-05T22:02:10+00:00",
        trading_date="2026-07-05",
        poll_seconds=0,
    )

    assert result["schema_version"] == "tiger-price-feed-acceptance-v1"
    assert result["status"] == "accepted"
    assert result["exit_code"] == 0
    assert result["ready_for_price_feed"] is True
    assert result["operator_next_action"]["status"] == "accepted"
    assert result["can_enable_broker_orders_from_this_gate"] is False
    assert result["steps"]["realtime_validation"]["market_hours_gate"]["exit_code"] == 0
    assert result["steps"]["price_feed_readiness"]["ready_for_price_feed"] is True
    assert result["steps"]["connector_catalog"]["tiger_price_feed_status"] == "ready"
    assert result["steps"]["data_source_preflight"]["source_key"] == "MGCmain_1m"
    assert result["steps"]["data_source_preflight"]["ready_for_live"] is True
    assert result["steps"]["data_source_preflight"]["latest_provider"] == "tiger_openapi:COMEX"
    assert result["steps"]["data_source_preflight"]["can_enable_broker_orders_from_this_gate"] is False
    assert result["safety"]["opens_trade_client"] is False
    assert result["safety"]["submits_orders"] is False
    assert result["safety"]["refreshes_data_source_preflight_artifact"] is True
    assert load_json(root / "tiger_price_feed_acceptance" / "current.json")[-1]["status"] == "accepted"
    assert load_json(root / "data_source_preflight" / "MGCmain_1m" / "current.json")[-1]["ready_for_live"] is True
    assert load_json(root / "connector_catalog" / "current.json")[-1]["summary"]["ready_price_feed_count"] == 4
    assert "private_key" not in json.dumps(result).lower()
    assert str(tmp_path / "tiger_openapi_config.properties") not in json.dumps(result)


def test_tiger_price_feed_acceptance_returns_tempfail_before_market_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    market_db = tmp_path / "market_data.db"
    _write_feed_artifact(root)
    client = _FakeQuoteClient(
        windows_by_date={"2026-07-06": [_window("2026-07-05T22:00:00+00:00", "2026-07-06T21:00:00+00:00")]}
    )

    result = TigerPriceFeedAcceptance(_config(), root, quote_client=client, sleeper=lambda _seconds: None, market_db=market_db).run(
        RUN_DATE,
        as_of="2026-07-05T14:46:00+00:00",
        poll_seconds=0,
    )

    assert result["status"] == "pending_market_open"
    assert result["exit_code"] == 75
    assert result["ready_for_price_feed"] is False
    assert result["operator_next_action"]["status"] == "waiting_market_open"
    assert result["operator_next_action"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    blocker_names = {item["name"] for item in result["blockers"]}
    assert "realtime_market_hours_gate" in blocker_names
    assert "connector_catalog_price_feed" in blocker_names
    assert result["steps"]["connector_catalog"]["tiger_price_feed_status"] == "blocked"
    assert result["steps"]["data_source_preflight"]["source_key"] == "MGCmain_1m"


def test_tiger_price_feed_acceptance_can_aggregate_local_artifacts_without_opening_quote_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    market_db = tmp_path / "market_data.db"
    _write_feed_artifact(root)
    write_json(
        root / "tiger_realtime_validation" / "current.json",
        [
            {
                "schema_version": "tiger-realtime-validation-v1",
                "status": "pending_market_open",
                "message": "COMEX futures session is not trading at validation time; rerun during the next trading window.",
                "contract": "MGCmain",
                "checked_at": "2026-07-05T15:21:57+00:00",
                "market_hours_gate": {
                    "required": True,
                    "ready_for_price_feed_promotion": False,
                    "market_hours_observed": False,
                    "exit_code": 75,
                    "operator_action": "rerun_after_next_trading_window",
                    "next_trading_window": {"start": "2026-07-05T22:00:00+00:00", "end": "2026-07-06T21:00:00+00:00", "trading_date": "2026-07-06"},
                },
                "safety": {
                    "read_only": True,
                    "writes_market_db": False,
                    "opens_trade_client": False,
                    "submits_orders": False,
                },
            }
        ],
    )

    result = TigerPriceFeedAcceptance(_config(), root, market_db=market_db).run(RUN_DATE, run_realtime=False)

    assert result["status"] == "pending_market_open"
    assert result["operator_next_action"]["status"] == "waiting_market_open"
    assert result["steps"]["realtime_validation"]["ran_this_acceptance"] is False
    assert result["steps"]["data_source_preflight"]["source_key"] == "MGCmain_1m"
    assert result["safety"]["opens_quote_client"] is False
    assert result["safety"]["submits_orders"] is False


def test_tiger_price_feed_acceptance_plan_only_does_not_open_quote_client_or_refresh_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "outputs"

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("plan-only must not run realtime validation")

    result = TigerPriceFeedAcceptance(_config(), root, realtime_runner=fail_if_called).plan(
        RUN_DATE,
        contract="MGCmain",
        poll_seconds=75,
    )

    assert result["schema_version"] == "tiger-price-feed-acceptance-plan-v1"
    assert result["status"] == "plan_ready"
    assert result["exit_code"] == 0
    assert result["ready_for_price_feed"] is False
    assert result["operator_next_action"]["status"] == "review_plan_then_run_acceptance"
    assert result["operator_next_action"]["next_command"] == (
        "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json"
    )
    assert result["command_sequence"][0]["name"] == "refresh_tiger_price_feed_acceptance"
    assert result["command_sequence"][0]["opens_quote_client"] is True
    assert result["command_sequence"][0]["opens_trade_client"] is False
    assert result["command_sequence"][0]["submits_orders"] is False
    assert result["safety"]["plan_only"] is True
    assert result["safety"]["opens_quote_client"] is False
    assert result["safety"]["opens_trade_client"] is False
    assert result["safety"]["submits_orders"] is False
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_market_db"] is False
    assert result["safety"]["writes_plan_artifact"] is True
    assert result["safety"]["writes_acceptance_artifact"] is False
    assert result["safety"]["refreshes_realtime_validation_artifact"] is False
    assert result["safety"]["refreshes_price_feed_readiness_artifact"] is False
    assert result["safety"]["refreshes_connector_catalog_artifact"] is False
    assert result["safety"]["refreshes_data_source_preflight_artifact"] is False
    assert load_json(root / "tiger_price_feed_acceptance_plan" / "current.json")[-1]["status"] == "plan_ready"
    assert load_json(root / "tiger_price_feed_acceptance" / "current.json") == []
    assert load_json(root / "tiger_realtime_validation" / "current.json") == []


def test_tiger_price_feed_acceptance_operator_next_action_time_states(tmp_path: Path) -> None:
    service = TigerPriceFeedAcceptance(_config(), tmp_path / "outputs")
    realtime_step = {
        "market_hours_gate": {
            "next_trading_window": {
                "start": "2026-07-05T22:00:00+00:00",
                "end": "2026-07-06T21:00:00+00:00",
                "trading_date": "2026-07-06",
            }
        }
    }
    commands = ["python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json"]

    waiting = service._operator_next_action("pending_market_open", realtime_step, "2026-07-05T17:00:00+00:00", commands)
    rerun = service._operator_next_action("pending_market_open", realtime_step, "2026-07-05T22:00:01+00:00", commands)
    expired = service._operator_next_action("pending_market_open", realtime_step, "2026-07-06T21:00:01+00:00", commands)

    assert waiting["status"] == "waiting_market_open"
    assert waiting["next_command"] == commands[0]
    assert rerun["status"] == "rerun_acceptance_now"
    assert expired["status"] == "window_expired"


def test_tiger_price_feed_acceptance_cli_exits_with_acceptance_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from pipelines import tiger_price_feed_acceptance as cli

    class FakeAcceptance:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            return {
                "status": "pending_market_open",
                "run_date": RUN_DATE,
                "exit_code": 75,
                "ready_for_price_feed": False,
                "blockers": [{"name": "realtime_market_hours_gate"}],
            }

    monkeypatch.setattr(cli, "TigerPriceFeedAcceptance", FakeAcceptance)
    monkeypatch.setattr(sys, "argv", ["tiger_price_feed_acceptance", "--json"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 75
    assert '"pending_market_open"' in capsys.readouterr().out


def test_tiger_price_feed_acceptance_cli_plan_only_uses_output_root_without_clients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    from pipelines import tiger_price_feed_acceptance as cli

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tiger_price_feed_acceptance",
            "--date",
            RUN_DATE,
            "--contract",
            "MGCmain",
            "--poll-seconds",
            "75",
            "--output-root",
            str(output_root),
            "--plan-only",
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "plan_ready"
    assert payload["safety"]["opens_quote_client"] is False
    assert payload["safety"]["writes_acceptance_artifact"] is False
    assert load_json(output_root / "tiger_price_feed_acceptance_plan" / "current.json")[-1]["status"] == "plan_ready"
