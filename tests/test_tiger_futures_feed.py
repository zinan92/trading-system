from pathlib import Path

from services.journal_store import load_json
from services.market_store import MarketStore
from services.tiger_futures_feed import (
    TigerFuturesFeedClient,
    is_trading_at,
    run_tiger_futures_backfill,
    run_tiger_futures_feed_import,
    run_tiger_futures_sessions,
)


class _FakeTigerQuoteClient:
    def get_quote_permission(self):
        return [{"name": "aStockQuoteLv1", "expire_at": -1}]

    def get_future_contract(self, identifier):
        return [{
            "identifier": identifier,
            "exchange": "COMEX",
            "trade": True,
            "multiplier": 10.0,
            "min_tick": 0.1,
        }]

    def get_future_bars(self, identifiers, period, begin_time, end_time, limit):
        assert identifiers == ["MGC2608"]
        assert period == "1m"
        assert begin_time == -1 and end_time == -1
        assert limit == 2
        return [
            {
                "identifier": "MGC2608",
                "time": 1783097940000,
                "open": 4200.0,
                "high": 4201.0,
                "low": 4199.5,
                "close": 4200.5,
                "volume": 12,
                "exchange": "COMEX",
            },
            {
                "identifier": "MGC2608",
                "time": 1783097880000,
                "open": 4199.0,
                "high": 4200.0,
                "low": 4198.5,
                "close": 4199.5,
                "volume": 8,
                "exchange": "COMEX",
            },
        ]

    def get_future_bars_by_page(self, identifier, period, begin_time, end_time, total, page_size, time_interval):
        assert identifier == "MGC2608"
        assert period == "1m"
        assert total == 3
        assert page_size == 2
        assert time_interval == 0
        return [
            {
                "identifier": "MGC2608",
                "time": 1783097880000,
                "open": 4199.0,
                "high": 4200.0,
                "low": 4198.5,
                "close": 4199.5,
                "volume": 8,
                "exchange": "COMEX",
            },
            {
                "identifier": "MGC2608",
                "time": 1783097940000,
                "open": 4200.0,
                "high": 4201.0,
                "low": 4199.5,
                "close": 4200.5,
                "volume": 12,
                "exchange": "COMEX",
            },
            {
                "identifier": "MGC2608",
                "time": 1783098000000,
                "open": 4200.5,
                "high": 4202.0,
                "low": 4200.0,
                "close": 4201.5,
                "volume": 14,
                "exchange": "COMEX",
            },
        ]

    def get_future_trading_times(self, identifier, trading_date=None):
        assert identifier == "MGC2608"
        assert trading_date == "2026-07-06"
        return [
            {"start": 1783285200000, "end": 1783288800000, "trading": False, "bidding": True, "zone": "America/New_York"},
            {"start": 1783288800000, "end": 1783371600000, "trading": True, "bidding": False, "zone": "America/New_York"},
        ]


def test_tiger_futures_feed_imports_official_broker_bars(tmp_path: Path):
    store = MarketStore(tmp_path / "market.db")
    client = TigerFuturesFeedClient(
        store,
        config={
            "contract": "MGC2608",
            "output_symbol": "MGC2608",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        quote_client=_FakeTigerQuoteClient(),
    )

    result = client.fetch_and_store(limit=2)
    bars = store.load_bars("MGC2608", "1m", 10)
    quote = store.load_latest_quote("MGC2608")

    assert result["status"] == "pass"
    assert result["imported_rows"] == 2
    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)
    assert bars[0].provider == "tiger_openapi:COMEX"
    assert bars[-1].close == 4200.5
    assert "official_broker_feed" in bars[-1].quality_flags
    assert "execution_venue_feed" in bars[-1].quality_flags
    assert "exchange_futures" in bars[-1].quality_flags
    assert "mgc2608" in bars[-1].quality_flags
    assert quote["close"] == 4200.5


