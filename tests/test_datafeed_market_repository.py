import json
from copy import deepcopy
from pathlib import Path

import pytest

from services.datafeed_market_client import DatafeedUnavailable
from services.datafeed_market_mapper import DatafeedContractError
from services.datafeed_market_repository import DatafeedMarketRepository


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


class FakeClient:
    def candles(self, **kwargs):
        assert kwargs["cache_policy"] == "require"
        payload = json.loads(
            (FIXTURES / "trusted_execution_candles_v1.json").read_text(
                encoding="utf-8"
            )
        )
        payload.update(
            cache_policy=kwargs["cache_policy"],
            quality_policy=kwargs["quality"],
            require_execution_venue=kwargs["require_execution_venue"],
        )
        return payload

    def health(self):
        return {
            "storage_coverage": [
                {
                    "source_id": "binance_usdm_futures",
                    "ticker": "XAUUSDT",
                    "timeframe": "1m",
                    "count": 10,
                    "first_timestamp": "2026-07-15T09:51:00+00:00",
                    "latest_timestamp": "2026-07-15T10:00:00+00:00",
                }
            ]
        }


def test_repository_preserves_trading_bar_contract_over_datafeed():
    repo = DatafeedMarketRepository(client=FakeClient(), config={"datafeed": {}})
    bars = repo.load_bars("GOLD", "1m", 1)
    assert bars[0].symbol == "GOLD"
    assert bars[0].provider == "binance_usdm_futures"
    assert repo.load_latest_quote("GOLD")["close"] == 4003
    assert repo.coverage()[0]["provider"] == "binance_usdm_futures"


