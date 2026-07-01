import json
from pathlib import Path

from services.paper_executor import PaperExecutor
from services.journal_store import JournalStore, load_json, write_json
from services.order_lifecycle import OrderLifecycleStore


def _seed_paper_fixture(root: Path, run_date: str) -> str:
    ticket_id = "ticket_gold_fixture"
    write_json(
        root / "trade_tickets" / f"{run_date}.json",
        [
            {
                "ticket_id": ticket_id,
                "signal_id": "sig_gold_fixture",
                "signal_regime": "trend_following",
                "signal_strength": 72,
                "signal_confidence": 66,
                "factor_scores": {"trend": 80, "macro": 55, "event": 70, "volatility": 50},
                "backtest": {"verdict": "supportive", "sample_size": 42},
                "source_artifacts": ["clean_bars/2026-06-25/GOLD_5m.json"],
                "asset": "GOLD",
                "asset_class": "commodity",
                "action": "prepare_buy",
                "entry_zone": "2790-2830",
                "stop_loss": 2750,
                "targets": [2920],
                "position_size_pct": 8,
                "max_loss_pct": 0.5,
                "order_type": "limit",
                "time_in_force": "day",
                "paper_only": True,
            }
        ],
    )
    write_json(
        root / "journal_pending" / f"{run_date}.json",
        [
            {
                "journal_id": "journal_fixture",
                "ticket_id": ticket_id,
                "signal_id": "sig_gold_fixture",
                "asset": "GOLD",
                "decision_status": "pending_manual_decision",
                "created_at": "2026-06-25T00:00:00+00:00",
                "required_user_action": "review",
                "notes": "",
            }
        ],
    )
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [
            {
                "symbol": "GOLD",
                "timeframe": "5m",
                "timestamp": "mock-5m-30",
                "open": 2800,
                "high": 2820,
                "low": 2790,
                "close": 2810,
                "volume": 1000,
                "provider": "mock",
                "quality_flags": ["mock"],
            }
        ],
    )
    write_json(
        root / "data_source_preflight" / f"{run_date}.json",
        [{"status": "warn", "ready_for_paper": True, "ready_for_live": False, "latest_provider": "broker_csv"}],
    )
    return ticket_id


def test_executed_paper_creates_order_and_position(tmp_path: Path):
    run_date = "2026-06-25"
    root = tmp_path / "outputs"
    ticket_id = _seed_paper_fixture(root, run_date)

    record = JournalStore(output_root=root).record_decision(
        run_date=run_date,
        ticket_id=ticket_id,
        decision="executed_paper",
        notes="paper test",
    )

    orders = load_json(root / "paper_orders" / f"{run_date}.json")
    assert record["decision_status"] == "executed_paper"
    assert record["paper_order"]["status"] in {"filled", "pending"}
    assert record["paper_order"]["total_cost"] > 0
    assert record["paper_order"]["fill_price"] > record["paper_order"]["requested_price"]
    assert any(item["ticket_id"] == ticket_id for item in orders)
    assert (root / "paper_positions" / "current.json").exists()
    trade = load_json(root / "paper_trades" / "current.json")[0]
    assert trade["status"] == "open"
    assert trade["signal_id"] == "sig_gold_fixture"
    assert trade["signal_regime"] == "trend_following"
    assert trade["signal_strength"] == 72
    assert trade["factor_scores"]["trend"] == 80
    assert trade["backtest_verdict"] == "supportive"
    assert trade["source_artifacts"] == ["clean_bars/2026-06-25/GOLD_5m.json"]


def test_paper_green_path_records_lifecycle_until_closed(tmp_path: Path):
    run_date = "2026-06-25"
    root = tmp_path / "outputs"
    ticket_id = _seed_paper_fixture(root, run_date)
    ticket = load_json(root / "trade_tickets" / f"{run_date}.json")[0]
    executor = PaperExecutor(root)

    order = executor.execute_ticket(run_date, ticket, latest_price=2810)
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-25T10:00:00+00:00", "high": 2930, "low": 2800, "close": 2925}],
    )
    closed = executor.evaluate_exits(run_date)

    lifecycle = load_json(root / "order_lifecycle" / f"{run_date}.json")[0]
    states = [item["to"] for item in lifecycle["transitions"]]
    assert order.status == "filled"
    assert closed[0]["exit_reason"] == "target"
    assert states == ["entry", "submitting", "accepted", "filled", "protective_attached", "closed"]
    assert round(lifecycle["filled_quantity"], 6) == order.quantity
    assert round(lifecycle["protective_quantity"], 6) == order.quantity
    assert load_json(root / "paper_trades" / "current.json") == []


