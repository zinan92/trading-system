import json
from pathlib import Path

from services.datafeed_market_repository import DatafeedMarketRepository


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


class FakeClient:
    def candles(self, **kwargs):
        assert kwargs["cache_policy"] == "require"
        return {
            "instrument_id": "GOLD",
            "provider": "binance_usdm_futures",
            "quality_flags": ["execution_venue"],
            "candles": [
                {
                    "timestamp": "2026-07-15T10:00:00+00:00",
                    "open": 4000,
                    "high": 4002,
                    "low": 3999,
                    "close": 4001,
                    "volume": 10,
                }
            ],
        }

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
    assert repo.load_latest_quote("GOLD")["close"] == 4001
    assert repo.coverage()[0]["provider"] == "binance_usdm_futures"


class EnvelopeClient:
    def __init__(self) -> None:
        self.calls = []

    def candles(self, **kwargs):
        self.calls.append(kwargs)
        return json.loads(
            (FIXTURES / "trusted_execution_candles_v1.json").read_text(
                encoding="utf-8"
            )
        )


def test_repository_exposes_opt_in_trusted_envelope_without_changing_old_reads():
    client = EnvelopeClient()
    config = {
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
    }
    repo = DatafeedMarketRepository(client=client, config=config)

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
