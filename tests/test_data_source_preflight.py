from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_data_source_preflight_warns_when_only_public_data_is_available(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4571.3, "provider": "gold-api.com", "timestamp": "2026-05-26T00:00:00+00:00"}])
    MarketStore(db_path).upsert_bars([
        Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4571, 4572, 4570, 4571.3, 0, "gold-api.com", ["live_snapshot"])
    ])
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", "2026-05-26T00:01:00+00:00", 4572, 4572, 4572, 4572, 0, "gold-api.com", ["live_quote"]))

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 0, 3, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "warn"
    assert result["ready_for_paper"] is True
    assert result["ready_for_live"] is False
    assert result["official_rows"] == 0
    assert result["latest_quote"]["close"] == 4572
    assert result["latest_record_age_minutes"] == 2
    assert result["latest_record_is_fresh"] is True
    assert load_json(root / "data_source_preflight" / "current.json")[0]["latest_provider"] == "gold-api.com"


def test_data_source_preflight_passes_with_official_broker_latest_bar(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4572, 4569, 4570 + index, 1, "broker_csv", ["csv_import"])
        for index in range(3)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])

    result = DataSourcePreflight(root, db_path, checked_at=start + timedelta(minutes=12)).run(run_date)

    assert result["status"] == "pass"
    assert result["ready_for_live"] is True
    assert result["official_rows"] == 3
    assert result["live_bar_is_fresh"] is True
    assert result["latest_record_is_fresh"] is True


def test_data_source_preflight_accepts_execution_venue_feed_for_live_when_enabled(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-06-02"
    start = datetime(2026, 6, 2, tzinfo=timezone.utc)
    config = {
        "market_data_sources": {
            "gold_5m": {
                "public_providers": ["gold-api.com", "binance_usdm"],
                "execution_venue_providers": ["binance_usdm"],
                "official_broker_providers": ["mt5_csv", "oanda"],
                "allow_execution_venue_for_live": True,
                "max_live_bar_lag_minutes": 15,
                "max_public_quote_age_minutes": 15,
                "price_sanity": {"enabled": True, "min_price": 3000, "max_price": 6000, "max_quote_bar_deviation_pct": 3},
            }
        }
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4530, 4532, 4529, 4530 + index, 10, "binance_usdm", ["execution_venue_feed"])
        for index in range(3)
    ]
    MarketStore(db_path).upsert_bars(bars)
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", (start + timedelta(minutes=11)).isoformat(), 4532, 4532, 4532, 4532, 0, "gold-api.com", ["live_quote"]))
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])

    result = DataSourcePreflight(root, db_path, checked_at=start + timedelta(minutes=12)).run(run_date)

    assert result["status"] == "pass"
    assert result["ready_for_live"] is True
    assert result["live_data_mode"] == "execution_venue"
    assert result["execution_venue_rows"] == 3
    assert result["official_rows"] == 0
    assert result["latest_provider"] == "binance_usdm"
    assert "execution venue market data is active" in result["message"]


def test_data_source_preflight_blocks_tiger_live_until_price_feed_acceptance(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-07-06"
    start = datetime(2026, 7, 6, tzinfo=timezone.utc)
    config = {
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
        }
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    bars = [
        Bar("MGCmain", "1m", (start + timedelta(minutes=index)).isoformat(), 4180, 4182, 4179, 4180 + index, 10, "tiger_openapi:COMEX", ["execution_venue_feed"])
        for index in range(3)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "MGCmain_1m.json", [bar.to_dict() for bar in bars])
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "blocked",
                "ready_for_price_feed": False,
                "contract": "MGCmain",
                "blockers": [{"name": "realtime_market_hours_gate", "status": "fail"}],
                "can_enable_broker_orders_from_this_gate": False,
            }
        ],
    )
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "ready_for_price_feed": False,
                "exit_code": 75,
                "can_enable_broker_orders_from_this_gate": False,
                "steps": {
                    "realtime_validation": {
                        "market_hours_gate": {
                            "next_trading_window": {"start": "2026-07-05T22:00:00+00:00", "end": "2026-07-06T21:00:00+00:00", "trading_date": "2026-07-06"}
                        }
                    }
                },
            }
        ],
    )

    result = DataSourcePreflight(root, db_path, checked_at=start + timedelta(minutes=3), symbol="MGCmain", timeframe="1m").run(run_date)

    assert result["status"] == "warn"
    assert result["ready_for_paper"] is True
    assert result["ready_for_live"] is False
    assert result["live_data_mode"] == "not_live_ready"
    assert result["execution_venue_rows"] == 3
    assert result["execution_venue_readiness_gate"]["required"] is True
    assert result["execution_venue_readiness_gate"]["status"] == "pending_market_open"
    assert result["execution_venue_readiness_gate"]["allows_live"] is False
    assert result["execution_venue_readiness_gate"]["evidence"]["readiness"]["can_enable_broker_orders_from_this_gate"] is False
    assert "waiting for market-hours acceptance" in result["message"]


