from datetime import datetime, timezone
from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
from services.journal_store import load_json, write_json
from services.strategy_cycle_package import _hash_payload
from services.trading_daily_24h_report import TradingDaily24hReportBuilder


def _write_package(
    output: Path,
    cycle_id: str,
    *,
    positions: list[dict],
    fills: list[dict],
    realized: float,
) -> dict:
    payload = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "blockers": [],
        "packaged_at": "2026-07-18T01:00:00+00:00",
        "strategy_plan": {"strategy_plan_id": f"plan-{cycle_id}", "version": 1},
        "execution": {
            "engine": "nautilus_paper",
            "orders": [],
            "fills": fills,
            "positions": positions,
            "account": {"starting_cash": 10_000.0, "ending_cash": 10_000.0 + realized},
            "pnl": {"realized": realized, "unrealized": 0.0},
            "reconciliation": {"status": "ok", "issues": []},
        },
        "traceability": {
            "strategy_plan_id": f"plan-{cycle_id}",
            "strategy_plan_version": 1,
        },
    }
    payload["package_hash"] = _hash_payload(payload)
    write_json(
        output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json",
        [payload],
    )
    return payload


def _position(cycle_id: str, suffix: str, *, exit_ts: str, realized: float) -> dict:
    return {
        "position_id": f"{cycle_id}-{suffix}",
        "trade_id": f"trade-{cycle_id}-{suffix}",
        "status": "closed",
        "exit_ts": exit_ts,
        "realized_pnl": realized,
    }


def _fills(cycle_id: str, *, entry_ts: str, exit_ts: str) -> list[dict]:
    trade_id = f"trade-{cycle_id}-1"
    return [
        {
            "fill_id": f"{cycle_id}-entry",
            "trade_id": trade_id,
            "event": "entry",
            "ts": entry_ts,
            "price": 100.0,
            "quantity": 2.0,
            "realized_pnl": 999_999.0,
        },
        {
            "fill_id": f"{cycle_id}-exit",
            "trade_id": trade_id,
            "event": "target",
            "ts": exit_ts,
            "price": 101.0,
            "quantity": 2.0,
            "realized_pnl": 999_999.0,
        },
    ]


def _seed_complete_beijing_day(output: Path) -> list[dict]:
    packages = []
    cases = [
        (
            "2026-07-16_NIGHT",
            "2026-07-16T18:00:00+00:00",
            "2026-07-16T19:00:00+00:00",
            3.0,
        ),
        (
            "2026-07-17_DAY",
            "2026-07-17T02:00:00+00:00",
            "2026-07-17T03:00:00+00:00",
            -1.0,
        ),
        (
            "2026-07-17_NIGHT",
            "2026-07-17T14:00:00+00:00",
            "2026-07-17T15:00:00+00:00",
            4.0,
        ),
    ]
    for cycle_id, entry_ts, exit_ts, realized in cases:
        packages.append(_write_package(
            output,
            cycle_id,
            positions=[_position(cycle_id, "1", exit_ts=exit_ts, realized=realized)],
            fills=_fills(cycle_id, entry_ts=entry_ts, exit_ts=exit_ts),
            realized=realized,
        ))
    return packages


def test_beijing_day_uses_closed_positions_for_pnl_and_fills_only_for_events(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    packages = _seed_complete_beijing_day(output)

    path = TradingDaily24hReportBuilder(output).build(
        now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc),
        report_date="2026-07-17",
    )
    payload = load_json(output / "dualtrack" / "daily_reports" / "2026-07-17.json")[-1]
    text = path.read_text(encoding="utf-8")

    assert path.name == "2026-07-17-daily-24h.md"
    assert payload["window"] == {
        "start": "2026-07-16T16:00:00+00:00",
        "end": "2026-07-17T16:00:00+00:00",
    }
    assert payload["execution"] == {
        "trade_count": 3,
        "fill_count": 6,
        "total_notional": 1206.0,
        "realized_pnl": 6.0,
        "realized_pnl_source": "terminal_cycle_packages.execution.positions.closed",
        "fill_usage": "event_count_and_notional_only",
    }
    assert payload["nav"]["ending_equity"] == 10_006.0
    assert payload["nav"]["normalized_nav"] == 1.0006
    assert [row["package_hash"] for row in payload["provenance"]["cycle_packages"]] == [
        package["package_hash"] for package in packages
    ]
    assert "已实现 PnL +6.00 USD" in text
    assert "fills 仅用于次数和名义金额" in text
    assert packages[0]["package_hash"] in text
    assert payload["report_hash"] in text


