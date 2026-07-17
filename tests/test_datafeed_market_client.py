import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from services.datafeed_market_client import DatafeedMarketClient, DatafeedUnavailable


FIXTURES = Path(__file__).parent / "fixtures" / "datafeed"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_client_calls_standard_datafeed_contract():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse({"schema_version": "kline-candles-v1", "candles": []})

    client = DatafeedMarketClient(base_url="http://datafeed.test", opener=opener)
    response = client.candles(
        asset_class="commodity",
        ticker="GOLD",
        timeframe="1m",
        limit=10,
        source="binance_usdm_futures",
        require_execution_venue=True,
    )

    assert response["schema_version"] == "kline-candles-v1"
    assert seen["url"].startswith("http://datafeed.test/api/candles/commodity/GOLD?")
    assert "source=binance_usdm_futures" in seen["url"]
    assert "require_execution_venue=true" in seen["url"]


def test_client_fails_closed_when_datafeed_is_unavailable():
    def opener(_request, timeout):
        raise OSError("connection refused")

    client = DatafeedMarketClient(opener=opener)
    with pytest.raises(DatafeedUnavailable, match="connection refused"):
        client.health()


def test_client_preserves_the_complete_upstream_502_contract():
    raw = (FIXTURES / "upstream_error_v1.json").read_bytes()

    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            hdrs=None,
            fp=BytesIO(raw),
        )

    client = DatafeedMarketClient(opener=opener)
    with pytest.raises(DatafeedUnavailable) as raised:
        client.health()

    message = str(raised.value)
    assert message.startswith("datafeed HTTP 502:")
    assert '"error": "upstream_error"' in message
    assert '"reject_reason": "upstream_error"' in message
    assert "Check network access to fapi.binance.com" in message
