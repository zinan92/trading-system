from __future__ import annotations

import json
import time
from pathlib import Path

from services.park_ai_chat import (
    DeepSeekIntentProvider,
    ParkAiChatService,
    ParkAiProviderGateway,
    record_strategy_snapshot_terminal,
)
from services.park_ai_chat import _safe_context


MARKET = {
    "price": 4250,
    "trusted": True,
    "fresh": True,
    "source": "paper-test-feed",
    "provider": "paper-test-feed",
    "observed_at": "2026-08-17T03:00:00+00:00",
}


def _account(*, positions=None, orders=None):
    positions = list(positions or [])
    orders = list(orders or [])
    return {
        "equity": 10_000,
        "reconciliation_healthy": True,
        "open_positions": len(positions),
        "open_or_accepted_orders": len(orders),
        "unresolved_runtime": False,
        "pending_terminal_actions": False,
        "snapshot": {"positions": positions, "orders": orders},
    }


def _candidate(**overrides):
    value = {
        "direction": "short",
        "strategy_type": "dca",
        "upper_price_boundary": 4444,
        "lower_price_boundary": 4200,
        "maximum_leverage": 10,
        "maximum_acceptable_loss": None,
        "stop_price": 4444,
        "take_profit_price": 4100,
        "order_count": 6,
        "interpretation": "做空 DCA",
        "confidence": "high",
    }
    value.update(overrides)
    return value


class FakeProvider:
    def __init__(self, candidate=None, *, status="ok"):
        self.candidate = candidate or _candidate()
        self.status = status
        self.calls = []

    def parse(self, text, *, context=None):
        self.calls.append((text, dict(context or {})))
        if self.status != "ok":
            return {"status": self.status, "metadata": {"provider": "fake", "status": self.status}}
        return {
            "status": "ok",
            "candidate": {**self.candidate, "source_text": text},
            "metadata": {"provider": "fake", "status": "returned", "elapsed_ms": 1},
        }


def _service(tmp_path: Path, provider=None, account=None, now=None):
    return ParkAiChatService(
        tmp_path / "outputs",
        park_user_id="park-dashboard",
        provider=provider or FakeProvider(),
        market_reader=lambda: dict(MARKET),
        account_reader=lambda _root, _cycle: dict(account or _account()),
        now=now or (lambda: "2026-08-17T03:00:00+00:00"),
    )