def test_data_source_preflight_treats_tiger_closed_session_bars_as_paper_usable(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-07-05"
    config = {
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
        }
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    latest_close = datetime(2026, 7, 3, 16, 59, tzinfo=timezone.utc)
    MarketStore(db_path).upsert_bars(
        [
            Bar("MGCmain", "1m", latest_close.isoformat(), 4186, 4187, 4185, 4186.9, 10, "tiger_openapi:COMEX", ["execution_venue_feed"])
        ]
    )
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "blocked",
                "ready_for_price_feed": False,
                "contract": "MGCmain",
                "blockers": [{"name": "realtime_market_hours_gate", "status": "fail"}],
                "can_enable_broker_orders_from_this_gate": False,
            }
        ],
    )
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "ready_for_price_feed": False,
                "exit_code": 75,
                "can_enable_broker_orders_from_this_gate": False,
                "steps": {
                    "realtime_validation": {
                        "market_hours_gate": {
                            "next_trading_window": {
                                "start": "2026-07-05T22:00:00+00:00",
                                "end": "2026-07-06T21:00:00+00:00",
                                "trading_date": "2026-07-06",
                            }
                        }
                    }
                },
            }
        ],
    )

    result = DataSourcePreflight(
        root,
        db_path,
        checked_at=datetime(2026, 7, 5, 17, 10, tzinfo=timezone.utc),
        symbol="MGCmain",
        timeframe="1m",
    ).run(run_date)

    assert result["status"] == "warn"
    assert result["ready_for_paper"] is True
    assert result["ready_for_live"] is False
    assert result["latest_record_age_minutes"] > 2800
    assert result["latest_record_is_fresh"] is True
    assert result["current_session_freshness_enforced"] is False
    assert result["market_session_freshness"]["status"] == "market_closed_pending_open"
    assert result["execution_venue_readiness_gate"]["status"] == "pending_market_open"
    assert "waiting for market-hours acceptance" in result["message"]


def test_data_source_preflight_enforces_tiger_freshness_after_pending_window_starts(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-07-05"
    config = {
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
        }
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    MarketStore(db_path).upsert_bars(
        [
            Bar("MGCmain", "1m", "2026-07-03T16:59:00+00:00", 4186, 4187, 4185, 4186.9, 10, "tiger_openapi:COMEX", ["execution_venue_feed"])
        ]
    )
    write_json(root / "tiger_price_feed_readiness" / "current.json", [{"status": "blocked", "ready_for_price_feed": False, "blockers": [{"name": "realtime_market_hours_gate"}]}])
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "ready_for_price_feed": False,
                "exit_code": 75,
                "steps": {
                    "realtime_validation": {
                        "market_hours_gate": {
                            "next_trading_window": {
                                "start": "2026-07-05T22:00:00+00:00",
                                "end": "2026-07-06T21:00:00+00:00",
                                "trading_date": "2026-07-06",
                            }
                        }
                    }
                },
            }
        ],
    )

    result = DataSourcePreflight(
        root,
        db_path,
        checked_at=datetime(2026, 7, 5, 22, 30, tzinfo=timezone.utc),
        symbol="MGCmain",
        timeframe="1m",
    ).run(run_date)

    assert result["status"] == "fail"
    assert result["ready_for_paper"] is False
    assert result["latest_record_is_fresh"] is False
    assert result["current_session_freshness_enforced"] is True
    assert result["market_session_freshness"]["status"] == "market_window_started"
    assert "stale" in result["message"]


def test_data_source_preflight_accepts_tiger_live_after_price_feed_readiness(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-07-06"
    start = datetime(2026, 7, 6, tzinfo=timezone.utc)
    config = {
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
        }
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    bars = [
        Bar("MGCmain", "1m", (start + timedelta(minutes=index)).isoformat(), 4180, 4182, 4179, 4180 + index, 10, "tiger_openapi:COMEX", ["execution_venue_feed"])
        for index in range(3)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "MGCmain_1m.json", [bar.to_dict() for bar in bars])
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "ready_for_price_feed",
                "ready_for_price_feed": True,
                "contract": "MGCmain",
                "blockers": [],
                "checked_at": "2026-07-06T00:03:00+00:00",
                "can_enable_broker_orders_from_this_gate": False,
            }
        ],
    )
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "accepted",
                "ready_for_price_feed": True,
                "exit_code": 0,
                "checked_at": "2026-07-06T00:03:00+00:00",
                "can_enable_broker_orders_from_this_gate": False,
            }
        ],
    )

    result = DataSourcePreflight(root, db_path, checked_at=start + timedelta(minutes=3), symbol="MGCmain", timeframe="1m").run(run_date)

    assert result["status"] == "pass"
    assert result["ready_for_live"] is True
    assert result["live_data_mode"] == "execution_venue"
    assert result["execution_venue_readiness_gate"]["status"] == "pass"
    assert result["execution_venue_readiness_gate"]["allows_live"] is True
    assert result["execution_venue_readiness_gate"]["evidence"]["acceptance"]["can_enable_broker_orders_from_this_gate"] is False


