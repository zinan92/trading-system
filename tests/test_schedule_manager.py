import os
from pathlib import Path
import plistlib
import subprocess

from services.journal_store import load_json, write_json
from services.schedule_installer import (
    SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
    SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
    ScheduleInstaller,
)
from services.schedule_manager import ScheduleManager
from services.schedule_post_install_verifier import SchedulePostInstallVerifier
from services.schedule_status import ScheduleStatus
from services.schedule_takeover_package import ScheduleTakeoverPackage

FULL_SCHEDULE_LABELS = {
    "com.wendy.trading-orchestrator.runner",
    "com.wendy.trading-orchestrator.trading-plan",
    "com.wendy.trading-orchestrator.evening-review",
    "com.wendy.trading-orchestrator.daily-review",
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.strategies",
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.deadman-ping",
}

FOCUS_SCHEDULE_LABELS = {
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.deadman-ping",
    "com.wendy.trading-orchestrator.gold-1m-feed",
}


def _full_schedule_manager(root: Path, repo: Path) -> ScheduleManager:
    return ScheduleManager(root, repo, profile="full")


def _takeover_package_id(
    root: Path,
    launch_agents: Path,
    runner,
    run_date: str = "2026-05-26",
) -> str:
    return ScheduleTakeoverPackage(root, launch_agents, runner).run(run_date)["package_id"]


def _stage_stale_launch_agents(schedule: dict, launch_agents: Path, *, stale_label: str = "com.wendy.trading-orchestrator.runner") -> None:
    launch_agents.mkdir(parents=True, exist_ok=True)
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == stale_label)
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = [*payload["ProgramArguments"], "--stale"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)


def _stateful_launchd_runner(initial_labels: list[str] | None = None, commands: list[list[str]] | None = None):
    loaded = {f"gui/{os.getuid()}/{label}" for label in (initial_labels or [])}

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        if commands is not None:
            commands.append(command)
        if command[:2] == ["launchctl", "bootout"]:
            service = command[2]
            if service in loaded:
                loaded.remove(service)
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 113, "", "not loaded")
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, "loaded", "") if command[2] in loaded else subprocess.CompletedProcess(command, 113, "", "not found")
        if command[:2] == ["launchctl", "bootstrap"]:
            loaded.add(command[2] + "/" + Path(command[3]).stem)
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    return fake_runner


def test_schedule_manager_generates_full_launch_agent_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)

    labels = {item["label"] for item in result["jobs"]}
    assert labels == FULL_SCHEDULE_LABELS
    assert result["profile"] == "full"
    assert result["status"] == "generated"
    assert "launchctl bootstrap" in "\n".join(result["install_commands"])
    assert (root / "schedules" / "README.md").exists()
    current = load_json(root / "schedules" / "current.json")[0]
    assert current["launch_agents_dir"].endswith("launch_agents")

    runner_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.runner.plist"
    trading_plan_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.trading-plan.plist"
    evening_review_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.evening-review.plist"
    daily_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.daily-review.plist"
    dashboard_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.dashboard.plist"
    deadman_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.deadman-ping.plist"
    with runner_plist.open("rb") as handle:
        runner = plistlib.load(handle)
    with trading_plan_plist.open("rb") as handle:
        trading_plan = plistlib.load(handle)
    with evening_review_plist.open("rb") as handle:
        evening_review = plistlib.load(handle)
    with daily_plist.open("rb") as handle:
        daily = plistlib.load(handle)
    with dashboard_plist.open("rb") as handle:
        dashboard = plistlib.load(handle)
    with deadman_plist.open("rb") as handle:
        deadman = plistlib.load(handle)

    assert runner["StartInterval"] == 300
    assert runner["ProgramArguments"][:3] == ["python3", "-m", "pipelines.runner"]
    assert "--date" not in runner["ProgramArguments"]
    assert runner["EnvironmentVariables"]["TZ"] == "UTC"
    assert trading_plan["StartCalendarInterval"] == {"Hour": 8, "Minute": 30}
    assert trading_plan["ProgramArguments"] == ["python3", "-m", "pipelines.trading_plan"]
    assert evening_review["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}
    assert evening_review["ProgramArguments"] == ["python3", "-m", "pipelines.evening_review"]
    assert daily["StartCalendarInterval"] == {"Hour": 22, "Minute": 30}
    assert daily["ProgramArguments"] == ["python3", "-m", "pipelines.daily_review"]
    assert dashboard["KeepAlive"] is True
    assert "9876" in dashboard["ProgramArguments"]
    assert deadman["StartInterval"] == 300
    assert deadman["RunAtLoad"] is True
    assert deadman["ProgramArguments"] == ["python3", "-m", "pipelines.deadman_ping"]
    assert deadman["EnvironmentVariables"]["TZ"] == "UTC"

    strategies_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.strategies.plist"
    with strategies_plist.open("rb") as handle:
        strategies = plistlib.load(handle)
    assert strategies["StartInterval"] == 300
    assert strategies["ProgramArguments"][:3] == ["python3", "-m", "pipelines.strategies"]
    assert "--paper-auto-approve" in strategies["ProgramArguments"]
    assert strategies["EnvironmentVariables"]["TZ"] == "UTC"

    dualtrack_live_tick_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.dualtrack-live-tick.plist"
    with dualtrack_live_tick_plist.open("rb") as handle:
        dualtrack_live_tick = plistlib.load(handle)
    assert dualtrack_live_tick["StartInterval"] == 60
    assert dualtrack_live_tick["RunAtLoad"] is True
    assert dualtrack_live_tick["ProgramArguments"] == ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "live-tick"]
    assert dualtrack_live_tick["EnvironmentVariables"]["TRADING_ORCHESTRATOR_MARKET_DB"].endswith("data/market_data.db")


