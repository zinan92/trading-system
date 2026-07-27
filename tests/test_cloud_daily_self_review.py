from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipelines.dashboard_server import build_daily_self_review_response
from services.cloud_daily_self_review import CloudDailySelfReview, load_daily_self_review
from services.journal_store import write_json


REPORT_DATE = "2026-07-26"
START = datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc)


def fixture(
    root: Path,
    *,
    pnl: float = 12.5,
    trades: int = 3,
    ticks: int = 1440,
    reconciliation: str = "pass",
    report: bool = True,
    package: bool = True,
) -> None:
    heartbeat_rows = [
        {
            "ts": (START + timedelta(minutes=index)).isoformat(),
            "cycle_id": "2026-07-26_DAY",
            "event": "live_tick_heartbeat",
            "detail": {"runner": "dualtrack-live-tick", "ledger_refreshed": True},
        }
        for index in range(ticks)
    ]
    write_json(
        root / "dualtrack" / "runner" / "2026-07-26_DAY.json",
        heartbeat_rows,
    )
    if report:
        report_payload = {
            "schema_version": "trading-daily-24h-v1",
            "status": "complete",
            "report_date": REPORT_DATE,
            "execution": {
                "trade_count": trades,
                "fill_count": trades * 2,
                "total_notional": 10000.0,
                "realized_pnl": pnl,
                "reconciliation_status": reconciliation,
            },
            "provenance": {
                "cycle_packages": (
                    [{"cycle_id": "2026-07-26_DAY", "package_hash": "pkg"}]
                    if package
                    else []
                )
            },
        }
        report_payload["report_hash"] = hashlib.sha256(
            json.dumps(
                report_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        write_json(
            root / "dualtrack" / "daily_reports" / f"{REPORT_DATE}.json",
            [report_payload],
        )
    if package:
        write_json(
            root
            / "dualtrack"
            / "strategy_cycle_packages"
            / "2026-07-26_DAY.json",
            [
                {
                    "cycle_id": "2026-07-26_DAY",
                    "status": "closed",
                    "strategy_plan": {
                        "strategy_plan_id": "plan-1",
                        "version": 2,
                        "strategy_type": "grid",
                        "direction": "long",
                        "style": "steady",
                        "range": {"low": 4000, "high": 4100},
                        "grid": {
                            "count": 30,
                            "target_net_profit_per_grid_usd": 10,
                            "actual_leverage": 10,
                        },
                    },
                }
            ],
        )


def build(root: Path) -> dict:
    return CloudDailySelfReview(
        root,
        now=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
        deployed_sha="a" * 40,
    ).build(REPORT_DATE)


def codes(rows: list[dict]) -> set[str]:
    return {str(row["code"]) for row in rows}


def test_positive_day_is_complete_and_idempotent(tmp_path: Path):
    fixture(tmp_path)

    first = build(tmp_path)
    revision_path = (
        tmp_path
        / "dualtrack"
        / "daily_self_reviews"
        / REPORT_DATE
        / "revisions"
        / f"{first['evidence_revision']}.json"
    )
    before = revision_path.read_bytes()
    second = CloudDailySelfReview(
        tmp_path,
        now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
        deployed_sha="different",
    ).build(REPORT_DATE)

    assert first["status"] == "complete"
    assert first["control_actions_executed"] == 0
    assert "profitable_day" in codes(first["went_well"])
    assert "terminal_reconciliation_pass" in codes(first["went_well"])
    assert first["strategy_truth"]["strategies"][0]["strategy_plan_id"] == "plan-1"
    assert second == first
    assert revision_path.read_bytes() == before
    assert load_daily_self_review(tmp_path)["review_hash"] == first["review_hash"]
    assert build_daily_self_review_response(output_root=tmp_path)["review_hash"] == first["review_hash"]


@pytest.mark.parametrize(
    ("pnl", "trades", "reconciliation", "expected_code", "expected_action"),
    [
        (-8.0, 2, "pass", "loss_day", "review_loss_attribution"),
        (0.0, 0, "pass", "no_trade_sample", "review_no_trade_market_fit"),
        (5.0, 2, "drift", "reconciliation_failed", "review_reconciliation_drift"),
    ],
)
def test_strategy_and_reconciliation_findings(
    tmp_path: Path,
    pnl: float,
    trades: int,
    reconciliation: str,
    expected_code: str,
    expected_action: str,
):
    fixture(
        tmp_path,
        pnl=pnl,
        trades=trades,
        reconciliation=reconciliation,
    )

    result = build(tmp_path)

    assert expected_code in codes(result["went_poorly"])
    assert expected_action in {row["action_id"] for row in result["tomorrow_actions"]}
    assert all(row["executed"] is False for row in result["tomorrow_actions"])


def test_missing_report_and_tick_history_remain_unknown(tmp_path: Path):
    fixture(tmp_path, ticks=0, report=False, package=False)

    result = build(tmp_path)

    assert result["status"] == "incomplete"
    assert result["operational_truth"]["status"] == "unknown"
    assert result["execution_truth"]["trade_count"] is None
    assert result["execution_truth"]["realized_pnl"] is None
    assert "tick_history_unknown" in codes(result["went_poorly"])
    assert "execution_truth_unavailable" in codes(result["went_poorly"])


def test_partial_tick_day_is_degraded_and_service_recovery_only(tmp_path: Path):
    fixture(tmp_path, ticks=60)

    result = build(tmp_path)

    assert result["operational_truth"]["status"] == "degraded"
    action = next(
        row
        for row in result["tomorrow_actions"]
        if row["action_id"] == "restore_tick_continuity"
    )
    assert action["permission_class"] == "automatic_service_recovery"
    assert action["executed"] is False


def test_datafeed_failure_is_explicit_and_does_not_execute_recovery(tmp_path: Path):
    fixture(tmp_path)
    write_json(
        tmp_path
        / "dualtrack"
        / "strategy_control"
        / "live_tick_failure.json",
        [
            {
                "recorded_at": (START + timedelta(hours=1)).isoformat(),
                "failure_phase": "route_datafeed",
            }
        ],
    )

    result = build(tmp_path)

    assert "datafeed_failures" in codes(result["went_poorly"])
    assert result["operational_truth"]["datafeed_failure_count"] == 1
    assert result["control_actions_executed"] == 0


def test_corrupt_daily_report_is_unknown_not_zero(tmp_path: Path):
    fixture(tmp_path)
    path = tmp_path / "dualtrack" / "daily_reports" / f"{REPORT_DATE}.json"
    rows = json.loads(path.read_text())
    rows[-1]["report_hash"] = "bad"
    write_json(path, rows)

    result = build(tmp_path)

    assert result["execution_truth"]["status"] == "unknown"
    assert result["execution_truth"]["realized_pnl"] is None
    assert result["evidence"]["daily_report"]["integrity"] == "invalid"


def test_changed_evidence_creates_new_revision_without_overwriting_old(tmp_path: Path):
    fixture(tmp_path, ticks=1439)
    first = build(tmp_path)
    first_path = (
        tmp_path
        / "dualtrack"
        / "daily_self_reviews"
        / REPORT_DATE
        / "revisions"
        / f"{first['evidence_revision']}.json"
    )
    first_bytes = first_path.read_bytes()
    runner = tmp_path / "dualtrack" / "runner" / "2026-07-26_DAY.json"
    rows = json.loads(runner.read_text())
    rows.append(
        {
            "ts": (START + timedelta(minutes=1439)).isoformat(),
            "cycle_id": "2026-07-26_DAY",
            "event": "live_tick_heartbeat",
            "detail": {"runner": "dualtrack-live-tick", "ledger_refreshed": True},
        }
    )
    write_json(runner, rows)

    second = build(tmp_path)

    assert second["evidence_revision"] != first["evidence_revision"]
    assert first_path.read_bytes() == first_bytes
    second_path = first_path.with_name(f"{second['evidence_revision']}.json")
    assert second_path.is_file()
