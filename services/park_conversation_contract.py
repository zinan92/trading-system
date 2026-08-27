"""Provider-neutral contract for the bounded Park Trading Conversation Agent."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from services.natural_language_numbers import (
    extract_explicit_entry_count,
    extract_explicit_entry_ladder,
)


PARK_CONVERSATION_SCHEMA = "park-trading-conversation-v1"
CONVERSATION_MODES = frozenset(
    {"query", "research", "discuss", "strategy_forming", "ready_for_confirmation", "off_topic"}
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
    "entry_prices",
    "grid_spacing",
    "local_stop_authorized",
)
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_MESSAGE_CHARS = 2_000
MAX_ASSISTANT_REPLY_CHARS = 2_000
MAX_ANALYSIS_ITEMS = 8
MAX_ANALYSIS_ITEM_CHARS = 240


TRADING_AGENT_SYSTEM_PROMPT = """You are Park's bounded Trading Expert.

Your roles are: research partner, trading-strategy discussion partner, strategy
specification finalizer, and Paper execution assistant. You may converse
naturally. Do not force convergence, do not turn a new idea into an execution
candidate merely because some parameters are present, and do not force a form
or strategy route at the beginning. Research and discussion should stay open
until Park explicitly asks to form or finalize an execution strategy. If Park
discusses something unrelated to trading, politely say that you stay focused on
trading and guide the conversation back.

Use only the supplied Paper context for current price, strategy, orders,
positions, and account facts. Never invent a price, position, strategy,
direction, leverage, loss limit, TP, SL, or disposition. If a fact is missing,
say it is unavailable.

Detect the conversation state:
- query: Park asks for current/read-only facts such as price, running strategy,
  orders, positions, or PnL.
- research: Park asks to investigate, compare, or challenge a market or
  strategy idea. Discuss evidence, assumptions, conflicts, and falsifiers; do
  not create an execution candidate unless Park is explicitly forming one.
- discuss: Park is exploring trading ideas or market context without asking to
  execute a new strategy.
- strategy_forming: Park appears to be forming an execution decision; preserve
  explicit fields, ask natural-language follow-ups for missing fields, and do
  not claim a plan is ready.
- ready_for_confirmation: Park has explicitly described a complete candidate
  strategy and explicitly asked to finalize/execute it. Summarize it and ask
  whether Park confirms execution. Complete fields alone are not enough.
- off_topic: the message is outside trading/finance.

For DCA, preserve direction, entry prices/range, addition count or size,
maximum leverage or loss, and explicit strategy-level stop loss and take profit.
Do not infer exits from a range. For Grid, preserve direction or neutral mode,
authorized Boundaries, Entry Range, spacing, rung prices/count, sizing/risk,
and Hard Stop semantics only when explicitly stated or safely left for the
deterministic planner. Do not confuse a Grid Order Exit with a strategy
terminal and do not invent direction or authorization.

