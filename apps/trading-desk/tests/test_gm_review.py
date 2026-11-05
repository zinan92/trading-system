"""The 交易回看 page logic (gm-review.js) says only what the timeline backs (#1306)."""
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
    script = (
        f"const R = require({json.dumps(str(STATIC / 'gm-review.js'))});"
        "const args = JSON.parse(process.argv[1]);"
        f"process.stdout.write(JSON.stringify(R.{call}(...args)));"
    )
    out = subprocess.run([NODE, "-e", script, json.dumps(list(args))], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)


def data(**result) -> dict:
    base = {"fill_count": 56, "entries": 28, "exits": 28, "flat": True, "gross": -117.63, "fees": 4.51, "net": -122.14, "ended_flat": False}
    return {"run": {"environment": "paper", "reason": "价格跌破区间下沿"}, "result": {**base, **result}}


def test_net_result_says_what_it_excludes_and_an_open_end_is_flagged() -> None:
    cells = run("summary", data())
    assert cells[0] == {"label": "这一轮结果（费后，未含资金费）", "value": "−122.14 USDT", "big": True, "bad": True}
    assert {"label": "结束时", "value": "未空仓", "bad": True} in cells


def test_a_book_that_does_not_net_to_flat_shows_no_result() -> None:
    cells = run("summary", data(flat=False, net=None, gross=None))
    assert cells[0]["value"] == "成交记录没有轧平，算不出结果"


def test_missing_end_record_is_not_called_flat() -> None:
    assert {"label": "结束时", "value": "记录缺失", "bad": True} in run("summary", data(ended_flat=None))


def test_times_are_beijing_and_a_bad_time_is_named() -> None:
    assert run("when", "2026-09-14T16:21:19+00:00") == "9/15 00:21"
    assert run("when", "nope") == "时间缺失"


def test_a_zero_author_shows_zero() -> None:
    board = {"ai": {"total": 3, "hit": 1, "miss": 1, "even": 0, "unverifiable": 0, "pending": 1}, "park": {"total": 0}}
    assert run("score", board) == ["AI：命中 1 · 未中 1 · 持平 0 · 无法核对 0 · 待定 1", "Park：0 条"]


def test_page_calls_get_endpoints_only() -> None:
    source = (STATIC / "gm-review.js").read_text(encoding="utf-8")
    assert "method:" not in source and '"POST"' not in source
    assert "/api/execute" not in source
