from services.yahoo_chart_client import YahooChartClient


def test_yahoo_chart_client_parses_valid_5m_bars():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1770000000, 1770000300, 1770000600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [4500.0, 4501.0, 4502.0],
                                "high": [4502.0, 4503.0, 4504.0],
                                "low": [4499.0, 4500.0, 4501.0],
                                "close": [4501.0, None, 4503.0],
                                "volume": [10, 0, 12],
                            }
                        ]
                    },
                }
            ]
        }
    }

    bars = YahooChartClient().parse_chart_payload(payload, yahoo_symbol="GC=F", output_symbol="GOLD")

    assert len(bars) == 2
    assert bars[0].symbol == "GOLD"
    assert bars[0].timeframe == "5m"
    assert bars[0].provider == "yahoo_chart:GC=F"
    assert bars[0].quality_flags == ["historical_5m", "futures_proxy"]
    assert bars[-1].close == 4503.0
