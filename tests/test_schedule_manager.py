from pathlib import Path
import plistlib
import subprocess

from services.journal_store import load_json
from services.schedule_installer import ScheduleInstaller
from services.schedule_manager import ScheduleManager
from services.schedule_status import ScheduleStatus


def test_schedule_manager_generates_launch_agent_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = ScheduleManager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)

    labels = {item["label"] for item in result["jobs"]}
    assert labels == {
        "com.wendy.trading-orchestrator.runner",
        "com.wendy.trading-orchestrator.trading-plan",
        "com.wendy.trading-orchestrator.evening-review",
        "com.wendy.trading-orchestrator.daily-review",
        "com.wendy.trading-orchestrator.dashboard",
        "com.wendy.trading-orchestrator.strategies",
        "com.wendy.trading-orchestrator.dualtrack-cycle",
        "com.wendy.trading-orchestrator.deadman-ping",
    }
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

    dualtrack_plist = Path(result["launch_agents_dir"]) / "com.wendy.trading-orchestrator.dualtrack-cycle.plist"
    with dualtrack_plist.open("rb") as handle:
        dualtrack = plistlib.load(handle)
    assert dualtrack["StartInterval"] == 60
    assert dualtrack["RunAtLoad"] is True
    assert dualtrack["ProgramArguments"] == ["python3", "-m", "pipelines.dualtrack_cycle_runner", "--event", "auto"]
    assert dualtrack["EnvironmentVariables"]["TRADING_ORCHESTRATOR_MARKET_DB"].endswith("data/market_data.db")


def test_strategies_job_uses_dedicated_python_others_unchanged(tmp_path: Path, monkeypatch):
    """The chan strategy needs Python >= 3.11 + pandas, so the strategies job
    runs on a dedicated interpreter while the runner/daily-review/dashboard jobs
    keep the base (3.9) python untouched."""
    monkeypatch.setenv("TRADING_ORCHESTRATOR_STRATEGIES_PYTHON", "/usr/local/bin/python3")
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()

    result = ScheduleManager(root, repo).build()
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
        "com.wendy.trading-orchestrator.dualtrack-cycle",
        "com.wendy.trading-orchestrator.deadman-ping",
    ):
        assert plists[label]["ProgramArguments"][0] == "python3"


def test_schedule_status_distinguishes_generated_from_installed(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    ScheduleManager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)

    result = ScheduleStatus(
        root,
        launch_agents_dir=tmp_path / "LaunchAgents",
        command_runner=lambda command: subprocess.CompletedProcess(command, 113, "", "not found"),
    ).run("2026-05-26")

    assert result["status"] == "generated_only"
    assert result["installed_count"] == 0
    assert result["loaded_count"] == 0
    assert load_json(root / "schedules" / "status_current.json")[0]["status"] == "generated_only"


def test_schedule_status_detects_installed_and_loaded_jobs(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule = ScheduleManager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
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
    assert all(job["matches_generated"] for job in result["jobs"])


def test_schedule_installer_copies_plists_and_records_receipt(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    ScheduleManager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        if command[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(command, 113, "", "not loaded")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    result = ScheduleInstaller(root, launch_agents, fake_runner).install("2026-05-26")

    assert result["status"] == "active"
    assert result["schedule_status"]["loaded_count"] == 8
    assert len(list(launch_agents.glob("com.wendy.trading-orchestrator.*.plist"))) == 8
    assert load_json(root / "schedules" / "install_current.json")[0]["status"] == "active"
    assert all(job["status"] == "installed" for job in result["jobs"])


def test_schedule_installer_no_restart_does_not_kickstart_jobs(tmp_path: Path):
    root = tmp_path / "outputs"
    repo = tmp_path / "repo"
    repo.mkdir()
    ScheduleManager(root, repo).build(review_hour=22, review_minute=30, dashboard_port=9876)
    launch_agents = tmp_path / "LaunchAgents"
    commands = []

    def fake_runner(command: list[str]) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    result = ScheduleInstaller(root, launch_agents, fake_runner).install("2026-05-26", restart_loaded=False)

    assert result["status"] == "active"
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert not any(command[:2] == ["launchctl", "bootstrap"] for command in commands)
    assert not any(command[:2] == ["launchctl", "kickstart"] for command in commands)
