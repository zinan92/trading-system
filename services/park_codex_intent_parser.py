"""Bounded Codex CLI intent extraction for the Park Telegram control plane.

Codex is used as a natural-language understanding helper only.  Its output is
an untrusted candidate that must pass the deterministic Park normalizer and
risk planner before a proposal can exist.  This module never receives a
Telegram token and never exposes an execution adapter to the subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping


PARK_CODEX_INTENT_SCHEMA = "park-codex-intent-v1"
DEFAULT_CODEX_CLI = "/opt/homebrew/bin/codex"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_STDOUT_BYTES = 128_000
MAX_STDERR_BYTES = 4_000


class ParkCodexIntentError(RuntimeError):
    def __init__(self, code: str, message: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.metadata = dict(metadata or {})


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "")
    return text[:limit]


def _digest(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _number(value: Any, field: str, *, integer: bool = False) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ParkCodexIntentError("provider_output_invalid", f"Codex returned a non-numeric {field}") from exc
    if parsed <= 0:
        raise ParkCodexIntentError("provider_output_invalid", f"Codex returned a non-positive {field}")
    return parsed


def _json_from_agent_message(stdout: str) -> dict[str, Any]:
    """Extract the final Codex agent message from ``codex exec --json``."""

    final_text = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            final_text = str(item.get("text") or "").strip()
    if not final_text:
        raise ParkCodexIntentError("provider_output_missing", "Codex returned no final intent message")
    if final_text.startswith("```"):
        lines = final_text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        final_text = "\n".join(lines).strip()
    try:
        value = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise ParkCodexIntentError("provider_output_invalid", "Codex final message was not JSON") from exc
    if not isinstance(value, dict):
        raise ParkCodexIntentError("provider_output_invalid", "Codex final message was not an object")
    return value


def _candidate(value: Mapping[str, Any], *, source_text: str) -> dict[str, Any]:
    direction = value.get("direction")
    if direction not in (None, "", "long", "short", "neutral"):
        raise ParkCodexIntentError("provider_output_invalid", "Codex returned an unsupported direction")
    strategy_type = value.get("strategy_type")
    if strategy_type not in (None, "", "dca", "grid"):
        raise ParkCodexIntentError("provider_output_invalid", "Codex returned an unsupported strategy type")
    order_count = _number(value.get("order_count"), "order_count", integer=True)
    if order_count is not None and int(order_count) != order_count:
        raise ParkCodexIntentError("provider_output_invalid", "Codex returned a non-integral order_count")
    clarification_fields = value.get("clarification_fields")
    if clarification_fields is None:
        clarification_fields = []
    if not isinstance(clarification_fields, list) or any(not isinstance(row, str) for row in clarification_fields):
        raise ParkCodexIntentError("provider_output_invalid", "Codex clarification_fields must be a string list")
    raw_confidence = value.get("confidence")
    if isinstance(raw_confidence, (int, float)):
        confidence = "high" if raw_confidence >= 0.8 else "medium" if raw_confidence >= 0.5 else "low"
    else:
        confidence = raw_confidence if raw_confidence in {"high", "medium", "low"} else "low"
    return {
        "schema_version": PARK_CODEX_INTENT_SCHEMA,
        "direction": direction or None,
        "strategy_type": strategy_type or None,
        "upper_price_boundary": _number(value.get("upper_price_boundary"), "upper_price_boundary"),
        "lower_price_boundary": _number(value.get("lower_price_boundary"), "lower_price_boundary"),
        "maximum_leverage": _number(value.get("maximum_leverage"), "maximum_leverage"),
        "maximum_acceptable_loss": _number(value.get("maximum_acceptable_loss"), "maximum_acceptable_loss"),
        "stop_price": _number(value.get("stop_price"), "stop_price"),
        "take_profit_price": _number(value.get("take_profit_price"), "take_profit_price"),
        "order_count": int(order_count) if order_count is not None else None,
        "needs_clarification": bool(value.get("needs_clarification")),
        "clarification_fields": clarification_fields[:12],
        "interpretation": _bounded(value.get("interpretation"), 500),
        "confidence": confidence,
        "source_text": source_text,
    }


def _prompt(text: str) -> str:
    return f"""You are a strict natural-language intent extractor for a Paper-only trading control plane.