def test_external_close_updates_local_mirror_even_when_lifecycle_transition_is_illegal(tmp_path: Path):
    run_date = "2026-06-25"
    root = tmp_path / "outputs"
    order_id = "order_already_reconciled"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-25T10:00:00+00:00", "high": 101, "low": 99, "close": 100}],
    )
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "trade_order_already_reconciled",
                "order_id": order_id,
                "ticket_id": "ticket_reconciled",
                "symbol": "GOLD",
                "side": "long",
                "status": "open",
                "quantity": 0.002,
                "entry_price": 100.0,
                "entry_total_cost": 0.0,
                "quality_flags": ["exchange_managed"],
            }
        ],
    )
    store = OrderLifecycleStore(root)
    store.write_intent(run_date, order_id=order_id, ticket_id="ticket_reconciled", idempotency_key=order_id, requested_quantity=0.002, requested_price=100.0, source="test")
    for state in ["submitting", "accepted", "filled", "protective_attached", "closed", "reconciled"]:
        store.transition(run_date, order_id, state, reason=f"test_{state}")

    result = PaperExecutor(root).record_external_close(
        run_date,
        order_id=order_id,
        exit_price=101.0,
        quantity=0.002,
        exit_reason="exchange_close_after_reconciled",
        close_order_id="close_1",
    )

    assert result["closed"] is True
    assert result["lifecycle_transition"]["status"] == "skipped"
    assert load_json(root / "paper_trades" / "current.json") == []
    closed = load_json(root / "paper_trades" / "closed" / f"{run_date}.json")[0]
    assert closed["exchange_close_order_id"] == "close_1"
    lifecycle = load_json(root / "order_lifecycle" / f"{run_date}.json")[0]
    assert lifecycle["state"] == "reconciled"


def test_skip_does_not_create_paper_order(tmp_path: Path):
    run_date = "2026-06-26"
    root = tmp_path / "outputs"
    ticket_id = _seed_paper_fixture(root, run_date)

    JournalStore(output_root=root).record_decision(run_date, ticket_id, "skipped", "skip test")

    orders = load_json(root / "paper_orders" / f"{run_date}.json")
    assert all(item["ticket_id"] != ticket_id for item in orders)


def test_latest_clean_close_reads_5m_by_default(tmp_path: Path):
    run_date = "2026-06-27"
    root = tmp_path / "outputs"
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"close": 4567.89}],
    )

    assert PaperExecutor(root).latest_clean_close(run_date, "GOLD") == 4567.89


def test_latest_clean_close_falls_back_to_present_timeframe(tmp_path: Path):
    """A 1m chan namespace has no GOLD_5m.json — the executor must still find the
    GOLD price from the 1m series so mark-to-market / exits aren't a silent no-op."""
    run_date = "2026-06-27"
    root = tmp_path / "outputs"
    write_json(root / "clean_bars" / run_date / "GOLD_1m.json", [{"close": 4480.5, "high": 4481, "low": 4479, "timestamp": "2026-06-27T00:01:00+00:00"}])

    executor = PaperExecutor(root)
    assert executor.latest_clean_close(run_date, "GOLD") == 4480.5  # default ask is 5m; falls back to the 1m series
    assert executor._latest_clean_bar(run_date, "GOLD")["close"] == 4480.5


def test_latest_clean_close_prefers_requested_timeframe_when_present(tmp_path: Path):
    """When both series exist, the requested timeframe wins — the 5m account is
    byte-identical and never silently reads a different series."""
    run_date = "2026-06-27"
    root = tmp_path / "outputs"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4600.0}])
    write_json(root / "clean_bars" / run_date / "GOLD_1m.json", [{"close": 4480.5}])

    assert PaperExecutor(root).latest_clean_close(run_date, "GOLD") == 4600.0


def test_mark_to_market_updates_unrealized_pnl(tmp_path: Path):
    run_date = "2026-06-28"
    root = tmp_path / "outputs"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4620.0}])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text(
        '{\n  "GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 4600, "unrealized_pnl": 0, "realized_pnl": 0, "risk_used_pct": 0.5}\n}\n',
        encoding="utf-8",
    )

    updated = PaperExecutor(root).mark_to_market(run_date)

    assert updated["GOLD"]["last_price"] == 4620.0
    assert updated["GOLD"]["unrealized_pnl"] == 40.0


