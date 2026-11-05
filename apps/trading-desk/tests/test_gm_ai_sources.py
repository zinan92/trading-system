"""The 依据 line under an AI call (gm-ai-sources.js) says only what the stored evidence backs (#1318)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "trading_desk" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(call: str, *args):
    script = (f"const S = require({json.dumps(str(STATIC / 'gm-ai-sources.js'))});"
              "const args = JSON.parse(process.argv[1]);"
              f"process.stdout.write(JSON.stringify(S.{call}(...args)));")
    out = subprocess.run([NODE, "-e", script, json.dumps(list(args))], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)


def call(**evidence):
    base = {"price": {"id": "price", "value": 4132.6, "source": "venue", "at": "2026-11-05T00:40:00+00:00"},
            "kline": {"id": "kline", "date": "2026-11-05", "cutoff_at": "2026-11-04T23:00:00+00:00", "synthesis": "日线弱势"},
            "news": [{"id": "n1", "title": "金价跳水", "source": "cls", "url": "https://x/1", "collected_at": "2026-11-05T00:10:00+00:00"},
                     {"id": "n2", "title": "美元走强", "source": "cls", "url": "", "collected_at": "2026-11-05T00:20:00+00:00"}],
            "news_stale": False, "three": [{"id": "w1", "title": "非农"}], "unverified": [],
            "model": {"channel": "codex", "asked_at": "2026-11-05T00:40:00+00:00"}}
    return {"cited": ["kline", "n2"], "evidence": {**base, **evidence}}


def test_summary_names_each_input_and_how_many_news_it_used() -> None:
    view = run("advice", call())
    assert view["summary"] == "现价 4,132.6（交易场所现价） · K 线日报 11/05 · 新闻 2 条，引用 1 条 · 三件事 1 件"
    assert view["flags"] == [] and view["unverified"] == []
    assert [(i["label"], i["cited"]) for i in view["items"]] == [("现价", False), ("K 线日报", True), ("n1", False), ("n2", True), ("w1", False)]
    assert view["items"][2]["url"] == "https://x/1" and "11/5 08:10" in view["items"][2]["text"]


def test_missing_price_old_news_no_cited_and_unsourced_numbers_are_said() -> None:
    data = call(price={"id": "price", "value": None, "source": "none", "at": "2026-11-05T00:40:00+00:00"}, news_stale=True, unverified=["4.2%"])
    data["cited"] = []
    view = run("advice", data)
    assert view["summary"].startswith("现价读不到")
    assert view["flags"] == ["模型拿到的现价是空的", "新闻是旧列表（Intel 暂时读不到）", "AI 没说依据哪几条"]
    assert view["unverified"] == ["4.2%"]


def test_an_old_call_without_evidence_says_so() -> None:
    assert run("advice", {"cited": [], "evidence": {}}) == {"empty": "这条建议生成时还没有记录依据"}


def test_view_calls_nothing() -> None:
    source = (STATIC / "gm-ai-sources.js").read_text(encoding="utf-8")
    assert "fetch(" not in source and "XMLHttpRequest" not in source


def test_a_watch_line_names_its_source_and_unbacked_numbers() -> None:
    item = {"why": "市场押注新增就业6.5万、失业率4.2%", "source_title": "美国 Non-Farm Employment Change", "detail": "预期 65K · 前值 22K", "unverified": ["4.2%"]}
    assert run("watch", item) == {"source": "美国 Non-Farm Employment Change · 预期 65K · 前值 22K", "unverified": ["4.2%"]}
    assert run("watch", {"why": "", "source_title": "x"}) == {"source": "", "unverified": []}  # rules fallback already shows the source