def test_deepseek_provider_extracts_json_without_logging_secret():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(_candidate(), ensure_ascii=False)
                            }
                        }
                    ]
                }
            ).encode()

    captured = {}

    def opener(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        captured["body"] = request.data.decode()
        return Response()

    provider = DeepSeekIntentProvider(
        api_key="secret-never-returned",
        opener=opener,
        timeout_seconds=3,
    )
    result = provider.parse("做空 DCA", context={"market": MARKET})
    assert result["status"] == "ok"
    assert result["candidate"]["direction"] == "short"
    assert captured["authorization"] == "Bearer secret-never-returned"
    assert captured["timeout"] == 3
    assert "secret-never-returned" not in json.dumps(result)


def test_provider_context_redacts_credentials_but_keeps_paper_facts():
    safe = _safe_context(
        {
            "market": {"price": 4250},
            "account": {"equity": 10000, "api_key": "never", "nested": {"password": "never"}},
        }
    )
    assert safe["market"]["price"] == 4250
    assert "api_key" not in safe["account"]
    assert "password" not in safe["account"]["nested"]


def test_provider_context_truncation_never_falls_back_to_unscrubbed_values():
    from services.dashboard_ai_provider import _safe_context

    safe = _safe_context(
        {
            "market": {"blob": "x" * 30_000, "api_key": "never"},
            "strategy": {"token": "never"},
        }
    )
    assert "api_key" not in json.dumps(safe)
    assert "token" not in json.dumps(safe)


def test_provider_gateway_falls_back_to_codex_model(monkeypatch):
    monkeypatch.setenv("PARK_CODEX_CLI", "/cloud/bin/codex")
    class Provider:
        def parse(self, *_args, **_kwargs):
            return {"status": "unavailable", "metadata": {"provider": "deepseek", "status": "timeout"}}

    class Codex:
        model = "gpt-5.6-sol"

        def parse(self, text, **_kwargs):
            return {"status": "ok", "candidate": _candidate(), "metadata": {"provider": "codex_cli", "status": "returned"}}

    gateway = ParkAiProviderGateway(deepseek=Provider(), codex=Codex())
    result = gateway.parse("做空 DCA", context={"market": MARKET})
    assert result["status"] == "ok"
    assert result["metadata"]["provider"] == "codex_cli"
    assert result["metadata"]["fallback_from"] == "deepseek"

    configured = ParkAiProviderGateway(deepseek=Provider(), codex=None)
    assert configured.codex.executable == "/cloud/bin/codex"
    assert configured.codex.model == "gpt-5.6-sol"


def test_provider_explicit_clarification_flag_cannot_become_a_confirmable_plan(tmp_path):
    provider = FakeProvider()
    provider.candidate = {**_candidate(), "needs_clarification": True, "clarification_fields": ["direction"]}
    service = _service(tmp_path, provider=provider)
    result = service.handle_message("做一个策略")
    assert result["status"] == "needs_clarification"
    assert result["code"] == "provider_clarification"
    assert result["confirmable"] is False


def test_clean_slate_draft_requires_explicit_dca_exit_fields(tmp_path):
    provider = FakeProvider(_candidate(stop_price=None, take_profit_price=None))
    service = _service(tmp_path, provider=provider)
    result = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    assert result["status"] == "needs_clarification"
    assert result["code"] == "dca_exit_fields_required"
    assert result["confirmable"] is False
    assert not (tmp_path / "outputs" / "park_strategy" / "strategy_snapshots.jsonl").exists()


def test_neutral_grid_is_a_first_class_confirmable_strategy(tmp_path):
    candidate = _candidate(
        direction="neutral",
        strategy_type="grid",
        upper_price_boundary=4450,
        lower_price_boundary=4100,
        maximum_leverage=20,
        stop_price=None,
        take_profit_price=None,
    )
    service = _service(tmp_path, provider=FakeProvider(candidate))
    result = service.handle_message("中性网格，区间4450~4100，最大20倍杠杆")
    assert result["status"] == "draft"
    assert result["confirmable"] is True
    assert result["draft"]["plan"]["normalized_input"]["direction"] == "neutral"


def test_clarification_keeps_a_short_lived_draft_for_natural_language_follow_up(tmp_path):
    class FollowUpProvider(FakeProvider):
        def parse(self, text, *, context=None):
            candidate = _candidate() if "止损" in text and "止盈" in text else _candidate(stop_price=None, take_profit_price=None)
            return {
                "status": "ok",
                "candidate": {**candidate, "source_text": text},
                "metadata": {"provider": "fake", "status": "returned", "elapsed_ms": 1},
            }

    service = _service(tmp_path, provider=FollowUpProvider())
    first = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    assert first["status"] == "needs_clarification"
    assert first["draft"]["draft_id"]
    assert service.read_model()["pending_draft"]["draft_id"] == first["draft"]["draft_id"]
    second = service.handle_message("止损 4444，止盈 4100")
    assert second["status"] == "draft"
    assert second["confirmable"] is True


def test_confirmed_clean_slate_creates_one_immutable_snapshot(tmp_path):
    service = _service(tmp_path)
    draft = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    assert draft["status"] == "draft"
    assert draft["confirmable"] is True
    confirmed = service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["snapshot"]["status"] == "confirmed"
    assert confirmed["snapshot"]["snapshot_digest"].startswith("sha256:")
    assert confirmed["snapshot"]["integrity"] == "verified"
    rows = (tmp_path / "outputs" / "park_strategy" / "strategy_snapshots.jsonl").read_text().splitlines()
    assert len(rows) == 1
    snapshot = json.loads(rows[0])
    assert snapshot["plan_digest"] == draft["draft"]["plan_digest"]
    again = service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])
    assert again["status"] == "confirmed"
    assert len((tmp_path / "outputs" / "park_strategy" / "strategy_snapshots.jsonl").read_text().splitlines()) == 1
    chat_rows = [
        json.loads(line)
        for line in (tmp_path / "outputs" / "park_strategy" / "dashboard_ai_chat.jsonl").read_text().splitlines()
    ]
    assert any(row["event"] == "assistant_response" and row["status"] == "confirmed" for row in chat_rows)


def test_reject_deletes_draft_and_does_not_create_snapshot(tmp_path):
    service = _service(tmp_path)
    draft = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    rejected = service.reject(draft["draft"]["draft_id"])
    assert rejected["status"] == "rejected"
    assert not (tmp_path / "outputs" / "park_strategy" / "strategy_snapshots.jsonl").exists()
    assert service.read_model()["pending_draft"] is None
    chat_rows = [
        json.loads(line)
        for line in (tmp_path / "outputs" / "park_strategy" / "dashboard_ai_chat.jsonl").read_text().splitlines()
    ]
    assert any(row["event"] == "message_received" and "做空 DCA" in row["message"] for row in chat_rows)
    assert any(row["event"] == "draft_rejected" and row["card_created"] is False for row in chat_rows)
    assert service.reject(draft["draft"]["draft_id"])["idempotent"] is True


