from __future__ import annotations

import json

from trading_desk.advice import Advisor
from trading_desk.app import current_state


class StubWatch:
    def current(self):
        return {"items": [{"title": "美联储利率决议", "at": "2026-09-17T02:00:00+08:00", "why": "市场押注降息"}]}


def advisor_for(store, sources, answer):
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return answer

    return Advisor(store, sources, StubWatch(), complete=model, asset_state=lambda a: current_state(sources, a)), prompts


def test_model_advice_is_stored_as_ai_calls_with_a_plan_and_only_valid_rows(store, sources):
    answer = json.dumps({"assets": [
        {"key": "BTC", "direction": "short", "confidence": 9, "reason": "美债收益率破5%，风险资产承压"},
        {"key": "XAU", "direction": "sideways", "confidence": 3, "reason": "无效方向"},
        {"key": "DOGE", "direction": "long", "confidence": 3, "reason": "不在清单"},
    ]}, ensure_ascii=False)
    advisor, prompts = advisor_for(store, sources, answer)
    result = advisor.generate()
    assert result == {"status": "ok", "saved": [{"asset": "BTC", "direction": "short"}]}
    assert "美联储利率决议" in prompts[0] and '"key": "BTC"' in prompts[0]
    ai = advisor.today("BTC")
    assert ai["author"] == "ai" and ai["confidence"] == 5 and ai["plan"]["direction"] == "short"
    assert store.judgments("BTC") == []  # Park's own list stays his
    assert advisor.generate()["status"] in {"ok", "model_unavailable"}  # only XAU is still missing
    assert len(prompts) == 2 and '"key": "BTC"' not in prompts[1]


def test_no_advice_is_invented_when_the_model_fails(store, sources):
    advisor, _ = advisor_for(store, sources, "sorry")
    assert advisor.generate() == {"status": "model_unavailable"}
    assert store.judgments(author="ai") == []


def test_brief_and_desk_show_ai_advice_and_score_you_and_ai_separately(config, sources, store, fetch):
    from tests.test_desk import client_for
    advisor, _ = advisor_for(store, sources, json.dumps({"assets": [{"key": "BTC", "direction": "long", "confidence": 4, "reason": "ETF 净流入"}]}, ensure_ascii=False))
    advisor.generate()
    old = store.add_judgment(asset="XAU", direction="long", confidence=3, reason="", cited=[], price_at=100.0, plan=None, action="recorded",
                             created_at="2026-09-01T00:00:00+00:00", author="ai")
    store.resolve(old["id"], price_after=101.0, move_pct=1.0, outcome="hit")
    config.morning_archive.mkdir()
    (config.morning_archive / "2026-09-15.html").write_text('<html><body><div class="digest">摘要</div></body></html>')
    client, _ = client_for(config, sources, store, fetch)
    desk = client.get("/api/desk/BTC").json()
    assert desk["ai_today"]["direction"] == "long" and desk["today_judgment"] is None
    review = client.get("/api/review").json()
    assert review["summary"]["resolved"] == 0 and review["ai_summary"] == {"resolved": 1, "hits": 1, "pending": 1, "unverifiable": 0}
    page = client.get("/newsletter/morning").text
    assert "AI 建议" in page and "ETF 净流入" in page and "AI 说中 1/1" in page
    assert client.get("/api/advice").json()["today"]["BTC"]["reason"] == "ETF 净流入"