Read the user's message below and return exactly one JSON object matching the requested fields.
Do not use tools, do not execute commands, do not read files, and do not propose an order.
Do not invent values. Null means the user did not clearly provide that value.

Canonical values:
- direction: long, short, or neutral. `neutral` is a directionless Grid intent only.
- strategy_type: dca or grid. Chinese terms 做多/做空/中性, 趋势/DCA, 震荡/网格/Grid are valid natural-language hints.
- A clearly stated price range such as 4450 4100 or 4450~4100 maps to upper/lower boundaries.
- maximum_leverage and maximum_acceptable_loss are alternatives; if either is present, do not mark the other as missing.
- Current price is intentionally not extracted; the trusted market reader supplies it later.

Return these keys only:
schema_version, direction, strategy_type, upper_price_boundary, lower_price_boundary,
maximum_leverage, maximum_acceptable_loss, stop_price, take_profit_price, order_count,
needs_clarification, clarification_fields, interpretation, confidence.

The user message is:
---
{text}
---
"""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class CodexCliIntentParser:
    """Call Codex CLI with read-only, ephemeral, bounded execution."""

    def __init__(
        self,
        *,
        executable: str | Path = DEFAULT_CODEX_CLI,
        model: str = "gpt-5.6-luna",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: str | Path | None = None,
        codex_home: str | Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.executable = str(executable)
        self.model = str(model or "gpt-5.6-luna")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.cwd = Path(cwd or tempfile.gettempdir())
        self.codex_home = str(codex_home or os.environ.get("CODEX_HOME") or "")
        self.runner = runner or subprocess.run

    def parse(self, text: str) -> dict[str, Any]:
        started = time.monotonic()
        metadata: dict[str, Any] = {
            "provider": "codex_cli",
            "executable": self.executable,
            "status": "started",
            "timed_out": False,
            "exit_code": None,
        }
        if not str(text or "").strip():
            metadata.update({"status": "skipped", "elapsed_ms": 0})
            return {"status": "skipped", "metadata": metadata}
        if not Path(self.executable).is_file() or not os.access(self.executable, os.X_OK):
            metadata.update({"status": "unavailable", "elapsed_ms": self._elapsed(started)})
            return {"status": "unavailable", "metadata": metadata}
        env = {
            "HOME": os.environ.get("HOME") or "/Users/wendy",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        try:
            completed = self.runner(
                [
                    self.executable,
                    "exec",
                    "--model",
                    self.model,
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--disable",
                    "skill_search",
                    "--json",
                    "--color",
                    "never",
                    "-",
                ],
                input=_prompt(str(text)),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                cwd=str(self.cwd),
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            metadata.update(
                {
                    "status": "timeout",
                    "timed_out": True,
                    "elapsed_ms": self._elapsed(started),
                    "stderr_digest": _digest(_bounded(exc.stderr, MAX_STDERR_BYTES)),
                }
            )
            return {"status": "unavailable", "metadata": metadata}
        except OSError as exc:
            metadata.update(
                {
                    "status": "unavailable",
                    "elapsed_ms": self._elapsed(started),
                    "error_type": type(exc).__name__,
                }
            )
            return {"status": "unavailable", "metadata": metadata}

        stdout = _bounded(completed.stdout, MAX_STDOUT_BYTES)
        stderr = _bounded(completed.stderr, MAX_STDERR_BYTES)
        metadata.update(
            {
                "status": "returned" if completed.returncode == 0 else "error",
                "exit_code": int(completed.returncode),
                "elapsed_ms": self._elapsed(started),
                "stderr_digest": _digest(stderr) if stderr else None,
            }
        )
        if completed.returncode != 0:
            return {"status": "unavailable", "metadata": metadata}
        try:
            candidate = _candidate(_json_from_agent_message(stdout), source_text=str(text))
        except ParkCodexIntentError as exc:
            metadata.update({"status": exc.code})
            return {"status": "unavailable", "metadata": metadata}
        return {"status": "ok", "candidate": candidate, "metadata": metadata}

    def _elapsed(self, started: float) -> int:
        return max(0, int(round((time.monotonic() - started) * 1000)))