def test_daily_report_is_idempotent_and_dashboard_exposes_same_nav(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _seed_complete_beijing_day(output)
    builder = TradingDaily24hReportBuilder(output)

    first = builder.build(
        now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc),
        report_date="2026-07-17",
    )
    second = builder.build(
        now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc),
        report_date="2026-07-17",
    )
    response = dashboard_server.build_strategy_console_daily_reports_response(output_root=output)

    assert first == second
    assert len(load_json(output / "dualtrack" / "daily_reports" / "2026-07-17.json")) == 1
    assert response["latest"]["nav"]["ending_equity"] == 10_006.0
    assert response["latest"]["report_hash"] == load_json(
        output / "dualtrack" / "daily_reports" / "2026-07-17.json"
    )[-1]["report_hash"]

    package_path = (
        output / "dualtrack" / "strategy_cycle_packages" / "2026-07-17_DAY.json"
    )
    original_package = load_json(package_path)
    corrupted_package = {**original_package[-1], "status": "blocked"}
    write_json(package_path, [corrupted_package])
    with pytest.raises(ValueError, match="package chain is invalid"):
        dashboard_server.build_strategy_console_daily_reports_response(output_root=output)
    write_json(package_path, original_package)

    artifact = output / "dualtrack" / "daily_reports" / "2026-07-17.json"
    tampered = load_json(artifact)[-1]
    tampered["nav"]["ending_equity"] = 99_999.0
    write_json(artifact, [tampered])
    with pytest.raises(ValueError, match="daily report hash is invalid"):
        dashboard_server.build_strategy_console_daily_reports_response(output_root=output)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_realized", "realized PnL is missing"),
        ("missing_positions", "closed-position evidence is missing"),
        ("nan_notional", "fill notional must be finite"),
        ("tampered_package", "package chain is invalid"),
    ],
)
def test_daily_report_fails_closed_on_incomplete_or_nonfinite_evidence(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    output = tmp_path / "outputs"
    _seed_complete_beijing_day(output)
    path = output / "dualtrack" / "strategy_cycle_packages" / "2026-07-17_DAY.json"
    package = load_json(path)[-1]
    if mutation == "missing_realized":
        package["execution"]["pnl"]["realized"] = None
        package.pop("package_hash", None)
        package["package_hash"] = _hash_payload(package)
    elif mutation == "missing_positions":
        package["execution"].pop("positions")
        package.pop("package_hash", None)
        package["package_hash"] = _hash_payload(package)
    elif mutation == "nan_notional":
        package["execution"]["fills"][0]["notional"] = float("nan")
        package.pop("package_hash", None)
        package["package_hash"] = _hash_payload(package)
    else:
        package["status"] = "blocked"
    write_json(path, [package])

    with pytest.raises(ValueError, match=message):
        TradingDaily24hReportBuilder(output).build(
            now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc),
            report_date="2026-07-17",
        )


def test_daily_report_waits_for_last_overlapping_cycle_to_close(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _seed_complete_beijing_day(output)

    with pytest.raises(ValueError, match="natural day is not terminal"):
        TradingDaily24hReportBuilder(output).build(
            now=datetime(2026, 7, 18, 0, 59, tzinfo=timezone.utc),
            report_date="2026-07-17",
        )
