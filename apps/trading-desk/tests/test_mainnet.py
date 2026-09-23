import json
import logging
import stat
import subprocess
from pathlib import Path

import pytest


KEY = "ab" * 32
SUB = "0x" + "cd" * 20


@pytest.fixture
def mainnet_config(config, tmp_path):
    from dataclasses import replace
    sb = tmp_path / "sb"
    sb.mkdir()
    subprocess.run(["git", "init", "-q", str(sb)], check=True)
    subprocess.run(["git", "-C", str(sb), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    return replace(config, mainnet_config=tmp_path / "cfg/hyperliquid-mainnet.json", mainnet_key=tmp_path / "cfg/hyperliquid-mainnet.pk",
                   standard_broker_mainnet=sb)


def client(cfg, fetch):
    from trading_desk.sources import Sources
    from trading_desk.store import Store
    from tests.test_desk import client_for
    c, _ = client_for(cfg, Sources(cfg, fetch=fetch), Store(cfg.db_path), fetch)
    return c


def test_setup_writes_the_key_0600_in_broker_format_and_never_echoes_it(mainnet_config, fetch, caplog):
    caplog.set_level(logging.DEBUG)
    c = client(mainnet_config, fetch)
    response = c.post("/api/mainnet/setup", json={"key": "0x" + KEY, "subaccount": SUB})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "subaccount": "0xcdcd…cdcd"}
    key_file = mainnet_config.mainnet_key
    assert key_file.read_text() == f"HYPERLIQUID_PK=0x{KEY}\n"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(mainnet_config.mainnet_config.stat().st_mode) == 0o600
    saved = json.loads(mainnet_config.mainnet_config.read_text())
    assert saved["subaccount"] == SUB and saved["approved_by"] == "park" and len(saved["release_sha"]) == 40
    assert KEY not in json.dumps(saved)
    assert KEY not in caplog.text
    assert KEY not in mainnet_config.db_path.read_bytes().decode("latin-1")
    status = c.get("/api/mainnet").json()
    assert KEY not in json.dumps(status) and status["configured"] is True and status["subaccount"] == "0xcdcd…cdcd"


@pytest.mark.parametrize("body", [
    {"key": "12345", "subaccount": SUB},
    {"key": KEY, "subaccount": "0x123"},
    {"key": KEY + "zz", "subaccount": SUB},
])
def test_invalid_setup_is_refused_without_writing_or_echoing(mainnet_config, fetch, body):
    c = client(mainnet_config, fetch)
    response = c.post("/api/mainnet/setup", json=body)
    assert response.status_code == 400
    assert body["key"] not in response.text
    assert not mainnet_config.mainnet_key.exists() and not mainnet_config.mainnet_config.exists()


def test_setup_through_the_phone_tunnel_is_refused_even_when_logged_in(mainnet_config, fetch, tmp_path):
    from dataclasses import replace
    from trading_desk import remote
    secret = tmp_path / "passcode"
    secret.write_text("phone-pass\n")
    secret.chmod(0o600)
    cfg = replace(mainnet_config, remote_passcode=secret)
    c = client(cfg, fetch)
    c.cookies.set(remote.COOKIE, remote._session("phone-pass"))
    tunnel = {"cf-connecting-ip": "1.2.3.4"}
    assert c.get("/api/mainnet", headers=tunnel).status_code == 200
    response = c.post("/api/mainnet/setup", json={"key": KEY, "subaccount": SUB}, headers=tunnel)
    assert response.status_code == 403 and "本机" in response.json()["detail"]
    assert not cfg.mainnet_key.exists()


def test_same_day_unlock_is_refused_and_logged(mainnet_config, fetch, monkeypatch):
    from trading_desk import mainnet
    monkeypatch.setattr(mainnet, "unlock", lambda cfg, now=None: {"ok": False, "reason": "same_day", "unlock_from": "2026-09-17"})
    c = client(mainnet_config, fetch)
    response = c.post("/api/mainnet/unlock", json={})
    assert response.status_code == 409 and "2026-09-17" in response.json()["detail"]


def test_status_without_the_observer_says_so(mainnet_config, fetch, tmp_path):
    from dataclasses import replace
    cfg = replace(mainnet_config, trading_system_checkout=tmp_path / "no-ts")
    import sys
    sys.modules.pop("services.mainnet_observer", None)
    status = client(cfg, fetch).get("/api/mainnet").json()
    assert status["configured"] is False
    assert status["state"] in {"observer_missing", "not_configured"}


@pytest.mark.parametrize("path", ["/api/mainnet/setup", "/api/mainnet/unlock"])
def test_cross_site_requests_are_refused_before_anything_happens(mainnet_config, fetch, monkeypatch, path):
    from trading_desk import mainnet
    monkeypatch.setattr(mainnet, "unlock", lambda cfg, now=None: pytest.fail("unlock must not run"))
    c = client(mainnet_config, fetch)
    body = json.dumps({"key": KEY, "subaccount": SUB})
    plain = c.post(path, content=body, headers={"content-type": "text/plain"})
    assert plain.status_code == 415
    foreign = c.post(path, content=body, headers={"content-type": "application/json", "origin": "https://evil.example"})
    assert foreign.status_code == 403
    assert not mainnet_config.mainnet_key.exists()
