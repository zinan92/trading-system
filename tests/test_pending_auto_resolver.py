import pytest

from services.pending_auto_resolver import resolve_pending_cycle


class _FakeStore:
    def __init__(self, raise_on_execute: bool = False) -> None:
        self.calls: list[dict] = []
        self.raise_on_execute = raise_on_execute

    def record_decision(self, run_date, ticket_id, decision, notes="", broker_adapter=None):
        if decision == "executed_paper" and self.raise_on_execute:
            raise ValueError("estimated account stop risk 0.80% exceeds max loss 0.50%")
        self.calls.append({"ticket_id": ticket_id, "decision": decision, "notes": notes})
        return {"ticket_id": ticket_id, "decision_status": decision}


def _pending(*ids):
    return [{"ticket_id": tid} for tid in ids]


def test_gate_allows_executes_primary_and_rejects_the_rest():
    store = _FakeStore()
    result = resolve_pending_cycle(
        "2026-07-03", _pending("t0", "t1", "t2"),
        auto_approve=True, gate_allows=True, gate_reasons=[], store=store,
    )
    assert result["executed"] == ["t0"]
    assert result["rejected"] == ["t1", "t2"]
    assert [c["decision"] for c in store.calls] == ["executed_paper", "rejected", "rejected"]
    assert "one auto-decision per cycle" in store.calls[1]["notes"]


def test_gate_blocks_auto_rejects_primary_with_reason():
    store = _FakeStore()
    result = resolve_pending_cycle(
        "2026-07-03", _pending("t0", "t1"),
        auto_approve=True, gate_allows=False,
        gate_reasons=["5 open paper trades already exist; do not add exposure."],
        store=store,
    )
    # every pending ticket terminates as rejected -> nothing stranded, nothing dropped
    assert result["executed"] == []
    assert result["rejected"] == ["t0", "t1"]
    assert all(c["decision"] == "rejected" for c in store.calls)
    assert "auto-rejected (safety)" in store.calls[0]["notes"]
    assert "do not add exposure" in store.calls[0]["notes"]


def test_execution_safety_limit_downgrades_to_reject():
    store = _FakeStore(raise_on_execute=True)
    result = resolve_pending_cycle(
        "2026-07-03", _pending("t0"),
        auto_approve=True, gate_allows=True, gate_reasons=[], store=store,
    )
    assert result["executed"] == []
    assert result["rejected"] == ["t0"]
    assert result["errors"] and "exceeds max loss" in result["errors"][0]["error"]
    assert store.calls[0]["decision"] == "rejected"
    assert "execution blocked" in store.calls[0]["notes"]


def test_manual_mode_leaves_pending_untouched():
    store = _FakeStore()
    result = resolve_pending_cycle(
        "2026-07-03", _pending("t0", "t1"),
        auto_approve=False, gate_allows=True, store=store,
    )
    assert result["skipped"] == ["t0", "t1"]
    assert store.calls == []


def test_no_pending_is_a_noop():
    store = _FakeStore()
    result = resolve_pending_cycle("2026-07-03", [], auto_approve=True, gate_allows=True, store=store)
    assert result == {"executed": [], "rejected": [], "skipped": [], "errors": [], "decisions": []}
    assert store.calls == []


def test_sweep_closes_prior_date_orphans_and_leaves_today(tmp_path):
    from services.journal_store import JournalStore, load_json, write_json
    from services.pending_auto_resolver import sweep_stale_pending

    root = tmp_path / "outputs"
    # a stale orphan from a prior date, with its trade_ticket artifact present
    write_json(root / "trade_tickets" / "2026-06-29.json", [{"ticket_id": "t_old", "asset": "GOLD", "max_loss_pct": 0.5}])
    write_json(root / "journal_pending" / "2026-06-29.json", [{"ticket_id": "t_old"}])
    # a prior-date ticket that was already decided must NOT be re-closed
    write_json(root / "trade_tickets" / "2026-06-30.json", [{"ticket_id": "t_done", "asset": "GOLD"}])
    write_json(root / "journal_pending" / "2026-06-30.json", [{"ticket_id": "t_done"}])
    write_json(root / "journal_decisions" / "2026-06-30.json", [{"ticket_id": "t_done", "decision_status": "executed_paper"}])
    # today's pending must be left for the cycle resolver
    write_json(root / "journal_pending" / "2026-07-03.json", [{"ticket_id": "t_today"}])

    result = sweep_stale_pending("2026-07-03", store=JournalStore(root))

    assert {"date": "2026-06-29", "ticket_id": "t_old"} in result["closed"]
    assert all(c["ticket_id"] != "t_done" for c in result["closed"])
    # orphan is now terminal (rejected) and out of pending
    assert load_json(root / "journal_pending" / "2026-06-29.json") == []
    old_dec = load_json(root / "journal_decisions" / "2026-06-29.json")
    assert old_dec[0]["ticket_id"] == "t_old" and old_dec[0]["decision_status"] == "rejected"
    assert "stale" in old_dec[0]["notes"]
    # today untouched
    assert [r["ticket_id"] for r in load_json(root / "journal_pending" / "2026-07-03.json")] == ["t_today"]


