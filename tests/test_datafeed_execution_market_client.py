from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.datafeed_execution_market_client import (
    DatafeedExecutionMarketClient,
    ExecutionMarketUnavailable,
    execution_market_payload,
    market_source_mode,
    write_compare_receipt,
)
from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader
from services.park_telegram_runtime import ParkTelegramRuntimeError, default_market_reader


class Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_client_reads_execution_market_v1_and_binds_venue_key() -> None:
    calls: list[str] = []

    def opener(request, timeout):
        calls.append(request.full_url)
        assert timeout == 2.0
        return Response({"schema_version": "execution-market-v1", "price": 4432.54, "fresh": True, "trusted": True})

    payload = DatafeedExecutionMarketClient(
        base_url="http://datafeed.test", timeout_seconds=2, opener=opener
    ).read(venue="binance", instrument_id="XAUUSDT.BINANCE")

    assert payload["price"] == 4432.54
    assert calls == ["http://datafeed.test/api/execution-market/binance/XAUUSDT.BINANCE"]


def test_client_rejects_wrong_schema() -> None:
    client = DatafeedExecutionMarketClient(opener=lambda *_args, **_kwargs: Response({}))
    with pytest.raises(ExecutionMarketUnavailable, match="schema mismatch"):
        client.read(venue="binance", instrument_id="XAUUSDT.BINANCE")


def test_mode_defaults_direct_and_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", raising=False)
    assert market_source_mode() == "direct"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "bad")
    with pytest.raises(ValueError, match="direct, dual, or datafeed"):
        market_source_mode()


def test_dual_receipt_contains_required_source_facts(tmp_path: Path) -> None:
    write_compare_receipt(
        tmp_path,
        mode="dual",
        direct={"price": 100.0, "observed_at": "d", "fresh": True, "age_seconds": 2.0},
        datafeed={"price": 100.5, "observed_at": "f", "fresh": True, "age_seconds": 3.0},
    )
    row = json.loads((tmp_path / "park_strategy" / "market_source_compare.jsonl").read_text())
    assert row["direct"] == {"price": 100.0, "observed_at": "d", "fresh": True, "age_seconds": 2.0}
    assert row["datafeed"]["price"] == 100.5
    assert row["price_delta"] == 0.5
    assert row["age_delta_seconds"] == 1.0
    assert row["fresh_equal"] is True


def test_hyperliquid_datafeed_mode_uses_source_bound_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "datafeed")
    calls: list[tuple[str, str]] = []

    def read(self, *, venue: str, instrument_id: str):
        calls.append((venue, instrument_id))
        return {
            "schema_version": "execution-market-v1",
            "price": 79289.0,
            "trusted": True,
            "fresh": True,
            "observed_at": "2026-09-08T03:13:59Z",
            "age_seconds": 20.0,
            "reason": "fresh",
        }

    monkeypatch.setattr(
        "services.datafeed_execution_market_client.DatafeedExecutionMarketClient.read", read
    )
    result = HyperliquidTestnetMarketReader(opener=lambda *_args, **_kwargs: pytest.fail("direct fallback"))
    market = result.read()
    assert calls == [("hyperliquid", "BTC-USD-PERP.HYPERLIQUID")]
    assert market["instrument_id"] == "BTC-USD-PERP"
    assert market["source"] == "hyperliquid.external_testnet"


def test_paper_datafeed_mode_fails_closed_when_execution_market_is_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "datafeed")
    monkeypatch.setattr(
        "services.datafeed_execution_market_client.DatafeedExecutionMarketClient.read",
        lambda *_args, **_kwargs: {
            "schema_version": "execution-market-v1",
            "price": None,
            "trusted": False,
            "fresh": False,
            "reason": "unsupported_instrument",
        },
    )
    with pytest.raises(ParkTelegramRuntimeError, match="execution market is not trusted and fresh") as raised:
        default_market_reader()
    assert raised.value.code == "market_unavailable"


def test_paper_dual_mode_keeps_direct_result_and_writes_to_router_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "dual")
    monkeypatch.setitem(
        sys.modules,
        "pipelines.dashboard_server",
        SimpleNamespace(build_dualtrack_market_bars_response=lambda **_kwargs: {
            "status": "ready",
            "latest_close": 4432.5,
            "fresh": True,
            "is_synthetic": False,
            "provider": "binance_direct",
            "latest_timestamp": "direct-time",
        }),
    )
    monkeypatch.setattr(
        "services.datafeed_execution_market_client.DatafeedExecutionMarketClient.read",
        lambda *_args, **_kwargs: {
            "schema_version": "execution-market-v1",
            "price": 4432.54,
            "trusted": True,
            "fresh": True,
            "observed_at": "datafeed-time",
            "age_seconds": 20.0,
        },
    )
    result = default_market_reader(output_root=tmp_path)
    assert result["price"] == 4432.5
    row = json.loads((tmp_path / "park_strategy" / "market_source_compare.jsonl").read_text())
    assert row["direct"]["price"] == 4432.5
    assert row["datafeed"]["price"] == 4432.54
