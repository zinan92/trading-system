from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import load_json, write_json
from services.live_readiness import LiveReadiness
from services.market_store import MarketStore
from schemas.market_data import Bar


def test_live_readiness_fails_when_system_is_only_paper_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = "2026-05-26"
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    bars = [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), 4570, 4572, 4569, 4570 + index, 1, "gold-api.com", ["live_snapshot"])
        for index in range(220)
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
    write_json(root / "clean_bars" / run_date / "manifest.json", [{"symbol": "GOLD", "timeframe": "5m"}])
    (root / "data_quality").mkdir(parents=True, exist_ok=True)
    (root / "data_quality" / f"{run_date}.json").write_text('{"GOLD": {"allows_trading": true}}\n', encoding="utf-8")
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text('{"state": "ok", "interval_seconds": 300}\n', encoding="utf-8")
    write_json(root / "oanda_feed" / "current.json", [{"status": "skipped", "ready": False, "missing_env": ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]}])
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "journals" / f"{run_date}.md").write_text("journal\n", encoding="utf-8")
    (root / "review_notes").mkdir(parents=True, exist_ok=True)
    (root / "review_notes" / f"{run_date}.md").write_text("review\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{run_date}.md").write_text("report\n", encoding="utf-8")
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "ok"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"state": "collect"}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold"}])

    result = LiveReadiness(root, db_path).run(run_date)

    assert result["status"] == "fail"
    assert result["live_ready"] is False
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["official_market_data"]["status"] == "fail"
    assert checks["execution_mode"]["status"] == "fail"
    assert checks["broker_provider"]["status"] == "fail"
    assert checks["risk_rules"]["status"] == "pass"
    assert checks["runner"]["status"] == "pass"
    assert checks["journal_review"]["status"] == "pass"
    assert load_json(root / "live_readiness" / "current.json")[0]["run_date"] == run_date


def test_live_readiness_accepts_execution_venue_market_data(tmp_path: Path):
    readiness = LiveReadiness(tmp_path / "outputs", tmp_path / "market_data.db")

    check = readiness._official_market_data(
        {
            "ready_for_live": True,
            "ready_for_paper": True,
            "live_data_mode": "execution_venue",
            "latest_provider": "binance_usdm",
            "official_rows": 0,
            "execution_venue_rows": 500,
        }
    )

    assert check["status"] == "pass"
    assert "execution venue 5m OHLC is ready" in check["summary"]


def test_live_readiness_accepts_tiger_execution_venue_market_data(tmp_path: Path):
    readiness = LiveReadiness(tmp_path / "outputs", tmp_path / "market_data.db")

    check = readiness._official_market_data(
        {
            "symbol": "MGCmain",
            "timeframe": "1m",
            "ready_for_live": True,
            "ready_for_paper": True,
            "live_data_mode": "execution_venue",
            "latest_provider": "tiger_openapi:COMEX",
            "official_rows": 0,
            "execution_venue_rows": 500,
            "execution_venue_providers": ["tiger_openapi:COMEX"],
        }
    )

    assert check["status"] == "pass"
    assert "MGCmain execution venue 1m OHLC is ready" in check["summary"]


def test_live_readiness_selects_tiger_market_data_identity(tmp_path: Path):
    config = {
        "tiger_futures_feed": {"output_symbol": "MGCmain", "timeframe": "1m"},
        "broker": {"provider": "tiger_openapi"},
    }
    readiness = LiveReadiness(tmp_path / "outputs", tmp_path / "market_data.db")
    readiness.config = config

    identity = readiness._market_data_identity_for_broker({"provider": "tiger_openapi"})

    assert identity == {
        "provider": "tiger_openapi",
        "symbol": "MGCmain",
        "timeframe": "1m",
        "write_legacy_artifacts": False,
    }


