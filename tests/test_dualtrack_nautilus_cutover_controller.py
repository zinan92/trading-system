from __future__ import annotations

import json
import plistlib
from pathlib import Path

from services.journal_store import write_json
from pipelines.dualtrack_nautilus_cutover_apply import (
    ACCELERATED_GATE_OVERRIDE_ACKNOWLEDGEMENT,
    CUTOVER_ACKNOWLEDGEMENT,
    ROLLBACK_ACKNOWLEDGEMENT,
    DualTrackNautilusCutoverController,
)


LABELS = (
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.dashboard",
)


def _fixture(tmp_path: Path, *, ready: bool = True, validator=None):
    output = tmp_path / "outputs"
    config_path = tmp_path / "dualtrack.yaml"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    config_path.write_text(json.dumps({
        "execution_engine": {
            "authoritative": "legacy_paper",
            "shadow": "nautilus_paper",
            "real_money_eligible": False,
        },
    }), encoding="utf-8")
    for label in LABELS:
        with (launch_agents / f"{label}.plist").open("wb") as handle:
            plistlib.dump({
                "Label": label,
                "ProgramArguments": ["python3", "-m", "pipelines.dashboard_server"],
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            }, handle)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def quiesce(labels):
        calls.append(("quiesce", tuple(labels)))
        return [{"label": label, "status": "stopped"} for label in labels]

    def start(paths):
        calls.append(("start", tuple(path.stem for path in paths)))
        return [{"label": path.stem, "status": "started"} for path in paths]

    def default_validator(expected_engine: str):
        selected = json.loads(config_path.read_text(encoding="utf-8"))["execution_engine"]["authoritative"]
        return {"status": "ok" if selected == expected_engine else "blocked", "engine": selected}

    controller = DualTrackNautilusCutoverController(
        output,
        config_path=config_path,
        launch_agents_dir=launch_agents,
        environ={
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
            "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": "/isolated/nautilus/bin/python",
        },
        precheck_builder=lambda *args, **kwargs: {
            "status": "ready_for_operator_cutover" if ready else "blocked",
            "blockers": [] if ready else [{"code": "shadow_gate_not_ready", "detail": {}}],
            "cycle_id": "2026-07-16_DAY",
        },
        rollback_precheck_builder=lambda *args, **kwargs: {
            "status": "ready_for_rollback",
            "blockers": [],
        },
        service_quiescer=quiesce,
        service_starter=start,
        engine_validator=validator or default_validator,
    )
    return controller, output, config_path, launch_agents, calls


def test_cutover_blocker_performs_no_file_or_service_mutation(tmp_path: Path) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path, ready=False)
    before = {
        path: path.read_bytes()
        for path in [config_path, *(launch_agents / f"{label}.plist" for label in LABELS)]
    }

    result = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)

    assert result["status"] == "blocked"
    assert result["blocker"] == "precheck_not_ready"
    assert calls == []
    assert {path: path.read_bytes() for path in before} == before
    assert result["config_write_performed"] is False
    assert result["orders_submitted"] is False


def test_missing_attended_acknowledgement_does_not_run_precheck_or_touch_targets(tmp_path: Path) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path)
    before = {
        path: path.read_bytes()
        for path in [config_path, *(launch_agents / f"{label}.plist" for label in LABELS)]
    }

    result = controller.apply(acknowledgement="yes")

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_cutover_acknowledgement"
    assert calls == []
    assert {path: path.read_bytes() for path in before} == before


def test_attended_cutover_backs_up_files_persists_service_approval_and_validates(tmp_path: Path) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path)

    result = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)

    assert result["status"] == "applied"
    assert result["post_validation"]["engine"] == "nautilus_paper"
    assert json.loads(config_path.read_text(encoding="utf-8"))["execution_engine"]["authoritative"] == "nautilus_paper"
    for label in LABELS:
        with (launch_agents / f"{label}.plist").open("rb") as handle:
            env = plistlib.load(handle)["EnvironmentVariables"]
        assert env["TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED"] == "1"
        assert env["TRADING_ORCHESTRATOR_NAUTILUS_PYTHON"] == "/isolated/nautilus/bin/python"
    assert [row[0] for row in calls] == ["quiesce", "start"]
    assert all(Path(row["backup_path"]).exists() for row in result["backups"])
    assert result["orders_submitted"] is False
    assert result["real_money_eligible"] is False


