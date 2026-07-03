"""Per-cycle pending resolver: make every pending ticket terminal.

The owner's system is fully autonomous — there is no human-approval middle state.
Each cycle, for a namespace's pending journal, this resolves EVERY ticket:

  * the gate-approved primary (pending[0]) auto-executes (executed_paper);
  * if the auto-approval gate blocks, or execution hits a safety limit, that
    ticket is auto-REJECTED with the reason (not left pending);
  * any additional same-cycle tickets (pending[1+]) are auto-rejected — the signal
    re-fires next cycle if it still holds (signal_id is keyed on the moving close),
    so reject ≈ retry and this cannot strand a still-valid setup.

This removes decided tickets from pending (killing the silent-overwrite drop) and
writes explicit journal_decisions (visible for replay). "autonomous" ≠ "auto-yes":
removing a human from a safety stop DROPS the trade, it does not fire it.
"""

from __future__ import annotations

from services.journal_store import JournalStore

_PER_CYCLE_NOTE = "auto-rejected: one auto-decision per cycle; signal re-fires next cycle if still valid"


def resolve_pending_cycle(
    run_date: str,
    pending: list[dict],
    *,
    auto_approve: bool,
    gate_allows: bool,
    gate_reasons: list[str] | None = None,
    store: JournalStore | None = None,
    broker_adapter=None,
) -> dict:
    """Terminate every pending ticket this cycle. Returns executed/rejected ids."""
    store = store or JournalStore()
    result: dict = {"executed": [], "rejected": [], "skipped": [], "errors": [], "decisions": []}
    ids = [str(item.get("ticket_id")) for item in pending if isinstance(item, dict) and item.get("ticket_id")]

    if not auto_approve:
        # Manual mode (not the owner's config): leave pending untouched.
        result["skipped"] = ids
        return result

    safety_reason = "; ".join(str(r) for r in (gate_reasons or []) if r) or "auto-approval gate blocked"

    for index, ticket_id in enumerate(ids):
        if index == 0 and gate_allows:
            try:
                # Only forward broker_adapter when a live adapter is actually injected
                # (multi-strategy live routing); the paper path leaves it to the store.
                extra = {"broker_adapter": broker_adapter} if broker_adapter is not None else {}
                record = store.record_decision(
                    run_date=run_date,
                    ticket_id=ticket_id,
                    decision="executed_paper",
                    notes="auto-approved paper execution",
                    **extra,
                )
                result["executed"].append(ticket_id)
                result["decisions"].append(record)
                continue
            except (ValueError, RuntimeError) as exc:
                store.record_decision(
                    run_date=run_date,
                    ticket_id=ticket_id,
                    decision="rejected",
                    notes=f"auto-rejected (execution blocked): {exc}",
                )
                result["rejected"].append(ticket_id)
                result["errors"].append({"ticket_id": ticket_id, "error": str(exc)})
                continue
        note = f"auto-rejected (safety): {safety_reason}" if index == 0 else _PER_CYCLE_NOTE
        store.record_decision(run_date=run_date, ticket_id=ticket_id, decision="rejected", notes=note)
        result["rejected"].append(ticket_id)

    return result