def test_data_source_preflight_namespaces_shared_non_default_artifacts(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-07-06"
    start = datetime(2026, 7, 6, tzinfo=timezone.utc)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    config = {
        "output_root": "outputs",
        "local_market_db": "data/market_data.db",
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
    }
    monkeypatch.setattr("services.data_source_preflight.load_pipeline_config", lambda: config)
    bar = Bar("MGCmain", "1m", start.isoformat(), 4180, 4182, 4179, 4180, 10, "tiger_openapi:COMEX", ["execution_venue_feed"])
    MarketStore(db_path).upsert_bars([bar])
    write_json(root / "data_source_preflight" / "current.json", [{"source_key": "GOLD_5m", "status": "pass"}])
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "contract": "MGCmain", "blockers": []}],
    )

    result = DataSourcePreflight(checked_at=start + timedelta(minutes=1), symbol="MGCmain", timeframe="1m").run(run_date)

    assert result["source_key"] == "MGCmain_1m"
    assert load_json(root / "data_source_preflight" / "current.json")[0]["source_key"] == "GOLD_5m"
    scoped = load_json(root / "data_source_preflight" / "MGCmain_1m" / "current.json")[0]
    assert scoped["source_key"] == "MGCmain_1m"
    assert scoped["latest_provider"] == "tiger_openapi:COMEX"
    assert scoped["latest_bar"]["timestamp"] == start.isoformat()


def test_data_source_preflight_blocks_paper_when_data_quality_blocks(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4571, 4572, 4570, 4571.3, 0, "local_synthetic_seed", ["synthetic_seed"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(
        root / "data_quality" / f"{run_date}.json",
        {
            "GOLD": {
                "allows_trading": False,
                "reasons": ["latest bar is synthetic; import official/recent GOLD 5m bars before trading"],
            }
        },
    )
    MarketStore(db_path).upsert_bars([bar])

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 0, 2, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "fail"
    assert result["base_ready_for_paper"] is True
    assert result["ready_for_paper"] is False
    assert result["data_quality_allows_trading"] is False
    assert "latest bar is synthetic" in result["message"]


def test_data_source_preflight_blocks_live_when_official_bar_is_stale_vs_quote(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T01:00:00+00:00", 4571, 4572, 4570, 4571.3, 1, "mt5_csv", ["csv_import"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    MarketStore(db_path).upsert_bars([bar])
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", "2026-05-26T01:30:00+00:00", 4575, 4575, 4575, 4575, 0, "gold-api.com", ["live_quote"]))

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 1, 31, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "warn"
    assert result["ready_for_paper"] is True
    assert result["ready_for_live"] is False
    assert result["live_bar_lag_minutes"] == 30
    assert result["live_bar_is_fresh"] is False
    assert "stale" in result["message"]


def test_data_source_preflight_blocks_current_paper_when_latest_quote_is_stale(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4571, 4572, 4570, 4571.3, 0, "gold-api.com", ["live_snapshot"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True, "reasons": []}})
    MarketStore(db_path).upsert_bars([bar])
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", "2026-05-26T00:01:00+00:00", 4572, 4572, 4572, 4572, 0, "gold-api.com", ["live_quote"]))

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 0, 30, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "fail"
    assert result["ready_for_paper"] is False
    assert result["ready_for_live"] is False
    assert result["latest_record_age_minutes"] == 29
    assert result["latest_record_is_fresh"] is False
    assert "latest GOLD quote/bar is stale" in result["message"]


def test_data_source_preflight_blocks_obviously_bad_gold_price(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 2200, 2200, 2200, 2200, 0, "gold-api.com", ["live_snapshot"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True, "reasons": []}})
    MarketStore(db_path).upsert_bars([bar])
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", "2026-05-26T00:01:00+00:00", 2201, 2201, 2201, 2201, 0, "gold-api.com", ["live_quote"]))

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 0, 3, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "fail"
    assert result["ready_for_paper"] is False
    assert result["ready_for_live"] is False
    assert result["price_sanity"]["passes"] is False
    assert result["price_sanity"]["latest_record_price"] == 2201
    assert "outside sanity range" in result["message"]


def test_data_source_preflight_blocks_quote_bar_price_divergence(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    bar = Bar("GOLD", "5m", "2026-05-26T00:00:00+00:00", 4571, 4571, 4571, 4571, 0, "gold-api.com", ["live_snapshot"])
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict()])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True, "reasons": []}})
    MarketStore(db_path).upsert_bars([bar])
    MarketStore(db_path).upsert_quote(Bar("GOLD", "5m", "2026-05-26T00:01:00+00:00", 4100, 4100, 4100, 4100, 0, "gold-api.com", ["live_quote"]))

    result = DataSourcePreflight(root, db_path, checked_at=datetime(2026, 5, 26, 0, 3, tzinfo=timezone.utc)).run(run_date)

    assert result["status"] == "fail"
    assert result["ready_for_paper"] is False
    assert result["price_sanity"]["quote_bar_deviation_pct"] > 3
    assert "quote/bar price deviation" in result["message"]