def test_sweep_drops_orphan_when_ticket_artifact_missing(tmp_path):
    from services.journal_store import JournalStore, load_json, write_json
    from services.pending_auto_resolver import sweep_stale_pending

    root = tmp_path / "outputs"
    # pending orphan whose trade_tickets artifact no longer exists
    write_json(root / "journal_pending" / "2026-06-20.json", [{"ticket_id": "t_gone"}])

    result = sweep_stale_pending("2026-07-03", store=JournalStore(root))

    assert load_json(root / "journal_pending" / "2026-06-20.json") == []
    assert result["closed"] and result["closed"][0]["ticket_id"] == "t_gone"


class _RaisingStore:
    def __init__(self, execute_exc=None, reject_exc=None):
        self.calls = []
        self.execute_exc = execute_exc
        self.reject_exc = reject_exc

    def record_decision(self, run_date, ticket_id, decision, notes="", broker_adapter=None):
        if decision == "executed_paper" and self.execute_exc is not None:
            raise self.execute_exc
        if decision == "rejected" and self.reject_exc is not None:
            raise self.reject_exc
        self.calls.append({"ticket_id": ticket_id, "decision": decision, "notes": notes})
        return {"ticket_id": ticket_id, "decision_status": decision}


def test_oserror_on_execute_downgrades_to_reject_not_escape():
    store = _RaisingStore(execute_exc=OSError("disk full"))
    result = resolve_pending_cycle("2026-07-03", _pending("t0"), auto_approve=True, gate_allows=True, store=store)
    assert result["executed"] == []
    assert result["rejected"] == ["t0"]
    assert store.calls[0]["decision"] == "rejected"
    assert any("disk full" in e["error"] for e in result["errors"])


def test_keyerror_on_execute_downgrades_to_reject_not_escape():
    store = _RaisingStore(execute_exc=KeyError("ticket_id"))
    result = resolve_pending_cycle("2026-07-03", _pending("t0"), auto_approve=True, gate_allows=True, store=store)
    assert result["rejected"] == ["t0"]
    assert store.calls[0]["decision"] == "rejected"


def test_failing_reject_write_is_recorded_not_raised():
    # execute fails AND the fallback reject write also fails -> recorded unresolved, never escapes
    store = _RaisingStore(execute_exc=ValueError("boom"), reject_exc=ValueError("ticket_id not found in trade tickets"))
    result = resolve_pending_cycle("2026-07-03", _pending("t0"), auto_approve=True, gate_allows=True, store=store)
    assert result["rejected"] == []
    assert any(e.get("unresolved") for e in result["errors"])


def _limit_ticket(ticket_id: str = "t_limit") -> dict:
    return {
        "ticket_id": ticket_id,
        "signal_id": f"sig_{ticket_id}",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "99.80-99.80",
        "entry_order_limit_price": 99.8,
        "entry_order_ttl_bars": 10,
        "entry_order_timeframe": "1m",
        "entry_order_created_bar_timestamp": "2026-07-03T00:00:00+00:00",
        "stop_loss": 99.6,
        "targets": [100.6],
        "position_size_pct": 50,
        "max_loss_pct": 2.0,
        "order_type": "limit",
        "time_in_force": "gtc",
    }


def _clean_bar(ts: str, *, high: float, low: float, close: float) -> dict:
    return {
        "symbol": "GOLD",
        "timeframe": "1m",
        "timestamp": ts,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "provider": "mock",
        "quality_flags": [],
    }


