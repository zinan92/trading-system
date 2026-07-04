"""Per-cycle pending resolver for auto-approved paper/demo strategy tickets.

The owner's system is fully autonomous — there is no human-approval middle state.
Each cycle, for a namespace's pending journal, this resolves tickets that are
ready to decide:

  * the gate-approved primary (pending[0]) auto-executes only when a market or
    touched-limit entry is executable;
  * an untouched limit entry stays pending until its configured bar TTL expires;
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

from pathlib import Path

from services.journal_store import JournalStore, load_json, write_json
from services.pending_entry_guard import evaluate_limit_entry_status, load_entry_candles

_PER_CYCLE_NOTE = "auto-rejected: one auto-decision per cycle; signal re-fires next cycle if still valid"
_STALE_NOTE = "auto-rejected (stale): undecided pending carried past its run_date; signal re-fires fresh if still valid"


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
    """Resolve pending tickets that are executable or expired this cycle.

    Limit entries remain pending until the bar trades through the limit price or
    their configured bar TTL expires.
    """
    store = store or JournalStore()
    result: dict = {"executed": [], "rejected": [], "skipped": [], "errors": [], "decisions": []}
    ids = [str(item.get("ticket_id")) for item in pending if isinstance(item, dict) and item.get("ticket_id")]
    pending_by_id = {str(item.get("ticket_id")): item for item in pending if isinstance(item, dict) and item.get("ticket_id")}

    if not auto_approve:
        # Manual mode (not the owner's config): leave pending untouched.
        result["skipped"] = ids
        return result

    safety_reason = "; ".join(str(r) for r in (gate_reasons or []) if r) or "auto-approval gate blocked"

    for index, ticket_id in enumerate(ids):
        if index == 0 and gate_allows:
            limit_status = _limit_entry_status(store, run_date, ticket_id, pending_by_id.get(ticket_id, {}))
            if limit_status.get("status") == "waiting" and broker_adapter is None:
                result["skipped"].append(ticket_id)
                continue
            if limit_status.get("status") == "expired":
                _safe_reject(store, run_date, ticket_id, result, str(limit_status.get("reason") or "auto-rejected: limit entry expired"))
                continue
            try:
                # Only forward broker_adapter when a live adapter is actually injected
                # (multi-strategy live routing); the paper path leaves it to the store.
                extra = {"broker_adapter": broker_adapter} if broker_adapter is not None else {}
                if limit_status.get("actual_entry") is not None:
                    extra["actual_entry"] = float(limit_status["actual_entry"])
                elif broker_adapter is not None and limit_status.get("limit_price") is not None:
                    extra["actual_entry"] = float(limit_status["limit_price"])
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
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                # Broad catch (matches the pre-refactor msr behaviour): a broker/disk/
                # malformed-row failure on the execute path must downgrade to an
                # auto-reject, never escape and abort the whole cycle. RuntimeError
                # also covers the live adapter's own gates.
                _safe_reject(store, run_date, ticket_id, result, f"auto-rejected (execution blocked): {exc}", error=str(exc))
                continue
        note = f"auto-rejected (safety): {safety_reason}" if index == 0 else _PER_CYCLE_NOTE
        _safe_reject(store, run_date, ticket_id, result, note)

    return result


def _limit_entry_status(store: JournalStore, run_date: str, ticket_id: str, pending_item: dict) -> dict:
    output_root = getattr(store, "output_root", None)
    if output_root is None:
        return {"status": "not_applicable"}
    ticket = _load_ticket(Path(output_root), run_date, ticket_id)
    if not ticket:
        ticket = dict(pending_item)
    if str(ticket.get("order_type", "")).lower() != "limit":
        return {"status": "not_applicable"}
    candles = load_entry_candles(Path(output_root), run_date, ticket)
    return evaluate_limit_entry_status({**ticket, **pending_item}, candles)


def _load_ticket(output_root: Path, run_date: str, ticket_id: str) -> dict:
    return next(
        (
            item for item in load_json(output_root / "trade_tickets" / f"{run_date}.json")
            if isinstance(item, dict) and item.get("ticket_id") == ticket_id
        ),
        {},
    )


def _safe_reject(store: JournalStore, run_date: str, ticket_id: str, result: dict, notes: str, *, error: str | None = None) -> None:
    """Record an auto-reject without ever letting a failing reject-write escape.

    "Removing a human DROPS the trade" only holds if the drop itself is fail-safe.
    If even the reject write fails (missing/unwritable artifact), degrade to a
    recorded error and leave the ticket in pending — the next cycle re-tries and
    the stale sweep eventually closes it — rather than crashing the cycle.
    """
    try:
        store.record_decision(run_date=run_date, ticket_id=ticket_id, decision="rejected", notes=notes)
        result["rejected"].append(ticket_id)
        if error is not None:
            result["errors"].append({"ticket_id": ticket_id, "error": error})
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        result["errors"].append({"ticket_id": ticket_id, "error": f"reject write failed: {exc}", "unresolved": True})


def sweep_stale_pending(today: str, *, store: JournalStore | None = None) -> dict:
    """Close undecided pending tickets stranded in prior-date journal files.

    bot/msr only read TODAY's journal_pending, and writers.py overwrites it
    current-state-only, so an undecided ticket from a prior run_date is never
    re-read — it sits forever (the 06-28/06-29 leftovers). Auto-reject them as
    stale so nothing is stuck; a still-valid setup re-fires fresh on the current
    candle. Runs per namespace (pass the scoped store).
    """
    store = store or JournalStore()
    output_root = Path(store.output_root)
    result: dict = {"closed": []}
    pending_dir = output_root / "journal_pending"
    if not pending_dir.exists():
        return result

    for path in sorted(pending_dir.glob("*.json")):
        date = path.stem
        if date == "current" or date >= today:
            continue  # today's pending is the resolver's job; skip current.json + future
        decided = {
            d.get("ticket_id")
            for d in load_json(output_root / "journal_decisions" / f"{date}.json")
            if isinstance(d, dict)
        }
        for item in load_json(path):
            ticket_id = item.get("ticket_id") if isinstance(item, dict) else None
            if not ticket_id or ticket_id in decided:
                continue
            try:
                store.record_decision(run_date=date, ticket_id=ticket_id, decision="rejected", notes=_STALE_NOTE)
                result["closed"].append({"date": date, "ticket_id": ticket_id})
            except ValueError:
                # Ticket artifact gone / already removed: drop it from pending so it
                # cannot re-strand, and record that we closed it.
                remaining = [r for r in load_json(path) if r.get("ticket_id") != ticket_id]
                write_json(path, remaining)
                result["closed"].append({"date": date, "ticket_id": ticket_id, "note": "dropped (artifact missing)"})
    return result
