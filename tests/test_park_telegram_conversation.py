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


def test_provider_timeout_returns_semantic_dca_risk_preview_instead_of_form_prompt(
    tmp_path: Path,
) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    result = router.handle_update(
        _update(
            45,
            "80000 到 81000这两个价位做空 DCA，每一次5x AUM，止损82000，止盈73000；你看看你理解么，还缺什么参数吗",
        )
    )

    assert result["status"] == "conversation_replied"
    assert result["mode"] == "strategy_forming"
    conversation = result["conversation"]
    assert conversation["strategy_patch"]["entry_notional_aum_multiple"] == 5.0
    assert conversation["risk_preview"]["status"] == "derived"
    assert conversation["derived_fields"]["total_entry_exposure_aum"] == 10.0
    assert round(conversation["derived_fields"]["gross_stop_loss_pct_aum"], 2) == 18.67
    assert "18.67% AUM" in conversation["assistant_reply"]
    assert "还缺" not in conversation["assistant_reply"]
    assert router.conversation_ledger.history()[-1]["derived_fields"]["total_entry_exposure_aum"] == 10.0
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_provider_success_uses_same_local_risk_preview_as_timeout_fallback(tmp_path: Path) -> None:
    provider = ConversationProvider(
        {
            "mode": "strategy_forming",
            "assistant_reply": "我还需要每笔仓位或总风险上限。",
            "strategy_patch": {
                "direction": "short",
                "strategy_type": "dca",
                "lower_price_boundary": 80000,
                "upper_price_boundary": 81000,
                "entry_prices": [80000, 81000],
                "order_count": 2,
                "entry_notional_aum_multiple": 5,
                "stop_price": 82000,
                "take_profit_price": 73000,
            },
            "missing_fields": ["position_size_or_total_risk_limit"],
            "needs_confirmation": False,
            "explicit_execution_intent": False,
        }
    )
    router = _router(tmp_path, provider)

    result = router.handle_update(
        _update(46, "80000 到 81000这两个价位做空 DCA，每一次5x AUM，止损82000，止盈73000；你理解吗")
    )

    assert result["status"] == "conversation_replied"
    assert result["conversation"]["risk_preview"]["status"] == "derived"
    assert "18.67% AUM" in result["conversation"]["assistant_reply"]
    assert "每笔仓位" not in result["conversation"]["assistant_reply"]
    assert provider.calls == []
    assert router.conversation_ledger.history()[-1]["risk_preview"]["status"] == "derived"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_strategy_discussion_with_open_position_does_not_enter_clean_slate_gate(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    router.account_reader = lambda _root, _cycle: {
        "equity": 1000.0,
        "reconciliation_healthy": True,
        "open_positions": 1,
        "open_or_accepted_orders": 0,
        "unresolved_runtime": False,
        "pending_terminal_actions": False,
        "snapshot": {"orders": [], "positions": [{"status": "open"}]},
    }

    result = router.handle_update(
        _update(47, "80000 到 81000这两个价位做空 DCA，每一次5x AUM，止损82000，止盈73000；你看看你理解么")
    )

    assert result["status"] == "conversation_replied"
    assert result.get("code") is None
    assert result["conversation"]["risk_preview"]["status"] == "derived"
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_testnet_conversation_uses_public_testnet_market_without_paper_fallback(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

    calls: list[str] = []

    def paper_market() -> dict:
        calls.append("paper")
        raise AssertionError("Testnet conversation must not read Paper market")

    def testnet_market() -> dict:
        calls.append("testnet")
        return {
            "price": 79665.5,
            "mid": 79665.5,
            "bid": 79660.0,
            "ask": 79670.0,
            "trusted": True,
            "fresh": True,
            "source": "hyperliquid.external_testnet",
            "provider": "hyperliquid",
            "environment": "testnet",
            "instrument_id": "BTC-USD-PERP",
            "observed_at": "2026-08-19T00:00:00+00:00",
        }

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=UnavailableConversationProvider(),
        market_reader=paper_market,
        testnet_market_reader=testnet_market,
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

    result = router.handle_update(
        _update(50, "Testnet BTC 做空 DCA，80000 到 81000 两个价位，每次5x AUM，止损82000，止盈73000；你理解吗")
    )

    assert result["status"] == "conversation_replied"
    assert calls == ["testnet"]
    assert result["conversation"]["risk_preview"]["status"] == "derived"


def test_provider_timeout_returns_deterministic_grid_loss_preview(tmp_path: Path) -> None:
    class UnavailableConversationProvider:
        def converse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

        def parse(self, text: str, **kwargs) -> dict:
            return {"status": "unavailable", "metadata": {"provider": "codex_cli", "status": "timeout"}}

    router = _router(tmp_path, UnavailableConversationProvider())
    result = router.handle_update(
        _update(49, "中性 Grid，区间4100~4500，最大20x杠杆，一共10笔；你看看最大亏损")
    )

    assert result["status"] == "conversation_replied"
    conversation = result["conversation"]
    assert conversation["strategy_patch"]["strategy_type"] == "grid"
    assert conversation["risk_preview"]["status"] == "derived"
    assert conversation["derived_fields"]["total_entry_exposure_aum"] == 20.0
    assert conversation["derived_fields"]["gross_hard_stop_loss_pct_aum"] > 0
    assert "Grid" in conversation["assistant_reply"]
    assert "最大亏损" in conversation["assistant_reply"]
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


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
