"""Bounded Codex CLI intent extraction for the Park Telegram control plane.

Codex is used as a natural-language understanding helper only.  Its output is
an untrusted candidate that must pass the deterministic Park normalizer and
risk planner before a proposal can exist.  This module never receives a
Telegram token and never exposes an execution adapter to the subprocess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

PARK_CODEX_INTENT_SCHEMA = "park-codex-intent-v1"
DEFAULT_CODEX_CLI = "/opt/homebrew/bin/codex"
DEFAULT_TIMEOUT_SECONDS = 30.0
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

    def action(name: str, allowed: set[str]) -> str | None:
        raw = value.get(name)
        if raw in (None, ""):
            return None
        normalized = str(raw).strip().lower()
        return normalized if normalized in allowed else None

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
        "position_action": action("position_action", {"keep", "preserve", "flatten", "close", "close_all"}),
        "entry_action": action("entry_action", {"keep", "preserve", "cancel", "cancel_entries", "cancel_orders"}),
        "exit_action": action("exit_action", {"keep", "preserve", "manage"}),
        "source_text": source_text,
    }


def deterministic_neutral_grid_candidate(text: str) -> dict[str, Any] | None:
    """Recognize the narrow, non-authoritative neutral-Grid fallback shape.

    This is deliberately smaller than the Codex parser.  It exists only so a
    provider timeout does not turn an otherwise clear ``中性网格`` message into
    a misleading ``missing_direction`` response.  The returned candidate still
    has to pass the deterministic neutral-Grid risk planner and exact Park
    confirmation before it can reach Paper execution.
    """

    source_text = str(text or "").strip()
    lowered = source_text.lower()
    neutral = "中性" in source_text or "neutral" in lowered
    grid = "网格" in source_text or "grid" in lowered
    if not (neutral and grid):
        return None

    range_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:~|～|-|到|至)\s*([0-9]+(?:\.[0-9]+)?)", source_text)
    if range_match is None:
        # Park commonly writes the two boundaries separated by spaces.  Only
        # use the first adjacent numeric pair here; Codex remains the general
        # natural-language parser when it is available.
        range_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)", source_text)
    upper: float | None = None
    lower: float | None = None
    if range_match is not None:
        first, second = float(range_match.group(1)), float(range_match.group(2))
        if first > 0 and second > 0 and first != second:
            upper, lower = max(first, second), min(first, second)

    leverage_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:倍\s*杠杆|倍|x)(?:\s*杠杆|\s*leverage)?",
        source_text,
        re.IGNORECASE,
    )
    maximum_leverage = float(leverage_match.group(1)) if leverage_match else None
    loss_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:最大可接受亏损|最大亏损|max(?:imum)?\s*loss)",
        source_text,
        re.IGNORECASE,
    )
    maximum_loss = float(loss_match.group(1)) if loss_match else None
    if upper is None or lower is None or (maximum_leverage is None and maximum_loss is None):
        return None

    return {
        "schema_version": PARK_CODEX_INTENT_SCHEMA,
        "direction": "neutral",
        "strategy_type": "grid",
        "upper_price_boundary": upper,
        "lower_price_boundary": lower,
        "maximum_leverage": maximum_leverage,
        "maximum_acceptable_loss": maximum_loss,
        "stop_price": None,
        "take_profit_price": None,
        "order_count": None,
        "needs_clarification": False,
        "clarification_fields": [],
        "interpretation": "中性网格意图（确定性超时后备识别）",
        "confidence": "medium",
        "source_text": source_text,
    }


def deterministic_legacy_clean_slate_candidate(text: str) -> dict[str, Any] | None:
    """Recognize the bounded, explicit legacy-order cutover instruction.

    This is intentionally narrower than the Codex intent parser.  It exists
    for the one operator phrase that must remain actionable when the optional
    provider is unavailable: Park explicitly names the old pending orders,
    requests a clean slate, and asks to enable the Paper track.  The returned
    candidate is only a request; the account snapshot and exact order set are
    re-read before any cancellation capability can be minted.
    """

    source_text = str(text or "").strip()
    lowered = source_text.lower()
    cancel = "取消" in source_text or "撤销" in source_text or "cancel" in lowered
    old_orders = (
        "旧挂单" in source_text
        or "旧订单" in source_text
        or ("old" in lowered and "order" in lowered)
    )
    clean_slate = (
        "clean slate" in lowered
        or "clean-slate" in lowered
        or "清空" in source_text
        or "清仓" in source_text
    )
    enable_park = (
        "启用 park" in lowered
        or "enable park" in lowered
        or "启用纸面" in source_text
        or "启用 paper" in lowered
    )
    if not (cancel and old_orders and clean_slate and enable_park):
        return None
    count_match = re.search(r"(?:这|共|共计|the)?\s*(\d+)\s*(?:个|笔|条)?\s*(?:旧挂单|旧订单|old\s+orders?)", source_text, re.IGNORECASE)
    if count_match is None:
        count_match = re.search(r"(\d+)\s*(?:old\s+orders?|挂单|订单)", source_text, re.IGNORECASE)
    expected_count = int(count_match.group(1)) if count_match else None
    if expected_count is not None and expected_count <= 0:
        return None
    return {
        "schema_version": PARK_CODEX_INTENT_SCHEMA,
        "intent": "legacy_clean_slate_cutover",
        "expected_order_count": expected_count,
        "source_text": source_text,
        "explicit_confirmation": "确认" in source_text or "confirm" in lowered,
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
needs_clarification, clarification_fields, interpretation, confidence,
position_action, entry_action, exit_action. For an old-portfolio disposition
message only, use keep/preserve/flatten/close/close_all for position_action,
keep/preserve/cancel/cancel_entries/cancel_orders for entry_action, and
keep/preserve/manage for exit_action. Otherwise return null; never infer an action.

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
