from __future__ import annotations

import subprocess
from pathlib import Path

from services.mac_paper_scheduler_isolation import (
    RESTORE_ACKNOWLEDGEMENT,
    MacPaperSchedulerIsolation,
)
from services.schedule_profiles import FOCUS_SCHEDULE_LABELS
from services.scheduler_ownership import SchedulerOwnershipStore


class FakeLaunchctl:
    def __init__(self, *, loaded: bool = True, fail_label: str = "") -> None:
        self.loaded = {label: loaded for label in FOCUS_SCHEDULE_LABELS}
        self.disabled = {label: False for label in FOCUS_SCHEDULE_LABELS}
        self.fail_label = fail_label
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess:
        self.commands.append(command)
        action = command[1]
        target = command[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        if action == "print-disabled":
            text = "\n".join(
                f'    "{item}" => {str(value).lower()}'
                for item, value in self.disabled.items()
            )
            return subprocess.CompletedProcess(command, 0, stdout=text, stderr="")
        if action == "print":
            return subprocess.CompletedProcess(
                command, 0 if self.loaded[label] else 113, stdout="", stderr=""
            )
        if label == self.fail_label:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
        if action == "disable":
            self.disabled[label] = True
        elif action == "bootout":
            self.loaded[label] = False
        elif action == "enable":
            self.disabled[label] = False
        elif action == "bootstrap":
            self.loaded[label] = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def simulate_login(self) -> None:
        for label in FOCUS_SCHEDULE_LABELS:
            if not self.disabled[label]:
                self.loaded[label] = True


def _service(tmp_path: Path, fake: FakeLaunchctl) -> MacPaperSchedulerIsolation:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    for label in FOCUS_SCHEDULE_LABELS:
        (agents / f"{label}.plist").write_text("plist", encoding="utf-8")
    return MacPaperSchedulerIsolation(
        tmp_path / "outputs",
        launch_agents_dir=agents,
        uid=501,
        command_runner=fake,
        platform="darwin",
        release_gate_validator=lambda: {"ok": True, "status": "pass"},
    )


def _paused_owner(tmp_path: Path) -> None:
    store = SchedulerOwnershipStore(tmp_path / "outputs")
    store.initialize_local()
    store.pause(expected_owner_id="local-mac", expected_epoch=1)


def test_isolate_disables_and_unloads_exact_focus_allowlist_across_login(
    tmp_path: Path,
) -> None:
    fake = FakeLaunchctl()
    _paused_owner(tmp_path)
    result = _service(tmp_path, fake).isolate()
    assert result["status"] == "pass"
    assert result["labels"] == FOCUS_SCHEDULE_LABELS
    assert all(fake.disabled.values())
    assert not any(fake.loaded.values())

    fake.simulate_login()
    assert not any(fake.loaded.values())
    touched = {
        command[-1].rsplit("/", 1)[-1].removesuffix(".plist")
        for command in fake.commands
        if command[1] in {"disable", "bootout", "enable", "bootstrap"}
    }
    assert touched == set(FOCUS_SCHEDULE_LABELS)


def test_restore_requires_active_local_owner_and_acknowledgement(
    tmp_path: Path,
) -> None:
    fake = FakeLaunchctl(loaded=False)
    fake.disabled = {label: True for label in FOCUS_SCHEDULE_LABELS}
    store = SchedulerOwnershipStore(tmp_path / "outputs")
    store.initialize_local()
    service = _service(tmp_path, fake)

    blocked = service.restore()
    assert blocked["status"] == "blocked"
    assert blocked["blocker"] == "missing_restore_acknowledgement"
    assert not any(command[1] == "enable" for command in fake.commands)

    restored = service.restore(acknowledgement=RESTORE_ACKNOWLEDGEMENT)
    assert restored["status"] == "pass"
    assert all(fake.loaded.values())
    assert not any(fake.disabled.values())


def test_partial_disable_failure_blocks_and_keeps_other_labels_fail_closed(
    tmp_path: Path,
) -> None:
    failed_label = FOCUS_SCHEDULE_LABELS[1]
    fake = FakeLaunchctl(fail_label=failed_label)
    _paused_owner(tmp_path)
    result = _service(tmp_path, fake).isolate()
    assert result["status"] == "blocked"
    assert result["blocker"] == "mac_scheduler_isolation_command_failed"
    assert all(
        fake.disabled[label]
        for label in FOCUS_SCHEDULE_LABELS
        if label != failed_label
    )
    assert result["safety"]["touches_live"] is False


def test_isolate_refuses_while_local_owner_is_active(tmp_path: Path) -> None:
    fake = FakeLaunchctl()
    SchedulerOwnershipStore(tmp_path / "outputs").initialize_local()
    result = _service(tmp_path, fake).isolate()
    assert result["status"] == "blocked"
    assert result["blocker"] == "local_scheduler_ownership_not_paused"
    assert not any(command[1] == "disable" for command in fake.commands)
