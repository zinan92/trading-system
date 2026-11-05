"""Docker one-command install (#1343): the properties that keep it as safe as a native run."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
IGNORE = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def test_the_base_image_is_pinned_to_an_exact_python() -> None:
    assert re.search(r"^FROM python:3\.13\.\d+-slim-bookworm$", DOCKERFILE, re.MULTILINE)


def test_every_published_port_is_on_the_host_loopback_only() -> None:
    ports = re.findall(r'^\s*-\s*"([^"]+)"', COMPOSE.split("ports:", 1)[1].split("environment:", 1)[0], re.MULTILINE)
    assert ports and all(port.startswith("127.0.0.1:") for port in ports), ports


def test_no_secret_or_env_file_enters_the_image() -> None:
    for pattern in (".env", ".env.*", "configs/live.env", "*.pk", "*.pem", "*.key"):
        assert pattern in IGNORE, pattern
    assert not re.search(r"^COPY\s+.*(\.env|secret|passcode|\.pk)", DOCKERFILE, re.MULTILINE | re.IGNORECASE)
    assert "USER park" in DOCKERFILE  # not root at runtime


def test_dockerignore_leaves_out_no_tracked_file() -> None:
    # A tracked file missing from the image makes its tree dirty and the boot receipt refuse it.
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    plain = [p.rstrip("/") for p in IGNORE if not p.startswith("!") and "*" not in p]
    negated = {p[1:] for p in IGNORE if p.startswith("!")}
    hits = [f for f in tracked if f not in negated and any(f == p or f.startswith(p + "/") for p in plain)]
    assert hits == []


def test_market_data_stays_on_loopback_and_has_no_stand_in() -> None:
    # config_loader refuses a non-loopback datafeed URL; compose must not route around it.
    for text in (COMPOSE, (ROOT / "compose.host-network.yaml").read_text(encoding="utf-8")):
        assert "DATAFEED_URL" not in text and "host.docker.internal" not in text and "fallback" not in text.lower()
    host = (ROOT / "compose.host-network.yaml").read_text(encoding="utf-8")
    assert "network_mode: host" in host and "BIND_HOST: 127.0.0.1" in host


def test_the_desk_binds_loopback_unless_told_otherwise() -> None:
    main = (ROOT / "apps/trading-desk/src/trading_desk/__main__.py").read_text(encoding="utf-8")
    assert 'os.environ.get("TRADING_DESK_HOST", "127.0.0.1")' in main
    entry = (ROOT / "deploy/docker/entrypoint.sh").read_text(encoding="utf-8")
    assert "paper_predeploy_gate" in entry.split("dashboard_server", 1)[0]  # receipt before the dashboard boots


def test_api_tokens_live_in_the_volume_and_exec_finds_the_desk() -> None:
    # #1344: `docker compose exec trading python -m trading_desk token create` runs without the entrypoint.
    assert "TRADING_DESK_API_TOKENS=/data/desk/api-tokens.json" in DOCKERFILE
    assert re.search(r"PYTHONPATH=[^\s]*/app/apps/trading-desk/src", DOCKERFILE)
