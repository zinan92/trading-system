"""Read-only MCP server (#1344): stdio JSON-RPC, every tool one GET on the token-gated /api/v1."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from trading_desk import mcp, open_api

SRC = Path(__file__).resolve().parents[1] / "src"


def call(name: str, arguments: dict | None = None, fetch=None, ident: int = 1) -> dict:
    return mcp.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}, fetch)


def test_every_tool_is_one_allow_listed_get() -> None:
    sample = {"asset": "XAU", "run": "r1", "key": "ab" * 12, "timeframe": "1h", "days": 7}
    for tool in mcp.TOOLS:
        schema = tool["inputSchema"]
        arguments = {key: sample[key] for key in schema["properties"] if key in sample}
        path = tool["path"](arguments).split("?", 1)[0]
        assert path == "/status" or any(pattern.fullmatch(path) for pattern in open_api.ALLOWED), (tool["name"], path)
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False
    words = {word for tool in mcp.TOOLS for word in tool["name"].split("_")}
    assert not words & {"execute", "order", "orders", "stop", "start", "approve", "create", "delete", "refresh", "set", "update", "place", "cancel", "close"}


def test_initialize_list_and_notifications() -> None:
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, None)
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, None) is None
    listed = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)["result"]["tools"]
    assert {tool["name"] for tool in listed} == set(mcp.BY_NAME) and all("path" not in tool for tool in listed)
    assert mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/write"}, None)["error"]["code"] == -32601


def test_arguments_are_checked_before_any_request() -> None:
    asked: list[str] = []
    fetch = lambda path: (asked.append(path), (200, {}))[1]  # noqa: E731
    assert call("asset_desk", {}, fetch)["result"]["isError"] is True
    assert call("asset_desk", {"asset": "../execute"}, fetch)["result"]["isError"] is True
    assert call("review_runs", {"days": 5}, fetch)["result"]["isError"] is True
    assert call("paper_status", {"extra": 1}, fetch)["result"]["isError"] is True
    assert call("place_order", {}, fetch)["error"]["code"] == -32602
    assert asked == []
    assert call("asset_bars", {"asset": "XAU", "timeframe": "1d"}, fetch)["result"]["isError"] is False
    assert asked == ["/bars/XAU?tf=1d"]


def test_without_a_token_the_tools_say_how_to_get_one() -> None:
    result = call("paper_status", fetch=None)["result"]
    assert result["isError"] is True and "token create" in result["content"][0]["text"]


def test_http_errors_are_tool_errors_not_crashes() -> None:
    assert "HTTP 401" in call("paper_status", fetch=lambda path: (401, {"detail": "需要有效的 API token"}))["result"]["content"][0]["text"]

    def down(path):
        raise OSError("refused")

    assert "连不上" in call("paper_status", fetch=down)["result"]["content"][0]["text"]


def test_serve_reads_lines_and_answers_only_requests() -> None:
    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
    ]) + "\n")
    out = io.StringIO()
    mcp.serve(stdin, out, fetch=None)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}]


def test_the_module_speaks_stdio_to_a_desk_with_the_token() -> None:
    seen: list[tuple[str, str | None]] = []

    class Desk(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"paper_only": True, "strategy": {"active": False}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Desk)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": mcp.PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "paper_status", "arguments": {}}},
        ]
        env = {**os.environ, "PYTHONPATH": str(SRC), "TRADING_DESK_API_TOKEN": "tsd_test", "TRADING_DESK_API_URL": f"http://127.0.0.1:{server.server_address[1]}"}
        done = subprocess.run([sys.executable, "-m", "trading_desk.mcp"], input="\n".join(json.dumps(m) for m in messages) + "\n",
                              env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"] == {"paper_only": True, "strategy": {"active": False}}
    assert seen == [("/api/v1/status", "Bearer tsd_test")]
