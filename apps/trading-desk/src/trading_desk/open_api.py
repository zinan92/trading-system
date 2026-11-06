"""只读开放 API (#1344): `/api/v1/<path>` mirrors an allow-list of the desk's own GET routes.

Rules, each pinned by a test:
- GET or HEAD only. Anything else is 405, before the token is even looked at.
- A bearer token is required, also from this Mac. Tokens are created with `trading_desk token
  create <name>`, which prints the token once; only its SHA-256 is kept, in a 0600 file.
- Only the paths in ALLOWED are served. A path that exists under /api/ but is not listed is 404
  here; nothing that places, approves, stops, changes or refreshes anything is listed, and GETs
  that write (`/api/review` resolves due judgments) are left out too.
- `status` is the one route of its own: a summary of the Paper read model, nothing else of it.

A request that passes is rewritten to the matching /api/ route, so the answer is byte-for-byte the
page's own. It carries a marker the phone passcode gate honours for /api/v1 only (remote.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

PREFIX = "/api/v1"
SCOPE_KEY = "desk.api_token"
_KEY = r"[A-Za-z0-9_-]{1,32}"
ALLOWED: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"/assets",
    rf"/desk/{_KEY}",
    rf"/bars/{_KEY}",
    rf"/news/{_KEY}",
    r"/watch",
    r"/advice",
    r"/review/runs",
    r"/review/timeline",
    r"/why",
    r"/backtest/compare/[0-9a-f]{24}",
    r"/backtest/[0-9a-f]{8,64}",
    r"/system",
))
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


class Tokens:
    """Named bearer tokens. The file holds {name: {sha256, created_at}} and is kept 0600."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            if self.path.stat().st_mode & 0o077:
                return {}  # a token file others can read is not trusted at all
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def create(self, name: str) -> str:
        if not NAME.fullmatch(name):
            raise ValueError("名字只能用小写字母、数字、- 和 _，最长 40 个字符")
        data = self._load()
        if name in data:
            raise ValueError(f"已经有叫 {name} 的 token；先 revoke 再建")
        token = "tsd_" + secrets.token_urlsafe(32)
        data[name] = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._save(data)
        return token

    def revoke(self, name: str) -> bool:
        data = self._load()
        if data.pop(name, None) is None:
            return False
        self._save(data)
        return True

    def names(self) -> list[dict[str, str]]:
        return [{"name": name, "created_at": row.get("created_at", "")} for name, row in sorted(self._load().items())]

    def check(self, header: str | None) -> str | None:
        """The token's name, or None. Constant-time against every stored hash."""

        if not header or not header.startswith("Bearer "):
            return None
        digest = hashlib.sha256(header[len("Bearer "):].strip().encode()).hexdigest()
        found = None
        for name, row in self._load().items():
            if hmac.compare_digest(digest, str(row.get("sha256") or "")):
                found = name
        return found


def paper_status(read_model: dict[str, Any]) -> dict[str, Any]:
    """The few Paper facts a reader needs; never orders, fills, account ids or control state."""

    strategy = read_model.get("current_strategy") or {}
    runtime = read_model.get("runtime") or {}
    execution = read_model.get("execution") or {}
    counts = execution.get("counts") or {}
    market = read_model.get("market") or {}
    return {
        "schema_version": "desk-open-status-v1",
        "paper_only": True,
        "strategy": {"active": bool(strategy.get("active")), "status": strategy.get("status"), "status_label": strategy.get("status_label"),
                     "type": (strategy.get("specification") or {}).get("strategy_type"), "direction": (strategy.get("specification") or {}).get("direction")},
        "runtime": {"status": runtime.get("status"), "status_label": runtime.get("status_label"),
                    "open_orders": counts.get("open_order_count", runtime.get("open_order_count")),
                    "unknown_orders": counts.get("unknown_order_count", runtime.get("unknown_order_count"))},
        "positions": {"open": counts.get("open_position_count"), "completed_round_trips": counts.get("completed_round_trip_count")},
        "pnl": execution.get("pnl"),
        "market": {key: market.get(key) for key in ("symbol", "provider", "provider_symbol", "timeframe", "trusted", "status", "latest_close", "latest_timestamp")},
    }


def install(app, tokens: Tokens, read_model: Callable[[], dict[str, Any]]) -> None:
    """Add the /api/v1 gate. Install it last so it runs first, before the passcode gate."""

    @app.middleware("http")
    async def open_api(request: Request, call_next):
        path = request.url.path
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            return await call_next(request)
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "只读接口：只接受 GET"}, status_code=405, headers={"Allow": "GET, HEAD"})
        name = tokens.check(request.headers.get("authorization"))
        if name is None:
            return JSONResponse({"detail": "需要有效的 API token（Authorization: Bearer …）"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        rest = path[len(PREFIX):]
        if rest == "/status":
            try:
                body = paper_status(await run_in_threadpool(read_model))
            except Exception as exc:  # noqa: BLE001 - say the backend is down; never guess a state
                return JSONResponse({"detail": f"交易后台读不到：{type(exc).__name__}"}, status_code=503, headers={"Cache-Control": "no-store"})
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        if not any(pattern.fullmatch(rest) for pattern in ALLOWED):
            return JSONResponse({"detail": "开放 API 没有这个地址"}, status_code=404)
        request.scope["path"] = "/api" + rest
        request.scope["raw_path"] = ("/api" + rest).encode()
        request.scope[SCOPE_KEY] = name
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