def test_live_readiness_run_uses_tiger_market_data_without_overwriting_global_gaps(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market_data.db"
    run_date = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [
        Bar("MGCmain", "1m", (now - timedelta(minutes=1)).isoformat(), 4186, 4187, 4185, 4186.1, 1, "tiger_openapi:COMEX", ["execution_venue_feed"]),
        Bar("MGCmain", "1m", now.isoformat(), 4186, 4187, 4185, 4186.2, 1, "tiger_openapi:COMEX", ["execution_venue_feed"]),
    ]
    MarketStore(db_path).upsert_bars(bars)
    write_json(root / "data_gaps" / "current.json", [{"source_key": "GOLD_5m", "status": "pass"}])
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "contract": "MGCmain", "blockers": [], "can_enable_broker_orders_from_this_gate": False}],
    )
    monkeypatch.setattr(
        "services.live_readiness.broker_preflight",
        lambda _root: {
            "provider": "tiger_openapi",
            "mode": "live",
            "ready": True,
            "missing_env": [],
            "props_path_exists": True,
            "props_path_owner_only": True,
            "dry_run": False,
        },
    )
    readiness = LiveReadiness(root, db_path)
    readiness.config = {
        "execution_mode": "live",
        "live_trading_enabled": True,
        "broker": {"provider": "tiger_openapi"},
        "tiger_futures_feed": {"output_symbol": "MGCmain", "timeframe": "1m"},
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

    result = readiness.run(run_date)

    checks = {item["name"]: item for item in result["checks"]}
    assert checks["official_market_data"]["status"] == "pass"
    assert checks["official_market_data"]["evidence"]["source_key"] == "MGCmain_1m"
    assert "MGCmain execution venue 1m OHLC is ready" in checks["official_market_data"]["summary"]
    assert checks["data_gaps"]["status"] == "pass"
    assert checks["data_gaps"]["evidence"]["source_key"] == "MGCmain_1m"
    assert load_json(root / "data_gaps" / "current.json")[0]["source_key"] == "GOLD_5m"
    assert load_json(root / "data_gaps" / "MGCmain_1m" / "current.json")[0]["source"] == "market_db"


def test_live_readiness_accepts_ready_tiger_provider_and_owner_only_props(tmp_path: Path):
    readiness = LiveReadiness(tmp_path / "outputs", tmp_path / "market_data.db")
    broker = {
        "provider": "tiger_openapi",
        "mode": "live",
        "ready": True,
        "missing_env": [],
        "props_path_exists": True,
        "props_path_owner_only": True,
    }

    provider = readiness._broker_provider(broker)
    credentials = readiness._broker_credentials(broker)

    assert provider["status"] == "pass"
    assert credentials["status"] == "pass"


def test_live_readiness_accepts_tiger_price_feed_readiness_as_broker_feedback(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "ready_for_price_feed",
                "ready_for_price_feed": True,
                "contract": "MGCmain",
                "blockers": [],
                "can_enable_broker_orders_from_this_gate": False,
                "checked_at": "2026-07-05T22:03:00+00:00",
            }
        ],
    )
    readiness = LiveReadiness(root, tmp_path / "market_data.db")

    check = readiness._broker_feedback({"provider": "tiger_openapi"})

    assert check["status"] == "pass"
    assert "Tiger OpenAPI price feed has passed readiness" in check["summary"]
    assert check["evidence"]["price_feed_readiness"]["ready_for_price_feed"] is True
    assert check["evidence"]["price_feed_readiness"]["can_enable_broker_orders_from_this_gate"] is False


def test_live_readiness_tiger_broker_feedback_explains_pending_market_open(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "blocked",
                "ready_for_price_feed": False,
                "contract": "MGCmain",
                "blockers": [{"name": "realtime_market_hours_gate", "status": "fail"}],
                "can_enable_broker_orders_from_this_gate": False,
                "checked_at": "2026-07-05T16:18:30+00:00",
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
                "checked_at": "2026-07-05T16:18:30+00:00",
                "blockers": [{"name": "realtime_market_hours_gate"}],
                "steps": {
                    "realtime_validation": {
                        "market_hours_gate": {
                            "operator_action": "rerun_after_next_trading_window",
                            "next_trading_window": {
                                "start": "2026-07-05T22:00:00+00:00",
                                "end": "2026-07-06T21:00:00+00:00",
                                "trading_date": "2026-07-06",
                            },
                        }
                    }
                },
            }
        ],
    )
    readiness = LiveReadiness(root, tmp_path / "market_data.db")

    check = readiness._broker_feedback({"provider": "tiger_openapi"})

    assert check["status"] == "fail"
    assert "waiting for market-hours acceptance" in check["summary"]
    assert "2026-07-05T22:00:00+00:00" in check["summary"]
    assert check["evidence"]["price_feed_acceptance"]["status"] == "pending_market_open"
    assert check["evidence"]["price_feed_acceptance"]["exit_code"] == 75
    assert check["evidence"]["price_feed_acceptance"]["can_enable_broker_orders_from_this_gate"] is False
