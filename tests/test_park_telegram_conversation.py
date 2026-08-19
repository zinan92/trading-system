from __future__ import annotations

import json
from pathlib import Path

from services.park_telegram_runtime import ParkTelegramRouter


def _update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": "park-user"},
            "chat": {"id": "park-chat"},
            "text": text,
        },
    }


def _router(tmp_path: Path, provider) -> ParkTelegramRouter:
    return ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=provider,
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "provider": "paper-feed",
            "observed_at": "2026-08-19T00:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
            "snapshot": {"orders": [], "positions": []},
        },
        now=lambda: "2026-08-19T00:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-19_DAY",
    )


class ConversationProvider:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def converse(self, text: str, *, context=None, history=None) -> dict:
        self.calls.append({"text": text, "context": context or {}, "history": history or []})
        return {"status": "ok", "conversation": self.response, "metadata": {"provider": "deepseek", "status": "returned", "elapsed_ms": 2}}


def test_status_and_price_question_is_answered_without_strategy_error(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "query",
            "assistant_reply": "当前没有已确认运行策略，当前 Paper 价格约为 4300。",
            "strategy_patch": {},
            "missing_fields": [],
            "needs_confirmation": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(1, "你好，现在有策略在跑吗？当前价格是多少？"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "query"
    assert "当前没有已确认运行策略" in router.telegram.pending_outbound()[0]["text"]
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    assert provider.calls[0]["context"]["market"]["price"] == 4300.0


def test_strategy_discussion_can_ask_for_missing_fields_without_creating_plan(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "strategy_forming",
            "assistant_reply": "我理解为做多 DCA，区间 4200~4400、最大 10 倍。还需要明确策略级止损和止盈。",
            "strategy_patch": {
                "direction": "long",
                "strategy_type": "dca",
                "lower_price_boundary": 4200,
                "upper_price_boundary": 4400,
                "maximum_leverage": 10,
            },
            "missing_fields": ["stop_price", "take_profit_price"],
            "needs_confirmation": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(2, "我想做多 DCA，4200 到 4400，最大 10 倍"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert "止损和止盈" in router.telegram.pending_outbound()[0]["text"]
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    ledger = tmp_path / "outputs" / "park_strategy" / "telegram_conversation.jsonl"
    assert ledger.exists()
    assert any(json.loads(line)["event"] == "assistant_message" for line in ledger.read_text().splitlines())


def test_complete_strategy_becomes_proposal_only_after_conversation_convergence(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "ready_for_confirmation",
            "assistant_reply": "我已整理好做多 DCA 4200~4400、最大 10 倍、止损 4190、止盈 4800。你确认执行吗？",
            "strategy_patch": {
                "direction": "long",
                "strategy_type": "dca",
                "lower_price_boundary": 4200,
                "upper_price_boundary": 4400,
                "maximum_leverage": 10,
                "stop_price": 4190,
                "take_profit_price": 4800,
            },
            "missing_fields": [],
            "needs_confirmation": True,
            "explicit_execution_intent": True,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(
        _update(3, "我决定执行做多 DCA：4200~4400，最大10倍，止损4190，止盈4800")
    )

    assert result["status"] == "proposal_created"
    assert result["proposal"]["execution_authorized"] is False
    assert result["conversation"]["mode"] == "ready_for_confirmation"
    assert result["conversation_outbound"]["message_type"] == "conversation_summary"
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_off_topic_conversation_is_redirected_without_strategy_parser(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "off_topic",
            "assistant_reply": "我主要和你讨论交易和市场，我们回到交易上吧。",
            "strategy_patch": {},
            "missing_fields": [],
            "needs_confirmation": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(4, "你今天心情怎么样？"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "off_topic"
    assert "交易和市场" in router.telegram.pending_outbound()[0]["text"]
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_outage_does_not_misclassify_explicit_price_based_strategy_as_query(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())

    result = router.handle_update(
        _update(5, "价格跌到4200，我决定做多 DCA，区间4200~4400，最大10倍，止损4190，止盈4800")
    )

    assert result["status"] == "proposal_created"
    assert result["proposal"]["execution_authorized"] is False
def test_ready_mode_without_explicit_execution_intent_stays_in_conversation(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "ready_for_confirmation",
            "assistant_reply": "我整理出了一个候选方案，但先继续讨论也可以。",
            "strategy_patch": {
                "direction": "long",
                "strategy_type": "dca",
                "lower_price_boundary": 4200,
                "upper_price_boundary": 4400,
                "maximum_leverage": 10,
                "stop_price": 4190,
                "take_profit_price": 4800,
            },
            "missing_fields": [],
            "needs_confirmation": True,
            "explicit_execution_intent": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(6, "我们先讨论一下这个方案"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_candidate_patch_is_merged_across_natural_language_turns(tmp_path: Path) -> None:
    class MultiTurnProvider:
        def __init__(self) -> None:
            self.calls = 0

        def converse(self, text: str, **kwargs) -> dict:
            self.calls += 1
            if self.calls == 1:
                conversation = {
                    "mode": "strategy_forming",
                    "assistant_reply": "我理解为做多 DCA 4200~4400、最大10倍，还需要止盈止损。",
                    "strategy_patch": {
                        "direction": "long",
                        "strategy_type": "dca",
                        "lower_price_boundary": 4200,
                        "upper_price_boundary": 4400,
                        "maximum_leverage": 10,
                    },
                    "missing_fields": ["stop_price", "take_profit_price"],
                    "needs_confirmation": False,
                }
            else:
                conversation = {
                    "mode": "ready_for_confirmation",
                    "assistant_reply": "现在策略完整了，是否确认执行？",
                    "strategy_patch": {"stop_price": 4190, "take_profit_price": 4800},
                    "missing_fields": [],
                    "needs_confirmation": True,
                    "explicit_execution_intent": True,
                }
            return {"status": "ok", "conversation": conversation, "metadata": {"provider": "deepseek", "status": "returned"}}

    provider = MultiTurnProvider()
    router = _router(tmp_path, provider)

    first = router.handle_update(_update(7, "我想做多 DCA，区间4200~4400，最大10倍"))
    second = router.handle_update(_update(8, "止损4190，止盈4800，我要执行"))

    assert first["status"] == "conversation_replied"
    assert second["status"] == "proposal_created"
    assert second["plan"]["normalized_input"]["direction"] == "long"
    assert second["plan"]["normalized_input"]["stop_price"] == 4190.0


def test_explicit_dca_fields_are_recovered_when_model_patch_is_incomplete(tmp_path: Path) -> None:
    class IncompletePatchProvider:
        def __init__(self) -> None:
            self.calls = 0

        def converse(self, text: str, **kwargs) -> dict:
            self.calls += 1
            conversation = {
                "mode": "strategy_forming" if self.calls == 1 else "ready_for_confirmation",
                "assistant_reply": "我已经理解了完整的做空 DCA 参数，请确认执行。",
                "strategy_patch": {"direction": "short"} if self.calls == 1 else {},
                "missing_fields": [] if self.calls > 1 else ["stop_loss", "take_profit"],
                "needs_confirmation": self.calls > 1,
                "explicit_execution_intent": self.calls > 1,
            }
            return {"status": "ok", "conversation": conversation, "metadata": {"provider": "deepseek", "status": "returned"}}

    router = _router(tmp_path, IncompletePatchProvider())
    router.market_reader = lambda: {
        "price": 4400.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-feed",
        "observed_at": "2026-08-19T00:00:00+00:00",
    }
    first = router.handle_update(_update(9, "我想让你设置一个做空的DCA，从4370~4420开两单，每一单是5倍杠杆。"))
    second = router.handle_update(
        _update(
            10,
            "对，具体操作如下：4370开一单，4420开一单；止损4444，止盈4200；每单notional 5万美金。",
        )
    )

    assert first["status"] == "conversation_replied"
    assert second["status"] == "proposal_created"
    normalized = second["plan"]["normalized_input"]
    assert normalized["direction"] == "short"
    assert normalized["strategy_type"] == "dca"
    assert normalized["order_count"] == 2
    assert normalized["entry_prices"] == [4370.0, 4420.0]
    assert normalized["stop_price"] == 4444.0
    assert normalized["take_profit_price"] == 4200.0
