from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.park_confirmation import ParkConfirmationLedger
from services.park_telegram_runtime import ParkTelegramRouter, ParkTelegramWorker, default_account_reader
from services.telegram_bot_transport import TelegramBotTransport, TelegramBotTransportError


def _update(update_id: int, text: str, *, user: str = "park-user", chat: str = "park-chat") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
        },
    }


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_account_reader_counts_legacy_exposure_across_all_paper_cycles(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "outputs"
    adapter_root = output

    class Adapter:
        output_root = adapter_root

        def snapshot(self, _cycle_id):
            return {
                "orders": [],
                "positions": [],
                "account": {"equity": 10000.0},
                "reconciliation": {"status": "ok", "issues": []},
            }

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    import services.park_paper_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "build_park_authoritative_adapter", lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()))
    snapshots = output / "dualtrack" / "nautilus_authoritative" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "legacy-cycle.json").write_text(json.dumps([{
        "cycle_id": "legacy-cycle",
        "orders": [{"order_id": "legacy-order", "state": "accepted", "side": "sell", "price": 4300.0, "quantity": 1.0}],
        "positions": [],
    }]), encoding="utf-8")

    account = default_account_reader(output, "2026-08-18_DAY")

    assert account["open_or_accepted_orders"] == 1
    assert account["snapshot"]["account_wide_legacy_exposure"]["ownership"] == "legacy_cycle_or_unknown"


def test_transport_requires_explicit_message_receipt_and_parses_updates() -> None:
    calls: list[tuple[str, int]] = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        if request.full_url.endswith("/getUpdates?timeout=3&limit=100&allowed_updates=%5B%22message%22%5D"):
            return _Response({"ok": True, "result": [{"update_id": 8}]})
        return _Response({"ok": True, "result": {"message_id": 99, "chat": {"id": "chat"}}})

    transport = TelegramBotTransport(token="token-never-logged", chat_id="chat", opener=opener)
    assert transport.get_updates(timeout_seconds=3) == [{"update_id": 8}]
    assert transport.send_message("hello")["message_id"] == "99"
    assert all("token-never-logged" in url for url, _ in calls)

    def missing_receipt(request, timeout):
        return _Response({"ok": True, "result": {}})

    with pytest.raises(TelegramBotTransportError, match="no message_id"):
        TelegramBotTransport(token="token", chat_id="chat", opener=missing_receipt).send_message("hello")


def test_default_account_reader_uses_park_authoritative_adapter_and_external_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}

    class FakeAdapter:
        def snapshot(self, cycle_id: str) -> dict:
            calls["snapshot_cycle"] = cycle_id
            return {
                "account": {"equity": 1234.0},
                "positions": [{"status": "open"}],
                "orders": [{"state": "accepted"}, {"state": "cancelled"}],
            }

        def reconcile(self, cycle_id: str) -> dict:
            calls["reconcile_cycle"] = cycle_id
            return {"status": "ok", "issues": []}

    class FakeBinding:
        adapter = FakeAdapter()

    def build(root: Path, *, config=None):
        calls["root"] = root
        calls["config"] = config
        return FakeBinding()

    monkeypatch.setattr("services.park_paper_runtime.build_park_authoritative_adapter", build)

    result = default_account_reader(
        tmp_path / "outputs",
        "2026-08-15_DAY",
        config={"feature_enabled": True, "runtime_mode": "paper_only"},
    )

    assert calls == {
        "root": tmp_path / "outputs",
        "config": {"feature_enabled": True, "runtime_mode": "paper_only"},
        "snapshot_cycle": "2026-08-15_DAY",
        "reconcile_cycle": "2026-08-15_DAY",
    }
    assert result["equity"] == 1234.0
    assert result["open_positions"] == 1
    assert result["open_or_accepted_orders"] == 1
    assert result["reconciliation_healthy"] is True


def test_router_passes_park_config_to_default_account_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_reader(_root: Path, _cycle_id: str, *, config=None) -> dict:
        seen.append(config)
        return {}

    monkeypatch.setattr("services.park_telegram_runtime.default_account_reader", fake_reader)
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        config={"execution_engine": {"authoritative": "nautilus_paper"}},
    )

    assert router.account_reader(router.output_root, "cycle") == {}
    assert seen == [{"execution_engine": {"authoritative": "nautilus_paper"}}]


def test_router_creates_deterministic_proposal_without_execution_mutation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "observed_at": "2026-08-14T10:00:00+00:00",
    }
    calls: list[str] = []

    def account_reader(root: Path, cycle_id: str) -> dict:
        calls.append(cycle_id)
        return {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        }

    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: market,
        account_reader=account_reader,
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )
    result = router.handle_update(_update(1, "做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200，止损4450，止盈4210"))

    assert result["status"] == "proposal_created"
    plan = result["plan"]
    assert plan["strategy_session_id"].startswith("session-")
    assert plan["strategy_revision_id"].startswith("revision-")
    assert plan["risk"]["effective_leverage"] <= 10
    assert plan["risk"]["theoretical_max_loss"] > 0
    assert calls == ["2026-08-14_DAY"]
    assert result["proposal"]["execution_authorized"] is False
    assert not list(output.glob("dualtrack/**/commands*.json"))
    assert router.telegram.pending_outbound()