def test_mark_to_market_blocks_when_data_quality_blocks(tmp_path: Path):
    run_date = "2026-06-28"
    root = tmp_path / "outputs"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 4620.0}])
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": False, "reasons": ["latest bar is synthetic"]}})
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text(
        '{\n  "GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 4600, "unrealized_pnl": 0, "realized_pnl": 0, "risk_used_pct": 0.5}\n}\n',
        encoding="utf-8",
    )

    updated = PaperExecutor(root).mark_to_market(run_date)

    assert "last_price" not in updated["GOLD"]
    assert updated["GOLD"]["unrealized_pnl"] == 0
    blocks = load_json(root / "paper_execution_blocks" / f"{run_date}.json")
    assert blocks[0]["operation"] == "mark_to_market"
    assert blocks[0]["source"] == "data_quality"


def test_evaluate_exits_closes_trade_on_target(tmp_path: Path):
    run_date = "2026-06-29"
    root = tmp_path / "outputs"
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text(
        '{\n  "GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 4600, "unrealized_pnl": 0, "realized_pnl": 0, "risk_used_pct": 0.5}\n}\n',
        encoding="utf-8",
    )
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "trade_target",
                "order_id": "paper_target",
                "ticket_id": "ticket_target",
                "signal_id": "sig_target",
                "signal_regime": "trend_following",
                "signal_strength": 71,
                "signal_confidence": 64,
                "factor_scores": {"trend": 75},
                "backtest_verdict": "supportive",
                "symbol": "GOLD",
                "side": "long",
                "status": "open",
                "quantity": 2,
                "entry_price": 4600,
                "stop_loss": 4550,
                "target": 4650,
                "opened_at": "now",
                "opened_run_date": run_date,
            }
        ],
    )
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-29T10:00:00+00:00", "high": 4652, "low": 4595, "close": 4651}],
    )

    closed = PaperExecutor(root).evaluate_exits(run_date)

    assert closed[0]["exit_reason"] == "target"
    assert closed[0]["signal_regime"] == "trend_following"
    assert closed[0]["backtest_verdict"] == "supportive"
    assert closed[0]["gross_realized_pnl"] == 100.0
    assert closed[0]["total_cost"] > 0
    assert closed[0]["realized_pnl"] < 100.0
    assert load_json(root / "paper_trades" / "current.json") == []
    assert load_json(root / "paper_trades" / "closed" / f"{run_date}.json")[0]["trade_id"] == "trade_target"


def test_evaluate_exits_blocks_when_preflight_blocks_paper(tmp_path: Path):
    run_date = "2026-06-29"
    root = tmp_path / "outputs"
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "trade_target",
                "order_id": "paper_target",
                "ticket_id": "ticket_target",
                "symbol": "GOLD",
                "side": "long",
                "status": "open",
                "quantity": 2,
                "entry_price": 4600,
                "stop_loss": 4550,
                "target": 4650,
                "opened_at": "now",
                "opened_run_date": run_date,
            }
        ],
    )
    write_json(
        root / "clean_bars" / run_date / "GOLD_5m.json",
        [{"timestamp": "2026-06-29T10:00:00+00:00", "high": 4652, "low": 4595, "close": 4651}],
    )
    write_json(
        root / "data_source_preflight" / f"{run_date}.json",
        [{"ready_for_paper": False, "message": "latest bar is synthetic"}],
    )

    closed = PaperExecutor(root).evaluate_exits(run_date)

    assert closed == []
    assert load_json(root / "paper_trades" / "current.json")[0]["status"] == "open"
    assert load_json(root / "paper_trades" / "closed" / f"{run_date}.json") == []
    blocks = load_json(root / "paper_execution_blocks" / f"{run_date}.json")
    assert blocks[0]["operation"] == "evaluate_exits"
    assert blocks[0]["source"] == "data_source_preflight"


