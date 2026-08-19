from __future__ import annotations

import json
import subprocess
import sys

from services.dashboard_ai_provider import DeepSeekIntentProvider
from services.park_ai_provider_gateway import ParkAiProviderGateway
from services.park_codex_intent_parser import CodexCliConversationParser


def _conversation_payload() -> dict:
    return {
        "mode": "query",
        "assistant_reply": "当前没有已确认策略，当前价格约为 4300。",
        "strategy_patch": {},
        "missing_fields": [],
        "needs_confirmation": False,
        "explicit_execution_intent": False,
        "confidence": "high",
    }


def test_deepseek_conversation_prompt_contains_history_and_bounded_context() -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps({"choices": [{"message": {"content": json.dumps(_conversation_payload(), ensure_ascii=False)}}]}).encode()

    def opener(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    provider = DeepSeekIntentProvider(api_key="secret-never-returned", opener=opener, timeout_seconds=4)
    result = provider.converse(
        "当前价格是多少？",
        history=[{"role": "user", "content": "我在考虑做多"}],
        context={"market": {"price": 4300}, "account": {"api_key": "never"}},
    )

    assert result["status"] == "ok"
    body = captured["body"]
    assert captured["timeout"] == 4
    assert "Trading Conversation Agent" in body["messages"][0]["content"]
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload["history"][0]["content"] == "我在考虑做多"
    assert "api_key" not in json.dumps(user_payload)


def test_conversation_gateway_falls_back_to_codex_after_deepseek_timeout() -> None:
    class DeepSeek:
        def converse(self, text, **kwargs):
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout", "elapsed_ms": 12000}}

    class Codex:
        def converse(self, text, **kwargs):
            return {"status": "ok", "conversation": _conversation_payload(), "metadata": {"provider": "codex_cli", "status": "returned"}}

    result = ParkAiProviderGateway(deepseek=DeepSeek(), codex=Codex()).converse("当前价格是多少？")

    assert result["status"] == "ok"
    assert result["conversation"]["mode"] == "query"
    assert result["metadata"]["fallback_from"] == "deepseek"
    assert result["metadata"]["fallback_status"] == "timeout"


def test_codex_conversation_parser_returns_bounded_json_result() -> None:
    def runner(*_args, **_kwargs):
        event = {
            "item": {
                "type": "agent_message",
                "text": json.dumps(_conversation_payload(), ensure_ascii=False),
            }
        }
        return subprocess.CompletedProcess(_args[0], 0, stdout=json.dumps(event) + "\n", stderr="")

    parser = CodexCliConversationParser(executable=sys.executable, runner=runner)
    result = parser.converse("当前价格是多少？", history=[{"role": "user", "content": "我在讨论行情"}])

    assert result["status"] == "ok"
    assert result["conversation"]["mode"] == "query"
    assert result["metadata"]["provider"] == "codex_cli"