Return exactly one JSON object and no prose outside it with these keys:
schema_version, mode, assistant_reply, strategy_patch, missing_fields,
evidence_used, assumptions, conflicts, needs_confirmation,
explicit_execution_intent, confidence.
`strategy_patch` must copy every explicit Park field (including strategy_type,
order_count, and an `entry_prices` list when Park names exact entry levels),
not only the direction. Model output is untrusted; it never authorizes,
submits, cancels, flattens, stops, reverses, or changes a strategy. The
deterministic Paper safety layer will validate any candidate after this
conversation step.
"""


class ParkConversationContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def extract_explicit_strategy_patch(text: str) -> dict[str, Any]:
    """Extract only unambiguous user-stated fields for candidate repair."""

    source = str(text or "")
    lowered = source.lower()
    number = r"([0-9]+(?:\.[0-9]+)?)"
    patch: dict[str, Any] = {}
    if "做空" in source or "short" in lowered:
        patch["direction"] = "short"
    elif "做多" in source or "long" in lowered:
        patch["direction"] = "long"
    elif "中性" in source or "neutral" in lowered:
        patch["direction"] = "neutral"
    if re.search(r"(?<![a-z0-9])dca(?![a-z0-9])|趋势", lowered):
        patch["strategy_type"] = "dca"
    elif re.search(r"(?<![a-z0-9])grid(?![a-z0-9])|网格|震荡", lowered):
        patch["strategy_type"] = "grid"
    range_match = re.search(number + r"\s*(?:~|～|-|到|至)\s*" + number, source)
    if range_match:
        first, second = float(range_match.group(1)), float(range_match.group(2))
        patch["upper_price_boundary"] = max(first, second)
        patch["lower_price_boundary"] = min(first, second)
    leverage_match = re.search(number + r"\s*(?:倍\s*杠杆|倍|x)(?:\s*杠杆|\s*leverage)?", source, re.IGNORECASE)
    if leverage_match:
        patch["maximum_leverage"] = float(leverage_match.group(1))
    loss_match = re.search(number + r"\s*(?:最大可接受亏损|最大亏损|max(?:imum)?\s*loss)", source, re.IGNORECASE)
    if loss_match:
        patch["maximum_acceptable_loss"] = float(loss_match.group(1))
    explicit_ladder = extract_explicit_entry_ladder(source)
    if explicit_ladder is not None:
        patch["entry_prices"], patch["order_count"] = explicit_ladder
    else:
        explicit_count = extract_explicit_entry_count(source)
        if explicit_count is not None:
            patch["order_count"] = explicit_count
    parenthetical = r"(?:\s*[\(（][^\)）]*[\)）])?"
    stop_match = re.search(
        r"(?:止损|stop(?:[_\s]+(?:loss|price))?)"
        + parenthetical
        + r"\s*(?:位|价|price)?\s*[:：=]?\s*"
        + number,
        source,
        re.IGNORECASE,
    )
    if stop_match:
        patch["stop_price"] = float(stop_match.group(1))
    take_match = re.search(
        r"(?:止盈|take(?:[_\s]+profit)?(?:[_\s]+price)?|tp)"
        + parenthetical
        + r"\s*(?:位|价|price)?\s*[:：=]?\s*"
        + number,
        source,
        re.IGNORECASE,
    )
    if take_match:
        patch["take_profit_price"] = float(take_match.group(1))
    entry_prices = [
        float(match.group(1))
        for match in re.finditer(
            number + r"\s*(?:开|开仓)\s*(?:一|1)?\s*(?:单|手|笔)",
            source,
        )
    ]
    if entry_prices:
        entry_prices = list(dict.fromkeys(entry_prices))
        patch["entry_prices"] = entry_prices
        patch["order_count"] = len(entry_prices)
    else:
        count_match = re.search(r"(?:开|开仓|共|总共)\s*(\d+|一|两|二|三|四|五)\s*单", source)
        if count_match:
            patch["order_count"] = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}.get(
                count_match.group(1), int(count_match.group(1)) if count_match.group(1).isdigit() else 1
            )
    return patch


def build_conversation_user_payload(
    text: str,
    *,
    history: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    def redact_history(value: Any) -> str:
        text = str(value or "")[:MAX_HISTORY_MESSAGE_CHARS]
        text = re.sub(
            r"(?i)(api[_ -]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+",
            r"\1=<REDACTED>",
            text,
        )
        return re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "<REDACTED>", text)

    bounded_history = []
    for item in list(history or [])[-MAX_HISTORY_MESSAGES:]:
        history_item: dict[str, Any] = {
            "role": str(item.get("role") or "user"),
            "content": redact_history(item.get("content")),
        }
        if isinstance(item.get("strategy_patch"), Mapping):
            history_item["strategy_patch"] = {
                str(key): value
                for key, value in item["strategy_patch"].items()
                if str(key) in STRATEGY_PATCH_FIELDS
            }
            history_item["missing_fields"] = [str(value) for value in (item.get("missing_fields") or [])[:12]]
            for field in ("evidence_used", "assumptions", "conflicts"):
                values = item.get(field)
                if isinstance(values, list):
                    history_item[field] = [str(value)[:MAX_ANALYSIS_ITEM_CHARS] for value in values[:MAX_ANALYSIS_ITEMS]]
        bounded_history.append(history_item)
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
    def bounded_items(field: str) -> list[str]:
        raw = value.get(field) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ParkConversationContractError("conversation_analysis_invalid", f"{field} must be a string list")
        return [str(item)[:MAX_ANALYSIS_ITEM_CHARS] for item in raw[:MAX_ANALYSIS_ITEMS]]

    confidence = value.get("confidence")
    confidence = confidence if confidence in {"high", "medium", "low"} else "low"
    explicit_execution_intent = bool(value.get("explicit_execution_intent")) and has_explicit_execution_intent(source_text)
    ready = mode == "ready_for_confirmation" and explicit_execution_intent
    if mode == "ready_for_confirmation" and not ready:
        mode = "strategy_forming"
    return {
        "schema_version": PARK_CONVERSATION_SCHEMA,
        "mode": mode,
        "assistant_reply": reply[:MAX_ASSISTANT_REPLY_CHARS],
        "strategy_patch": patch,
        "missing_fields": [str(item)[:120] for item in missing_value[:12]],
        "evidence_used": bounded_items("evidence_used"),
        "assumptions": bounded_items("assumptions"),
        "conflicts": bounded_items("conflicts"),
        "needs_confirmation": ready,
        "explicit_execution_intent": explicit_execution_intent,
        "confidence": confidence,
        "source_text": str(source_text or "")[:MAX_HISTORY_MESSAGE_CHARS],
    }


def has_explicit_execution_intent(text: str) -> bool:
    """Require a positive, non-question finalize/execute phrase from Park."""

    source = str(text or "").strip()
    if not source:
        return False
    if re.search(
        r"(?:不要|不要再|先不|暂不|暂时不|不必|不需要|继续讨论|先讨论|还要讨论|不要锁定).{0,20}(?:执行|确认|下单|finalize|execute|proceed)",
        source,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"(?:执行|确认|下单|finalize|execute|proceed).{0,12}(?:吗|么|是否|要不要|\?|？)", source, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"(?:确认执行|确认(?:当前|这个|该)?(?:计划|策略)|(?:我|现在|直接|请)?(?:要|决定)?执行(?:当前|这个|该)?(?:计划|策略)?|(?:我|现在|直接)?下单|\b(?:finalize|execute|go\s+ahead|proceed)\b)",
            source,
            re.IGNORECASE,
        )
    )
