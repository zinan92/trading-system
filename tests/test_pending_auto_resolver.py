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
