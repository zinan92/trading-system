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
