from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import write_json
from services.park_public_read_model import build_park_public_read_model


NOW = datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(root: Path) -> None:
    identity = {
        "event": "session_started",
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "plan_digest": "sha256:" + "a" * 64,
        "observed_at": "2026-08-16T23:00:00+00:00",
    }
    lifecycle = {
        **identity,
        "event": "plan_activated",
        "state": "ACTIVE_LOCKED",
        "strategy_type": "grid",
        "direction": "neutral",
        "lower_price_boundary": 4100.0,
        "upper_price_boundary": 4450.0,
        "maximum_leverage": 20.0,
        "maximum_acceptable_loss": 1200.0,
    }
    plan = {
        **identity,
        "event": "plan_proposed",
        "normalized_input": {
            "strategy_type": "grid",
            "direction": "neutral",
            "lower_price_boundary": 4100.0,
            "upper_price_boundary": 4450.0,
            "maximum_leverage": 20.0,
        },
        "risk": {
            "maximum_notional": 20000.0,
            "theoretical_max_loss": 1200.0,
            "order_count": 30,
            "selected_constraint": "maximum_leverage",
        },
    }
    _append_jsonl(root / "park_strategy" / "identity.jsonl", [identity])
    _append_jsonl(root / "park_strategy" / "lifecycle.jsonl", [lifecycle])
    _append_jsonl(root / "park_strategy" / "plans.jsonl", [plan])
    write_json(
        root
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "park-session-1.json",
        [
            {
                "engine": "nautilus_paper",
                "cycle_id": "park-session-1",
                "mark": {"price": 4400.0, "fresh": True, "source": "trusted-test"},
                "capabilities": {"paper_only": True, "immutable_fill_guard": True},
                "reconciliation": {"status": "ok", "issues": []},
                "orders": [
                    {"order_id": "order-1", "state": "accepted", "side": "buy", "price": 4300.0},
                    {"order_id": "order-2", "state": "filled", "side": "sell", "price": 4400.0},
                ],
                "fills": [{"fill_id": "fill-1", "side": "sell", "price": 4400.0}],
                "positions": [
                    {"position_id": "position-1", "status": "open", "side": "short", "remaining_units": 1.0},
                    {"position_id": "position-2", "status": "closed", "side": "long", "remaining_units": 0.0},
                ],
                "account": {"equity": 10025.0},
                "pnl": {"realized": 30.0, "unrealized": -5.0},
            }
        ],
    )
    write_json(
        root / "park_strategy" / "safety_evidence.json",
        [
            {
                "status": "pass",
                "checked_at": "2026-08-17T00:00:00+00:00",
                "release_sha": "b" * 40,
                "tracked_tree_clean": True,
                "boot_verified": True,
                "paper_only": True,
                "immutable_fill": True,
                "supervisor_fail_closed": True,
            }
        ],
    )


def _digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_public_read_model_projects_persisted_facts_without_writes(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    before = _digests(output)

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert _digests(output) == before
    assert result["status"] == "ok"
    assert result["blockers"] == []
    assert result["viewer"] == {
        "mode": "public_read_only",
        "paper_only": True,
        "control_plane": "telegram_only",
        "mutations_allowed": False,
    }
    assert result["strategy"]["strategy_session_id"] == "session-1"
    assert result["strategy"]["direction"] == "neutral"
    assert result["strategy"]["maximum_leverage"] == 20.0
    assert result["execution"]["counts"] == {
        "accepted_orders": 1,
        "filled_orders": 1,
        "fills": 1,
        "open_positions": 1,
        "closed_positions": 1,
    }
    assert result["execution"]["reconciliation"]["status"] == "ok"
    assert result["market"] == {
        "price": 4400.0,
        "fresh": True,
        "source": "trusted-test",
    }


def test_public_read_model_fails_closed_when_facts_are_missing(tmp_path: Path) -> None:
    result = build_park_public_read_model(tmp_path / "missing", now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "active_strategy_missing" in result["blockers"]
    assert "safety_evidence_not_passing" in result["blockers"]
    assert result["execution"]["orders"] == []
    assert result["viewer"]["mutations_allowed"] is False


def test_public_read_model_fails_closed_on_corrupt_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    snapshot = (
        output
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "park-session-1.json"
    )
    snapshot.write_text("not-json", encoding="utf-8")

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "park_snapshot_invalid" in result["blockers"]
    assert "authoritative_snapshot_missing" in result["blockers"]
    assert result["execution"]["engine"] == "unavailable"