def test_router_blocks_dca_without_explicit_strategy_tp_and_sl(tmp_path: Path) -> None:
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper", "observed_at": "2026-08-14T10:00:00+00:00"},
        account_reader=lambda _root, _cycle: {"equity": 1000.0, "reconciliation_healthy": True, "open_positions": 0, "open_or_accepted_orders": 0, "unresolved_runtime": False, "pending_terminal_actions": False},
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )

    result = router.handle_update(_update(26, "short DCA 10x 4444~4200"))

    assert result["status"] == "blocked"
    assert result["code"] == "dca_exit_levels_missing"
    assert router.telegram.pending_outbound()


def test_router_snapshot_exposes_grid_boundary_entry_range_spacing_and_hard_stop(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 3900.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )

    result = router.handle_update(_update(23, "中性网格，区间 3800~4000，间距 10，最大10倍杠杆"))

    assert result["status"] == "proposal_created"
    text = router.telegram.pending_outbound()[0]["text"]
    assert "entry_range=3810.0~3990.0" in text
    assert "spacing=10.0 rungs=19" in text
    assert "grid_hard_stop={'long': 3800.0, 'short': 4000.0}" in text
    assert result["plan"]["risk"]["grid_rung_prices"][:2] == [3810.0, 3820.0]


def test_router_accepts_bounded_confirmation_shortcut_for_current_proposal(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )
    proposal = router.handle_update(_update(10, "short DCA 10x 4444~4200 stop 4444 tp 4200"))

    confirmed = router.handle_update(_update(11, "确认当前计划"))

    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmation_mode"] == "pending_proposal_shortcut"
    assert confirmed["decision"]["plan_digest"] == proposal["proposal"]["plan_digest"]


def test_router_releases_expired_unconfirmed_session_only_on_clean_slate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"
    clock = {"value": 1000.0}
    monkeypatch.setattr("services.park_telegram_runtime.time.time", lambda: clock["value"])
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )
    first = router.handle_update(_update(20, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    clock["value"] = 2000.0

    replacement = router.handle_update(_update(21, "short DCA 10x 4444~4200 stop 4444 tp 4200"))

    assert first["status"] == "proposal_created"
    assert replacement["status"] == "proposal_created"
    assert replacement["proposal"]["plan_digest"] != first["proposal"]["plan_digest"]
    identity_rows = [
        json.loads(line)
        for line in (output / "park_strategy" / "identity.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(row.get("event") == "session_closed" and row.get("reason") == "confirmation_expired" for row in identity_rows)
    assert any(row.get("message_type") == "confirmation_expired" for row in router.telegram.pending_outbound())


def test_confirmation_is_exact_idempotent_and_still_zero_execution(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )
    proposal = router.handle_update(_update(2, "short DCA 10x 4444~4200 stop 4444 tp 4200"))["proposal"]
    command = f"confirm {proposal['plan_digest']}"
    first = router.handle_update(_update(3, command))
    duplicate = router.handle_update(_update(3, command))

    assert first == duplicate
    assert first["status"] == "confirmed"
    confirmation = ParkConfirmationLedger(output, park_user_id="park-user").rows()
    assert [row for row in confirmation if row.get("event") == "confirmed"]
    assert not (output / "dualtrack" / "nautilus_paper").exists()


def test_worker_persists_cursor_and_replays_duplicate_without_new_result(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )

    class FakeTransport:
        def __init__(self) -> None:
            self.updates = [[_update(20, "short DCA 10x 4444~4200 stop 4444 tp 4200")], [_update(20, "short DCA 10x 4444~4200 stop 4444 tp 4200")]]
            self.sent = 0

        def get_updates(self, **_kwargs):
            return self.updates.pop(0)

        def send_message(self, text: str, *, chat_id: str):
            self.sent += 1
            return {"ok": True, "message_id": str(self.sent)}

    transport = FakeTransport()
    first = ParkTelegramWorker(router, timeout_seconds=3).run_once(transport)
    second = ParkTelegramWorker(router, timeout_seconds=3).run_once(transport)

    assert first["next_offset"] == 21
    assert second["next_offset"] == 21
    assert second["updates_handled"][0]["status"] == "proposal_created"
    assert transport.sent == 1


def test_duplicate_update_id_with_changed_content_is_blocked_and_audited(tmp_path: Path) -> None:
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_reader=lambda _root, _cycle: {
            "equity": 1000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda now: "2026-08-14_DAY",
    )
    first = router.handle_update(_update(21, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    conflict = router.handle_update(_update(21, "short Grid 10x 4444~4200"))

    assert first["status"] == "proposal_created"
    assert conflict == {
        "status": "blocked",
        "code": "duplicate_update_conflict",
        "next_action": "notify_park_and_wait",
    }
    rejected = [row for row in router.telegram.inbox_rows() if row.get("event") == "inbound_rejected"]
    assert rejected and rejected[-1]["code"] == "duplicate_update_conflict"


def test_unauthorized_update_is_persisted_and_never_notified_to_attacker(tmp_path: Path) -> None:
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {},
        account_reader=lambda _root, _cycle: {},
    )
    result = router.handle_update(_update(30, "short DCA 10x 4444~4200 stop 4444 tp 4200", user="intruder"))
    assert result["event"] == "inbound_rejected"
    assert result["code"] == "unauthorized_user"
    assert router.telegram.pending_outbound() == []
