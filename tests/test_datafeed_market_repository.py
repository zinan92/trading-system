from services.datafeed_market_repository import DatafeedMarketRepository


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