def _seed_limit_pending(root, run_date: str, *, bars: list[dict]) -> str:
    from services.journal_store import write_json

    ticket = _limit_ticket()
    write_json(root / "trade_tickets" / f"{run_date}.json", [ticket])
    write_json(
        root / "journal_pending" / f"{run_date}.json",
        [
            {
                "journal_id": f"journal_{ticket['ticket_id']}",
                "ticket_id": ticket["ticket_id"],
                "signal_id": ticket["signal_id"],
                "asset": "GOLD",
                "decision_status": "pending_entry_order",
                "created_at": ticket["entry_order_created_bar_timestamp"],
                "required_user_action": "None; waiting for limit entry or expiry.",
                "entry_order_limit_price": ticket["entry_order_limit_price"],
                "entry_order_ttl_bars": ticket["entry_order_ttl_bars"],
                "entry_order_timeframe": ticket["entry_order_timeframe"],
                "entry_order_created_bar_timestamp": ticket["entry_order_created_bar_timestamp"],
                "order_type": "limit",
            }
        ],
    )
    write_json(root / "clean_bars" / run_date / "GOLD_1m.json", bars)
    write_json(root / "data_source_preflight" / f"{run_date}.json", [{"ready_for_paper": True, "ready_for_live": False}])
    return ticket["ticket_id"]


def test_limit_pending_stays_pending_when_price_has_not_touched_entry(tmp_path):
    from services.journal_store import JournalStore, load_json

    root = tmp_path / "outputs"
    run_date = "2026-07-03"
    ticket_id = _seed_limit_pending(
        root,
        run_date,
        bars=[
            _clean_bar("2026-07-03T00:00:00+00:00", high=100.2, low=100.0, close=100.0),
            _clean_bar("2026-07-03T00:01:00+00:00", high=100.1, low=99.9, close=100.0),
        ],
    )

    result = resolve_pending_cycle(run_date, _pending(ticket_id), auto_approve=True, gate_allows=True, store=JournalStore(root))

    assert result["executed"] == []
    assert result["rejected"] == []
    assert result["skipped"] == [ticket_id]
    assert load_json(root / "journal_pending" / f"{run_date}.json")[0]["ticket_id"] == ticket_id
    assert load_json(root / "journal_decisions" / f"{run_date}.json") == []
    assert load_json(root / "paper_orders" / f"{run_date}.json") == []


def test_limit_pending_executes_at_limit_price_when_bar_touches_entry(tmp_path):
    from services.journal_store import JournalStore, load_json

    root = tmp_path / "outputs"
    run_date = "2026-07-03"
    ticket_id = _seed_limit_pending(
        root,
        run_date,
        bars=[
            _clean_bar("2026-07-03T00:00:00+00:00", high=100.2, low=100.0, close=100.0),
            _clean_bar("2026-07-03T00:01:00+00:00", high=100.1, low=99.79, close=100.0),
        ],
    )

    result = resolve_pending_cycle(run_date, _pending(ticket_id), auto_approve=True, gate_allows=True, store=JournalStore(root))

    assert result["executed"] == [ticket_id]
    assert load_json(root / "journal_pending" / f"{run_date}.json") == []
    decision = load_json(root / "journal_decisions" / f"{run_date}.json")[0]
    assert decision["decision_status"] == "executed_paper"
    assert decision["actual_entry"] == 99.8
    assert decision["paper_order"]["status"] == "filled"
    assert decision["paper_order"]["requested_price"] == 99.8


def test_limit_pending_expires_after_ten_bars_without_touch(tmp_path):
    from services.journal_store import JournalStore, load_json

    root = tmp_path / "outputs"
    run_date = "2026-07-03"
    bars = [
        _clean_bar(f"2026-07-03T00:{minute:02d}:00+00:00", high=100.2, low=99.9, close=100.0)
        for minute in range(11)
    ]
    ticket_id = _seed_limit_pending(root, run_date, bars=bars)

    result = resolve_pending_cycle(run_date, _pending(ticket_id), auto_approve=True, gate_allows=True, store=JournalStore(root))

    assert result["executed"] == []
    assert result["rejected"] == [ticket_id]
    assert load_json(root / "journal_pending" / f"{run_date}.json") == []
    decision = load_json(root / "journal_decisions" / f"{run_date}.json")[0]
    assert decision["decision_status"] == "rejected"
    assert "expired after 10 bars" in decision["notes"]
