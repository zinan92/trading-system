"""ops/deploy-paper.sh (#1341): every refusal leaves production untouched; a clean run restarts the desk only.

Runs the real script against a throwaway origin + production clone, a fake read model / desk on a
local port, a fake gate receipt and a `launchctl` stub that only records its arguments.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "deploy-paper.sh"
IDLE = {"current_strategy": {"active": False}, "runtime": {"open_order_count": 0, "unknown_order_count": 0},
        "execution": {"counts": {"open_position_count": 0}}}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path):
    origin, work, prod = tmp_path / "origin.git", tmp_path / "work", tmp_path / "prod"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for name, value in (("user.email", "t@example.com"), ("user.name", "t")):
        git(work, "config", name, value)
    (work / "a.txt").write_text("1")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "one")
    git(work, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(prod)], check=True)
    (work / "a.txt").write_text("2")
    git(work, "commit", "-q", "-am", "two")
    git(work, "push", "-q", "origin", "main")
    head = git(work, "rev-parse", "HEAD")

    state = {"model": dict(IDLE), "codes": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/trading-system/read-model":
                body, code = json.dumps(state["model"]).encode(), 200
            else:
                body, code = b"ok", state["codes"].get(self.path, 200)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "launchctl.calls"
    (bin_dir / "launchctl").write_text(f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\n')
    (bin_dir / "launchctl").chmod(0o755)
    gate = tmp_path / "gate.json"

    def run(*args: str, gate_status: str = "pass", gate_sha: str | None = None) -> subprocess.CompletedProcess:
        gate.write_text(json.dumps([{"status": gate_status, "source_sha": gate_sha or head}]))
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TRADING_PLATFORM_CHECKOUT": str(prod),
               "PAPER_PREDEPLOY_RECEIPT": str(gate), "PAPER_DASHBOARD_URL": url, "PAPER_DESK_URL": url,
               "GATE_WAIT_SECONDS": "0", "GATE_POLL_SECONDS": "0", "DESK_WAIT_TRIES": "1"}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def restarts() -> list[str]:
        return calls.read_text().splitlines() if calls.exists() else []

    yield type("World", (), {"run": staticmethod(run), "head": head, "prod": prod, "state": state, "restarts": staticmethod(restarts),
                             "first": git(prod, "rev-parse", "HEAD")})
    server.shutdown()


def test_a_clean_run_fast_forwards_waits_for_the_gate_and_restarts_only_the_desk(world) -> None:
    result = world.run(world.head)
    assert result.returncode == 0, result.stderr
    assert git(world.prod, "rev-parse", "HEAD") == world.head
    assert [line.split()[-1].rsplit("/", 1)[-1] for line in world.restarts()] == ["com.wendy.trading-desk"]
    assert "/static/web/web.js 200" in result.stdout and "deployed" in result.stdout


def test_dashboard_restart_is_opt_in(world) -> None:
    assert world.run(world.head, "--dashboard").returncode == 0
    assert [line.split()[-1].rsplit("/", 1)[-1] for line in world.restarts()] == ["com.wendy.trading-orchestrator.dashboard", "com.wendy.trading-desk"]


def test_dry_run_changes_nothing(world) -> None:
    result = world.run(world.head, "--dry-run")
    assert result.returncode == 0 and "dry run" in result.stdout
    assert git(world.prod, "rev-parse", "HEAD") == world.first and world.restarts() == []


@pytest.mark.parametrize(("busy", "word"), [
    ({"current_strategy": {"active": True}}, "active_strategy"),
    ({"runtime": {"open_order_count": 2, "unknown_order_count": 0}}, "open_orders"),
    ({"runtime": {"open_order_count": 0, "unknown_order_count": 1}}, "unknown_orders"),
    ({"execution": {"counts": {"open_position_count": 1}}}, "open_positions"),
])
def test_a_busy_paper_is_refused_before_anything_changes(world, busy: dict, word: str) -> None:
    world.state["model"] = {**IDLE, **busy}
    result = world.run(world.head)
    assert result.returncode == 1 and "not idle" in result.stderr and word in result.stderr
    assert git(world.prod, "rev-parse", "HEAD") == world.first and world.restarts() == []


def test_only_origin_main_s_head_is_deployed(world) -> None:
    result = world.run(world.first)
    assert result.returncode == 1 and "is not origin/main" in result.stderr
    assert world.run("not-a-commit").returncode == 1
    assert world.restarts() == []


def test_a_gate_that_is_not_pass_at_the_sha_restarts_nothing(world) -> None:
    for kwargs in ({"gate_status": "blocked"}, {"gate_sha": world.first}):
        result = world.run(world.head, **kwargs)
        assert result.returncode == 1 and "gate is" in result.stderr and "nothing was restarted" in result.stderr
    assert world.restarts() == []


def test_a_desk_that_does_not_come_back_fails_loudly(world) -> None:
    world.state["codes"]["/static/web/web.js"] = 404
    result = world.run(world.head)
    assert result.returncode == 1 and "/static/web/web.js returned 404" in result.stderr


def test_a_dirty_production_checkout_is_refused(world) -> None:
    (world.prod / "a.txt").write_text("edited")
    result = world.run(world.head)
    assert result.returncode == 1 and "tracked changes" in result.stderr and world.restarts() == []
