"""#1364: every API request leaves a line, and an unhandled exception leaves a number the page can show."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from services import structured_log as sl
from test_desk import client_for
from trading_desk import sources as sources_module


@pytest.fixture
def log(tmp_path: Path):
    path = sl.configure("desk", tmp_path / "logs")
    yield path
    sl.configure("desk", None)
    root = logging.getLogger(sl.ROOT)
    for handler in list(root.handlers):
        if not isinstance(handler, logging.NullHandler):
            root.removeHandler(handler)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_an_api_request_leaves_one_line_without_its_query(config, sources, store, fetch, log) -> None:
    client, _ = client_for(config, sources, store, fetch)
    response = client.get("/api/assets?token=hunter2")
    line = next(row for row in rows(log) if row["event"] == "http")
    assert line["path"] == "/api/assets" and line["status"] == 200 and line["method"] == "GET" and line["ms"] >= 0
    assert line["request_id"] == response.headers["x-request-id"]
    assert "hunter2" not in log.read_text(encoding="utf-8")


def test_static_files_stay_out(config, sources, store, fetch, log) -> None:
    client, _ = client_for(config, sources, store, fetch)
    client.get("/static/style.css")
    assert not rows(log)


def test_an_unhandled_exception_gives_a_number_that_finds_the_traceback(config, sources, store, fetch, log) -> None:
    client, _ = client_for(config, sources, store, fetch)
    store.assets = lambda: (_ for _ in ()).throw(RuntimeError("boom token=hunter2"))
    response = client.get("/api/assets")
    assert response.status_code == 500
    ref = response.json()["ref"]
    assert ref in response.json()["detail"] and "boom" not in response.text
    line = next(row for row in rows(log) if row["level"] == "ERROR")
    assert line["request_id"] == ref and line["path"] == "/api/assets"
    assert "RuntimeError" in line["exception"] and "hunter2" not in line["exception"]


def test_a_caught_upstream_failure_keeps_its_traceback(sources, btc, fetch, log) -> None:
    fetch.routes["/api/ui/realtime"] = ConnectionError("intel down")
    sources.news(btc)
    line = next(row for row in rows(log) if row["event"] == "caught_exception")
    assert line["level"] == "WARNING" and "ConnectionError" in line["exception"] and "desk.sources" in line["where"]


def test_execution_records_are_logged_by_name_only(store, log) -> None:
    store.log_execution(asset="BTC", stage="preview", judgment_id=1, preview_digest="sha256:a", detail={"secret_material": "x", "orders": [1]})
    line = next(row for row in rows(log) if row["event"] == "execution")
    assert (line["stage"], line["asset"], line["judgment_id"], line["preview_digest"]) == ("preview", "BTC", 1, "sha256:a")
    assert "orders" not in line


def test_the_system_page_counts_problems_and_names_the_latest(config, sources, store, fetch, tmp_path) -> None:
    logs = config.paper_output / "logs"
    path = sl.configure("desk", logs)
    sl.set_request_id("abcdef012345")
    sl.get_logger("t").error("preview_crashed")
    sl.configure("desk", None)
    client, _ = client_for(config, sources, store, fetch)
    row = next(check for check in client.get("/api/system").json()["checks"] if check["name"] == "日志（24 小时）")
    assert row["ok"] is False and "错误 1" in row["detail"] and "preview_crashed" in row["detail"] and "abcdef012345" in row["detail"]
    assert path.exists()


def test_the_request_id_travels_to_the_dashboard(monkeypatch) -> None:
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        seen.update(request.headers)
        return Response()

    monkeypatch.setattr(sources_module.urllib.request, "urlopen", urlopen)
    sl.set_request_id("abcdef012345")
    sources_module.http_json("http://x/y")
    assert seen["X-request-id"] == "abcdef012345"
    sl._request_id.set("")
    seen.clear()
    sources_module.http_json("http://x/y")
    assert "X-request-id" not in seen