def test_tiger_futures_feed_pipeline_writes_artifacts(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    result = run_tiger_futures_feed_import(
        "2026-07-05",
        config={
            "contract": "MGC2608",
            "output_symbol": "MGC2608",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        quote_client=_FakeTigerQuoteClient(),
        limit=2,
    )

    assert result["run_date"] == "2026-07-05"
    assert result["market_db"] == str(db_path)
    assert load_json(root / "tiger_futures_feed" / "current.json")[0]["imported_rows"] == 2
    assert len(MarketStore(db_path).load_bars("MGC2608", "1m", 10)) == 2


def test_tiger_futures_backfill_pages_and_writes_artifacts(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    result = run_tiger_futures_backfill(
        "2026-07-03T16:58:00+00:00",
        "2026-07-03T17:00:00+00:00",
        config={
            "contract": "MGC2608",
            "output_symbol": "MGC2608",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        quote_client=_FakeTigerQuoteClient(),
        total=3,
        page_size=2,
        time_interval=0,
    )

    assert result["status"] == "pass"
    assert result["backfilled_rows"] == 3
    assert load_json(root / "tiger_futures_backfill" / "current.json")[0]["backfilled_rows"] == 3
    bars = MarketStore(db_path).load_bars("MGC2608", "1m", 10)
    assert [bar.timestamp for bar in bars] == [
        "2026-07-03T16:58:00+00:00",
        "2026-07-03T16:59:00+00:00",
        "2026-07-03T17:00:00+00:00",
    ]


def test_tiger_futures_feed_fails_closed_without_props_path(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TIGER_OPENAPI_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    client = TigerFuturesFeedClient(
        MarketStore(tmp_path / "market.db"),
        config={"props_path_env": "TIGER_OPENAPI_CONFIG_PATH", "contract": "MGC2608"},
    )

    result = client.fetch_and_store(limit=1)

    assert result["status"] == "fail"
    assert result["ready"] is False
    assert result["imported_rows"] == 0


def test_tiger_futures_feed_fails_closed_on_group_readable_props(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o644)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    client = TigerFuturesFeedClient(
        MarketStore(tmp_path / "market.db"),
        config={"props_path_env": "TIGER_OPENAPI_CONFIG_PATH", "contract": "MGC2608"},
    )

    result = client.fetch_and_store(limit=1)

    assert result["status"] == "fail"
    assert result["ready"] is False
    assert result["message"] == "Tiger OpenAPI config file must be owner-only (chmod 600)."
    assert result["checks"]["props_path_owner_only"] is False


def test_tiger_futures_sessions_normalize_windows_and_mask(tmp_path: Path):
    client = TigerFuturesFeedClient(
        MarketStore(tmp_path / "market.db"),
        config={
            "contract": "MGC2608",
            "output_symbol": "MGC2608",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        quote_client=_FakeTigerQuoteClient(),
    )

    sessions = client.fetch_trading_times("2026-07-06")

    assert sessions["timezone"] == "America/New_York"
    assert len(sessions["windows"]) == 2
    assert sessions["trading_windows"][0]["start"] == "2026-07-05T22:00:00+00:00"
    assert sessions["trading_windows"][0]["end"] == "2026-07-06T21:00:00+00:00"
    assert is_trading_at("2026-07-05T22:00:00+00:00", sessions) is True
    assert is_trading_at("2026-07-05T21:59:59+00:00", sessions) is False
    assert is_trading_at("2026-07-06T21:00:00+00:00", sessions) is False


def test_tiger_futures_sessions_pipeline_writes_artifacts(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))

    result = run_tiger_futures_sessions(
        "2026-07-05",
        trading_date="2026-07-06",
        config={
            "contract": "MGC2608",
            "output_symbol": "MGC2608",
            "timeframe": "1m",
            "period": "1m",
            "provider": "tiger_openapi:COMEX",
        },
        quote_client=_FakeTigerQuoteClient(),
    )

    assert result["run_date"] == "2026-07-05"
    assert load_json(root / "tiger_futures_sessions" / "current.json")[0]["trading_windows"][0]["start"] == "2026-07-05T22:00:00+00:00"
