"""只读开放 API (#1344): token-gated, GET-only, allow-listed mirror of the desk's own routes."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_desk import open_api
from trading_desk.app import create_app
from trading_desk.backtest import Comparisons
from trading_desk.config import Config

NOW = __import__("datetime").datetime(2026, 11, 6, tzinfo=__import__("datetime").timezone.utc)


@pytest.fixture
def desk(tmp_path: Path, monkeypatch):
    config = Config(db_path=tmp_path / "desk.db", api_tokens=tmp_path / "api-tokens.json", watch_folder=tmp_path / "watch",
                    remote_passcode=tmp_path / "no-passcode", paper_output=tmp_path / "out")
    reads: list[str] = []

    def fake_http_json(url, body, timeout=10):
        reads.append(url)
        return {"current_strategy": {"active": False, "status": "evidence_blocked", "status_label": "证据阻塞", "specification": {"strategy_type": None}},
                "runtime": {"status": "stopped", "status_label": "已停止", "open_order_count": 0, "unknown_order_count": 0,
                            "last_control_event": {"secret": "never shown"}},
                "execution": {"counts": {"open_position_count": 0, "open_order_count": 0, "unknown_order_count": 0, "completed_round_trip_count": 3},
                              "pnl": {"currency": "USDT", "net_realized": -1.5}, "orders": [{"id": "never shown"}], "account": {"id": "never shown"}},
                "market": {"symbol": "GOLD", "provider": "okx", "trusted": True, "latest_close": 4106.7, "status": "ready"}}

    monkeypatch.setattr("trading_desk.app.http_json", fake_http_json)
    comparisons = Comparisons(config, launch=lambda *a, **k: None, now=lambda: NOW)
    client = TestClient(create_app(config, comparisons=comparisons))
    tokens = open_api.Tokens(config.api_tokens)
    return type("Desk", (), {"client": client, "tokens": tokens, "config": config, "comparisons": comparisons, "reads": reads})


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_token_is_required_even_from_this_mac(desk) -> None:
    assert desk.client.get("/api/v1/assets").status_code == 401
    assert desk.client.get("/api/v1/assets", headers=auth("tsd_wrong")).status_code == 401
    token = desk.tokens.create("notebook")
    assert desk.client.get("/api/v1/assets", headers=auth(token)).status_code == 200
    assert desk.tokens.revoke("notebook") and not desk.tokens.revoke("notebook")
    assert desk.client.get("/api/v1/assets", headers=auth(token)).status_code == 401


def test_the_token_file_is_private_and_holds_no_token(desk) -> None:
    token = desk.tokens.create("agent")
    assert oct(os.stat(desk.config.api_tokens).st_mode & 0o777) == "0o600"
    stored = desk.config.api_tokens.read_text()
    assert token not in stored and json.loads(stored)["agent"]["sha256"]
    assert [row["name"] for row in desk.tokens.names()] == ["agent"]
    with pytest.raises(ValueError):
        desk.tokens.create("agent")
    with pytest.raises(ValueError):
        desk.tokens.create("Bad Name")
    os.chmod(desk.config.api_tokens, 0o644)  # readable by others → no token is trusted
    assert desk.client.get("/api/v1/assets", headers=auth(token)).status_code == 401


def test_only_get_is_accepted_before_anything_else(desk) -> None:
    token = desk.tokens.create("agent")
    for method in ("post", "put", "delete", "patch"):
        response = getattr(desk.client, method)("/api/v1/assets", headers=auth(token))
        assert response.status_code == 405 and response.headers["allow"] == "GET, HEAD"
    assert desk.client.post("/api/v1/backtest/compare", json={}).status_code == 405  # no token needed to be told no


@pytest.mark.parametrize("path", [
    "/api/v1/execute", "/api/v1/plan", "/api/v1/judgments", "/api/v1/grid/stop", "/api/v1/mainnet", "/api/v1/mainnet/setup",
    "/api/v1/advice/refresh", "/api/v1/notes", "/api/v1/review", "/api/v1/accounts", "/api/v1/catalog",
    "/api/v1/trading-system/read-model", "/api/v1/backtest/compare/nope", "/api/v1", "/api/v1/",
])
def test_paths_off_the_list_are_404_even_when_the_desk_has_them(desk, path: str) -> None:
    token = desk.tokens.create("agent")
    assert desk.client.get(path, headers=auth(token)).status_code == 404


def test_an_allowed_path_answers_exactly_what_the_page_gets(desk) -> None:
    token = desk.tokens.create("agent")
    for path in ("/assets", "/watch"):
        mirrored = desk.client.get(f"/api/v1{path}", headers=auth(token))
        assert mirrored.status_code == 200 and mirrored.json() == desk.client.get(f"/api/{path.lstrip('/')}").json()
        assert mirrored.headers["cache-control"] == "no-store"
    key = desk.comparisons.start({"asset": "XAU", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}]})
    assert desk.client.get(f"/api/v1/backtest/compare/{key}", headers=auth(token)).json()["status"] == "running"
    assert desk.client.get("/api/v1/review/runs?days=5", headers=auth(token)).status_code == 422  # the route's own checks still apply


def test_status_is_a_summary_of_paper_and_nothing_more(desk) -> None:
    token = desk.tokens.create("agent")
    body = desk.client.get("/api/v1/status", headers=auth(token)).json()
    assert body["paper_only"] is True and body["strategy"]["active"] is False
    assert body["runtime"] == {"status": "stopped", "status_label": "已停止", "open_orders": 0, "unknown_orders": 0}
    assert body["positions"] == {"open": 0, "completed_round_trips": 3} and body["pnl"]["net_realized"] == -1.5
    assert body["market"]["trusted"] is True and "never shown" not in json.dumps(body)
    assert desk.reads == ["http://127.0.0.1:8765/api/trading-system/read-model"]


def test_status_says_when_the_backend_is_down(desk, monkeypatch) -> None:
    def down(*args, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr("trading_desk.app.http_json", down)
    token = desk.tokens.create("agent")
    response = desk.client.get("/api/v1/status", headers=auth(token))
    assert response.status_code == 503 and "交易后台读不到" in response.json()["detail"]


def test_a_token_passes_the_phone_gate_for_v1_only(desk) -> None:
    token = desk.tokens.create("phone-agent")
    remote = {"cf-connecting-ip": "203.0.113.9"}
    assert desk.client.get("/api/v1/assets", headers={**remote, **auth(token)}).status_code == 200
    assert desk.client.get("/api/v1/assets", headers=remote).status_code == 401  # not the login page
    # The token opens nothing else: other paths still meet the passcode gate.
    assert desk.client.get("/api/assets", headers={**remote, **auth(token)}, follow_redirects=False).status_code in {302, 303, 401, 403}


def test_every_allowed_pattern_only_reaches_get_routes(desk) -> None:
    routes = [(route.path, route.methods) for route in desk.client.app.routes if hasattr(route, "methods")]
    for pattern in open_api.ALLOWED:
        reached = [(path, methods) for path, methods in routes if path.startswith("/api/")
                   and pattern.fullmatch(path[len("/api"):].replace("{key}", "abcdef12" * 3))]
        # Each listed path has a GET route; a POST/PUT/DELETE on the same path (POST /api/assets) is
        # unreachable because the gate answers 405 to any non-GET before routing.
        assert any("GET" in methods for _, methods in reached), (pattern.pattern, reached)
    token = desk.tokens.create("agent")
    assert desk.client.post("/api/v1/assets", headers=auth(token), json={"key": "SOL"}).status_code == 405