def test_accelerated_cutover_requires_second_ack_and_persists_audited_override(tmp_path: Path) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path)

    blocked = controller.apply(
        acknowledgement=CUTOVER_ACKNOWLEDGEMENT,
        allow_shadow_gate_override=True,
        shadow_gate_override_acknowledgement="yes",
    )
    assert blocked["status"] == "blocked"
    assert blocked["blocker"] == "missing_shadow_gate_override_acknowledgement"
    assert calls == []

    result = controller.apply(
        acknowledgement=CUTOVER_ACKNOWLEDGEMENT,
        allow_shadow_gate_override=True,
        shadow_gate_override_acknowledgement=ACCELERATED_GATE_OVERRIDE_ACKNOWLEDGEMENT,
    )

    assert result["status"] == "applied"
    assert result["shadow_gate_override"]["used"] is True
    for label in LABELS:
        with (launch_agents / f"{label}.plist").open("rb") as handle:
            env = plistlib.load(handle)["EnvironmentVariables"]
        assert env["TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE"] == ACCELERATED_GATE_OVERRIDE_ACKNOWLEDGEMENT


def test_failed_post_validation_automatically_restores_every_file(tmp_path: Path) -> None:
    validation_calls: list[str] = []

    def validator(expected_engine: str):
        validation_calls.append(expected_engine)
        return {
            "status": "blocked" if expected_engine == "nautilus_paper" else "ok",
            "engine": "legacy_paper" if expected_engine == "legacy_paper" else "unknown",
        }

    controller, output, config_path, launch_agents, calls = _fixture(tmp_path, validator=validator)
    before = {
        path: path.read_bytes()
        for path in [config_path, *(launch_agents / f"{label}.plist" for label in LABELS)]
    }

    result = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)

    assert result["status"] == "rolled_back_after_failed_validation"
    assert validation_calls == ["nautilus_paper", "legacy_paper"]
    assert [row[0] for row in calls] == ["quiesce", "start", "quiesce", "start"]
    assert {path: path.read_bytes() for path in before} == before
    assert result["rollback_validation"]["status"] == "ok"


def test_automatic_rollback_failure_is_persisted_as_explicit_incident(tmp_path: Path, monkeypatch) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(
        tmp_path,
        validator=lambda expected: {"status": "blocked", "engine": "unknown"},
    )
    monkeypatch.setattr(
        controller,
        "_restore_backups",
        lambda backups: (_ for _ in ()).throw(OSError("backup restore failed")),
    )

    result = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)

    assert result["status"] == "automatic_rollback_failed"
    assert result["automatic_rollback"]["status"] == "failed"
    assert "backup restore failed" in result["automatic_rollback"]["error"]
    saved = json.loads((output / "dualtrack" / "cutover" / "apply_current.json").read_text(encoding="utf-8"))[-1]
    assert saved["status"] == "automatic_rollback_failed"


def test_explicit_rollback_restores_apply_backup_byte_for_byte(tmp_path: Path) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path)
    before = {
        path: path.read_bytes()
        for path in [config_path, *(launch_agents / f"{label}.plist" for label in LABELS)]
    }
    applied = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)

    result = controller.rollback(applied, acknowledgement=ROLLBACK_ACKNOWLEDGEMENT)

    assert result["status"] == "rolled_back"
    assert result["post_validation"]["engine"] == "legacy_paper"
    assert {path: path.read_bytes() for path in before} == before
    assert result["orders_submitted"] is False
    assert result["real_money_eligible"] is False


def test_default_rollback_precheck_requires_stopped_flat_reconciled_nautilus(tmp_path: Path, monkeypatch) -> None:
    controller, output, config_path, launch_agents, calls = _fixture(tmp_path)
    applied = controller.apply(acknowledgement=CUTOVER_ACKNOWLEDGEMENT)
    cycle_id = applied["cycle_id"]
    write_json(output / "dualtrack" / "strategy_control" / "runtime.json", [{
        "cycle_id": cycle_id,
        "desired_state": "stopped",
        "actual_state": "stopped",
    }])

    class FakeAdapter:
        def snapshot(self, selected_cycle):
            return {"orders": [], "positions": []}

        def reconcile(self, selected_cycle):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.dualtrack_execution_adapter.build_configured_execution_engine_adapter",
        lambda *args, **kwargs: FakeAdapter(),
    )

    result = controller._default_rollback_precheck(
        output,
        config=json.loads(config_path.read_text(encoding="utf-8")),
        environ=controller.environ,
        cycle_id=cycle_id,
    )

    assert result["status"] == "ready_for_rollback"
    assert result["blockers"] == []
