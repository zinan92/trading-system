import json
from pathlib import Path

import pytest

from services import control_audit
from services.strategy_control_plane import StrategyControlPlane


def actor(email: str | None = "zinan92@hotmail.com", transport: str = "public_gateway") -> dict:
    return {"email": email, "transport": transport, "client": "127.0.0.1"}


def events_on_disk(output_root: Path) -> list[dict]:
    root = output_root / "dualtrack" / "strategy_control" / "control_events"
    rows: list[dict] = []
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rows.append(json.loads(line))
    return rows


def test_append_is_append_only_one_json_per_line(tmp_path: Path) -> None:
    for index in range(2):
        event = control_audit.build_control_event(
            cycle_id="2026-07-15_DAY",
            action="stop",
            actor=actor(),
            payload={"note": f"event-{index}"},
            result="accepted",
            error=None,
            runtime={"actual_state": "stopped"},
            now="2026-07-15T10:00:00+00:00",
        )
        control_audit.append_control_event(tmp_path, event)
    rows = events_on_disk(tmp_path)
    assert len(rows) == 2
    assert rows[0]["schema_version"] == "strategy-control-event-v1"
    assert rows[0]["request"]["note"] == "event-0"
    assert rows[1]["request"]["note"] == "event-1"


def test_event_bounds_payload_and_never_stores_credentials(tmp_path: Path) -> None:
    event = control_audit.build_control_event(
        cycle_id="2026-07-15_DAY",
        action="start",
        actor=actor(),
        payload={
            "grid": {"count": 40},
            "huge": "x" * 20_000,
            "Cf-Access-Jwt-Assertion": "secret-token",
            "authorization": "Bearer abc",
            "nested": {"api_token": "abc", "ok": 1},
        },
        result="accepted",
        error=None,
        runtime={},
        now="2026-07-15T10:00:00+00:00",
    )
    raw = json.dumps(event)
    assert "secret-token" not in raw
    assert "Bearer abc" not in raw
    assert event["request"]["nested"]["api_token"] == "[redacted]"
    assert len(raw) < 6_000  # oversized fields are truncated, event stays bounded
    assert event["actor"]["email"] == "zinan92@hotmail.com"


def test_event_bounds_safe_action_evidence_and_redacts_credentials(tmp_path: Path) -> None:
    event = control_audit.build_control_event(
        cycle_id="2026-07-15_DAY",
        action="stop",
        actor=actor(),
        payload={
            "apiKey": "request-api-key",
            "accessKey": "request-access-key",
            "privateKey": "request-private-key",
            "credential": "request-credential",
        },
        result="rejected",
        error=(
            "api_key=error-api-key token=error-token "
            "Authorization: Bearer error-bearer " + ("x" * 600)
        ),
        runtime={"actual_state": "stopped"},
        evidence={
            "safe_action_market_gates": [{
                "action_class": "reduce_only",
                "market_fresh": False,
                "pricing_source": "last_known_server_mark",
                "api_token": "must-not-survive",
                "apiKey": "evidence-api-key",
            }],
        },
        now="2026-07-15T10:00:00+00:00",
    )

    gate = event["evidence"]["safe_action_market_gates"][0]
    assert gate["action_class"] == "reduce_only"
    assert gate["market_fresh"] is False
    assert gate["api_token"] == "[redacted]"
    assert gate["apiKey"] == "[redacted]"
    assert event["request"] == {
        "apiKey": "[redacted]",
        "accessKey": "[redacted]",
        "privateKey": "[redacted]",
        "credential": "[redacted]",
    }
    assert "error-api-key" not in event["error"]
    assert "error-token" not in event["error"]
    assert "error-bearer" not in event["error"]
    assert len(event["error"]) <= 530

    control_audit.append_control_event(tmp_path, event)
    persisted = control_audit.read_last_control_event(tmp_path)
    raw = next(tmp_path.rglob("*.jsonl")).read_text(encoding="utf-8")
    assert persisted == event
    for secret in (
        "request-api-key",
        "request-access-key",
        "request-private-key",
        "request-credential",
        "evidence-api-key",
        "must-not-survive",
        "error-api-key",
        "error-token",
        "error-bearer",
    ):
        assert secret not in raw


def test_accepted_control_writes_attributed_event(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    result = plane.control(
        "2026-07-15_DAY", "reset_statistics", {},
        now="2026-07-15T10:00:00+00:00",
        actor=actor(),
    )
    assert result["audit_recorded"] is True
    rows = events_on_disk(tmp_path / "outputs")
    assert len(rows) == 1
    event = rows[0]
    assert event["action"] == "reset_statistics"
    assert event["result"] == "accepted"
    assert event["actor"] == {"email": "zinan92@hotmail.com", "transport": "public_gateway", "client": "127.0.0.1"}
    assert event["runtime_after"]["last_action"] == "reset_statistics"


def test_rejected_control_is_recorded_and_reraised(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError):
        plane.control(
            "2026-07-15_DAY", "warp_drive", {},
            now="2026-07-15T10:00:00+00:00",
            actor=actor(transport="local", email=None),
        )
    rows = events_on_disk(tmp_path / "outputs")
    assert len(rows) == 1
    assert rows[0]["result"] == "rejected"
    assert rows[0]["error"]
    assert rows[0]["actor"]["transport"] == "local"


def test_preview_is_not_audited(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    with pytest.raises(ValueError):
        # invalid empty market still proves the routing: preview never audits
        plane.control("2026-07-15_DAY", "preview", {}, market={}, actor=actor())
    assert events_on_disk(tmp_path / "outputs") == []


def test_audit_write_failure_does_not_block_the_action(tmp_path: Path, monkeypatch) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")

    def broken_append(root, event):
        raise OSError("disk full")

    monkeypatch.setattr("services.strategy_control_plane.append_control_event", broken_append)
    result = plane.control(
        "2026-07-15_DAY", "reset_statistics", {},
        now="2026-07-15T10:00:00+00:00",
        actor=actor(),
    )
    assert result["runtime"]["last_action"] == "reset_statistics"
    assert result["audit_recorded"] is False


def test_runtime_state_exposes_last_control_event(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    plane.control("2026-07-15_DAY", "reset_statistics", {}, now="2026-07-15T10:00:00+00:00", actor=actor())
    runtime = plane.runtime_state("2026-07-15_DAY")
    event = runtime["last_control_event"]
    assert event["action"] == "reset_statistics"
    assert event["actor"]["email"] == "zinan92@hotmail.com"
    assert event["actor"]["transport"] == "public_gateway"


def test_unattributed_control_defaults_to_local_actor(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    plane.control("2026-07-15_DAY", "reset_statistics", {}, now="2026-07-15T10:00:00+00:00")
    rows = events_on_disk(tmp_path / "outputs")
    assert rows[0]["actor"] == {"email": None, "transport": "local", "client": None}
