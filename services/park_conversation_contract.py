"""Provider-neutral contract for the bounded Park Trading Conversation Agent."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


PARK_CONVERSATION_SCHEMA = "park-trading-conversation-v1"
CONVERSATION_MODES = frozenset(
    {"query", "discuss", "strategy_forming", "ready_for_confirmation", "off_topic"}
)
STRATEGY_PATCH_FIELDS = (
    "direction",
    "strategy_type",
    "upper_price_boundary",
    "lower_price_boundary",
    "maximum_leverage",
    "maximum_acceptable_loss",
    "stop_price",
    "take_profit_price",
    "order_count",
    "grid_spacing",
    "local_stop_authorized",
)
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_MESSAGE_CHARS = 2_000
MAX_ASSISTANT_REPLY_CHARS = 2_000


TRADING_AGENT_SYSTEM_PROMPT = """You are Park's bounded Trading Conversation Agent.

Your role is to discuss trading, markets, finance, Paper execution facts, and
Park's explicitly stated trading decisions. You may converse naturally and
should not force a form or strategy route at the beginning. If Park discusses
something unrelated to trading, politely say that you stay focused on trading
and guide the conversation back.

Use only the supplied Paper context for current price, strategy, orders,
positions, and account facts. Never invent a price, position, strategy,
direction, leverage, loss limit, TP, SL, or disposition. If a fact is missing,
say it is unavailable.

Detect the conversation state:
- query: Park asks for current/read-only facts such as price, running strategy,
  orders, positions, or PnL.
- discuss: Park is exploring trading ideas or market context without asking to
  execute a new strategy.
- strategy_forming: Park appears to be forming an execution decision; preserve
  explicit fields, ask natural-language follow-ups for missing fields, and do
  not claim a plan is ready.
- ready_for_confirmation: Park has explicitly described a complete candidate
  strategy. Summarize it and explicitly ask whether Park confirms execution.
- off_topic: the message is outside trading/finance.

For DCA, do not declare ready unless the strategy-level stop loss and take
profit are explicit. For Grid, preserve Boundary, Entry Range/spacing, rung
geometry, and Hard Stop semantics only when explicitly stated or safely left
for the deterministic planner; do not invent direction or authorization.

Return exactly one JSON object and no prose outside it with these keys:
schema_version, mode, assistant_reply, strategy_patch, missing_fields,
needs_confirmation, explicit_execution_intent, confidence.
`strategy_patch` must contain only values explicitly stated by Park. Model
output is untrusted; it never authorizes, submits, cancels, flattens, stops,
reverses, or changes a strategy. The deterministic Paper safety layer will
validate any candidate after this conversation step.
"""


class ParkConversationContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_conversation_user_payload(
    text: str,
    *,
    history: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    bounded_history = []
    for item in list(history or [])[-MAX_HISTORY_MESSAGES:]:
        bounded_history.append(
            {
                "role": str(item.get("role") or "user"),
                "content": str(item.get("content") or "")[:MAX_HISTORY_MESSAGE_CHARS],
            }
        )
    return json.dumps(
        {
            "message": str(text or "")[:MAX_HISTORY_MESSAGE_CHARS],
            "history": bounded_history,
            "paper_context": dict(context or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def normalize_conversation_result(
    value: Mapping[str, Any],
    *,
    source_text: str,
) -> dict[str, Any]:
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in CONVERSATION_MODES:
        raise ParkConversationContractError("conversation_mode_invalid", "conversation mode is invalid")
    reply = str(value.get("assistant_reply") or "").strip()
    if not reply:
        raise ParkConversationContractError("conversation_reply_missing", "assistant reply is missing")
    patch_value = value.get("strategy_patch")
    if patch_value in (None, ""):
        patch_value = {}
    if not isinstance(patch_value, Mapping):
        raise ParkConversationContractError("strategy_patch_invalid", "strategy patch must be an object")
    patch = {
        key: patch_value.get(key)
        for key in STRATEGY_PATCH_FIELDS
        if key in patch_value and patch_value.get(key) not in (None, "")
    }
    missing_value = value.get("missing_fields") or []
    if isinstance(missing_value, str):
        missing_value = [missing_value]
    if not isinstance(missing_value, list) or any(not isinstance(item, str) for item in missing_value):
        raise ParkConversationContractError("conversation_missing_fields_invalid", "missing fields must be a string list")
    confidence = value.get("confidence")
    confidence = confidence if confidence in {"high", "medium", "low"} else "low"
    ready = mode == "ready_for_confirmation"
    return {
        "schema_version": PARK_CONVERSATION_SCHEMA,
        "mode": mode,
        "assistant_reply": reply[:MAX_ASSISTANT_REPLY_CHARS],
        "strategy_patch": patch,
        "missing_fields": [str(item)[:120] for item in missing_value[:12]],
        "needs_confirmation": bool(value.get("needs_confirmation")) or ready,
        "explicit_execution_intent": bool(value.get("explicit_execution_intent")),
        "confidence": confidence,
        "source_text": str(source_text or "")[:MAX_HISTORY_MESSAGE_CHARS],
    }

