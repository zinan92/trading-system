"""#1359 acceptance (#1374): induce three failures and find each break from what the page reads.

1. the market feed is down (venue mid and K-lines);
2. the preview is blocked;
3. a mock order is refused (confirm rejected).

Every upstream is faked; nothing reaches a venue. A fake secret is planted in the upstream errors,
and the log file written during all three must not contain it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from services import structured_log
from tests.test_desk import PREVIEW_OK, client_for

PLANTED = ("hunter2", "abc.def-123", "tsd_plantedtoken0000")
LEAK = f"upstream said token=hunter2 with Bearer abc.def-123 and tsd_plantedtoken0000"


@pytest.fixture
def log_file(config):
    path = structured_log.configure("desk", config.paper_output / "logs")
    yield path
    structured_log.configure("desk", None)
    root = logging.getLogger(structured_log.ROOT)
    for handler in [h for h in root.handlers if not isinstance(h, logging.NullHandler)]:
        root.removeHandler(handler)


def _lines(path: Path) -> list[dict]:
    for handler in logging.getLogger(structured_log.ROOT).handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _idle(paper_output: Path) -> None:
    (paper_output / "testnet_automation").mkdir(parents=True, exist_ok=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "idle"}))


def _approve(client, asset: str = "BTC"):
    return client.post("/api/judgments", json={"asset": asset, "direction": "short", "confidence": 3, "reason": "acceptance", "action": "approved"})


def test_1_feed_down_blocks_the_plan_and_the_page_says_where(config, sources, store, fetch, log_file) -> None:
    fetch.routes["allMids"] = ConnectionError(LEAK)
    fetch.routes["candleSnapshot"] = ConnectionError(LEAK)
    client, ex = client_for(config, sources, store, fetch)

    refused = _approve(client)
    assert refused.status_code == 409 and "现价读不到" in refused.json()["detail"]
    ident = refused.headers["x-request-id"]

    checks = {c["name"]: c for c in client.get("/api/system").json()["checks"]}
    assert checks["测试盘 K 线（Hyperliquid）"]["ok"] is False and "ConnectionError" in checks["测试盘 K 线（Hyperliquid）"]["detail"]
    assert checks["日志（24 小时）"]["detail"].startswith("错误 0 · 警告 ") and not checks["日志（24 小时）"]["detail"].startswith("错误 0 · 警告 0")

    mine = [row for row in _lines(log_file) if row.get("request_id") == ident]
    assert any(row["event"] == "caught_exception" and "hl_mid" in row["where"] for row in mine)
    assert any(row["event"] == "http" and row["status"] == 409 for row in mine)
    assert ex.driver_calls == [] and not any("dashboard-control" in url for url, _ in fetch.calls)


def test_2_a_blocked_preview_is_the_first_break_of_its_judgment(config, sources, store, fetch, log_file) -> None:
    fetch.routes["/api/dashboard-control/preview"] = {"preview": {"execution_ready": False, "blockers": ["market_facts_required"], "preview_digest": None}}
    client, ex = client_for(config, sources, store, fetch)

    saved = _approve(client).json()
    assert saved["preview"]["execution_ready"] is False and "缺少实时行情" in saved["handoff"]
    chain = client.get(f"/api/why?judgment={saved['id']}").json()
    assert chain["status"] == "broken" and chain["first_problem"]["key"] == "preview"
    assert "缺少实时行情" in chain["first_problem"]["summary"]
    assert [s["status"] for s in chain["steps"] if s["key"] in {"press", "result"}] == ["not_reached", "not_reached"]
    assert ex.driver_calls == []


def test_3_a_refused_mock_order_is_the_first_break_and_nothing_is_driven(config, sources, store, fetch, paper_output, log_file) -> None:
    _idle(paper_output)
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/confirm"] = {"confirmation": {"status": "rejected", "blockers": ["mock_venue_rejected"], "detail": LEAK}}
    client, ex = client_for(config, sources, store, fetch)

    saved = _approve(client).json()
    response = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 4.2, "confirm_text": "执行"})
    assert response.status_code == 409 and "确认没有通过" in response.json()["detail"]
    chain = client.get(f"/api/why?judgment={saved['id']}").json()
    assert chain["first_problem"]["key"] == "result" and "确认没有通过" in chain["first_problem"]["summary"]
    assert [s["status"] for s in chain["steps"] if s["key"] in {"preview", "press"}] == ["ok", "ok"]
    assert ex.driver_calls == []


def test_4_no_planted_secret_reaches_the_log(config, sources, store, fetch, paper_output, log_file) -> None:
    """Run all three failures into one log file, then search it."""
    _idle(paper_output)
    client, _ = client_for(config, sources, store, fetch)
    fetch.routes["allMids"] = ConnectionError(LEAK)
    fetch.routes["candleSnapshot"] = ConnectionError(LEAK)
    _approve(client)
    client.get("/api/system")
    fetch.routes["candleSnapshot"] = [{"t": 1788998400000, "o": "100", "h": "102", "l": "99", "c": "101"}]
    fetch.routes["/api/dashboard-control/preview"] = RuntimeError(LEAK)
    _approve(client)
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/confirm"] = RuntimeError(LEAK)
    judgment = _approve(client, "ETH").json()
    client.post("/api/execute", json={"judgment_id": judgment.get("id"), "shown_max_loss": 4.2, "confirm_text": "执行"})

    text = log_file.read_text(encoding="utf-8")
    assert "caught_exception" in text and "unhandled_exception" in text  # the failures were logged, with tracebacks
    assert text.count("upstream said") >= 3 and structured_log.REDACTED in text  # the planted text did reach the log, masked
    assert not [secret for secret in PLANTED if secret in text]
