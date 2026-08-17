"""Provider transport for the Park Dashboard AI proposal boundary.

This module is intentionally separate from the ``park_*.py`` safety modules:
the Park cutover guard statically rejects network imports in execution and
recording modules.  The provider remains proposal-only and has no execution
authority.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 12.0
MAX_CONTEXT_CHARS = 20_000
_SECRET_CONTEXT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
        "authorization",
    }
)


def _strip_json_fence(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _safe_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep provider context to Paper facts; never include credentials."""

    raw = dict(context or {})

    def scrub(value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[truncated]"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key).strip().lower().replace("-", "_")
                if key_text in _SECRET_CONTEXT_KEYS or any(
                    marker in key_text
                    for marker in ("secret", "password", "token", "private_key", "api_key")
                ):
                    continue
                result[str(key)] = scrub(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [scrub(item, depth + 1) for item in value[:200]]
        return value

    allowed = {
        "market": scrub(raw.get("market")),
        "strategy": scrub(raw.get("strategy")),
        "execution": scrub(raw.get("execution")),
        "account": scrub(raw.get("account")),
        "positions": scrub(raw.get("positions")),
        "orders": scrub(raw.get("orders")),
    }
    encoded = json.dumps(allowed, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > MAX_CONTEXT_CHARS:
        encoded = encoded[:MAX_CONTEXT_CHARS]
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        # Never fall back to the unsanitized source after a bounded truncation.
        # Keep only the already-scrubbed, small top-level facts.
        value = {
            "market": scrub(raw.get("market"), depth=0),
            "strategy": scrub(raw.get("strategy"), depth=0),
        }
    return value if isinstance(value, dict) else {}


class DeepSeekIntentProvider:
    """Bounded DeepSeek JSON intent extraction."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_DEEPSEEK_URL,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        timeout_seconds: float = DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = str(api_key or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.base_url = str(base_url or DEFAULT_DEEPSEEK_URL)
        self.model = str(model or DEFAULT_DEEPSEEK_MODEL)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.opener = opener or urlopen

    def parse(self, text: str, *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        metadata: dict[str, Any] = {
            "provider": "deepseek",
            "model": self.model,
            "status": "started",
            "timed_out": False,
        }
        if not self.api_key:
            metadata.update({"status": "missing_api_key", "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        system = (
            "You extract only the user's explicitly stated Paper strategy intent. "
            "Return one JSON object and no prose. Never invent values, authorize, "
            "or execute orders. Current price is read-only context. "
            "Allowed direction: long, short, neutral; neutral requires grid. "
            "Allowed strategy_type: dca, grid. Use null for missing values. "
            "Keys: direction, strategy_type, upper_price_boundary, lower_price_boundary, "
            "maximum_leverage, maximum_acceptable_loss, stop_price, take_profit_price, "
            "order_count, needs_clarification, clarification_fields, interpretation, confidence, "
            "position_action, entry_action, exit_action. For disposition extraction only, "
            "use keep, flatten, cancel, or null; do not invent an action."
        )
        user = json.dumps(
            {"message": str(text), "paper_context": _safe_context(context)},
            ensure_ascii=False,
            sort_keys=True,
        )
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(128_000)
            payload = json.loads(raw.decode("utf-8"))
            content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            candidate = json.loads(_strip_json_fence(str(content)))
            if not isinstance(candidate, dict):
                raise ValueError("DeepSeek content was not a JSON object")
        except TimeoutError:
            metadata.update({"status": "timeout", "timed_out": True, "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        except HTTPError as exc:
            metadata.update({"status": "http_error", "http_status": int(exc.code), "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        except (URLError, OSError) as exc:
            metadata.update({"status": "transport_error", "error_type": type(exc).__name__, "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        except (ValueError, TypeError, json.JSONDecodeError):
            metadata.update({"status": "invalid_output", "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        metadata.update({"status": "returned", "elapsed_ms": self._elapsed(started)})
        return {"status": "ok", "candidate": candidate, "metadata": metadata}

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, int(round((time.monotonic() - started) * 1000)))
