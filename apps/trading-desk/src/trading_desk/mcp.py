"""Read-only MCP server for the desk (#1344): `python -m trading_desk.mcp` over stdio.

Every tool is one GET on the token-gated open API (/api/v1). The server holds no permissions of its
own: the desk checks the token and the allow-list, so an MCP client can read Paper status, the
week's watch list, AI advice with its sources, review runs and backtest results, and can never place,
approve, stop or change anything. There is deliberately no write tool.

Environment:
  TRADING_DESK_API_TOKEN  a token from `python -m trading_desk token create <name>` (required)
  TRADING_DESK_API_URL    the desk, default http://127.0.0.1:8790

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport). Implemented by
hand (initialize, ping, tools/list, tools/call) so the desk gains no dependency.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "park-trading-desk", "version": "1.0.0"}
ASSET = {"type": "string", "description": "品种代码，例如 XAU、BTC（见 list_assets）", "pattern": "^[A-Za-z0-9_-]{1,32}$"}


def _tool(name: str, description: str, path: Callable[[dict[str, Any]], str], properties: dict[str, Any] | None = None,
          required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema, "path": path,
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}


def _q(value: Any) -> str:
    return quote(str(value), safe="")


TOOLS: tuple[dict[str, Any], ...] = (
    _tool("paper_status", "Paper 纸面盘现在的状态：有没有在跑的策略、挂单和持仓数、盈亏、行情是否可信。", lambda a: "/status"),
    _tool("list_assets", "交易台上的品种列表。", lambda a: "/assets"),
    _tool("asset_desk", "一个品种的交易台：价格、网格、今天的判断、AI 建议、最近执行。", lambda a: f"/desk/{_q(a['asset'])}",
          {"asset": ASSET}, ("asset",)),
    _tool("asset_bars", "一个品种的 K 线（最近 60 根）。", lambda a: f"/bars/{_q(a['asset'])}?{urlencode({'tf': a.get('timeframe', '4h')})}",
          {"asset": ASSET, "timeframe": {"type": "string", "enum": ["1h", "4h", "1d"], "default": "4h"}}, ("asset",)),
    _tool("asset_news", "一个品种的新闻。", lambda a: f"/news/{_q(a['asset'])}", {"asset": ASSET}, ("asset",)),
    _tool("watch_this_week", "本周要看的三件事，每件带来源；理由里没被来源支持的数字会列在 unverified。", lambda a: "/watch"),
    _tool("ai_advice", "今天每个品种的 AI 建议，带依据和价格来源。", lambda a: "/advice"),
    _tool("review_runs", "跑过的策略回合，可按品种和时间（0=全部、7、30、90 天）筛选。",
          lambda a: "/review/runs?" + urlencode({k: v for k, v in (("asset", a.get("asset")), ("days", a.get("days", 0))) if v is not None}),
          {"asset": ASSET, "days": {"type": "integer", "enum": [0, 7, 30, 90], "default": 0}}),
    _tool("review_timeline", "一个回合的时间线：K 线、挂单、成交和当时的判断。", lambda a: "/review/timeline?" + urlencode({"run": a["run"]}),
          {"run": {"type": "string", "description": "review_runs 返回的回合 id"}}, ("run",)),
    _tool("backtest_comparison", "读取一次回测工作台对比的结果（running / ready / unavailable）。只读：开始回测要在页面上按。",
          lambda a: f"/backtest/compare/{_q(a['key'])}", {"key": {"type": "string", "pattern": "^[0-9a-f]{24}$"}}, ("key",)),
    _tool("system_health", "各个服务的健康检查和最近执行记录。", lambda a: "/system"),
)
BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _valid(tool: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    schema = tool["inputSchema"]
    for key in schema.get("required", []):
        if arguments.get(key) in (None, ""):
            return f"缺少参数 {key}"
    for key, value in arguments.items():
        spec = schema["properties"].get(key)
        if spec is None:
            return f"没有参数 {key}"
        if "enum" in spec and value not in spec["enum"]:
            return f"{key} 只能是 {spec['enum']}"
        if "pattern" in spec and not re.fullmatch(spec["pattern"], str(value)):
            return f"{key} 格式不对"
    return None


Fetch = Callable[[str], tuple[int, Any]]


def http_fetch(base: str, token: str) -> Fetch:
    def fetch(path: str) -> tuple[int, Any]:
        request = Request(f"{base.rstrip('/')}/api/v1{path}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - the desk URL is the operator's own
                return response.status, json.loads(response.read() or b"null")
        except HTTPError as error:
            try:
                body = json.loads(error.read() or b"null")
            except ValueError:
                body = None
            return error.code, body
    return fetch


def handle(message: dict[str, Any], fetch: Fetch | None) -> dict[str, Any] | None:
    """One JSON-RPC message in, its response out (None for notifications)."""

    method, ident = message.get("method"), message.get("id")
    if ident is None:  # a notification (e.g. notifications/initialized): never answered
        return None

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return ok({"protocolVersion": requested or PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                   "instructions": "只读：交易台的状态、关注、AI 建议、复盘和回测结果。没有任何下单、停止或修改的工具。"})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{key: tool[key] for key in ("name", "description", "inputSchema", "annotations")} for tool in TOOLS]})
    if method == "tools/call":
        params = message.get("params") or {}
        tool = BY_NAME.get(str(params.get("name")))
        if tool is None:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": f"没有这个工具：{params.get('name')}"}}
        arguments = params.get("arguments") or {}
        problem = _valid(tool, arguments) if isinstance(arguments, dict) else "arguments 要是对象"
        if problem:
            return ok({"content": [{"type": "text", "text": problem}], "isError": True})
        if fetch is None:
            return ok({"content": [{"type": "text", "text": "没有设置 TRADING_DESK_API_TOKEN：先运行 python -m trading_desk token create <name>"}], "isError": True})
        try:
            status, body = fetch(tool["path"](arguments))
        except (URLError, OSError, ValueError) as error:
            return ok({"content": [{"type": "text", "text": f"交易台连不上：{type(error).__name__}"}], "isError": True})
        text = json.dumps(body, ensure_ascii=False)
        if status != 200:
            return ok({"content": [{"type": "text", "text": f"HTTP {status}: {text}"}], "isError": True})
        return ok({"content": [{"type": "text", "text": text}], "structuredContent": body if isinstance(body, dict) else {"items": body}, "isError": False})
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def serve(stdin=sys.stdin, stdout=sys.stdout, fetch: Fetch | None = None) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message, fetch) if isinstance(message, dict) else {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    token = os.environ.get("TRADING_DESK_API_TOKEN", "").strip()
    base = os.environ.get("TRADING_DESK_API_URL", "http://127.0.0.1:8790")
    serve(fetch=http_fetch(base, token) if token else None)


if __name__ == "__main__":
    main()
