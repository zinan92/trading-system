"""#1407 (ADR 0009): silver and crude run as Paper grids, one instrument at a time.

Every Paper call here goes to a fake: no grid is started anywhere. The Testnet driver is never called.
"""
from __future__ import annotations

import copy

from conftest import okx_candles
from test_desk import client_for

SILVER = "/api/candles/commodity/XAG-USDT-SWAP"
CRUDE = "/api/candles/commodity/CL-USDT-SWAP"
IDLE = {"strategy": {"active": False}, "market": {"price": 4304.0}, "terminal": {}}
SILVER_DRAFT = {"status": "draft", "confirmable": True, "message": "ok",
                "draft": {"draft_id": "draft-s", "plan_digest": "sha256:silver",
                          "plan": {"paper_instrument": "silver",
                                   "risk": {"theoretical_max_loss": 120.0, "grid_rung_prices": [51.0, 52.0],
                                            "maximum_notional": 1500.0, "grid_entry_range": {"lower": 51.0, "upper": 53.0}}}}}


def _chat(fetch, draft):
    """Answers /api/park-paper/ai-chat with ``draft``; records every body sent to it."""
    original = fetch.__call__
    bodies = []

    def call(url, body):
        if url.endswith("/api/park-paper/ai-chat"):
            fetch.calls.append((url, body))
            bodies.append(body)
            if body is None:
                return {"pending_draft": None}
            return {"message": copy.deepcopy(draft), "reject": {"status": "rejected"},
                    "confirm": {"status": "confirmed", "snapshot": {"snapshot_id": "s1"}}}[body["action"]]
        return original(url, body)

    fetch.__class__ = type("ChatFetch", (fetch.__class__,), {"__call__": lambda self, url, body: call(url, body)})
    return bodies


def _approve(client, asset, direction="long"):
    return client.post("/api/judgments", json={"asset": asset, "direction": direction, "confidence": 3, "action": "approved"}).json()


def test_an_idle_paper_drafts_silver_and_execute_confirms_once(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = IDLE
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    client, ex = client_for(config, sources, store, fetch)
    chat = _chat(fetch, SILVER_DRAFT)
    desk = client.get("/api/desk/SILVER").json()
    assert desk["grid"]["reason"] == "现在没有运行中的白银网格。"  # never gold's mark or gold's ended grid
    saved = _approve(client, "SILVER")
    assert saved["plan"]["range"][0] < 52.14 < saved["plan"]["range"][1]  # priced from OKX's own swap candles
    assert saved["preview"]["execution_ready"] is True and saved["preview"]["max_loss"] == 120.0
    message = chat[-1]
    assert message["paper_instrument"] == "silver" and "纸面白银 Paper" in message["message"]
    assert not any(b and b.get("action") == "confirm" for b in chat)  # approving drafts only
    ok = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 120.0, "confirm_text": "执行"}).json()
    assert ok["started"] is True
    assert [b for b in chat if b and b.get("action") == "confirm"] == [{"action": "confirm", "draft_id": "draft-s", "plan_digest": "sha256:silver"}]
    assert ex.driver_calls == []


def test_a_draft_for_another_instrument_is_never_silvers_preview(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = IDLE
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    client, _ex = client_for(config, sources, store, fetch)
    wrong = copy.deepcopy(SILVER_DRAFT)
    wrong["draft"]["plan"].pop("paper_instrument")  # a gold draft carries no instrument
    chat = _chat(fetch, wrong)
    saved = _approve(client, "SILVER")
    assert saved["preview"]["execution_ready"] is False and saved["preview"]["preview_digest"] is None
    refused = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 120.0, "confirm_text": "执行"})
    assert refused.status_code == 409
    assert not any(b and b.get("action") == "confirm" for b in chat)


def test_while_gold_runs_silver_and_crude_are_refused_before_any_draft(config, sources, store, fetch):
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    fetch.routes[CRUDE] = okx_candles("CL-USDT-SWAP", [61.0, 61.2])
    client, _ex = client_for(config, sources, store, fetch)  # the default read model runs gold
    chat = _chat(fetch, SILVER_DRAFT)
    for asset in ("SILVER", "WTI"):
        desk = client.get(f"/api/desk/{asset}").json()
        assert desk["grid"]["reason"].startswith("纸面盘正在跑黄金网格，一次只跑一个")
        saved = _approve(client, asset)
        assert saved["preview"]["execution_ready"] is False
        assert saved["preview"]["blockers"] == ["纸面盘已经有黄金网格在跑，一次只跑一个。"]
    assert chat == []


def test_while_silver_runs_gold_and_crude_see_no_grid_and_cannot_start(config, sources, store, fetch):
    model = copy.deepcopy(fetch.routes["/api/park-paper/read-model"])
    model["strategy"].update({"paper_instrument": "silver", "lower_price_boundary": 50.0, "upper_price_boundary": 54.0,
                              "hard_stop": 49.0, "grid_rung_prices": [51.0, 52.0], "theoretical_max_loss": 90.0})
    model["market"] = {"price": 52.14}
    model["execution"]["positions"] = [{"status": "open", "side": "long", "remaining_units": 15, "entry_price": 51.0, "tp": 52.0, "sl": 49.0}]
    fetch.routes["/api/park-paper/read-model"] = model
    fetch.routes[CRUDE] = okx_candles("CL-USDT-SWAP", [61.0, 61.2])
    client, _ex = client_for(config, sources, store, fetch)
    chat = _chat(fetch, SILVER_DRAFT)
    silver = client.get("/api/desk/SILVER").json()
    assert silver["grid"]["ok"] is True and silver["grid"]["lower"] == 50.0
    gold = client.get("/api/desk/XAU").json()
    assert gold["grid"]["reason"] == "纸面盘正在跑白银网格，一次只跑一个；黄金现在没有网格。"
    for asset in ("XAU", "WTI"):
        assert _approve(client, asset)["preview"]["blockers"] == ["纸面盘已经有白银网格在跑，一次只跑一个。"]
    assert chat == []


def test_a_stale_okx_feed_gives_no_plan_no_ready_preview_and_no_confirm(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = IDLE
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14], hours_old=3)
    client, _ex = client_for(config, sources, store, fetch)
    chat = _chat(fetch, SILVER_DRAFT)
    plan = client.post("/api/plan", json={"asset": "SILVER", "direction": "long"}).json()
    assert plan["kind"] == "unavailable"  # never gold's mark, never the last trusted close
    saved = client.post("/api/judgments", json={"asset": "SILVER", "direction": "long", "confidence": 3, "action": "approved"})
    assert saved.status_code == 409 and "现价读不到" in saved.json()["detail"]  # approving is refused; nothing to execute
    assert not [b for b in chat if b]  # no draft message, no confirm


def test_the_gold_ai_chat_body_is_unchanged(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = IDLE
    client, _ex = client_for(config, sources, store, fetch)
    gold = copy.deepcopy(SILVER_DRAFT)
    gold["draft"]["plan"].pop("paper_instrument")
    chat = _chat(fetch, gold)
    saved = _approve(client, "XAU")
    assert saved["preview"]["execution_ready"] is True
    assert set(chat[-1]) == {"action", "message"} and "纸面黄金 Paper" in chat[-1]["message"]  # no instrument key for gold
