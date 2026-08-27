from __future__ import annotations

import json
from pathlib import Path

from services.park_telegram_runtime import ParkTelegramRouter, ParkTelegramRuntimeError


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


def test_active_strategy_does_not_block_macro_read_only_question_on_provider_fallback(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    router.identity.active_session = lambda: {
        "strategy_session_id": "session-active",
        "strategy_revision_id": "revision-active",
        "plan_digest": "sha256:" + "a" * 64,
    }

    result = router.handle_update(_update(20, "今晚有美联储会议或重要数据要公布吗？"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "query"
    assert result.get("code") != "strategy_locked"


def test_provider_outage_keeps_explicit_strategy_as_unconfirmed_draft(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())

    result = router.handle_update(
        _update(5, "价格跌到4200，我决定做多 DCA，区间4200~4400，最大10倍，止损4190，止盈4800")
    )

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_outage_preserves_parenthetical_dca_exit_fields(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())

    result = router.handle_update(
        _update(
            25,
            "你帮我做一个比特币做空的 Testnet DCA 策略，具体参数如下："
            "1. 杠杆：最高 10 倍；"
            "2. 价格区间：78000 ~ 80000；"
            "3. 止损 (Stop Loss)：81000；"
            "4. 止盈 (Take Profit)：73000",
        )
    )

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    patch = result["conversation"]["strategy_patch"]
    assert patch["direction"] == "short"
    assert patch["strategy_type"] == "dca"
    assert patch["stop_price"] == 81000.0
    assert patch["take_profit_price"] == 73000.0
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_outage_preserves_chinese_number_dca_entry_ladder(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())

    result = router.handle_update(
        _update(
            26,
            "做空 DCA，区间 78000~80000，最大 10 倍杠杆，止损 81000，止盈 73000；"
            "七万八、七万九、八万，一共三笔",
        )
    )

    assert result["status"] == "conversation_replied"
    patch = result["conversation"]["strategy_patch"]
    assert patch["entry_prices"] == [78000.0, 79000.0, 80000.0]
    assert patch["order_count"] == 3
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_outage_finalize_reuses_complete_durable_strategy_patch(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    router.market_reader = lambda: {
        "price": 79000.0,
        "trusted": True,
        "fresh": True,
        "source": "hyperliquid.external_testnet",
        "provider": "hyperliquid",
        "observed_at": "2026-08-19T00:00:00+00:00",
    }
    message = (
        "你帮我做一个比特币做空的 Testnet DCA 策略，具体参数如下："
        "1. 杠杆：最高 10 倍；"
        "2. 价格区间：78000 ~ 80000；"
        "3. 止损 (Stop Loss)：81000；"
        "4. 止盈 (Take Profit)：73000；"
        "七万八、七万九、八万，一共三笔"
    )

    first = router.handle_update(_update(27, message))
    second = router.handle_update(
        _update(28, "finalize 执行这个 Testnet BTC 做空 DCA 策略")
    )

    assert first["status"] == "conversation_replied"
    assert second["status"] == "proposal_created"
    normalized = second["plan"]["normalized_input"]
    assert normalized["direction"] == "short"
    assert normalized["entry_prices"] == [78000.0, 79000.0, 80000.0]
    assert normalized["stop_price"] == 81000.0
    assert normalized["take_profit_price"] == 73000.0
    assert second["proposal"]["execution_environment"] == "testnet"
    assert second["proposal"]["execution_authorized"] is False
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_provider_outage_finalize_surfaces_market_mismatch_after_reusing_draft(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    message = (
        "你帮我做一个比特币做空的 Testnet DCA 策略，具体参数如下："
        "最高 10 倍，价格区间 78000 ~ 80000，止损 81000，止盈 73000；"
        "七万八、七万九、八万，一共三笔"
    )

    first = router.handle_update(_update(29, message))
    second = router.handle_update(_update(30, "finalize 执行这个 Testnet BTC 做空 DCA 策略"))

    assert first["status"] == "conversation_replied"
    assert second["status"] == "blocked"
    assert second["code"] == "current_price_outside_range"
    assert second["code"] not in {
        "missing_direction",
        "missing_price_boundary",
        "dca_exit_levels_missing",
        "addition_count_or_size",
    }
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_provider_outage_replays_revised_dca_geometry_without_raw_market_exception(
    tmp_path: Path,
) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

    def unavailable_market() -> dict:
        raise ParkTelegramRuntimeError("market_unavailable", "TypeError")

    router = _router(tmp_path, UnavailableConversationProvider())
    router.market_reader = unavailable_market
    messages = [
        (
            "你帮我做一个比特币做空的 DCA 策略，具体参数如下："
            "最高10倍，价格区间78000~80000，止损81000，止盈73000；"
            "七万八、七万九、八万，一共三笔。"
        ),
        "现在价格出现变动了 计划有变 80000 到 81000这两个价位做空 "
        "每一次5x aum 然后止损82000 止盈73000",
        "finalize 执行这个 Testnet BTC 做空 DCA 策略",
        "80000 到 81000 就两个价位 做空 dca 82k止损 73k止盈",
        "最大10x杠杆",
    ]

    results = [router.handle_update(_update(40 + index, text)) for index, text in enumerate(messages)]
    final_patch = router.conversation_ledger.latest_strategy_patch()

    assert {
        "finalize_status": results[2]["status"],
        "finalize_code": results[2]["code"],
        "raw_exception_visible": "TypeError" in results[2]["outbound"]["text"],
        "market_guidance_visible": "行情" in results[2]["outbound"]["text"],
        "direction": final_patch.get("direction"),
        "strategy_type": final_patch.get("strategy_type"),
        "lower_price_boundary": final_patch.get("lower_price_boundary"),
        "upper_price_boundary": final_patch.get("upper_price_boundary"),
        "entry_prices": final_patch.get("entry_prices"),
        "order_count": final_patch.get("order_count"),
        "maximum_leverage": final_patch.get("maximum_leverage"),
        "stop_price": final_patch.get("stop_price"),
        "take_profit_price": final_patch.get("take_profit_price"),
    } == {
        "finalize_status": "blocked",
        "finalize_code": "market_unavailable",
        "raw_exception_visible": False,
        "market_guidance_visible": True,
        "direction": "short",
        "strategy_type": "dca",
        "lower_price_boundary": 80000.0,
        "upper_price_boundary": 81000.0,
        "entry_prices": [80000.0, 81000.0],
        "order_count": 2,
        "maximum_leverage": 10.0,
        "stop_price": 82000.0,
        "take_profit_price": 73000.0,
    }
    recent_replies = "\n".join(
        str(row.get("text") or "")
        for row in router.telegram.pending_outbound()
        if row.get("idempotency_key") in {"park-conversation:43", "park-conversation:44"}
    )
    assert not any(term in recent_replies for term in ("缺方向", "缺价格区间", "缺风险上限", "缺止损", "缺止盈"))
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    assert not (tmp_path / "outputs" / "dualtrack").exists()


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


def test_explicit_finalize_uses_deterministic_completeness_when_provider_contradicts_candidate(
    tmp_path: Path,
) -> None:
    class ContradictoryProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {
                "status": "ok",
                "conversation": {
                    "mode": "strategy_forming",
                    "assistant_reply": (
                        "参数已记录；但每笔下单数量或总风险上限尚未提供，"
                        "因此还不能形成可执行的 Paper 计划。"
                    ),
                    "strategy_patch": {
                        "direction": "short",
                        "strategy_type": "dca",
                        "lower_price_boundary": 78000,
                        "upper_price_boundary": 80000,
                        "maximum_leverage": 10,
                        "entry_prices": [78000, 79000, 80000],
                        "order_count": 3,
                        "stop_price": 81000,
                        "take_profit_price": 73000,
                    },
                    "missing_fields": ["position_size_or_total_risk_limit"],
                    "needs_confirmation": False,
                    "explicit_execution_intent": False,
                },
                "metadata": {"provider": "codex_cli", "status": "returned"},
            }

    router = _router(tmp_path, ContradictoryProvider())
    router.market_reader = lambda: {
        "price": 79000.0,
        "trusted": True,
        "fresh": True,
        "source": "hyperliquid.external_testnet",
        "provider": "hyperliquid",
        "observed_at": "2026-08-19T00:00:00+00:00",
    }
    draft = (
        "比特币做空 Testnet DCA，最高10倍杠杆，区间78000~80000，"
        "止损81000，止盈73000；七万八、七万九、八万，一共三笔"
    )

    first = router.handle_update(_update(31, draft))
    second = router.handle_update(_update(32, "finalize 执行这个 Testnet BTC 做空 DCA 策略"))

    assert first["status"] == "conversation_replied"
    assert second["status"] == "proposal_created"
    assert second["plan"]["risk"]["selected_constraint"] == "maximum_leverage"
    assert second["proposal"]["execution_environment"] == "testnet"
    assert second["proposal"]["execution_authorized"] is False
    assert second["provider_mode_overridden"] == "strategy_forming"
    assert "conversation_outbound" not in second
    assert router.telegram.pending_outbound()[-1]["message_type"] == "strategy_proposal"
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_explicit_finalize_keeps_genuinely_incomplete_provider_candidate_in_conversation(
    tmp_path: Path,
) -> None:
    provider = ConversationProvider(
        {
            "mode": "strategy_forming",
            "assistant_reply": "还需要明确策略级止损和止盈。",
            "strategy_patch": {
                "direction": "short",
                "strategy_type": "dca",
                "lower_price_boundary": 78000,
                "upper_price_boundary": 80000,
                "maximum_leverage": 10,
                "entry_prices": [78000, 79000, 80000],
                "order_count": 3,
            },
            "missing_fields": ["stop_price", "take_profit_price"],
            "needs_confirmation": False,
            "explicit_execution_intent": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(33, "finalize 执行这个 Testnet BTC 做空 DCA 策略"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


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
            "对，具体操作如下：4370开一单，4420开一单；止损4444，止盈4200；每单notional 5万美金；确认执行。",
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


def test_research_mode_keeps_evidence_and_grid_draft_without_plan(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "research",
            "assistant_reply": "先比较 Grid 与 DCA 的适用条件，不形成执行计划。",
            "strategy_patch": {
                "strategy_type": "grid",
                "direction": "neutral",
                "lower_price_boundary": 4100,
                "upper_price_boundary": 4450,
                "grid_spacing": 10,
                "entry_prices": [4110, 4120, 4430, 4440],
                "order_count": 4,
            },
            "missing_fields": ["maximum_leverage"],
            "evidence_used": ["用户提供的策略想法", "Paper context"],
            "assumptions": ["当前只做研究，不请求执行"],
            "conflicts": ["Grid Hard Stop 仍需明确授权"],
            "needs_confirmation": False,
            "explicit_execution_intent": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(_update(11, "比较一个中性 Grid 和 DCA"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "research"
    conversation = result["conversation"]
    assert conversation["strategy_patch"]["grid_spacing"] == 10
    assert conversation["strategy_patch"]["entry_prices"] == [4110, 4120, 4430, 4440]
    assert conversation["evidence_used"] == ["用户提供的策略想法", "Paper context"]
    assert conversation["assumptions"] == ["当前只做研究，不请求执行"]
    assert conversation["conflicts"] == ["Grid Hard Stop 仍需明确授权"]
    history = router.conversation_ledger.history()
    assert history[-1]["evidence_used"] == ["用户提供的策略想法", "Paper context"]
    assert history[-1]["assumptions"] == ["当前只做研究，不请求执行"]
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_cannot_finalize_when_user_is_only_discussing(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "ready_for_confirmation",
            "assistant_reply": "参数看起来完整，但先继续讨论。",
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

    result = router.handle_update(_update(12, "这些参数都齐了，但我们先讨论一下，不要执行"))

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_provider_outage_does_not_turn_complete_discussion_into_proposal(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "unavailable"}}

    router = _router(tmp_path, UnavailableConversationProvider())

    result = router.handle_update(
        _update(13, "做多 DCA，区间4200~4400，最大10倍，止损4190，止盈4800，但我还要继续研究")
    )

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
