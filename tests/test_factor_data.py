from schemas.asset import Asset
from schemas.market_data import Bar
from services.kline_client import KlineClient
from services.market_store import MarketStore


def test_local_factor_requires_daily_timeframe(tmp_path):
    client = KlineClient("local_gold_api", fallback_to_mock=False, local_db_path=tmp_path / "market.db")

    try:
        client.fetch(Asset("DXY", "Dollar", "macro", "high", "UTC", role="factor"), timeframe="5m")
    except ValueError as exc:
        assert "only supports 1d" in str(exc)
    else:
        raise AssertionError("factor 5m fetch should fail in local mode")


def test_fred_factor_rows_are_stored(monkeypatch, tmp_path):
    csv = "observation_date,DFII10\n2026-05-20,1.95\n2026-05-21,1.90\n"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return csv.encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse())
    client = KlineClient("local_gold_api", fallback_to_mock=False, local_db_path=tmp_path / "market.db")

    bars = client.fetch(Asset("US10Y_REAL", "Real Yield", "macro", "high", "UTC", role="factor"), timeframe="1d", limit=2)

    assert [bar.close for bar in bars] == [1.95, 1.90]
    assert bars[-1].provider == "fred:DFII10"
    assert "real_yield" in bars[-1].quality_flags


def test_fred_factor_uses_cached_rows_when_network_fails(monkeypatch, tmp_path):
    db_path = tmp_path / "market.db"
    MarketStore(db_path).upsert_bars(
        [
            Bar("DXY", "1d", "2026-05-20T00:00:00+00:00", 100, 101, 99, 100.5, 0, "fred:DTWEXBGS", ["cached"]),
            Bar("DXY", "1d", "2026-05-21T00:00:00+00:00", 101, 102, 100, 101.5, 0, "fred:DTWEXBGS", ["cached"]),
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("network down")))
    client = KlineClient("local_gold_api", fallback_to_mock=False, local_db_path=db_path)

    bars = client.fetch(Asset("DXY", "Dollar", "macro", "high", "UTC", role="factor"), timeframe="1d", limit=2)

    assert [bar.close for bar in bars] == [100.5, 101.5]
    assert bars[-1].quality_flags == ["cached"]