def test_expired_draft_is_deleted(tmp_path):
    clock = [1000.0]
    service = _service(tmp_path, now=lambda: "2026-08-17T03:00:00+00:00")
    service._time = lambda: clock[0]
    draft = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    assert draft["status"] == "draft"
    clock[0] += 1801
    expired = service.read_model()
    assert expired["pending_draft"] is None


def test_active_strategy_requires_natural_language_disposition(tmp_path):
    service = _service(
        tmp_path,
        provider=FakeProvider(
            _candidate(
                direction="long",
                strategy_type="grid",
                upper_price_boundary=4500,
                lower_price_boundary=4200,
                maximum_leverage=5,
                stop_price=None,
                take_profit_price=None,
            )
        ),
        account=_account(positions=[{"status": "open", "notional": 1000}]),
    )
    service.identity.start_clean_session(
        observed_at="2026-08-17T03:00:00+00:00",
        plan_digest="sha256:" + "1" * 64,
        reconciliation_healthy=True,
        strategy_session_id="session-old",
        strategy_revision_id="revision-old",
    )
    result = service.handle_message("做多 Grid，区间 4200~4500，最大5倍杠杆")
    assert result["status"] == "needs_disposition"
    assert result["draft"]["requires_disposition"] is True
    disposition = service.handle_message("旧仓继续原来的止盈止损，撤掉未成交挂单")
    assert disposition["status"] == "draft"
    assert disposition["confirmable"] is True
    assert disposition["draft"]["disposition"]["entry_action"] == "cancel"
    assert disposition["draft"]["disposition"]["position_action"] == "keep"


def test_active_strategy_can_use_llm_candidate_for_non_template_disposition(tmp_path):
    class DispositionProvider(FakeProvider):
        def __init__(self):
            super().__init__(_candidate(direction="long", strategy_type="grid", stop_price=None, take_profit_price=None))
            self.calls_count = 0

        def parse(self, text, *, context=None):
            self.calls_count += 1
            result = super().parse(text, context=context)
            if self.calls_count > 1:
                result["candidate"].update({"position_action": "keep", "entry_action": "cancel", "exit_action": "keep"})
            return result

    provider = DispositionProvider()
    service = _service(
        tmp_path,
        provider=provider,
        account=_account(),
    )
    service.identity.start_clean_session(
        observed_at="2026-08-17T03:00:00+00:00",
        plan_digest="sha256:" + "1" * 64,
        reconciliation_healthy=True,
        strategy_session_id="session-old",
        strategy_revision_id="revision-old",
    )
    first = service.handle_message("做多 Grid，区间 4200~4500，最大5倍杠杆")
    assert first["status"] == "needs_disposition"
    second = service.handle_message("继续原有退出管理，但不要再挂旧入口")
    assert second["status"] == "draft"
    assert second["draft"]["disposition"]["entry_action"] == "cancel"


def test_existing_exposure_without_identity_still_requires_disposition_and_stays_fail_closed(tmp_path):
    service = _service(tmp_path, account=_account(positions=[{"status": "open", "notional": 1000}]))
    result = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    assert result["status"] == "needs_disposition"
    assert result["confirmable"] is False
    disposition = service.handle_message("旧仓继续原来的止盈止损，撤掉未成交挂单")
    assert disposition["status"] == "draft"
    confirmed = service.confirm(disposition["draft"]["draft_id"], disposition["draft"]["plan_digest"])
    assert confirmed["status"] == "blocked"
    assert confirmed["code"] == "portfolio_reconciliation_required"


def test_confirmation_records_control_and_plan_facts_without_raw_rejected_cards(tmp_path):
    service = _service(tmp_path)
    draft = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])
    events_path = tmp_path / "outputs" / "park_strategy" / "recording" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    facts = [row for row in events if row.get("event") in {"fact", "late_amendment"}]
    assert {row["category"] for row in facts} >= {"control", "plan"}
    assert all("source_text" not in row.get("payload", {}) for row in facts)


def test_snapshot_terminal_event_seals_card_without_mutating_original_snapshot(tmp_path):
    service = _service(tmp_path)
    draft = service.handle_message("做空 DCA，区间 4444~4200，最大10倍杠杆")
    confirmed = service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])
    event = record_strategy_snapshot_terminal(
        tmp_path / "outputs",
        strategy_session_id=confirmed["snapshot"]["strategy_session_id"],
        strategy_revision_id=confirmed["snapshot"]["strategy_revision_id"],
        plan_digest=confirmed["snapshot"]["plan_digest"],
        reason="take_profit_price",
        observed_at="2026-08-17T04:00:00+00:00",
    )
    assert event["event"] == "strategy_snapshot_sealed"
    model = service.read_model()
    assert model["snapshots"][0]["status"] == "expired"
    assert model["snapshots"][0]["sealed"] is True
    original = json.loads((tmp_path / "outputs" / "park_strategy" / "strategy_snapshots.jsonl").read_text())
    assert original["sealed"] is False
