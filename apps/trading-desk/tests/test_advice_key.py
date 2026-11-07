"""#1370: a judgment drafted from AI advice records which advice; 这一笔为什么 joins on that key.

The key is recorded, never checked as a gate: a bad key is dropped (and logged), the approval goes on,
and the preview sent to the Dashboard is the same with or without it.
"""
from __future__ import annotations

import json
from pathlib import Path

from test_desk import PREVIEW_OK, client_for
from test_why import DIGEST, PLAN_ID, SESSION, _step, ai_call, execution, judgment, paper, testnet  # noqa: F401 - fixtures
from trading_desk import why


def _advice(store, asset="BTC", direction="short", **extra):
    return store.add_judgment(asset=asset, direction=direction, confidence=3, reason="AI 看空", cited=[], price_at=100.0,
                              plan=None, action="recorded", author="ai", **extra)


def _post(client, advice_id=None, direction="short", action="approved", asset="BTC"):
    body = {"asset": asset, "direction": direction, "confidence": 3, "reason": "t", "action": action}
    if advice_id is not None:
        body["advice_id"] = advice_id
    return client.post("/api/judgments", json=body)


# ---- recording -------------------------------------------------------------------------------

def test_an_adopted_advice_is_recorded_on_the_judgment(config, sources, store, fetch) -> None:
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    client, _ = client_for(config, sources, store, fetch)
    ai = _advice(store)
    saved = _post(client, ai["id"]).json()
    assert saved["advice"] == {"id": ai["id"], "direction": "short", "created_at": ai["created_at"]}
    assert store.judgment(saved["id"])["advice"] == saved["advice"]


def test_a_judgment_without_advice_records_none(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    assert _post(client, action="recorded").json()["advice"] is None


def test_a_bad_key_is_dropped_and_the_judgment_still_saves(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    park = store.add_judgment(asset="BTC", direction="short", confidence=3, reason="p", cited=[], price_at=1.0, plan=None, action="recorded")
    other_asset = _advice(store, asset="XAU")
    other_side = _advice(store, direction="long")
    for bad in (99999, park["id"], other_asset["id"], other_side["id"]):
        response = _post(client, bad, action="recorded")
        assert response.status_code == 200 and response.json()["advice"] is None


def test_a_same_day_revision_sets_and_clears_the_key(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    ai = _advice(store)
    first = _post(client, ai["id"], action="recorded").json()
    assert first["advice"]["id"] == ai["id"]
    second = _post(client, None, action="recorded").json()
    assert second["id"] == first["id"] and second["advice"] is None


def test_the_preview_sent_is_the_same_with_or_without_the_key(config, sources, store, fetch) -> None:
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    sent = []
    for advice in (False, True):
        client, _ = client_for(config, sources, store, fetch)
        ai = _advice(store)
        fetch.calls.clear()
        _post(client, ai["id"] if advice else None)
        sent.append([(url, body) for url, body in fetch.calls if "dashboard-control" in url])
        with store._conn() as conn:  # one Park call per asset per day: drop it so the next post previews afresh
            conn.execute("delete from judgments where author='park'")
    assert sent[0] and sent[0] == sent[1]


# ---- reading: 这一笔为什么 ------------------------------------------------------------------------

def test_a_judgment_with_a_key_names_its_advice(tmp_path: Path) -> None:
    ai = ai_call(id=7, asset="BTC", direction="long")
    later = ai_call(id=8, asset="BTC", direction="short", created_at="2026-09-14T00:50:00+00:00")  # nearer in time, not the one adopted
    row = judgment(advice={"id": 7, "direction": "long", "created_at": ai["created_at"]})
    step = _step(why.explain_judgment(tmp_path, row, [], [ai, later]), "ai")
    assert step["status"] == "ok" and step["at"] == ai["created_at"] and "#7" in step["summary"]
    assert {"label": "对上方式", "value": "编号"} in step["facts"]


def test_a_judgment_without_a_key_keeps_the_time_match(tmp_path: Path) -> None:
    ai = ai_call(id=7, asset="BTC", created_at="2026-09-13T12:00:00+00:00")
    step = _step(why.explain_judgment(tmp_path, judgment(), [], [ai]), "ai")
    assert "按时间" in step["summary"] and {"label": "对上方式", "value": "时间"} in step["facts"]


def test_advice_refreshed_after_it_was_adopted_needs_attention(tmp_path: Path) -> None:
    refreshed = ai_call(id=7, asset="BTC", direction="short", created_at="2026-09-14T03:00:00+00:00")
    row = judgment(advice={"id": 7, "direction": "long", "created_at": "2026-09-14T00:40:00+00:00"})
    step = _step(why.explain_judgment(tmp_path, row, [], [refreshed]), "ai")
    assert step["status"] == "attention" and "刷新" in step["summary"] and "看多" in step["summary"]


def test_a_key_whose_advice_is_gone_is_a_gap(tmp_path: Path) -> None:
    row = judgment(advice={"id": 7, "direction": "long", "created_at": "2026-09-14T00:40:00+00:00"})
    result = why.explain_judgment(tmp_path, row, [], [])
    assert result["first_problem"]["key"] == "ai" and _step(result, "ai")["status"] == "unknown"


def test_a_run_started_from_the_desk_follows_the_key(testnet: Path) -> None:  # noqa: F811
    ai = ai_call(id=7, asset="BTC", direction="long", created_at="2026-09-13T00:40:00+00:00")
    nearer = ai_call(id=8, asset="BTC", direction="short", created_at="2026-09-14T00:40:00+00:00")
    origin = judgment(advice={"id": 7, "direction": "long", "created_at": ai["created_at"]})
    step = _step(why.explain_run(testnet, f"testnet:{PLAN_ID}", [ai, nearer], origins={DIGEST: origin}), "ai")
    assert step["at"] == ai["created_at"] and "#7" in step["summary"]


def test_a_run_with_no_desk_origin_keeps_the_time_match(paper: Path) -> None:  # noqa: F811
    step = _step(why.explain_run(paper, f"paper:{SESSION}", [ai_call()], origins={}), "ai")
    assert "按时间" in step["summary"]


def test_the_route_joins_a_run_to_its_judgment_through_the_started_digest(config, sources, store, fetch, paper_output) -> None:
    client, _ = client_for(config, sources, store, fetch)
    ai = _advice(store, direction="long")
    park = store.add_judgment(asset="BTC", direction="long", confidence=3, reason="p", cited=[], price_at=1.0, plan=None, action="approved",
                              advice={"id": ai["id"], "direction": "long", "created_at": ai["created_at"]})
    store.log_execution(asset="BTC", stage="executed", judgment_id=park["id"], preview_digest="sha256:run", detail={})
    lifecycle = paper_output / "dualtrack" / "grid_testnet_lifecycle" / "dashboard-plan:run.json"
    lifecycle.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.write_text(json.dumps([{"strategy_plan_id": "dashboard-plan:run", "plan_digest": "sha256:run", "instrument_id": "BTC-USD-PERP",
                                      "status": "terminal", "terminal_reason": "hard_stop", "created_at": ai["created_at"], "orders": [], "fills": [], "events": []}]))
    run = client.get("/api/why?run=testnet:dashboard-plan:run").json()
    assert f"#{ai['id']}" in _step(run, "ai")["summary"]
