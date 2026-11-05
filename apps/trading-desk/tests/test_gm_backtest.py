"""The 开仓前回测 box (gm-backtest.js) shows numbers only, and says what they leave out (#1309)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "trading_desk" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def view(result):
    script = (f"const B = require({json.dumps(str(STATIC / 'gm-backtest.js'))});"
              "process.stdout.write(JSON.stringify(B.view(JSON.parse(process.argv[1]))));")
    out = subprocess.run([NODE, "-e", script, json.dumps(result)], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)


READY = {"status": "ready", "history_start": "2026-05-09T06:38:00+00:00", "history_end": "2026-11-05T06:37:00+00:00",
         "summary": {"runs": 25, "median_return_pct": 0.25, "worst_return_pct": -2.05, "best_return_pct": 0.99, "stopped_pct": 40.0,
                     "mean_fills": 30.1, "funded_runs": 12, "histogram": {"low": -2.05, "width": 0.25, "counts": [4, 5, 1, 0, 0, 1, 0, 1, 0, 10, 2, 1]}}}


def test_ready_shows_the_numbers_and_what_is_assumed() -> None:
    v = view(READY)
    assert v["title"] == "这组网格过去 180 天的表现（OKX，费后）" and v["sub"] == "跑了 25 段，每段 7 天，互不重叠"
    assert v["cells"] == [["中位收益", "+0.25%"], ["最差一次", "−2.05%"], ["跌破下沿或止损", "40%"], ["平均成交", "30.1 笔"]]
    assert "假设费率" in v["notes"][0] and v["notes"][1] == "资金费只计入了 12 / 25 段（OKX 只给最近约 94 天）"


def test_unavailable_names_the_reason_and_says_start_is_unaffected() -> None:
    v = view({"status": "unavailable", "reason": "历史数据有缺口：3 分钟"})
    assert v == {"state": "unavailable", "text": "回测不可用：历史数据有缺口：3 分钟。不影响启动。"}


def test_no_result_yet_is_running() -> None:
    assert view(None)["state"] == "running" and view({"status": "running"})["state"] == "running"


def test_box_has_no_grade_and_no_control() -> None:
    source = (STATIC / "gm-backtest.js").read_text(encoding="utf-8")
    assert "<button" not in source and "method:" not in source and "/api/execute" not in source
    assert not any(word in source for word in ("健康", "退役", "推荐", "评分"))