def test_ensure_trade_records_backfills_filled_orders(tmp_path: Path):
    run_date = "2026-06-30"
    root = tmp_path / "outputs"
    write_json(
        root / "paper_orders" / f"{run_date}.json",
        [
            {
                "order_id": "paper_legacy",
                "ticket_id": "ticket_legacy",
                "status": "filled",
                "requested_price": 4570,
                "fill_price": 4570,
                "quantity": 1,
                "filled_at": "2026-06-30T00:00:00+00:00",
            }
        ],
    )

    added = PaperExecutor(root).ensure_trade_records(run_date)

    assert added[0]["trade_id"] == "trade_legacy"
    assert added[0]["quality_flags"] == ["backfilled_from_order"]
    assert load_json(root / "paper_trades" / "current.json")[0]["target"] == 4752.8


def _live_ticket() -> dict:
    return {
        "ticket_id": "ticket_live_x",
        "signal_id": "sig_live_x",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4470.00-4480.00",
        "stop_loss": 4450.0,
        "targets": [4500.0],
        "position_size_pct": 8,
        "signal_regime": "chan_second_buy",
        "signal_strength": 70,
        "signal_confidence": 65,
    }


def test_record_external_fill_mirrors_real_exchange_fill(tmp_path: Path):
    root = tmp_path / "outputs"
    order = PaperExecutor(root).record_external_fill(
        "2026-06-03", _live_ticket(), fill_price=4475.5, quantity=0.05, order_id="live_abc", commission=0.02
    )

    assert order.status == "filled" and order.fill_price == 4475.5 and order.quantity == 0.05
    position = json.loads((root / "paper_positions" / "current.json").read_text())["GOLD"]
    assert position["side"] == "long" and position["quantity"] == 0.05
    assert position["avg_price"] == 4475.5  # the REAL fill, not a re-modelled midpoint
    trade = load_json(root / "paper_trades" / "current.json")[0]
    assert trade["entry_price"] == 4475.5
    assert "exchange_managed" in trade["quality_flags"] and "live_fill" in trade["quality_flags"]
    lifecycle = load_json(root / "order_lifecycle" / "2026-06-03.json")[0]
    assert lifecycle["state"] == "filled"


def test_record_external_fill_only_marks_protective_attached_when_verified(tmp_path: Path):
    root = tmp_path / "outputs"
    PaperExecutor(root).record_external_fill(
        "2026-06-03",
        _live_ticket(),
        fill_price=4475.5,
        quantity=0.05,
        order_id="live_verified",
        protection_verified=True,
    )

    lifecycle = load_json(root / "order_lifecycle" / "2026-06-03.json")[0]
    assert lifecycle["state"] == "protective_attached"
    assert lifecycle["protective_quantity"] == 0.05


def test_rebuild_positions_preserves_mixed_long_short_legs(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-03"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"close": 105.0}])
    write_json(root / "paper_trades" / "current.json", [
        {"trade_id": "long", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 2, "entry_price": 100, "entry_total_cost": 0.5},
        {"trade_id": "short", "symbol": "GOLD", "side": "short", "status": "open", "quantity": 1, "entry_price": 110, "entry_total_cost": 0.25},
    ])

    positions = PaperExecutor(root).rebuild_positions_from_open_trades(run_date)
    position = positions["GOLD"]

    assert position["side"] == "mixed"
    assert position["quantity"] == 3
    assert position["avg_price"] == 103.3333
    assert position["net_quantity"] == 1
    assert position["net_side"] == "long"
    assert position["legs"]["long"]["quantity"] == 2
    assert position["legs"]["short"]["quantity"] == 1
    assert position["gross_unrealized_pnl"] == 15.0
    assert position["unrealized_pnl"] == 14.25


def test_evaluate_exits_skips_exchange_managed_positions(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-03"
    # an open, exchange-managed live trade whose target the latest bar clearly hits
    write_json(root / "data_quality" / f"{run_date}.json", {"GOLD": {"allows_trading": True}})
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True}])
    PaperExecutor(root).record_external_fill(run_date, _live_ticket(), fill_price=4475.5, quantity=0.05, order_id="live_abc")
    write_json(root / "clean_bars" / run_date / "GOLD_1m.json", [{"close": 4999, "high": 9999, "low": 4998, "timestamp": f"{run_date}T00:05:00+00:00"}])

    closed = PaperExecutor(root).evaluate_exits(run_date)

    # the exchange owns the stop/target — local must NOT close it even though the bar blew through target
    assert closed == []
    assert load_json(root / "paper_trades" / "current.json")[0]["status"] == "open"
