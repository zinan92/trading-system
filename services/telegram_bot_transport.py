"""Small Telegram Bot API transport used by the Park control worker.

This module is intentionally outside the ``park_*.py`` contract-kernel
namespace.  It contains the only network client: Park strategy code consumes
its typed, receipt-bearing interface and never sees a token-bearing URL.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


TELEGRAM_BOT_TRANSPORT_SCHEMA = "telegram-bot-transport-v1"
_DEFAULT_API = "https://api.telegram.org"


class TelegramBotTransportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _required(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TelegramBotTransportError("transport_not_configured", f"{field} is required")
    return result


class TelegramBotTransport:
    """Minimal Telegram Bot API client with bounded, auditable responses."""

    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        api_base: str = _DEFAULT_API,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.token = _required(token or os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN"), "bot token")
        self.chat_id = _required(chat_id or os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID"), "chat id")
        base = str(api_base or _DEFAULT_API).rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise TelegramBotTransportError("transport_config_invalid", "Telegram API base must be HTTP(S)")
        self.api_base = base
        self._opener = opener or urllib.request.urlopen

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 20,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        timeout = self._bounded_timeout(timeout_seconds)
        if int(limit) < 1 or int(limit) > 100:
            raise TelegramBotTransportError("transport_config_invalid", "Telegram update limit must be 1..100")
        query: dict[str, str] = {
            "timeout": str(timeout),
            "limit": str(int(limit)),
            "allowed_updates": json.dumps(["message"], separators=(",", ":")),
        }
        if offset is not None:
            query["offset"] = str(int(offset))
        payload = self._request("getUpdates", query=query, timeout=timeout + 5)
        result = payload.get("result")
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise TelegramBotTransportError("transport_response_invalid", "Telegram getUpdates result is invalid")
        return [dict(row) for row in result]

    def send_message(self, text: str, *, chat_id: str | None = None) -> dict[str, Any]:
        body = str(text or "")
        if not body.strip():
            raise TelegramBotTransportError("transport_message_invalid", "Telegram message text is empty")
        payload = self._request(
            "sendMessage",
            data={"chat_id": str(chat_id or self.chat_id), "text": body},
            timeout=15,
        )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise TelegramBotTransportError("transport_response_invalid", "Telegram sendMessage result is invalid")
        message_id = result.get("message_id")
        if message_id in (None, ""):
            raise TelegramBotTransportError("transport_receipt_missing", "Telegram response has no message_id")
        chat = result.get("chat") if isinstance(result.get("chat"), Mapping) else {}
        return {
            "schema_version": TELEGRAM_BOT_TRANSPORT_SCHEMA,
            "ok": True,
            "message_id": str(message_id),
            "chat_id": str(chat.get("id") or chat_id or self.chat_id),
        }

    @staticmethod
    def _bounded_timeout(value: int) -> int:
        try:
            timeout = int(value)
        except (TypeError, ValueError) as exc:
            raise TelegramBotTransportError("transport_config_invalid", "Telegram timeout must be an integer") from exc
        if timeout < 1 or timeout > 60:
            raise TelegramBotTransportError("transport_config_invalid", "Telegram timeout must be 1..60 seconds")
        return timeout

    def _request(
        self,
        method: str,
        *,
        query: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        timeout: int,
    ) -> dict[str, Any]:
        # The token is present only in this local URL object and is never
        # returned in an exception, receipt, or log payload.
        url = f"{self.api_base}/bot{self.token}/{method}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(dict(query))}"
        encoded = urllib.parse.urlencode(dict(data)).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"} if encoded is not None else {},
            method="POST" if encoded is not None else "GET",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise TelegramBotTransportError("transport_unavailable", type(exc).__name__) from exc
        if status != 200:
            raise TelegramBotTransportError("transport_http_error", f"Telegram HTTP status {status}")
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TelegramBotTransportError("transport_response_invalid", "Telegram response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramBotTransportError("transport_api_error", "Telegram API returned ok=false")
        return dict(payload)

