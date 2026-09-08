from __future__ import annotations

from pathlib import Path

import pytest

from services.park_ai_provider_gateway import ParkAiProviderGateway
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


def test_deepseek_is_called_before_codex_and_short_circuits_on_success() -> None:
    calls: list[str] = []

    class DeepSeek:
        def parse(self, text: str, *, context=None) -> dict:
            calls.append("deepseek")
            return {
                "status": "ok",
                "candidate": {"direction": "long", "strategy_type": "dca"},
                "metadata": {"provider": "deepseek", "status": "returned"},
            }

    class Codex:
        def parse(self, text: str, **kwargs) -> dict:
            calls.append("codex")
            return {
                "status": "ok",
                "candidate": {"direction": "short", "strategy_type": "grid"},
                "metadata": {"provider": "codex_cli", "status": "returned"},
            }

    result = ParkAiProviderGateway(deepseek=DeepSeek(), codex=Codex()).parse(
        "做多 DCA",
        context={"market": {"price": 4300}},
    )

    assert result["status"] == "ok"
    assert result["candidate"]["direction"] == "long"
    assert calls == ["deepseek"]


def test_codex_is_called_only_after_deepseek_failure_and_records_fallback() -> None:
    calls: list[str] = []

    class DeepSeek:
        def parse(self, text: str, *, context=None) -> dict:
            calls.append("deepseek")
            return {
                "status": "unavailable",
                "metadata": {
                    "provider": "deepseek",
                    "status": "timeout",
                    "timed_out": True,
                    "elapsed_ms": 12000,
                },
            }

    class Codex:
        def parse(self, text: str, **kwargs) -> dict:
            calls.append("codex")
            return {
                "status": "ok",
                "candidate": {"direction": "long", "strategy_type": "dca"},
                "metadata": {"provider": "codex_cli", "status": "returned"},
            }

    result = ParkAiProviderGateway(deepseek=DeepSeek(), codex=Codex()).parse(
        "做多 DCA",
        context={"market": {"price": 4300}},
    )

    assert result["status"] == "ok"
    assert calls == ["deepseek", "codex"]
    assert result["metadata"]["fallback_from"] == "deepseek"
    assert result["metadata"]["fallback_status"] == "timeout"
    assert result["metadata"]["fallback_elapsed_ms"] == 12000


@pytest.mark.xfail(strict=True, reason="Telegram strategy entry is disabled by issue #1148")
def test_telegram_router_uses_gateway_candidate_but_keeps_confirmation_gate(tmp_path: Path) -> None:
    class DeepSeek:
        def parse(self, text: str, *, context=None) -> dict:
            return {
                "status": "ok",
                "candidate": {
                    "direction": "long",
                    "strategy_type": "dca",
                    "upper_price_boundary": 4400,
                    "lower_price_boundary": 4200,
                    "maximum_leverage": 10,
                    "stop_price": 4190,
                    "take_profit_price": 4800,
                },
                "metadata": {"provider": "deepseek", "status": "returned", "elapsed_ms": 3},
            }

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=ParkAiProviderGateway(deepseek=DeepSeek(), codex=None),
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
        },
        now=lambda: "2026-08-19T00:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-19_DAY",
    )
    router.conversation_agent = None

    result = router.handle_update(_update(801, "这次做多 DCA，4200~4400，最大10倍，止损4190，止盈4800，确认执行"))

    assert result["status"] == "proposal_created"
    assert result["proposal"]["execution_authorized"] is False
    assert result["provider"]["provider"] == "deepseek"
    assert not (tmp_path / "outputs" / "dualtrack").exists()


def test_gateway_honors_cloud_codex_override(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_CODEX_CLI", "/usr/local/bin/codex")
    gateway = ParkAiProviderGateway(
        deepseek=object(),
        codex=None,
    )

    assert gateway.codex.executable == "/usr/local/bin/codex"
    assert gateway.codex.model == "gpt-5.6-sol"


@pytest.mark.xfail(strict=True, reason="Telegram strategy entry is disabled by issue #1148")
def test_both_provider_failures_leave_only_deterministic_neutral_grid_fallback(tmp_path: Path) -> None:
    class Unavailable:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def parse(self, text: str, **kwargs) -> dict:
            return {
                "status": "unavailable",
                "metadata": {"provider": self.provider, "status": "timeout"},
            }

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=ParkAiProviderGateway(
            deepseek=Unavailable("deepseek"),
            codex=Unavailable("codex_cli"),
        ),
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
        },
        now=lambda: "2026-08-19T00:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-19_DAY",
    )
    router.conversation_agent = None

    result = router.handle_update(_update(802, "中性网格 4450 4100 最大20倍杠杆，确认执行"))

    assert result["status"] == "proposal_created"
    assert result["plan"]["normalized_input"]["direction"] == "neutral"
    assert result["proposal"]["execution_authorized"] is False
