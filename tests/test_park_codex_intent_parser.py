from __future__ import annotations

import json
import subprocess
from pathlib import Path

from services.park_codex_intent_parser import CodexCliIntentParser, deterministic_neutral_grid_candidate
from services.park_telegram_runtime import ParkTelegramRouter


_MARKET = {
    "price": 4300.0,
    "trusted": True,
    "fresh": True,
    "source": "test-feed",
    "provider": "test-provider",
    "observed_at": "2026-08-15T02:30:00+00:00",
}


def _account(_root: Path, _cycle_id: str) -> dict:
    return {
        "equity": 1000.0,
        "reconciliation_healthy": True,
        "open_positions": 0,
        "open_or_accepted_orders": 0,
        "unresolved_runtime": False,
        "pending_terminal_actions": False,
    }


class _Completed:
    returncode = 0
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "schema_version": "park-codex-intent-v1",
                        "direction": "neutral",
                        "strategy_type": "grid",
                        "upper_price_boundary": 4450,
                        "lower_price_boundary": 4100,
                        "maximum_leverage": 20,
                        "maximum_acceptable_loss": None,
                        "stop_price": None,
                        "take_profit_price": None,
                        "order_count": None,
                        "needs_clarification": False,
                        "clarification_fields": [],
                        "interpretation": "中性网格，区间 4100-4450，最大杠杆 20x",
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                ),
            },
        },
        ensure_ascii=False,
    )
    stderr = ""


def test_codex_parser_extracts_candidate_without_authority(tmp_path: Path) -> None:
    calls: list[dict] = []

    def runner(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return _Completed()

    parser = CodexCliIntentParser(
        executable="/bin/sh",
        cwd=tmp_path,
        codex_home=tmp_path / "codex-home",
        runner=runner,
    )
    result = parser.parse("中性网格策略 4450 4100 最大20x杠杆")

    assert result["status"] == "ok"
    candidate = result["candidate"]
    assert candidate["direction"] == "neutral"
    assert candidate["strategy_type"] == "grid"
    assert candidate["upper_price_boundary"] == 4450.0
    assert candidate["lower_price_boundary"] == 4100.0
    assert candidate["maximum_leverage"] == 20.0
    assert calls[0]["command"][0:5] == ["/bin/sh", "exec", "--model", "gpt-5.6-luna", "--ephemeral"]
    assert calls[0]["timeout"] == 30.0
    assert "Telegram" not in calls[0]["input"]


def test_deterministic_neutral_grid_fallback_accepts_space_separated_boundaries() -> None:
    result = deterministic_neutral_grid_candidate("中性网格策略 4450 4100 最大20x杠杆")

    assert result is not None
    assert result["direction"] == "neutral"
    assert result["strategy_type"] == "grid"
    assert result["upper_price_boundary"] == 4450.0
    assert result["lower_price_boundary"] == 4100.0
    assert result["maximum_leverage"] == 20.0


def test_deterministic_neutral_grid_fallback_does_not_guess_other_intents() -> None:
    assert deterministic_neutral_grid_candidate("做空 DCA，区间 4444~4200，最大10倍杠杆") is None
    assert deterministic_neutral_grid_candidate("中性网格，区间 4450~4100") is None


def test_codex_parser_timeout_is_bounded_and_redacted() -> None:
    def runner(_command, **_kwargs):
        raise subprocess.TimeoutExpired("codex", 12, stderr=b"provider secret")

    parser = CodexCliIntentParser(executable="/bin/sh", runner=runner)
    result = parser.parse("做空 DCA")

    assert result["status"] == "unavailable"
    assert result["metadata"]["status"] == "timeout"
    assert result["metadata"]["timed_out"] is True
    assert "provider secret" not in json.dumps(result)


def test_router_gives_natural_guidance_for_neutral_grid(tmp_path: Path) -> None:
    class FakeParser:
        def parse(self, _text):
            return {
                "status": "ok",
                "candidate": {
                    "direction": "neutral",
                    "strategy_type": "grid",
                    "upper_price_boundary": 4450,
                    "lower_price_boundary": 4100,
                    "maximum_leverage": 20,
                },
                "metadata": {"provider": "codex_cli", "status": "returned", "elapsed_ms": 8},
            }

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(_MARKET),
        account_reader=_account,
        intent_parser=FakeParser(),
    )
    result = router.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "from": {"id": "park-user"},
                "chat": {"id": "park-chat"},
                "text": "中性网格策略 4450 4100 最大20x杠杆",
            },
        }
    )

    assert result["status"] == "proposal_created"
    assert result["plan"]["normalized_input"]["direction"] == "neutral"
    assert result["plan"]["risk"]["order_count"] == 30
    outbound = router.telegram.pending_outbound()[0]["text"]
    assert "direction=neutral type=grid" in outbound
    assert "neutral_legs=buy" in outbound
    assert "Reply exactly: confirm" in outbound
    assert not list((tmp_path / "outputs" / "park_strategy").glob("executions.jsonl"))
    provider_rows = (tmp_path / "outputs" / "park_strategy" / "provider_calls.jsonl").read_text().splitlines()
    assert json.loads(provider_rows[-1])["provider"] == "codex_cli"


def test_router_uses_safe_neutral_grid_fallback_after_provider_timeout(tmp_path: Path) -> None:
    class TimeoutParser:
        def parse(self, _text):
            return {
                "status": "unavailable",
                "metadata": {
                    "provider": "codex_cli",
                    "status": "timeout",
                    "timed_out": True,
                    "elapsed_ms": 15016,
                },
            }

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(_MARKET),
        account_reader=_account,
        intent_parser=TimeoutParser(),
    )
    result = router.handle_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 6,
                "from": {"id": "park-user"},
                "chat": {"id": "park-chat"},
                "text": "中性网格策略 4450 4100 最大20x杠杆",
            },
        }
    )

    assert result["status"] == "proposal_created"
    assert result["plan"]["normalized_input"]["direction"] == "neutral"
    assert "没读清楚你的方向" not in router.telegram.pending_outbound()[0]["text"]
    assert result["provider"]["fallback"] == "deterministic_neutral_grid"
    assert list((tmp_path / "outputs" / "park_strategy").glob("plans.jsonl"))


def test_router_help_is_local_and_does_not_call_provider(tmp_path: Path) -> None:
    class ExplodingParser:
        def parse(self, _text):
            raise AssertionError("help must not call Codex")

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=ExplodingParser(),
    )
    result = router.handle_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 4,
                "from": {"id": "park-user"},
                "chat": {"id": "park-chat"},
                "text": "/start",
            },
        }
    )

    assert result["code"] == "strategy_input_help"
    assert "中性网格" in router.telegram.pending_outbound()[0]["text"]
