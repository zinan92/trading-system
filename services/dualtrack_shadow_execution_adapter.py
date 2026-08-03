"""Non-authoritative execution shadow which can never mutate production truth."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dualtrack_execution_contract import canonical_market_event
from services.journal_store import load_json, write_json


_STATUS_LOCK = threading.RLock()


class ShadowingExecutionEngineAdapter:
    """Mirror commands/events after authoritative acceptance.

    The authoritative return value and ledger always win. Shadow errors are
    persisted for retry/inspection but are deliberately not raised into the
    production paper path.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        authoritative: Any,
        shadow: Any | None,
        blocker: str = "",
    ) -> None:
        self.output_root = Path(output_root)
        self.authoritative = authoritative
        self.shadow = shadow
        self.blocker = str(blocker or "")
        self.name = str(authoritative.name)

    @property
    def market_event_batch_capable(self) -> bool:
        return callable(getattr(self.authoritative, "process_market_events", None))

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        shadow_command = dict(command)
        if str(command.get("event") or "entry").lower() in {"exit", "stop", "target", "flatten"}:
            snapshot = self.authoritative.snapshot(str(command.get("cycle_id") or ""))
            target = _target_position(snapshot.get("positions") or [], command)
            if target:
                shadow_command.update({
                    "target_authoritative_trade_id": target.get("trade_id"),
                    "target_authoritative_position_id": target.get("position_id"),
                    "target_position_side": target.get("side"),
                    "target_entry_price": target.get("entry_price"),
                    "target_remaining_units": target.get("remaining_units"),
                })
                if shadow_command.get("quantity") in (None, "") and shadow_command.get("contracts") in (None, ""):
                    shadow_command["quantity"] = target.get("remaining_units")
        receipt = self.authoritative.submit_order(command)
        authoritative_order_id = str(receipt.get("order_id") or receipt.get("fill_id") or "")
        if authoritative_order_id:
            shadow_command["authoritative_order_id"] = authoritative_order_id
        self._mirror("submit_order", shadow_command)
        return receipt

    def cancel_orders(
        self,
        cycle_id: str,
        *,
        order_ids: list[str] | None = None,
        strategy_plan_id: str | None = None,
        ts: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        kwargs = {
            "order_ids": list(order_ids or []),
            "strategy_plan_id": strategy_plan_id,
            "ts": ts,
            "reason": reason,
        }
        receipt = self.authoritative.cancel_orders(cycle_id, **kwargs)
        payload = {"cycle_id": cycle_id, **kwargs}
        self._mirror("cancel_orders", payload)
        return receipt

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        result = self.authoritative.process_market_event(event)
        self._mirror("process_market_event", event)
        return result

    def process_market_events(
        self,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Batch authority first, then mirror without changing its result."""

        if not events:
            raise ValueError("market event batch is empty")
        normalized = [canonical_market_event(event) for event in events]
        cycle_ids = {str(event.get("cycle_id") or "") for event in normalized}
        event_ids = [str(event.get("event_id") or "") for event in normalized]
        if len(cycle_ids) != 1 or "" in cycle_ids:
            raise ValueError("market event batch mixes cycles")
        if "" in event_ids or len(event_ids) != len(set(event_ids)):
            raise ValueError("market event batch identity is invalid")
        authoritative_batch = getattr(
            self.authoritative,
            "process_market_events",
            None,
        )
        if callable(authoritative_batch):
            result = authoritative_batch([dict(event) for event in events])
        else:
            result = self._process_authoritative_events_fallback(events)
        self._mirror_market_events(events)
        return result

    def settled_market_event_ids(self, cycle_id: str) -> frozenset[str]:
        """Return events settled by authority and durably handed to shadow.

        The shadow is non-authoritative, but intersecting its persisted event
        identities preserves crash-gap catch-up without repeatedly mirroring
        every old event on every tick.  Missing verification capabilities fail
        closed to an empty reusable set.
        """

        authoritative_resolver = getattr(
            self.authoritative,
            "processed_market_event_ids",
            None,
        )
        if not callable(authoritative_resolver):
            return frozenset()
        authoritative_ids = frozenset(authoritative_resolver(cycle_id))
        if self.shadow is None:
            return authoritative_ids
        shadow_resolver = getattr(
            self.shadow,
            "persisted_market_event_ids",
            None,
        )
        if not callable(shadow_resolver):
            return frozenset()
        return authoritative_ids & frozenset(shadow_resolver(cycle_id))

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        return self.authoritative.snapshot(
            cycle_id,
            mark_price=mark_price,
            mark_fresh=mark_fresh,
            mark_source=mark_source,
        )

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        return self.authoritative.reconcile(cycle_id)

    def handoff_cycle(
        self,
        previous_cycle_id: str,
        current_cycle_id: str,
        *,
        current_strategy_plan_id: str,
        boundary_at: str,
    ) -> dict[str, Any]:
        handoff = getattr(self.authoritative, "handoff_cycle", None)
        if not callable(handoff):
            raise RuntimeError("authoritative Paper adapter does not support cycle handoff")
        receipt = handoff(
            previous_cycle_id,
            current_cycle_id,
            current_strategy_plan_id=current_strategy_plan_id,
            boundary_at=boundary_at,
        )
        shadow_handoff = getattr(self.shadow, "handoff_cycle", None)
        if callable(shadow_handoff):
            try:
                shadow_handoff(
                    previous_cycle_id,
                    current_cycle_id,
                    current_strategy_plan_id=current_strategy_plan_id,
                    boundary_at=boundary_at,
                )
            except Exception as exc:
                self._record(
                    {
                        "schema_version": "dualtrack-nautilus-shadow-runtime-v1",
                        "cycle_id": current_cycle_id,
                        "operation": "handoff_cycle",
                        "identity": f"{previous_cycle_id}->{current_cycle_id}",
                        "authoritative_engine": self.name,
                        "shadow_engine": str(getattr(self.shadow, "name", "")),
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "authoritative_unchanged": True,
                        "status": "error",
                        "error": str(exc)[-1000:],
                    }
                )
        return receipt

    def flush_shadow(self, cycle_id: str, *, cycle_complete: bool = False) -> dict[str, Any]:
        base = {
            "schema_version": "dualtrack-nautilus-shadow-runtime-v1",
            "cycle_id": cycle_id,
            "operation": "flush",
            "identity": cycle_id,
            "authoritative_engine": self.name,
            "shadow_engine": str(getattr(self.shadow, "name", "nautilus_paper")),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "authoritative_unchanged": True,
        }
        if self.shadow is None:
            result = {**base, "status": "blocked", "blocker": self.blocker or "shadow_unavailable"}
            self._record(result)
            return result
        flush = getattr(self.shadow, "flush", None)
        if not callable(flush):
            result = {**base, "status": "blocked", "blocker": "shadow_flush_unsupported"}
            self._record(result)
            return result
        try:
            shadow_result = flush(cycle_id)
        except Exception as exc:
            result = {**base, "status": "error", "error": str(exc)[-1000:]}
            self._record(result)
            return result
        reconciliation_status = "not_recorded"
        cutover_status = "not_evaluated"
        candidate = shadow_result.get("snapshot") if isinstance(shadow_result.get("snapshot"), dict) else None
        if candidate is not None:
            try:
                candidate = dict(candidate)
                mark = dict(candidate.get("mark") or {})
                authoritative = self.authoritative.snapshot(
                    cycle_id,
                    mark_price=mark.get("price"),
                    mark_fresh=bool(mark.get("fresh")),
                    mark_source=str(mark.get("source") or ""),
                )
                authoritative["reconciliation"] = self.authoritative.reconcile(cycle_id)
                candidate["reconciliation"] = self.shadow.reconcile(cycle_id)
                qualification = self._qualification_evidence(
                    cycle_id,
                    candidate=candidate,
                    cycle_complete=cycle_complete,
                )
                candidate["shadow_evidence"] = {
                    "mode": "continuous_nautilus_paper_runtime",
                    "replay_version": str((candidate.get("capabilities") or {}).get("replay_version") or ""),
                    **qualification,
                }
                from services.dualtrack_shadow_reconciliation import DualTrackShadowReconciler

                reconciliation = DualTrackShadowReconciler(self.output_root).record(
                    cycle_id,
                    authoritative=authoritative,
                    candidate=candidate,
                )
                reconciliation_status = str(reconciliation.get("status") or "missing")
                from pipelines.dualtrack_shadow_cutover_status import build_cutover_status

                cutover = build_cutover_status(self.output_root)
                write_json(
                    self.output_root / "dualtrack" / "cutover" / "shadow_gate_current.json",
                    [cutover],
                )
                cutover_status = str(cutover.get("status") or "missing")
            except Exception as exc:
                reconciliation_status = f"error:{str(exc)[-500:]}"
                cutover_status = "not_evaluated"
        result = {
            **base,
            "status": "ok",
            "shadow_result_status": str(shadow_result.get("status") or "ok"),
            "processed_event_count": int(shadow_result.get("processed_event_count") or 0),
            "reconciliation_status": reconciliation_status,
            "cutover_status": cutover_status,
        }
        self._record(result)
        return result

    def _qualification_evidence(
        self,
        cycle_id: str,
        *,
        candidate: dict[str, Any],
        cycle_complete: bool,
    ) -> dict[str, Any]:
        shadow_root = Path(getattr(self.shadow, "root", self.output_root / "dualtrack" / "nautilus_paper"))
        commands = load_json(shadow_root / "commands" / f"{cycle_id}.json")
        events = load_json(shadow_root / "events" / f"{cycle_id}.json")
        config = dict(getattr(self.shadow, "config", {}) or {})
        preflight_path = Path(getattr(
            self.shadow,
            "preflight_path",
            self.output_root / "dualtrack" / "nautilus" / "instrument_preflight.json",
        ))
        preflight_rows = load_json(preflight_path)
        preflight = preflight_rows[-1] if preflight_rows and isinstance(preflight_rows[-1], dict) else {}
        observed_fees = dict(preflight.get("fee_model") or {})
        configured_fees = dict(config.get("paper_fee_model") or {})
        fee_contract_match = (
            observed_fees.get("mode") == "account_observed"
            and _same_rate(observed_fees.get("maker_fee_rate"), configured_fees.get("maker_fee_rate"))
            and _same_rate(observed_fees.get("taker_fee_rate"), configured_fees.get("taker_fee_rate"))
            and observed_fees.get("real_money_eligible") is False
            and configured_fees.get("real_money_eligible") is False
        )
        expected_provider = str((config.get("market_data") or {}).get("provider") or "")
        event_providers = sorted({str(row.get("provider") or "") for row in events if isinstance(row, dict)})
        cycle_close_artifact = load_json(
            self.output_root / "dualtrack" / "attribution" / f"{cycle_id}.json"
        )
        trusted_market_events = bool(events) and all(
            isinstance(row, dict)
            and row.get("fresh") is True
            and row.get("is_synthetic") is False
            and bool(str(row.get("source") or ""))
            for row in events
        )
        from services.dualtrack_nautilus_execution_adapter import REPLAY_VERSION

        candidate_replay_version = str((candidate.get("capabilities") or {}).get("replay_version") or "")
        checks = {
            "cycle_complete": bool(cycle_close_artifact),
            "cycle_close_artifact": bool(cycle_close_artifact),
            "command_activity": bool(commands),
            "market_event_activity": bool(events),
            "trusted_market_events": trusted_market_events,
            "market_provider_match": bool(expected_provider) and event_providers == [expected_provider],
            "fee_contract_match": fee_contract_match,
            "replay_version_pinned": candidate_replay_version == REPLAY_VERSION,
        }
        blocker_by_check = {
            "cycle_complete": "cycle_incomplete",
            "cycle_close_artifact": "cycle_close_evidence_missing",
            "command_activity": "candidate_activity_insufficient",
            "market_event_activity": "market_event_history_missing",
            "trusted_market_events": "market_event_contract_untrusted",
            "market_provider_match": "market_provider_contract_mismatch",
            "fee_contract_match": "paper_fee_contract_mismatch",
            "replay_version_pinned": "replay_version_missing",
        }
        blocker = next((blocker_by_check[key] for key, passed in checks.items() if not passed), "")
        fee_contract = {
            key: observed_fees.get(key)
            for key in ("mode", "maker_fee_rate", "taker_fee_rate", "source", "observed_at", "real_money_eligible")
        }
        return {
            "qualification_contract_version": "dualtrack-shadow-qualification-v2",
            "cycle_complete_requested": bool(cycle_complete),
            "expected_replay_version": REPLAY_VERSION,
            "authoritative_command_count": len(commands),
            "market_event_count": len(events),
            "market_providers": event_providers,
            "fee_contract": fee_contract,
            "fee_contract_sha256": hashlib.sha256(
                json.dumps(fee_contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
            "qualification_checks": checks,
            "qualification_blocker": blocker,
            "qualifies_for_cutover": not blocker,
        }

    def _mirror(self, operation: str, payload: dict[str, Any]) -> None:
        cycle_id = str(payload.get("cycle_id") or "")
        identity = str(
            payload.get("event_id")
            or payload.get("source_fill_id")
            or payload.get("external_fill_id")
            or ""
        )
        base = {
            "schema_version": "dualtrack-nautilus-shadow-runtime-v1",
            "cycle_id": cycle_id,
            "operation": operation,
            "identity": identity,
            "authoritative_engine": self.name,
            "shadow_engine": str(getattr(self.shadow, "name", "nautilus_paper")),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "authoritative_unchanged": True,
        }
        if self.shadow is None:
            self._record({**base, "status": "blocked", "blocker": self.blocker or "shadow_unavailable"})
            return
        try:
            if operation == "submit_order":
                result = self.shadow.submit_order(dict(payload))
            elif operation == "cancel_orders":
                args = dict(payload)
                args.pop("cycle_id", None)
                result = self.shadow.cancel_orders(cycle_id, **args)
            else:
                result = self.shadow.process_market_event(dict(payload))
        except Exception as exc:  # Shadow failure is evidence, never production control flow.
            self._record({**base, "status": "error", "error": str(exc)[-1000:]})
            return
        self._record({
            **base,
            "status": "ok",
            "shadow_result_status": str(result.get("status") or result.get("state") or "ok"),
        })

    def _process_authoritative_events_fallback(
        self,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok", "triggered": []}
        triggered: list[dict[str, Any]] = []
        accepted_limit_fills: list[dict[str, Any]] = []
        for event in events:
            result = self.authoritative.process_market_event(dict(event))
            triggered.extend(result.get("triggered") or [])
            accepted_limit_fills.extend(result.get("accepted_limit_fills") or [])
        return {
            **result,
            "status": "triggered" if triggered else str(result.get("status") or "ok"),
            "triggered": triggered,
            "accepted_limit_fills": accepted_limit_fills,
            "accepted_limit_fill_count": len(accepted_limit_fills),
        }

    def _mirror_market_events(self, events: list[dict[str, Any]]) -> None:
        cycle_ids = {str(event.get("cycle_id") or "") for event in events}
        event_ids = [str(event.get("event_id") or "") for event in events]
        cycle_id = next(iter(cycle_ids)) if len(cycle_ids) == 1 else ""
        base = {
            "schema_version": "dualtrack-nautilus-shadow-runtime-v1",
            "cycle_id": cycle_id,
            "operation": "process_market_events",
            "identity": f"{event_ids[0]}..{event_ids[-1]}" if event_ids else "",
            "event_ids": event_ids,
            "event_count": len(events),
            "authoritative_engine": self.name,
            "shadow_engine": str(getattr(self.shadow, "name", "nautilus_paper")),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "authoritative_unchanged": True,
        }
        if self.shadow is None:
            self._record({
                **base,
                "status": "blocked",
                "blocker": self.blocker or "shadow_unavailable",
            })
            return
        try:
            shadow_batch = getattr(self.shadow, "process_market_events", None)
            if callable(shadow_batch):
                shadow_result = shadow_batch([dict(event) for event in events])
            else:
                shadow_result = {"status": "ok"}
                for event in events:
                    shadow_result = self.shadow.process_market_event(dict(event))
        except Exception as exc:
            self._record({**base, "status": "error", "error": str(exc)[-1000:]})
            return
        self._record({
            **base,
            "status": "ok",
            "shadow_result_status": str(
                shadow_result.get("status")
                or shadow_result.get("state")
                or "ok"
            ),
        })

    def _record(self, row: dict[str, Any]) -> None:
        cycle_id = str(row.get("cycle_id") or "unknown")
        root = self.output_root / "dualtrack" / "nautilus_shadow_runtime"
        path = root / f"{cycle_id}.json"
        with _STATUS_LOCK:
            rows = load_json(path)
            if row.get("status") == "blocked" and rows:
                last = rows[-1]
                if (
                    last.get("status") == "blocked"
                    and last.get("operation") == row.get("operation")
                    and last.get("blocker") == row.get("blocker")
                ):
                    write_json(root / "current.json", [row])
                    return
            rows.append(row)
            write_json(path, rows)
            write_json(root / "current.json", [row])


def _target_position(positions: list[dict[str, Any]], command: dict[str, Any]) -> dict[str, Any] | None:
    trade_id = str(command.get("trade_id") or "")
    position_id = str(command.get("position_id") or "")
    open_positions = [row for row in positions if row.get("status") == "open"]
    if trade_id:
        matches = [row for row in open_positions if str(row.get("trade_id") or "") == trade_id]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    if position_id:
        matches = [row for row in open_positions if str(row.get("position_id") or "") == position_id]
        if len(matches) == 1:
            return matches[0]
    return None


def _same_rate(left: Any, right: Any) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False
