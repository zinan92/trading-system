from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from trading_desk import digest
from trading_desk.app import create_app
from trading_desk.watch import Watch, when_label

NOW = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)  # 14:00 Beijing
CALENDAR = [
    {"title": "Federal Funds Rate", "country": "USD", "date": "2026-09-16T14:00:00-04:00", "impact": "High", "forecast": "4.00%", "previous": "3.75%"},
    {"title": "FOMC Statement", "country": "USD", "date": "2026-09-16T14:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "FOMC Press Conference", "country": "USD", "date": "2026-09-16T14:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "CPI y/y", "country": "GBP", "date": "2026-09-16T02:00:00-04:00", "impact": "High", "forecast": "3.1%", "previous": "2.9%"},
    {"title": "Unemployment Claims", "country": "USD", "date": "2026-09-17T08:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Retail Sales", "country": "USD", "date": "2026-09-01T08:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},  # long past
    {"title": "Housing Starts", "country": "USD", "date": "2026-09-16T08:30:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
]
FEED = {"items": [
    {"id": 7, "title": "财联社9月15日电，美债收益率破5%", "source": "cls_telegraph", "collected_at": "2026-09-15T05:09:00Z", "triage": {"bucket": "high_impact"}},
    {"id": 8, "title": "Reddit says BTC", "source": "reddit", "collected_at": "2026-09-15T05:10:00Z", "triage": {"bucket": "high_impact"}},
    {"id": 9, "title": "某公司发布新品", "source": "cls_telegraph", "collected_at": "2026-09-15T05:11:00Z", "triage": {"bucket": "watch"}},
]}


def fetcher(calls):
    def fetch(url):
        calls.append(url)
        if "thisweek" in url:
            return CALENDAR
        if "nextweek" in url:
            raise OSError("404")
        return FEED
    return fetch


def test_candidates_keep_high_impact_upcoming_events_and_flashes(tmp_path):
    watch = Watch(tmp_path, "http://intel", fetch=fetcher([]), complete=lambda p: "", now=lambda: NOW)
    events, breaking = watch.candidates()
    assert [c.title for c in events] == ["英国 CPI y/y", "美国 Federal Funds Rate", "美国 FOMC Statement", "美国 FOMC Press Conference", "美国 Unemployment Claims"]
    assert events[1].at == "2026-09-17T02:00:00+08:00" and events[1].detail == "预期 4.00% · 前值 3.75%"
    assert [c.title for c in breaking] == ["美债收益率破5%"]


def test_model_may_only_choose_among_inputs_and_rules_take_over_when_it_fails(tmp_path):
    events_seen = {}

    def model(prompt):
        events_seen["prompt"] = prompt
        watch_refs = [line for line in prompt.split('"ref": "')[1:]]
        refs = [ref.split('"')[0] for ref in watch_refs]
        return json.dumps({"items": [
            {"ref": "event:made-up", "title": "编造的事件", "markets": ["美股"], "why": "x", "volatility": "高"},
            {"ref": refs[1], "title": "美联储利率决议", "markets": ["美股", "美债", "火星"], "why": "市场押注降息", "volatility": "高"},
            {"ref": refs[-1], "title": "美债收益率破5%", "markets": ["美债"], "why": "利率锚上移", "volatility": "高"},
            {"ref": refs[0], "title": "英国 CPI", "markets": ["欧洲"], "why": "英镑", "volatility": "中"},
        ]}, ensure_ascii=False)

    watch = Watch(tmp_path, "http://intel", fetch=fetcher([]), complete=model, now=lambda: NOW)
    ranked = watch.rank(*watch.candidates())
    assert "整个金融市场" in events_seen["prompt"]
    assert ranked["provider"] == "codex"
    assert [i["title"] for i in ranked["items"]] == ["美联储利率决议", "美债收益率破5%", "英国 CPI"]
    assert ranked["items"][0]["markets"] == ["美股", "美债"] and ranked["items"][0]["at"] == "2026-09-17T02:00:00+08:00"

    rules = Watch(tmp_path / "r", "http://intel", fetch=fetcher([]), complete=lambda p: "not json", now=lambda: NOW)
    fallback = rules.rank(*rules.candidates())
    assert fallback["provider"] == "rules"
    assert [i["title"] for i in fallback["items"]] == ["美联储利率决议", "美国 就业", "英国 CPI 通胀"]  # one FOMC slot, not three


def test_refresh_reranks_on_new_flash_at_most_hourly_and_after_0830(tmp_path):
    clock = [NOW]
    calls = []
    answers = []

    def model(prompt):
        answers.append(prompt)
        return ""

    watch = Watch(tmp_path, "http://intel", fetch=fetcher(calls), complete=model, now=lambda: clock[0])
    first = watch.refresh()
    assert first["provider"] == "rules" and len(first["items"]) == 3 and len(answers) == 1
    clock[0] = NOW + timedelta(minutes=20)
    assert watch.refresh()["generated_at"] == first["generated_at"]  # nothing new
    FEED["items"].append({"id": 10, "title": "霍尔木兹石油流量骤降", "source": "cls_telegraph", "collected_at": "2026-09-15T06:15:00Z", "triage": {"bucket": "high_impact"}})
    try:
        assert watch.refresh()["generated_at"] == first["generated_at"]  # new flash, but ranked < 1 h ago
        clock[0] = NOW + timedelta(minutes=61)
        again = watch.refresh()
        assert again["generated_at"] != first["generated_at"] and len(answers) == 2
    finally:
        FEED["items"].pop()
    clock[0] = datetime(2026, 9, 16, 0, 35, tzinfo=timezone.utc)  # 08:35 Beijing next day
    assert watch.refresh()["generated_at"].startswith("2026-09-16T08:35")
    assert sum("thisweek" in url for url in calls) == 1  # calendar cached for two hours... then refetched once
    assert (tmp_path / "history.jsonl").read_text().count("\n") == 3


def test_when_label_and_brief_and_trade_page_show_the_list(config, sources, store, fetch, tmp_path):
    item = {"kind": "event", "at": "2026-09-17T02:00:00+08:00"}
    assert when_label(item, NOW) == "周四 02:00 · 还有 1 天 12 小时"
    assert when_label({"kind": "breaking", "at": "2026-09-15T13:09:00+08:00"}, NOW) == "突发 · 13:09"
    assert when_label(item, datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)) == "周四 02:00 · 已公布"
    config.watch_folder.mkdir()
    (config.watch_folder / "current.json").write_text(json.dumps({"generated_at": "2026-09-15T14:26:10+08:00", "provider": "codex", "items": [
        {"ref": "event:1", "kind": "event", "title": "美联储利率决议", "at": "2026-09-17T02:00:00+08:00", "markets": ["美股", "美债"], "why": "市场押注降息", "volatility": "高", "detail": "预期 4.00%", "source_title": "美国 Federal Funds Rate"}]}, ensure_ascii=False))
    config.morning_archive.mkdir()
    (config.morning_archive / "2026-09-15.html").write_text('<html><body><nav class="nav">晨报</nav><div class="digest">摘要</div></body></html>')
    from tests.test_desk import client_for
    client, _ = client_for(config, sources, store, fetch)
    assert client.get("/api/watch").json()["items"][0]["title"] == "美联储利率决议"
    page = client.get("/newsletter/morning").text
    assert page.index("本周要看的三件事") < page.index("昨天判断对了吗") < page.index('class="digest"')
    assert "按全市场冲击排序" in page and "<span>美债</span>" in page
    assert "本周三件事" in [c["name"] for c in client.get("/api/system").json()["checks"]]
    assert digest.watch_block({}, NOW).count("还在生成") == 1