def test_schedule_manager_generates_dualtrack_focus_profile_and_removes_stale_generated_plists(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build()

    result = ScheduleManager(root, repo, profile="dualtrack_focus").build(dashboard_port=9876)

    labels = {item["label"] for item in result["jobs"]}
    launch_dir = Path(result["launch_agents_dir"])
    generated_files = {path.stem for path in launch_dir.glob("com.wendy.trading-orchestrator.*.plist")}
    assert result["profile"] == "dualtrack_focus"
    assert labels == FOCUS_SCHEDULE_LABELS
    assert generated_files == FOCUS_SCHEDULE_LABELS
    assert "com.wendy.trading-orchestrator.runner" not in labels
    assert "com.wendy.trading-orchestrator.strategies" not in labels
    gold_feed_plist = launch_dir / "com.wendy.trading-orchestrator.gold-1m-feed.plist"
    with gold_feed_plist.open("rb") as handle:
        gold_feed = plistlib.load(handle)
    assert gold_feed["StartInterval"] == 60
    assert gold_feed["RunAtLoad"] is True
    assert gold_feed["ProgramArguments"] == ["python3", "-m", "pipelines.gold_1m_feed_heartbeat"]


def test_strategies_job_uses_dedicated_python_others_unchanged(tmp_path: Path, monkeypatch):
    """The chan strategy needs Python >= 3.11 + pandas, so the strategies job
    runs on a dedicated interpreter while the runner/daily-review/dashboard jobs
    keep the base (3.9) python untouched."""
    monkeypatch.setenv("TRADING_ORCHESTRATOR_STRATEGIES_PYTHON", "/usr/local/bin/python3")
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _full_schedule_manager(root, repo).build()
    plists = {}
    for job in result["jobs"]:
        with Path(job["plist"]).open("rb") as handle:
            plists[job["label"]] = plistlib.load(handle)

    assert plists["com.wendy.trading-orchestrator.strategies"]["ProgramArguments"][0] == "/usr/local/bin/python3"
    for label in (
        "com.wendy.trading-orchestrator.runner",
        "com.wendy.trading-orchestrator.trading-plan",
        "com.wendy.trading-orchestrator.evening-review",
        "com.wendy.trading-orchestrator.daily-review",
        "com.wendy.trading-orchestrator.dashboard",
        "com.wendy.trading-orchestrator.dualtrack-live-tick",
        "com.wendy.trading-orchestrator.deadman-ping",
    ):
        assert plists[label]["ProgramArguments"][0] == "python3"


def test_schedule_manager_loads_deadman_urls_from_live_env_when_shell_env_is_clean(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", raising=False)
    live_env = tmp_path / "live.env"
    live_env.write_text(
        "\n".join(
            [
                "TRADING_ORCHESTRATOR_DEADMAN_URL=https://example.invalid/deadman",
                "TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL=https://example.invalid/position",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(live_env))
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _full_schedule_manager(root, repo).build()

    for job in result["jobs"]:
        with Path(job["plist"]).open("rb") as handle:
            env = plistlib.load(handle)["EnvironmentVariables"]
        assert env["TRADING_ORCHESTRATOR_DEADMAN_URL"] == "https://example.invalid/deadman"
        assert env["TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL"] == "https://example.invalid/position"


def test_schedule_manager_omits_deadman_urls_when_live_env_and_shell_env_are_empty(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", raising=False)
    live_env = tmp_path / "live.env"
    live_env.write_text("TRADING_ORCHESTRATOR_DEADMAN_URL=\nTRADING_ORCHESTRATOR_DEADMAN_POSITION_URL=\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(live_env))
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _full_schedule_manager(root, repo).build()

    for job in result["jobs"]:
        with Path(job["plist"]).open("rb") as handle:
            env = plistlib.load(handle)["EnvironmentVariables"]
        assert "TRADING_ORCHESTRATOR_DEADMAN_URL" not in env
        assert "TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL" not in env


def test_schedule_manager_builds_byte_identical_plists_from_same_live_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_URL", raising=False)
    monkeypatch.delenv("TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL", raising=False)
    live_env = tmp_path / "live.env"
    live_env.write_text("TRADING_ORCHESTRATOR_DEADMAN_URL=https://example.invalid/deadman\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(live_env))
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _full_schedule_manager(root, repo)

    first = manager.build()
    first_bytes = {job["label"]: Path(job["plist"]).read_bytes() for job in first["jobs"]}
    second = manager.build()
    second_bytes = {job["label"]: Path(job["plist"]).read_bytes() for job in second["jobs"]}

    assert second_bytes == first_bytes


def test_schedule_status_distinguishes_generated_from_installed(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)

    result = ScheduleStatus(
        root,
        launch_agents_dir=tmp_path / "LaunchAgents",
        command_runner=lambda command: subprocess.CompletedProcess(command, 113, "", "not found"),
    ).run("2026-05-26")

    assert result["status"] == "generated_only"
    assert result["installed_count"] == 0
    assert result["loaded_count"] == 0
    assert result["matching_generated_count"] == 0
    assert result["active_current_count"] == 0
    assert result["missing_installed_jobs"]
    assert load_json(root / "schedules" / "status_current.json")[0]["status"] == "generated_only"


def test_schedule_status_detects_stale_loaded_launch_agents(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)

    result = ScheduleStatus(
        root,
        launch_agents_dir=launch_agents,
        command_runner=lambda command: subprocess.CompletedProcess(command, 0, "loaded", ""),
    ).run("2026-05-26")

    assert result["status"] == "stale_installed"
    assert result["installed_count"] == 8
    assert result["loaded_count"] == 8
    assert result["matching_generated_count"] == 7
    assert result["active_current_count"] == 7
    assert result["mismatched_jobs"] == ["com.wendy.trading-orchestrator.dualtrack-live-tick"]
    assert result["jobs"][-2]["installed"] is True
    assert result["jobs"][-2]["loaded"] is True
    assert result["jobs"][-2]["matches_generated"] is False


def test_schedule_installer_plan_reports_stale_loaded_job_without_modifying_launchd(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "loaded", "")

    result = ScheduleInstaller(root, launch_agents, fake_runner).plan("2026-05-26")
    live_tick = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")

    assert result["status"] == "ready"
    assert result["mode"] == "dry_run"
    assert result["summary"]["requires_reinstall_count"] == 1
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert result["safety"]["runs_launchctl_print"] is True
    assert live_tick["action"] == "replace_stale_and_restart"
    assert ["launchctl", "bootout", live_tick["planned_commands"][0][2]] == live_tick["planned_commands"][0]
    assert live_tick["planned_commands"][1][0] == "copy"
    assert live_tick["planned_commands"][2][0:2] == ["launchctl", "bootstrap"]
    assert live_tick["planned_commands"][3][0:2] == ["launchctl", "kickstart"]
    assert all(command[:2] == ["launchctl", "print"] for command in commands)
    with stale_plist.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    assert load_json(root / "schedules" / "install_plan_current.json")[0]["status"] == "ready"


def test_schedule_installer_plan_warns_no_restart_only_stages_loaded_stale_job(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)

    result = ScheduleInstaller(
        root,
        launch_agents,
        lambda command: subprocess.CompletedProcess(command, 0, "loaded", ""),
    ).plan("2026-05-26", restart_loaded=False)
    live_tick = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")

    assert live_tick["action"] == "stage_current_plist_without_restart"
    assert "may keep running the old definition" in live_tick["note"]
    assert live_tick["planned_commands"][0][0] == "copy"
    assert live_tick["planned_commands"][1][:2] == ["launchctl", "print"]
    assert not any(command[:2] == ["launchctl", "bootout"] for command in live_tick["planned_commands"])
    assert not any(command[:2] == ["launchctl", "kickstart"] for command in live_tick["planned_commands"])


def test_schedule_installer_plan_marks_full_profile_jobs_as_orphans_in_focus_profile(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    full = _full_schedule_manager(root, repo).build()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in full["jobs"]:
        source = Path(job["plist"])
        (launch_agents / source.name).write_bytes(source.read_bytes())
    ScheduleManager(root, repo, profile="dualtrack_focus").build()
    commands = []
    runner = _stateful_launchd_runner(sorted(FULL_SCHEDULE_LABELS), commands)

    result = ScheduleInstaller(root, launch_agents, runner).plan("2026-05-26")

    orphan_labels = {item["label"] for item in result["orphans"]}
    assert result["status"] == "ready"
    assert result["summary"]["orphan_count"] == 5
    assert orphan_labels == FULL_SCHEDULE_LABELS - FOCUS_SCHEDULE_LABELS
    assert all(label.startswith("com.wendy.trading-orchestrator.") for label in orphan_labels)
    assert all(item["action"] == "remove_orphan_and_bootout" for item in result["orphans"])


def test_schedule_installer_removes_focus_orphans_and_rollback_restores_them(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    full = _full_schedule_manager(root, repo).build()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in full["jobs"]:
        source = Path(job["plist"])
        (launch_agents / source.name).write_bytes(source.read_bytes())
    ScheduleManager(root, repo, profile="dualtrack_focus").build()
    commands = []
    runner = _stateful_launchd_runner(sorted(FULL_SCHEDULE_LABELS), commands)
    installer = ScheduleInstaller(
        root,
        launch_agents,
        runner,
        sleep=lambda seconds: None,
        teardown_poll_seconds=0,
        dashboard_poll_seconds=0,
        dashboard_timeout_seconds=1,
    )
    package_id = _takeover_package_id(root, launch_agents, runner)

    result = installer.install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)

    assert result["status"] == "active"
    assert {path.stem for path in launch_agents.glob("com.wendy.trading-orchestrator.*.plist")} == FOCUS_SCHEDULE_LABELS
    assert {item["label"] for item in result["orphans"]} == FULL_SCHEDULE_LABELS - FOCUS_SCHEDULE_LABELS
    assert all(item["status"] == "removed" for item in result["orphans"])
    assert result["backup_count"] == 8

    rollback = installer.rollback("2026-05-26", acknowledgement=SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT)

    assert rollback["status"] == "rolled_back"
    assert {path.stem for path in launch_agents.glob("com.wendy.trading-orchestrator.*.plist")} == FULL_SCHEDULE_LABELS
    assert len(rollback["jobs"]) == 9
    gold_feed = next(job for job in rollback["jobs"] if job["label"] == "com.wendy.trading-orchestrator.gold-1m-feed")
    assert gold_feed["status"] == "removed"


def test_schedule_installer_blocks_apply_without_acknowledgement(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    result = ScheduleInstaller(root, launch_agents, fake_runner).install("2026-05-26")

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_acknowledgement"
    assert result["acknowledgement_ok"] is False
    assert result["required_acknowledgement"] == SCHEDULE_INSTALL_ACKNOWLEDGEMENT
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)
    assert load_json(root / "schedules" / "install_current.json")[0]["status"] == "blocked"


def test_schedule_installer_blocks_apply_without_package_id_after_acknowledgement(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_package_id"
    assert result["acknowledgement_ok"] is True
    assert result["package_gate"]["ok"] is False
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)


def test_schedule_installer_blocks_apply_with_stale_package_id(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    expected = _takeover_package_id(root, launch_agents, fake_runner)
    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id="stale-package-id",
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "stale_or_mismatched_package"
    assert result["package_gate"]["expected_package_id"] == expected
    assert result["package_gate"]["provided_package_id"] == "stale-package-id"
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)


def test_schedule_installer_blocks_apply_when_takeover_package_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id="missing-artifact-package",
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_takeover_package"
    assert result["package_gate"]["provided_package_id"] == "missing-artifact-package"
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)


def test_schedule_installer_blocks_apply_when_takeover_package_expired(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    package_path = root / "schedules" / "takeover_package_current.json"
    package = load_json(package_path)[-1]
    package["expires_at"] = "2026-01-01T00:00:00+00:00"
    write_json(package_path, [package])

    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "expired_package"
    assert result["package_gate"]["expected_package_id"] == package_id
    assert result["package_gate"]["expires_at"] == "2026-01-01T00:00:00+00:00"
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)


def test_schedule_installer_blocks_apply_when_takeover_package_not_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    package_path = root / "schedules" / "takeover_package_current.json"
    package = load_json(package_path)[-1]
    package["status"] = "blocked"
    write_json(package_path, [package])

    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )

    assert result["status"] == "blocked"
    assert result["blocker"] == "takeover_package_not_ready"
    assert result["package_gate"]["expected_package_id"] == package_id
    assert result["package_gate"]["status"] == "blocked"
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert not launch_agents.exists()
    assert all(command[:2] == ["launchctl", "print"] for command in commands)


def test_schedule_status_detects_installed_and_loaded_jobs(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())

    result = ScheduleStatus(
        root,
        launch_agents_dir=launch_agents,
        command_runner=lambda command: subprocess.CompletedProcess(command, 0, "loaded", ""),
    ).run("2026-05-26")

    assert result["status"] == "active"
    assert result["installed_count"] == 8
    assert result["loaded_count"] == 8
    assert result["matching_generated_count"] == 8
    assert result["active_current_count"] == 8
    assert result["healthy_current_count"] == 8
    assert all(job["matches_generated"] for job in result["jobs"])


def test_schedule_status_rejects_loaded_job_with_failed_last_execution(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        (launch_agents / source.name).write_bytes(source.read_bytes())

    failed_label = "com.wendy.trading-orchestrator.dualtrack-live-tick"

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        if command[-1].endswith(failed_label):
            stdout = "state = not running\nruns = 296\nlast exit code = 78: EX_CONFIG\n"
        else:
            stdout = "state = not running\nruns = 10\nlast exit code = 0\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = ScheduleStatus(root, launch_agents_dir=launch_agents, command_runner=fake_runner).run("2026-07-13")

    assert result["status"] == "runtime_failed"
    assert result["active_current_count"] == 8
    assert result["healthy_current_count"] == 7
    assert result["runtime_failed_jobs"] == [failed_label]
    failed_job = next(job for job in result["jobs"] if job["label"] == failed_label)
    assert failed_job["loaded"] is True
    assert failed_job["runtime_healthy"] is False
    assert failed_job["last_exit_code"] == 78


def test_schedule_installer_copies_plists_and_records_receipt(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"

    fake_runner = _stateful_launchd_runner()

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )

    assert result["status"] == "active"
    assert result["acknowledgement_ok"] is True
    assert result["schedule_status"]["loaded_count"] == 8
    assert len(list(launch_agents.glob("com.wendy.trading-orchestrator.*.plist"))) == 8
    assert load_json(root / "schedules" / "install_current.json")[0]["status"] == "active"
    assert all(job["status"] == "installed" for job in result["jobs"])


def test_schedule_installer_backs_up_existing_plists_before_replacing(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)

    fake_runner = _stateful_launchd_runner([job["label"] for job in schedule["jobs"]])
    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )
    live_tick = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")

    assert result["status"] == "active"
    assert result["backup_count"] == 8
    assert Path(result["backup_dir"]).exists()
    assert live_tick["backup"].endswith("com.wendy.trading-orchestrator.dualtrack-live-tick.plist")
    with Path(live_tick["backup"]).open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] != ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]


def test_schedule_installer_waits_for_bootout_to_disappear_before_bootstrap(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    _stage_stale_launch_agents(schedule, launch_agents)
    commands = []
    print_after_bootout = {}
    bootstrapped = set()

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        if command[:2] == ["launchctl", "bootout"]:
            print_after_bootout[command[2]] = 0
            bootstrapped.discard(command[2])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["launchctl", "print"]:
            service = command[2]
            if service in bootstrapped:
                return subprocess.CompletedProcess(command, 0, "loaded", "")
            if service in print_after_bootout:
                print_after_bootout[service] += 1
                if print_after_bootout[service] < 3:
                    return subprocess.CompletedProcess(command, 0, "still loaded", "")
                return subprocess.CompletedProcess(command, 113, "", "not found")
            return subprocess.CompletedProcess(command, 0 if service in bootstrapped else 113, "loaded", "")
        if command[:2] == ["launchctl", "bootstrap"]:
            bootstrapped.add(command[2] + "/" + Path(command[3]).stem)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    commands.clear()
    result = ScheduleInstaller(
        root,
        launch_agents,
        fake_runner,
        sleep=lambda seconds: None,
        teardown_poll_seconds=0,
        teardown_timeout_seconds=1,
        dashboard_poll_seconds=0,
        dashboard_timeout_seconds=1,
    ).install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    runner_service = f"gui/__UID__/com.wendy.trading-orchestrator.runner"
    normalized = [
        [runner_service if item.startswith("gui/") and item.endswith("com.wendy.trading-orchestrator.runner") else item for item in command]
        for command in commands
    ]
    runner_bootout_index = next(i for i, command in enumerate(normalized) if command[:2] == ["launchctl", "bootout"] and command[2] == runner_service)
    runner_bootstrap_index = next(
        i
        for i, command in enumerate(normalized)
        if command[:2] == ["launchctl", "bootstrap"] and command[3].endswith("com.wendy.trading-orchestrator.runner.plist")
    )
    runner_waits = [
        i
        for i, command in enumerate(normalized)
        if command[:2] == ["launchctl", "print"] and command[2] == runner_service and runner_bootout_index < i < runner_bootstrap_index
    ]
    dashboard_bootstrap_index = next(
        i
        for i, command in enumerate(commands)
        if command[:2] == ["launchctl", "bootstrap"] and command[3].endswith("com.wendy.trading-orchestrator.dashboard.plist")
    )
    other_bootstrap_indexes = [
        i
        for i, command in enumerate(commands)
        if command[:2] == ["launchctl", "bootstrap"] and not command[3].endswith("com.wendy.trading-orchestrator.dashboard.plist")
    ]

    assert result["status"] == "active"
    assert len(runner_waits) == 3
    assert max(other_bootstrap_indexes) < dashboard_bootstrap_index
    assert any(command[:2] == ["curl", "-fsS"] and "dashboard-v4.html" in command[-1] for command in commands)


def test_schedule_installer_retries_bootstrap_eio_and_records_retry_count(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    _stage_stale_launch_agents(schedule, launch_agents)
    attempts = {}
    bootstrapped = set()

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        if command[:2] == ["launchctl", "bootout"]:
            bootstrapped.discard(command[2])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, "loaded", "") if command[2] in bootstrapped else subprocess.CompletedProcess(command, 113, "", "not found")
        if command[:2] == ["launchctl", "bootstrap"] and command[3].endswith("com.wendy.trading-orchestrator.runner.plist"):
            attempts[command[3]] = attempts.get(command[3], 0) + 1
            if attempts[command[3]] == 1:
                return subprocess.CompletedProcess(command, 5, "", "Bootstrap failed: 5: Input/output error")
        if command[:2] == ["launchctl", "bootstrap"]:
            bootstrapped.add(command[2] + "/" + Path(command[3]).stem)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    result = ScheduleInstaller(
        root,
        launch_agents,
        fake_runner,
        sleep=lambda seconds: None,
        teardown_poll_seconds=0,
        bootstrap_retry_delay_seconds=0,
        dashboard_poll_seconds=0,
        dashboard_timeout_seconds=1,
    ).install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    runner = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.runner")

    assert result["status"] == "active"
    assert runner["bootstrap_attempts"] == 2
    assert runner["bootstrap_retry_count"] == 1


def test_schedule_installer_rolls_back_after_repeated_bootstrap_eio_with_teardown_wait(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    _stage_stale_launch_agents(schedule, launch_agents)
    commands = []
    phase = {"rollback": False}

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 113, "", "not found")
        if command[:2] == ["launchctl", "bootstrap"] and command[3].endswith("com.wendy.trading-orchestrator.runner.plist") and not phase["rollback"]:
            return subprocess.CompletedProcess(command, 5, "", "Bootstrap failed: 5: Input/output error")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    commands.clear()
    installer = ScheduleInstaller(
        root,
        launch_agents,
        fake_runner,
        sleep=lambda seconds: None,
        teardown_poll_seconds=0,
        bootstrap_retry_delay_seconds=0,
        dashboard_poll_seconds=0,
        dashboard_timeout_seconds=1,
    )
    original_rollback_job = installer._rollback_job

    def mark_rollback(job: dict, restart_loaded: bool, pre_rollback_backup_dir: Path) -> dict:
        phase["rollback"] = True
        return original_rollback_job(job, restart_loaded, pre_rollback_backup_dir)

    installer._rollback_job = mark_rollback
    result = installer.install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    runner = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.runner")

    assert result["status"] == "fail"
    assert runner["bootstrap_attempts"] == 3
    assert result["rollback"]["status"] == "rolled_back"
    assert any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert any(command[:2] == ["launchctl", "print"] for command in commands)
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in commands)


def test_schedule_installer_teardown_wait_timeout_fails_without_bootstrap(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    _stage_stale_launch_agents(schedule, launch_agents)
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, "still loaded", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    commands.clear()
    result = ScheduleInstaller(
        root,
        launch_agents,
        fake_runner,
        sleep=lambda seconds: None,
        teardown_poll_seconds=0,
        teardown_timeout_seconds=0,
        dashboard_poll_seconds=0,
        dashboard_timeout_seconds=1,
    ).install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    runner = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.runner")

    assert result["status"] == "fail"
    assert runner["status"] == "fail"
    assert runner["error"] == "launchctl service did not disappear after bootout"
    assert not any(command[:2] == ["launchctl", "bootstrap"] for command in commands)


def test_schedule_rollback_plan_blocks_when_install_receipt_has_no_backups(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    installer = ScheduleInstaller(root, tmp_path / "LaunchAgents", fake_runner)
    installer.install("2026-05-26")
    result = installer.rollback_plan("2026-05-26")

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_backup_records"
    assert result["summary"]["restorable_count"] == 0
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert all(command[:2] == ["launchctl", "print"] for command in commands)
    assert load_json(root / "schedules" / "rollback_plan_current.json")[0]["status"] == "blocked"


def test_schedule_rollback_blocks_apply_without_acknowledgement(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    stale_args = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    payload["ProgramArguments"] = stale_args
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)

    commands = []

    fake_runner = _stateful_launchd_runner([job["label"] for job in schedule["jobs"]], commands)

    installer = ScheduleInstaller(root, launch_agents, fake_runner)
    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    installer.install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    commands.clear()
    result = installer.rollback("2026-05-26")

    assert result["status"] == "blocked"
    assert result["blocker"] == "missing_acknowledgement"
    assert result["acknowledgement_ok"] is False
    assert result["required_acknowledgement"] == SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT
    assert result["plan"]["status"] == "ready"
    assert result["safety"]["writes_launch_agents"] is False
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert not any(command[:2] == ["launchctl", "bootstrap"] for command in commands)
    with stale_plist.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] != stale_args
    assert load_json(root / "schedules" / "rollback_current.json")[0]["status"] == "blocked"


def test_schedule_rollback_restores_backup_with_acknowledgement(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    stale_args = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    payload["ProgramArguments"] = stale_args
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    commands = []

    fake_runner = _stateful_launchd_runner([job["label"] for job in schedule["jobs"]], commands)

    installer = ScheduleInstaller(root, launch_agents, fake_runner)
    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    installer.install("2026-05-26", acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT, package_id=package_id)
    commands.clear()
    result = installer.rollback("2026-05-26", acknowledgement=SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT)
    live_tick = next(job for job in result["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")

    assert result["status"] == "rolled_back"
    assert result["acknowledgement_ok"] is True
    assert result["pre_rollback_backup_count"] == 8
    assert Path(result["pre_rollback_backup_dir"]).exists()
    assert live_tick["status"] == "restored"
    assert Path(live_tick["pre_rollback_backup"]).exists()
    assert any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in commands)
    assert any(command[:2] == ["launchctl", "kickstart"] for command in commands)
    with stale_plist.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == stale_args
    assert load_json(root / "schedules" / "rollback_current.json")[0]["status"] == "rolled_back"


def test_schedule_post_install_verifier_blocks_when_schedule_is_stale(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    installer = ScheduleInstaller(root, launch_agents, fake_runner)
    installer.install("2026-05-26")
    result = SchedulePostInstallVerifier(root, launch_agents, fake_runner).run("2026-05-26")

    checks = {check["name"]: check for check in result["checks"]}
    assert result["status"] == "blocked"
    assert checks["schedule_current_active"]["status"] == "fail"
    assert checks["install_receipt"]["status"] == "fail"
    assert checks["rollback_ready"]["status"] == "fail"
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert all(command[:2] == ["launchctl", "print"] for command in commands)
    assert load_json(root / "schedules" / "post_install_verify_current.json")[0]["status"] == "blocked"


def test_schedule_post_install_verifier_passes_after_successful_install_with_fresh_runner(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    now = "2026-05-26T12:00:00+00:00"
    (root / "runner_status").mkdir(parents=True, exist_ok=True)
    (root / "runner_status" / "current.json").write_text(
        f'{{"state":"ok","updated_at":"{now}","interval_seconds":300}}\n',
        encoding="utf-8",
    )

    runner = _stateful_launchd_runner([job["label"] for job in schedule["jobs"]])
    package_id = _takeover_package_id(root, launch_agents, runner)
    ScheduleInstaller(root, launch_agents, runner).install(
        "2026-05-26",
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )
    result = SchedulePostInstallVerifier(
        root,
        launch_agents,
        runner,
        now=lambda: __import__("datetime").datetime.fromisoformat(now),
    ).run("2026-05-26")

    checks = {check["name"]: check for check in result["checks"]}
    assert result["status"] == "pass"
    assert checks["schedule_current_active"]["status"] == "pass"
    assert checks["install_receipt"]["status"] == "pass"
    assert checks["rollback_ready"]["status"] == "pass"
    assert checks["runner_heartbeat"]["status"] == "pass"
    assert result["rollback_plan"]["restorable_count"] == 8
    assert load_json(root / "schedules" / "post_install_verify_current.json")[0]["status"] == "pass"


def test_schedule_takeover_package_prepares_attended_install_commands_without_writes(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "loaded", "")

    result = ScheduleTakeoverPackage(root, launch_agents, fake_runner).run("2026-05-26")

    assert result["status"] == "ready_for_attended_install"
    assert result["package_id"]
    assert result["valid_for_minutes"] == 15
    assert "--package-id" in result["commands"]["attended_install"]
    assert result["package_id"] in result["commands"]["attended_install"]
    assert result["summary"]["requires_reinstall_count"] == 1
    assert result["summary"]["post_install_verify_status"] == "blocked"
    assert "I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS" in result["commands"]["attended_install"]
    assert "pipelines.schedule_post_install_verify" in result["commands"]["post_install_verify"]
    assert "I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP" in result["commands"]["attended_rollback"]
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert all(command[:2] == ["launchctl", "print"] for command in commands)
    with stale_plist.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    assert load_json(root / "schedules" / "takeover_package_current.json")[0]["status"] == "ready_for_attended_install"


def test_schedule_takeover_package_check_current_reports_usable_package_without_writes(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    for job in schedule["jobs"]:
        source = Path(job["plist"])
        target = launch_agents / source.name
        target.write_bytes(source.read_bytes())
    stale_job = next(job for job in schedule["jobs"] if job["label"] == "com.wendy.trading-orchestrator.dualtrack-live-tick")
    stale_plist = launch_agents / Path(stale_job["plist"]).name
    with stale_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    with stale_plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "loaded", "")

    service = ScheduleTakeoverPackage(root, launch_agents, fake_runner)
    package = service.run("2026-05-26")
    commands.clear()
    result = service.check_current("2026-05-26")

    assert result["status"] == "ready_for_attended_install"
    assert result["usable_for_attended_install"] is True
    assert result["package"]["package_id"] == package["package_id"]
    assert result["package_gate"]["ok"] is True
    assert result["operator_next_action"]["action"] == "authorize_attended_install"
    assert package["package_id"] in result["commands"]["attended_install"]
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False
    assert result["safety"]["runs_launchctl_print"] is False
    assert commands == []
    assert load_json(root / "schedules" / "takeover_package_check_current.json")[0]["status"] == "ready_for_attended_install"


def test_schedule_takeover_package_check_current_blocks_expired_package(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 113, "", "not loaded")

    service = ScheduleTakeoverPackage(root, launch_agents, fake_runner)
    package = service.run("2026-05-26")
    package["expires_at"] = "2026-01-01T00:00:00+00:00"
    write_json(root / "schedules" / "takeover_package_current.json", [package])

    result = service.check_current("2026-05-26")

    assert result["status"] == "blocked"
    assert result["usable_for_attended_install"] is False
    assert result["blocker"] == "expired_package"
    assert result["operator_next_action"]["action"] == "regenerate_takeover_package"
    assert result["commands"]["attended_install"] == ""
    assert result["safety"]["writes_launch_agents"] is False
    assert result["safety"]["runs_launchctl_modification"] is False


def test_schedule_installer_no_restart_does_not_kickstart_jobs(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    _full_schedule_manager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    package_id = _takeover_package_id(root, launch_agents, fake_runner)
    result = ScheduleInstaller(root, launch_agents, fake_runner).install(
        "2026-05-26",
        restart_loaded=False,
        acknowledgement=SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
        package_id=package_id,
    )

    assert result["status"] == "active"
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert not any(command[:2] == ["launchctl", "bootstrap"] for command in commands)
    assert not any(command[:2] == ["launchctl", "kickstart"] for command in commands)
