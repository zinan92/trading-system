from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json, write_json
from services.tiger_price_feed_readiness import TigerPriceFeedReadiness


RUN_DATE = "2026-07-05"


def _write_feed_artifact(root: Path, *, imported_rows: int = 500) -> None:
    write_json(
        root / "tiger_futures_feed" / "current.json",
        [
            {
                "status": "pass",
                "ready": True,
                "message": "imported Tiger OpenAPI futures bars",
                "contract": "MGCmain",
                "output_symbol": "MGCmain",
                "timeframe": "1m",
                "provider": "tiger_openapi:COMEX",
                "imported_rows": imported_rows,
                "latest_timestamp": "2026-07-03T16:59:00+00:00",
                "latest_price": 4186.9,
                "checked_at": "2026-07-05T14:44:11+00:00",
            }
        ],
    )


def _write_realtime_artifact(root: Path, *, status: str = "pass", gate_exit_code: int = 0, safe: bool = True) -> None:
    ready = status == "pass" and gate_exit_code == 0
    write_json(
        root / "tiger_realtime_validation" / "current.json",
        [
            {
                "schema_version": "tiger-realtime-validation-v1",
                "status": status,
                "message": "Tiger futures bars advanced during the market-hours validation window." if ready else "COMEX futures session is not trading at validation time; rerun during the next trading window.",
                "provider": "tiger_openapi:COMEX",
                "contract": "MGCmain",
                "output_symbol": "MGCmain",
                "timeframe": "1m",
                "checked_at": "2026-07-05T22:02:10+00:00" if ready else "2026-07-05T15:21:57+00:00",
                "market_hours_gate": {
                    "required": True,
                    "ready_for_price_feed_promotion": ready,
                    "market_hours_observed": ready,
                    "exit_code": gate_exit_code,
                    "operator_action": "passed" if ready else "rerun_after_next_trading_window",
                    "next_trading_window": {
                        "start": "2026-07-05T22:00:00+00:00",
                        "end": "2026-07-06T21:00:00+00:00",
                        "trading_date": "2026-07-06",
                    },
                },
                "safety": {
                    "read_only": True,
                    "writes_market_db": False,
                    "opens_quote_client": True,
                    "opens_trade_client": False,
                    "opens_order_clients": False,
                    "submits_orders": False if safe else True,
                },
            }
        ],
    )


def test_tiger_price_feed_readiness_reports_ready_after_feed_and_market_hours_gate(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_feed_artifact(root)
    _write_realtime_artifact(root)

    result = TigerPriceFeedReadiness(root).run(RUN_DATE)

    assert result["schema_version"] == "tiger-price-feed-readiness-v1"
    assert result["status"] == "ready_for_price_feed"
    assert result["ready_for_price_feed"] is True
    assert result["can_enable_broker_orders_from_this_gate"] is False
    assert result["blockers"] == []
    assert {item["name"] for item in result["checks"]} == {
        "feed_import",
        "feed_window",
        "realtime_market_hours_gate",
        "realtime_safety",
    }
    assert result["summary"]["market_hours_gate"]["exit_code"] == 0
    assert result["safety"]["opens_tiger_sdk_clients"] is False
    assert result["safety"]["writes_market_db"] is False
    assert result["safety"]["submits_orders"] is False
    assert load_json(root / "tiger_price_feed_readiness" / "current.json")[-1]["status"] == "ready_for_price_feed"


def test_tiger_price_feed_readiness_blocks_until_market_hours_gate_passes(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_feed_artifact(root)
    _write_realtime_artifact(root, status="pending_market_open", gate_exit_code=75)

    result = TigerPriceFeedReadiness(root).run(RUN_DATE)

    assert result["status"] == "blocked"
    assert result["ready_for_price_feed"] is False
    blockers = {item["name"]: item for item in result["blockers"]}
    assert set(blockers) == {"realtime_market_hours_gate"}
    assert blockers["realtime_market_hours_gate"]["evidence"]["market_hours_gate"]["exit_code"] == 75
    assert "--require-market-hours-pass" in " ".join(result["next_commands"])


def test_tiger_price_feed_readiness_blocks_weak_feed_window_and_realtime_safety(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write_feed_artifact(root, imported_rows=20)
    _write_realtime_artifact(root, safe=False)

    result = TigerPriceFeedReadiness(root).run(RUN_DATE)

    blockers = {item["name"]: item for item in result["blockers"]}
    assert "feed_window" in blockers
    assert "realtime_safety" in blockers
    assert blockers["feed_window"]["evidence"]["imported_rows"] == 20
    assert blockers["realtime_safety"]["evidence"]["submits_orders"] is True
