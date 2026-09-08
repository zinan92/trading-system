from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.park_confirmation import ParkConfirmationLedger
from services.park_telegram_runtime import (
    ParkTelegramRouter,
    ParkTelegramWorker,
    default_account_reader,
    mark_paper_account_to_market,
)
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
    assert account["snapshot"]["account_wide_legacy_exposure"]["orders"][0]["cycle_id"] == "legacy-cycle"


def test_account_reader_uses_active_park_session_equity_over_recording_window_starting_cash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"

    class Adapter:
        output_root = output

        def snapshot(self, _cycle_id):
            return {
                "orders": [],
                "positions": [],
                "account": {"starting_cash": 10000.0, "equity": 10000.0},
            }

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    identity = output / "park_strategy" / "identity.jsonl"
    identity.parent.mkdir(parents=True)
    identity.write_text(
        json.dumps(
            {
                "event": "session_started",
                "strategy_session_id": "session-active",
                "strategy_revision_id": "revision-active",
                "plan_digest": "sha256:" + "a" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    snapshots = output / "dualtrack" / "nautilus_authoritative" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "park-session-active.json").write_text(
        json.dumps(
            {
                "cycle_id": "park-session-active",
                "account": {
                    "starting_cash": 10000.0,
                    "ending_cash": 9444.93,
                    "equity": 9434.93,
                    "realized_pnl": -555.07,
                    "fees": 10.0,
                    "funding": 0.0,
                },
                "orders": [],
                "positions": [],
            }
        ),
        encoding="utf-8",
    )

    result = default_account_reader(output, "2026-08-20_DAY", config={"feature_enabled": True})

    assert result["equity"] == 9434.93
    assert result["snapshot"]["account"]["equity"] == 9434.93


def test_account_reader_uses_latest_authoritative_snapshot_after_session_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"

    class Adapter:
        output_root = output

        def snapshot(self, _cycle_id):
            return {"account": {"starting_cash": 10000.0, "equity": 10000.0}, "orders": [], "positions": []}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    snapshots = output / "dualtrack" / "nautilus_authoritative" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "park-session-closed.json").write_text(
        json.dumps(
            {
                "cycle_id": "park-session-closed",
                "account": {"starting_cash": 10000.0, "ending_cash": 9444.93, "equity": 9434.93},
                "orders": [],
                "positions": [],
            }
        ),
        encoding="utf-8",
    )

    result = default_account_reader(output, "2026-08-20_DAY", config={"feature_enabled": True})

    assert result["equity"] == 9434.93


def test_mark_paper_account_to_market_reprices_long_and_short_without_mutating_history() -> None:
    account = {"ending_cash": 10000.0, "equity": 9900.0}
    positions = [
        {"status": "open", "side": "long", "entry_price": 100.0, "remaining_units": 2.0},
        {"status": "open", "side": "short", "entry_price": 110.0, "remaining_units": 3.0},
    ]

    projected, marked = mark_paper_account_to_market(
        account,
        positions,
        {
            "price": 105.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-20T02:00:00+00:00",
        },
    )

    assert projected["equity"] == 10025.0
    assert projected["unrealized_pnl"] == 25.0
    assert projected["historical_snapshot_equity"] == 9900.0
    assert projected["nav_status"] == "marked_to_market"
    assert projected["nav_is_current"] is True
    assert marked[0]["unrealized_pnl"] == 10.0
    assert marked[1]["unrealized_pnl"] == 15.0
    assert account == {"ending_cash": 10000.0, "equity": 9900.0}


def test_mark_paper_account_to_market_blocks_untrusted_mark_for_open_positions() -> None:
    projected, marked = mark_paper_account_to_market(
        {"ending_cash": 10000.0, "equity": 9500.0},
        [{"status": "open", "side": "short", "entry_price": 100.0, "remaining_units": 1.0}],
        {"price": 120.0, "trusted": False, "fresh": True, "source": "synthetic"},
    )

    assert projected["equity"] is None
    assert projected["nav_is_current"] is False
    assert projected["nav_status"] == "blocked"
    assert projected["nav_blocker"] == "market_not_authoritative"
    assert marked[0].get("unrealized_pnl") is None


def test_account_reader_reprices_active_park_session_against_current_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"

    class Adapter:
        output_root = output

        def snapshot(self, _cycle_id):
            return {
                "orders": [],
                "positions": [],
                "account": {"starting_cash": 10000.0, "ending_cash": 9989.99598479, "equity": 9434.93258479},
            }

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    identity = output / "park_strategy" / "identity.jsonl"
    identity.parent.mkdir(parents=True)
    identity.write_text(
        json.dumps({"event": "session_started", "strategy_session_id": "session-active"}) + "\n",
        encoding="utf-8",
    )
    snapshots = output / "dualtrack" / "nautilus_authoritative" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "park-session-active.json").write_text(
        json.dumps(
            {
                "cycle_id": "park-session-active",
                "account": {
                    "starting_cash": 10000.0,
                    "ending_cash": 9989.99598479,
                    "equity": 9434.93258479,
                },
                "orders": [],
                "positions": [
                    {"position_id": "p1", "status": "open", "side": "short", "entry_price": 4371.62, "remaining_units": 5.721},
                    {"position_id": "p2", "status": "open", "side": "short", "entry_price": 4420.0, "remaining_units": 5.656},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = default_account_reader(
        output,
        "2026-08-20_DAY",
        config={"feature_enabled": True},
        market={
            "price": 4503.87,
            "trusted": True,
            "fresh": True,
            "source": "binance_usdm_futures",
            "observed_at": "2026-08-20T02:13:00+00:00",
        },
    )

    assert result["equity"] == 8759.02501479
    assert result["unrealized_pnl"] == -1230.97097
    assert result["nav_status"] == "marked_to_market"
    assert result["mark"]["price"] == 4503.87
    assert result["positions"][0]["unrealized_pnl"] == -756.60225
    assert result["positions"][1]["unrealized_pnl"] == -474.36872


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
    assert result["equity"] is None
    assert result["nav_status"] == "blocked"
    assert result["nav_blocker"] == "current_market_required"
    assert result["open_positions"] == 1
    assert result["open_or_accepted_orders"] == 1
    assert result["reconciliation_healthy"] is True


@pytest.mark.parametrize(
    ("runtime", "expected_unresolved"),
    (
        (
            {
                "actual_state": "running",
                "stale_cycle": True,
                "previous_runtime_unresolved": False,
                "last_control_event": {"runtime_after": {"actual_state": "stopped", "desired_state": "stopped"}},
            },
            False,
        ),
        ({"actual_state": "running", "stale_cycle": False, "previous_runtime_unresolved": False}, True),
    ),
)
def test_account_reader_distinguishes_proven_stale_legacy_record_from_unresolved_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: dict, expected_unresolved: bool
) -> None:
    class Adapter:
        output_root = tmp_path

        def snapshot(self, _cycle_id):
            return {"account": {"equity": 1000}, "orders": [], "positions": []}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    monkeypatch.setattr(
        "services.strategy_control_plane.StrategyControlPlane.runtime_state",
        lambda _self, _cycle_id: runtime,
    )

    result = default_account_reader(tmp_path, "2026-08-18_DAY")

    assert result["unresolved_runtime"] is expected_unresolved
    assert result["legacy_runtime_stale_record"] is (not expected_unresolved)


def test_account_reader_releases_proven_completed_legacy_cutover_from_runtime_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter:
        output_root = tmp_path

        def snapshot(self, _cycle_id):
            return {"account": {"equity": 1000}, "orders": [], "positions": []}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    monkeypatch.setattr(
        "services.strategy_control_plane.StrategyControlPlane.runtime_state",
        lambda _self, _cycle_id: {
            "actual_state": "running",
            "desired_state": "running",
            "previous_runtime_unresolved": False,
        },
    )
    path = tmp_path / "park_strategy" / "legacy_cutover.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "event": "completed",
                "clean_slate_verified": True,
                "enabled_park_paper": True,
                "legacy_runner_quarantine": {
                    "ok": True,
                    "park_control": True,
                    "legacy_cycle_runner": False,
                    "ownership": {"ok": True, "owner_id": "cloud-primary"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = default_account_reader(tmp_path, "2026-08-19_DAY")

    assert result["unresolved_runtime"] is False
    assert result["legacy_cutover_completed"] is True


def test_account_reader_completed_clean_slate_overrides_historical_unresolved_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter:
        output_root = tmp_path

        def snapshot(self, _cycle_id):
            return {"account": {"equity": 1000}, "orders": [], "positions": []}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(adapter=Adapter()),
    )
    monkeypatch.setattr(
        "services.strategy_control_plane.StrategyControlPlane.runtime_state",
        lambda _self, _cycle_id: {
            "actual_state": "stopped",
            "desired_state": "stopped",
            "stale_cycle": True,
            "previous_runtime_unresolved": True,
        },
    )
    path = tmp_path / "park_strategy" / "legacy_cutover.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "event": "completed",
                "clean_slate_verified": True,
                "enabled_park_paper": True,
                "legacy_runner_quarantine": {
                    "ok": True,
                    "park_control": True,
                    "legacy_cycle_runner": False,
                    "ownership": {"ok": True, "owner_id": "cloud-primary"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = default_account_reader(tmp_path, "2026-08-19_DAY")

    assert result["unresolved_runtime"] is False
    assert result["legacy_cutover_completed"] is True


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
    result = router._handle_strategy(
        "做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200，止损4450，止盈4210",
        active=None,
        update_id=1,
    )

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


def test_telegram_strategy_entry_is_disabled_before_draft_or_provider(tmp_path: Path) -> None:
    calls: list[str] = []

    class UnexpectedParser:
        def parse(self, _text: str):
            calls.append("parser")
            raise AssertionError("Telegram entry guard must run before parsing")

    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        intent_parser=UnexpectedParser(),
        market_reader=lambda: calls.append("market") or {},
        account_reader=lambda *_args: calls.append("account") or {},
    )

    result = router.handle_update(_update(100, "做多网格，区间 4200~4400，确认执行"))

    assert result["status"] == "blocked"
    assert result["code"] == "entry_disabled"
    assert result["receipt"]["code"] == "entry_disabled"
    assert "dashboard-v5.html" in result["message"]
    assert calls == []
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()
    assert not (tmp_path / "outputs" / "park_strategy" / "telegram_continuations.jsonl").exists()
    assert any(row.get("message_type") == "entry_disabled" for row in router.telegram.pending_outbound())


def test_telegram_read_only_question_remains_available(tmp_path: Path) -> None:
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: {"price": 4300.0, "trusted": True, "fresh": True},
        account_reader=lambda *_args: {
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "nav_is_current": False,
            "status": "ok",
        },
    )

    result = router.handle_update(_update(101, "当前策略状态是什么？"))

    assert result["status"] == "conversation_replied"
    assert result["execution_authorized"] is False
    assert not (tmp_path / "outputs" / "park_strategy" / "plans.jsonl").exists()


def test_router_understands_screenshot_style_short_dca_without_json_rephrasing(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 79000.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "observed_at": "2026-08-14T10:00:00+00:00",
    }
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: market,
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

    result = router._handle_strategy(
            "你帮我做一个比特币做空的 Testnet DCA 策略，具体参数如下："
            "1. 杠杆：最高 10 倍；"
            "2. 价格区间：78000 ~ 80000；"
            "3. 止损 (Stop Loss)：81000；"
            "4. 止盈 (Take Profit)：73000",
            active=None,
            update_id=2,
        )

    assert result["status"] == "proposal_created"
    normalized = result["plan"]["normalized_input"]
    assert normalized["direction"] == "short"
    assert normalized["strategy_type"] == "dca"
    assert normalized["stop_price"] == 81000.0
    assert normalized["take_profit_price"] == 73000.0
    assert result["proposal"]["execution_environment"] == "testnet"


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

    result = router._handle_strategy("short DCA 10x 4444~4200", active=None, update_id=26)

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

    result = router._handle_strategy("中性网格，区间 3800~4000，间距 10，最大10倍杠杆", active=None, update_id=23)

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
    proposal = router._handle_strategy("short DCA 10x 4444~4200 stop 4444 tp 4200", active=None, update_id=10)

    confirmed = router._handle_confirmation("确认当前计划", active=router.identity.active_session(), update_id=11)

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
    first = router._handle_strategy("short DCA 10x 4444~4200 stop 4444 tp 4200", active=None, update_id=20)
    clock["value"] = 2000.0

    replacement = router._handle_strategy("short DCA 10x 4444~4200 stop 4444 tp 4200", active=router.identity.active_session(), update_id=21)

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
    proposal = router._handle_strategy("short DCA 10x 4444~4200 stop 4444 tp 4200", active=None, update_id=2)["proposal"]
    command = f"confirm {proposal['plan_digest']}"
    first = router._handle_confirmation(command, active=router.identity.active_session(), update_id=3)
    duplicate = router._handle_confirmation(command, active=router.identity.active_session(), update_id=3)

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
    assert second["updates_handled"][0]["status"] == "blocked"
    assert transport.sent == 1


def test_worker_keeps_durable_recovery_when_telegram_poll_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router = ParkTelegramRouter(
        tmp_path / "outputs",
        park_user_id="park-user",
        chat_id="park-chat",
    )
    monkeypatch.setattr(
        router,
        "recover_pending_legacy_cutovers",
        lambda: [{"status": "legacy_cutover_confirmed"}],
    )

    class UnavailableTransport:
        def get_updates(self, **_kwargs):
            raise TelegramBotTransportError("transport_unavailable", "timeout")

        def send_message(self, text: str, *, chat_id: str):
            raise TelegramBotTransportError("transport_unavailable", "timeout")

    result = ParkTelegramWorker(router, timeout_seconds=3).run_once(UnavailableTransport())

    assert result["status"] == "blocked"
    assert result["code"] == "transport_unavailable"
    assert result["updates_handled"] == [{"status": "legacy_cutover_confirmed"}]
    assert result["next_action"] == "retry_telegram_poll"


@pytest.mark.xfail(strict=True, reason="Telegram strategy recovery is disabled by issue #1148")
def test_worker_recovers_explicit_dca_after_provider_type_misclassification(tmp_path: Path) -> None:
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
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )
    update = _update(31, "现在行情是震荡向上 做4200 4400的做多dca吧，然后最大10倍杠杆")
    router.telegram.ingest_update(update)
    router._remember_result(
        31,
        {"status": "blocked", "code": "ambiguous_strategy_type"},
        update_digest="sha256:previous",
    )

    recovered = router.recover_pending_strategy_inputs()

    assert recovered[0]["status"] == "blocked"
    assert recovered[0]["code"] == "dca_exit_levels_missing"
    pending = router.telegram.pending_outbound()
    assert any("止损" in str(row.get("text") or "") for row in pending)
    assert any("strategy-recovery" in str(row.get("idempotency_key") or "") for row in pending)
    assert not (output / "park_strategy" / "identity.jsonl").exists()
    assert not (output / "park_strategy" / "plans.jsonl").exists()


def test_dca_followup_merges_stop_and_take_profit_into_pending_intent(tmp_path: Path) -> None:
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
            "provider": "paper-provider",
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

    first = router._handle_strategy("做多 4200~4400 的 DCA，然后最大10倍杠杆。", active=None, update_id=40)
    completed = router._handle_strategy("止损4190 止盈4800", active=router.identity.active_session(), update_id=41)

    assert first["code"] == "dca_exit_levels_missing"
    assert completed["status"] == "proposal_created"
    assert completed["plan"]["normalized_input"]["direction"] == "long"
    assert completed["plan"]["normalized_input"]["strategy_type"] == "dca"
    assert completed["plan"]["normalized_input"]["stop_price"] == 4190.0
    assert completed["plan"]["normalized_input"]["take_profit_price"] == 4800.0
    assert not (output / "dualtrack").exists()


@pytest.mark.xfail(strict=True, reason="Telegram strategy recovery is disabled by issue #1148")
def test_recovery_merges_already_consumed_dca_and_exit_followup(tmp_path: Path) -> None:
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
            "provider": "paper-provider",
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
    first = _update(50, "做多 4200~4400 的 DCA，然后最大10倍杠杆。")
    second = _update(51, "止损4190 止盈4800")
    router.telegram.ingest_update(first)
    router.telegram.ingest_update(second)
    router._remember_result(50, {"status": "blocked", "code": "dca_exit_levels_missing"}, update_digest="sha256:first")
    router._remember_result(51, {"status": "blocked", "code": "missing_direction"}, update_digest="sha256:second")

    recovered = router.recover_pending_strategy_inputs()

    assert recovered[0]["status"] == "blocked"
    assert recovered[0]["code"] == "dca_exit_levels_missing"
    assert recovered[1]["status"] == "proposal_created"
    assert recovered[1]["plan"]["normalized_input"]["stop_price"] == 4190.0


@pytest.mark.xfail(strict=True, reason="Telegram strategy recovery is disabled by issue #1148")
def test_worker_refreshes_stale_missing_risk_guidance_once(tmp_path: Path) -> None:
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
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )
    update = _update(32, "现在行情是震荡向上 做4200 4400的做多dca吧")
    router.telegram.ingest_update(update)
    router.telegram.queue_outbound(
        idempotency_key="park-strategy-rejected:32",
        message_type="park_blocker",
        text="Park strategy not accepted: strategy type is ambiguous",
    )
    router._remember_result(
        32,
        {"status": "blocked", "code": "missing_risk_authority"},
        update_digest="sha256:previous",
    )

    first = router.recover_pending_strategy_inputs()
    second = router.recover_pending_strategy_inputs()

    assert first[0]["code"] == "missing_risk_authority"
    assert second == []
    assert any(
        row.get("idempotency_key") == "park-strategy-rejected:32:strategy-recovery"
        and "风险上限" in str(row.get("text") or "")
        for row in router.telegram.outbox_rows()
    )


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

    assert first["code"] == "entry_disabled"
    assert conflict == {
        "status": "blocked",
        "code": "duplicate_update_conflict",
        "next_action": "notify_park_and_wait",
    }
    rejected = [row for row in router.telegram.inbox_rows() if row.get("event") == "inbound_rejected"]
    assert rejected and rejected[-1]["code"] == "entry_disabled"


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