class EnvelopeClient:
    def __init__(self, payload: dict | None = None) -> None:
        self.calls = []
        self.payload = payload

    def candles(self, **kwargs):
        self.calls.append(kwargs)
        payload = deepcopy(
            self.payload
            or json.loads(
                (FIXTURES / "trusted_execution_candles_v1.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        candles = payload.get("candles", [])
        if kwargs.get("start"):
            candles = [
                row
                for row in candles
                if row["timestamp"] >= kwargs["start"]
            ]
        if kwargs.get("end"):
            candles = [
                row for row in candles if row["timestamp"] <= kwargs["end"]
            ]
        candles = candles[-int(kwargs["limit"]) :]
        payload["candles"] = candles
        payload["count"] = len(candles)
        payload["latest_timestamp"] = (
            candles[-1]["timestamp"] if candles else None
        )
        return payload


class UnavailableClient:
    def __init__(self) -> None:
        self.calls = 0

    def candles(self, **_kwargs):
        self.calls += 1
        raise DatafeedUnavailable("datafeed HTTP 502: upstream_error")


def _trusted_repository(client) -> DatafeedMarketRepository:
    return DatafeedMarketRepository(
        client=client,
        config={
            "datafeed": {
                "instrument_routes": {
                    "GOLD": {
                        "asset_class": "commodity",
                        "ticker": "GOLD",
                        "source": "binance_usdm_futures",
                        "cache_policy": "bypass",
                        "quality_policy": "strict",
                        "require_execution_venue": True,
                    }
                }
            }
        },
    )


def test_repository_exposes_authoritative_trusted_envelope():
    client = EnvelopeClient()
    repo = _trusted_repository(client)

    envelope = repo.load_envelope("GOLD", "1m", 2)

    assert envelope.instrument_id == "GOLD"
    assert envelope.provider_symbol == "XAUUSDT"
    assert envelope.execution_ready is True
    assert [bar.close for bar in envelope.bars] == [4001.0, 4003.0]
    assert client.calls == [
        {
            "asset_class": "commodity",
            "ticker": "GOLD",
            "timeframe": "1m",
            "limit": 2,
            "source": "binance_usdm_futures",
            "cache_policy": "bypass",
            "quality": "strict",
            "require_execution_venue": True,
            "start": None,
            "end": None,
        }
    ]


def test_repository_bar_reads_reject_an_invalid_success_payload() -> None:
    payload = json.loads(
        (FIXTURES / "trusted_execution_candles_v2.json").read_text(
            encoding="utf-8"
        )
    )
    payload.pop("schema_version")
    client = EnvelopeClient(payload)
    repo = _trusted_repository(client)

    with pytest.raises(DatafeedContractError, match="schema"):
        repo.load_bars("GOLD", "1m", 2)


def test_repository_propagates_upstream_failure_without_fallback() -> None:
    client = UnavailableClient()
    repo = _trusted_repository(client)

    with pytest.raises(DatafeedUnavailable, match="HTTP 502"):
        repo.load_bars("GOLD", "1m", 2)

    assert client.calls == 1


@pytest.mark.parametrize(
    "upstream_schema",
    ["kline-candles-v1", "kline-candles-v2"],
)
def test_repository_bar_projection_has_v1_v2_envelope_parity(
    upstream_schema: str,
) -> None:
    payload = json.loads(
        (FIXTURES / "trusted_execution_candles_v1.json").read_text(
            encoding="utf-8"
        )
    )
    if upstream_schema == "kline-candles-v2":
        payload.update(
            schema_version=upstream_schema,
            continuous_market=True,
            market_open=True,
            session_status="continuous",
            session_checked_at="2026-07-18T12:01:05+00:00",
            current_session_end=None,
        )
    client = EnvelopeClient(payload)
    repo = _trusted_repository(client)

    bars = repo.load_bars("GOLD", "1m", 2)

    assert [bar.close for bar in bars] == [4001.0, 4003.0]
    assert all(bar.symbol == "GOLD" for bar in bars)
    assert all(bar.provider == "binance_usdm_futures" for bar in bars)


def test_repository_range_and_point_reads_preserve_request_boundaries() -> None:
    client = EnvelopeClient()
    repo = _trusted_repository(client)

    ranged = repo.load_bars_between(
        "GOLD",
        "1m",
        "2026-07-18T12:00:00+00:00",
        "2026-07-18T12:01:00+00:00",
    )
    point = repo.load_bar_at_or_before(
        "GOLD",
        "1m",
        "2026-07-18T12:00:30+00:00",
    )

    assert len(ranged) == 2
    assert point["close"] == 4001.0
    assert client.calls[0]["limit"] == 2_000
    assert client.calls[0]["start"] == "2026-07-18T12:00:00+00:00"
    assert client.calls[0]["end"] == "2026-07-18T12:01:00+00:00"
    assert client.calls[1]["limit"] == 1
    assert client.calls[1]["start"] is None
    assert client.calls[1]["end"] == "2026-07-18T12:00:30+00:00"


def test_repository_uses_refill_policy_only_for_bounded_history() -> None:
    client = EnvelopeClient()
    repo = DatafeedMarketRepository(
        client=client,
        config={
            "datafeed": {
                "instrument_routes": {
                    "GOLD": {
                        "asset_class": "commodity",
                        "ticker": "GOLD",
                        "source": "binance_usdm_futures",
                        "historical_cache_policy": "allow",
                        "historical_quality_policy": "standard",
                        "live_cache_policy": "bypass",
                        "live_quality_policy": "strict",
                        "require_execution_venue": True,
                    }
                }
            }
        },
    )

    repo.load_bars_between(
        "GOLD",
        "1m",
        "2026-07-18T12:00:00+00:00",
        "2026-07-18T12:01:00+00:00",
    )
    repo.load_latest_bar("GOLD", "1m")

    historical, latest = client.calls
    assert historical["cache_policy"] == "allow"
    assert historical["quality"] == "standard"
    assert historical["source"] == "binance_usdm_futures"
    assert historical["require_execution_venue"] is True
    assert latest["cache_policy"] == "bypass"
    assert latest["quality"] == "strict"
    assert latest["source"] == "binance_usdm_futures"
    assert latest["require_execution_venue"] is True


def test_repository_contains_no_second_raw_candle_interpreter() -> None:
    source = (
        Path(__file__).parents[1] / "services" / "datafeed_market_repository.py"
    ).read_text(encoding="utf-8")

    assert "def _fetch(" not in source
    assert 'row["open"]' not in source
    assert 'payload.get("candles"' not in source
