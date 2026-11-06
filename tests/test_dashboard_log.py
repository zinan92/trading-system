"""#1364: the Dashboard writes one redacted line per request, with the desk's request id."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pipelines.dashboard_server import BoundedThreadingHTTPServer, DashboardHandler
from services import structured_log as sl


@pytest.fixture
def served(tmp_path: Path):
    path = sl.configure("dashboard", tmp_path)
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", path, server
    server.shutdown()
    server.server_close()
    sl.configure("dashboard", None)
    root = logging.getLogger(sl.ROOT)
    for handler in list(root.handlers):
        if not isinstance(handler, logging.NullHandler):
            root.removeHandler(handler)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_request_is_one_line_with_the_desk_id_and_no_query(served) -> None:
    base, path, _ = served
    with pytest.raises(HTTPError):
        urlopen(Request(f"{base}/no-such-file?token=hunter2", headers={"X-Request-Id": "abcdef012345"}), timeout=5)
    line = next(row for row in rows(path) if row["event"] == "http")
    assert line["path"] == "/no-such-file" and line["status"] == "404" and line["request_id"] == "abcdef012345"
    assert "hunter2" not in path.read_text(encoding="utf-8")


def test_a_crash_in_a_handler_thread_reaches_the_log(served) -> None:
    _, path, server = served
    try:
        raise RuntimeError("handler died")
    except RuntimeError:
        server.handle_error(None, ("127.0.0.1", 1))
    line = next(row for row in rows(path) if row["level"] == "ERROR")
    assert line["event"] == "unhandled_exception" and "handler died" in line["exception"]
