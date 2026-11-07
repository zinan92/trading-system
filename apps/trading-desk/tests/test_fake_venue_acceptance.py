"""#1360 acceptance (#1379): a new venue is one module; the desk runs it end to end with no business-code change.

The fake venue lives in tests/fake_venue.py and is registered only in this test's registry. Every desk
route below is the shipped code.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fake_venue import API, FakeVenue
from test_desk import FakeBacktests, FakeExecutor
from trading_desk.app import create_app
from trading_desk.sources import Sources
from trading_desk.venues import DEFAULT, build

REGISTRY = build(*DEFAULT.modules.values(), FakeVenue())
BARS = [{"t": f"2026-11-0{d}T00:00:00+00:00", "o": 49.0 + d, "h": 51.0 + d, "l": 48.0 + d, "c": 50.0 + d} for d in range(1, 8)]


def _desk(config, store, fetch):
    fetch.routes[f"{API}/quote/FAKE-PERP"] = {"price": 50.0}
    fetch.routes[f"{API}/bars/FAKE-PERP"] = BARS
    fetch.routes[f"{API}/preview"] = {"ready": True, "digest": "sha256:fake", "max_loss": 2.5, "blockers": []}
    fetch.routes[f"{API}/confirm"] = {"status": "rejected"}
    store.add_asset({"key": "FAKE", "label": "假币", "kind": "fake_venue", "instrument_id": "FAKE-PERP", "news_queries": ["假币"]}, kinds=REGISTRY.kinds())
    executor = FakeExecutor(config, fetch, venues=REGISTRY)
    return TestClient(create_app(config, Sources(config, fetch=fetch, venues=REGISTRY), store, executor, backtests=FakeBacktests())), executor


def test_a_fake_venue_runs_the_desk_end_to_end(config, store, fetch) -> None:
    client, executor = _desk(config, store, fetch)

    listed = {row["key"]: row for row in client.get("/api/assets").json()["assets"]}
    assert listed["FAKE"]["kind"] == "fake_venue" and listed["FAKE"]["instrument_id"] == "FAKE-PERP"

    desk = client.get("/api/desk/FAKE").json()
    assert desk["meta"]["venue"] == "Fake Exchange" and desk["price"] == 50.0
    assert desk["control"]["can_stop"] is False and desk["control"]["rearm_paused"] is None  # it did not declare stop / rearm_hold

    bars = client.get("/api/bars/FAKE?tf=1h").json()
    assert bars["ok"] is True and bars["bars"][-1][4] == 57.0

    plan = client.post("/api/plan", json={"asset": "FAKE", "direction": "long"}).json()
    assert plan["kind"] == "new" and len(plan["rungs"]) == 4 and "FUSD" in plan["lines"][-1][1]

    saved = client.post("/api/judgments", json={"asset": "FAKE", "direction": "long", "confidence": 3, "reason": "acceptance", "action": "approved"}).json()
    assert saved["preview"]["execution_ready"] is True and saved["preview"]["preview_digest"] == "sha256:fake"

    refused = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 2.5, "confirm_text": "执行"})
    assert refused.status_code == 409 and "确认没有通过" in refused.json()["detail"]
    assert client.get(f"/api/why?judgment={saved['id']}").json()["first_problem"]["key"] == "result"

    assert executor.driver_calls == []
    assert not [url for url, _ in fetch.calls if any(path in url for path in ("dashboard-control/preview", "dashboard-control/confirm", "dashboard-control/control", "park-paper/ai-chat"))]
    assert client.post("/api/grid/stop", json={"asset": "FAKE", "confirm_text": "停止"}).status_code == 409
    assert client.post("/api/grid/rearm-hold", json={"asset": "FAKE", "paused": True}).status_code == 409


def test_a_fake_venue_without_its_feed_refuses_to_plan(config, store, fetch) -> None:
    client, _ = _desk(config, store, fetch)
    fetch.routes[f"{API}/quote/FAKE-PERP"] = ConnectionError("down")
    fetch.routes[f"{API}/bars/FAKE-PERP"] = ConnectionError("down")
    approved = client.post("/api/judgments", json={"asset": "FAKE", "direction": "long", "confidence": 3, "reason": "", "action": "approved"})
    assert approved.status_code == 409 and "现价读不到" in approved.json()["detail"]


def test_the_confirmed_path_hands_the_plan_to_the_fake_venue(config, store, fetch) -> None:
    client, _ = _desk(config, store, fetch)
    fetch.routes[f"{API}/confirm"] = {"status": "confirmed"}
    saved = client.post("/api/judgments", json={"asset": "FAKE", "direction": "short", "confidence": 3, "reason": "", "action": "approved"}).json()
    done = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 2.5, "confirm_text": "执行"}).json()
    assert done["started"] is True and done["message"] == "假交易所已接手。"
