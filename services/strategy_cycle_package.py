"""Immutable terminal evidence for one production strategy cycle."""

from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.dualtrack_clock import cycle_window_from_id
from services.execution_plugin_composition import build_configured_execution_engine_adapter
from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane, production_mutation_lock
from services.strategy_shadow import load_strategy_shadow_runs


ShadowEvidenceBuilder = Callable[[str, dict[str, Any] | None, list[dict[str, Any]]], Any]


class StrategyCyclePackager:
    """Create one append-only, hash-addressed terminal cycle package.

    Execution truth is captured first. Optional shadow evidence is isolated:
    its failure is recorded in the package and can never reopen production or
    prevent a reconciled cycle from closing.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        config: dict[str, Any] | None = None,
        adapter: Any = None,
        shadow_evidence_builder: ShadowEvidenceBuilder | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack"
        self.config = config
        self.adapter = adapter
        self.shadow_evidence_builder = shadow_evidence_builder

    def package(self, cycle_id: str, *, now: str | None = None) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._package_locked(cycle_id, now=now)

    def _package_locked(self, cycle_id: str, *, now: str | None = None) -> dict[str, Any]:
        path = self._path(cycle_id)
        existing = load_json(path)
        supersedes_hash = ""
        if existing:
            latest = existing[-1]
            chain_failure = _package_chain_failure(existing)
            if chain_failure:
                incident = self._integrity_incident(
                    chain_failure.get("record"),
                    cycle_id=cycle_id,
                    now=now,
                    source_record_index=int(chain_failure["source_record_index"]),
                    failure_reason=str(chain_failure["failure_reason"]),
                    observed=str(chain_failure.get("observed") or ""),
                    expected=str(chain_failure.get("expected") or ""),
                )
                write_json(path, [*existing, incident])
                return incident
            if "package_hash_mismatch" in set(latest.get("blockers") or []):
                return latest
            if latest.get("status") == "closed":
                repaired = self._repair_existing_package(latest, cycle_id=cycle_id, now=now)
                if repaired is None:
                    return latest
                write_json(path, [*existing, repaired])
                return repaired
            supersedes_hash = str(latest.get("package_hash") or "")

        adapter = self.adapter or build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        control = StrategyControlPlane(self.output_root)
        plan = control.active_plan(cycle_id) or control.latest_plan(cycle_id)
        proposals = control.proposals(cycle_id)
        snapshot = adapter.snapshot(cycle_id)
        reconciliation = adapter.reconcile(cycle_id)
        orders = [dict(row) for row in snapshot.get("orders") or []]
        fills = [dict(row) for row in snapshot.get("fills") or []]
        positions = [dict(row) for row in snapshot.get("positions") or []]
        accepted = [row for row in orders if str(row.get("state") or "").lower() == "accepted"]
        open_positions = [row for row in positions if str(row.get("status") or "").lower() == "open"]
        reconciliation_ok = (
            str(reconciliation.get("status") or "").lower() == "ok"
            and not reconciliation.get("issues")
        )
        execution_terminal = not accepted and not open_positions and reconciliation_ok
        plan_linked = (
            isinstance(plan, dict)
            and plan.get("strategy_plan_id") not in (None, "")
            and plan.get("version") not in (None, "")
        )
        terminal = execution_terminal and plan_linked
        blockers = []
        if accepted:
            blockers.append("accepted_orders_remain")
        if open_positions:
            blockers.append("open_positions_remain")
        if not reconciliation_ok:
            blockers.append("execution_reconciliation_not_ok")
        if not plan_linked:
            blockers.append("strategy_plan_link_missing")
        shadow_generation = (
            self._build_shadow_evidence(cycle_id, plan, proposals)
            if terminal
            else {"status": "skipped", "attempted": False, "reason": "terminal_package_blocked"}
        )
        packaged_at = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        pnl = dict(snapshot.get("pnl") or {})
        payload: dict[str, Any] = {
            "schema_version": "strategy-cycle-package-v1",
            "cycle_id": cycle_id,
            "status": "closed" if terminal else "blocked",
            "blockers": blockers,
            "packaged_at": packaged_at,
            "window": cycle_window_from_id(cycle_id).to_dict(),
            "strategy_plan": plan,
            "proposals": proposals,
            "execution": {
                "engine": snapshot.get("engine") or getattr(adapter, "name", ""),
                "orders": orders,
                "fills": fills,
                "positions": positions,
                "account": dict(snapshot.get("account") or {}),
                "pnl": pnl,
                "reconciliation": dict(reconciliation),
            },
            "review": self._review(
                cycle_id,
                orders=orders,
                fills=fills,
                accepted=accepted,
                open_positions=open_positions,
                reconciliation=reconciliation,
                pnl=pnl,
            ),
            "strategy_shadows": load_strategy_shadow_runs(self.output_root, cycle_id),
            "shadow_generation": shadow_generation,
            "traceability": {
                "strategy_plan_id": (plan or {}).get("strategy_plan_id"),
                "strategy_plan_version": (plan or {}).get("version"),
                "orders_linked": all(row.get("strategy_plan_id") not in (None, "") for row in orders),
                "fills_linked": all(row.get("strategy_plan_id") not in (None, "") for row in fills),
                "production_ledger_immutable": True,
            },
            "safety": {
                "real_orders": False,
                "writes_production_ledger": False,
                "historical_records_preserved": True,
                "shadow_failure_blocks_production_close": False,
            },
        }
        if supersedes_hash:
            payload["revision"] = {
                "reasons": ["refresh_blocked_terminal_evidence"],
                "revised_at": packaged_at,
                "supersedes_package_hash": supersedes_hash,
                "original_record_preserved": True,
            }
        payload["package_hash"] = _hash_payload(payload)
        write_json(path, [*existing, payload])
        return payload

    def list_packages(self, *, limit: int = 12) -> list[dict[str, Any]]:
        folder = self.root / "strategy_cycle_packages"
        if not folder.exists():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            rows = load_json(path)
            if rows and isinstance(rows[-1], dict):
                result.append(rows[-1])
            if len(result) >= max(1, int(limit)):
                break
        return result

    def acknowledge_integrity_incident(
        self,
        cycle_id: str,
        *,
        incident_package_hash: str,
        actor: dict[str, Any],
        now: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly acknowledge a latched package-integrity incident."""

        with production_mutation_lock(self.output_root):
            return self._acknowledge_integrity_incident_locked(
                cycle_id,
                incident_package_hash=incident_package_hash,
                actor=actor,
                now=now,
            )

    def _acknowledge_integrity_incident_locked(
        self,
        cycle_id: str,
        *,
        incident_package_hash: str,
        actor: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:

        if not isinstance(actor, dict):
            raise ValueError("strategy cycle package integrity acknowledgement requires an actor")
        actor_id = str(actor.get("id") or actor.get("client") or "").strip()
        transport = str(actor.get("transport") or "").strip()
        if not actor_id or not transport:
            raise ValueError("strategy cycle package integrity acknowledgement requires an actor")
        actor_identity = {"id": actor_id, "transport": transport}
        path = self._path(cycle_id)
        rows = load_json(path)
        latest = rows[-1] if rows else None
        if _package_chain_failure(rows):
            raise ValueError("strategy cycle package chain is invalid")
        if "package_hash_mismatch" not in set(latest.get("blockers") or []):
            raise ValueError("latest strategy cycle package is not an integrity incident")
        expected = str(latest.get("package_hash") or "")
        if not hmac.compare_digest(expected, str(incident_package_hash or "")):
            raise ValueError("strategy cycle package integrity incident changed")
        acknowledged_at = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload: dict[str, Any] = {
            "schema_version": "strategy-cycle-package-v1",
            "cycle_id": cycle_id,
            "status": "blocked",
            "blockers": ["integrity_repair_acknowledged"],
            "packaged_at": acknowledged_at,
            "integrity": {
                "status": "acknowledged",
                "acknowledged_by": actor_identity,
                "acknowledged_at": acknowledged_at,
            },
            "revision": {
                "reasons": ["acknowledge_package_hash_mismatch"],
                "supersedes_package_hash": expected,
                "original_record_preserved": True,
            },
            "safety": {
                "fail_closed": True,
                "writes_production_ledger": False,
                "real_orders": False,
            },
        }
        payload["package_hash"] = _hash_payload(payload)
        write_json(path, [*rows, payload])
        return payload

    def _build_shadow_evidence(
        self,
        cycle_id: str,
        plan: dict[str, Any] | None,
        proposals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.shadow_evidence_builder is None:
            return {"status": "not_configured", "attempted": False}
        try:
            result = self.shadow_evidence_builder(cycle_id, plan, proposals)
            return {"status": "complete", "attempted": True, "result": result}
        except Exception as exc:  # Shadow analysis must never unwind terminal production closure.
            return {
                "status": "blocked",
                "attempted": True,
                "error": f"{type(exc).__name__}: {exc}",
                "retry": "explicit_new_package_revision_only",
            }

    def _review(
        self,
        cycle_id: str,
        *,
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
        reconciliation: dict[str, Any],
        pnl: dict[str, Any],
    ) -> dict[str, Any]:
        realized = (
            float(pnl["realized"])
            if pnl.get("realized") is not None
            else sum(float(row.get("realized_pnl") or 0.0) for row in fills)
        )
        legacy_rows = load_json(self.root / "reviews" / f"{cycle_id}_machine.json")
        legacy = dict(legacy_rows[-1]) if legacy_rows and isinstance(legacy_rows[-1], dict) else {}
        terminal = (
            not accepted
            and not open_positions
            and str(reconciliation.get("status") or "") == "ok"
            and not reconciliation.get("issues")
        )
        return {
            "schema_version": "production-cycle-review-v2",
            "cycle_id": cycle_id,
            "status": "complete" if terminal else "blocked",
            "realized_pnl": round(realized, 8),
            "realized_pnl_source": (
                "execution.pnl.realized"
                if pnl.get("realized") is not None
                else "fill.realized_pnl_compatibility_fallback"
            ),
            "fill_count": len(fills),
            "order_count": len(orders),
            "accepted_order_count_at_close": len(accepted),
            "open_position_count_at_close": len(open_positions),
            "reconciliation_status": reconciliation.get("status"),
            "legacy_review": legacy,
        }

    def _repair_existing_package(
        self,
        package: dict[str, Any],
        *,
        cycle_id: str,
        now: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(package, dict):
            return None
        repaired = deepcopy(package)
        reasons: list[str] = []
        execution = repaired.get("execution") if isinstance(repaired.get("execution"), dict) else {}
        pnl = execution.get("pnl") if isinstance(execution.get("pnl"), dict) else {}
        review = repaired.get("review") if isinstance(repaired.get("review"), dict) else {}
        if pnl.get("realized") is not None:
            authoritative = round(float(pnl["realized"]), 8)
            recorded = round(float(review.get("realized_pnl") or 0.0), 8)
            if authoritative != recorded:
                repaired["review"] = {
                    **review,
                    "schema_version": "production-cycle-review-v2",
                    "realized_pnl": authoritative,
                    "realized_pnl_source": "execution.pnl.realized",
                }
                reasons.append("repair_review_realized_pnl_source")
        if not isinstance(repaired.get("strategy_plan"), dict):
            restored = StrategyControlPlane(self.output_root).latest_plan(cycle_id)
            if restored:
                repaired["strategy_plan"] = restored
                repaired["traceability"] = {
                    **dict(repaired.get("traceability") or {}),
                    "strategy_plan_id": restored.get("strategy_plan_id"),
                    "strategy_plan_version": restored.get("version"),
                }
                reasons.append("restore_archived_strategy_plan_link")
        if not reasons:
            return None
        previous_hash = str(repaired.pop("package_hash", "") or "")
        repaired["revision"] = {
            "reasons": reasons,
            "revised_at": now or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "supersedes_package_hash": previous_hash,
            "original_record_preserved": True,
        }
        repaired["package_hash"] = _hash_payload(repaired)
        return repaired

    def _integrity_incident(
        self,
        package: Any,
        *,
        cycle_id: str,
        now: str | None,
        source_record_index: int,
        failure_reason: str,
        observed: str,
        expected: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "strategy-cycle-package-v1",
            "cycle_id": cycle_id,
            "status": "blocked",
            "blockers": ["package_hash_mismatch"],
            "packaged_at": now or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "integrity": {
                "status": "failed",
                "observed_package_hash": observed,
                "expected_package_hash": expected,
                "source_record_index": source_record_index,
                "failure_reason": failure_reason,
                "source_record_trusted": False,
            },
            "revision": {
                "reasons": ["record_package_hash_mismatch"],
                "supersedes_package_hash": None,
                "original_record_preserved": True,
            },
            "safety": {
                "fail_closed": True,
                "writes_production_ledger": False,
                "real_orders": False,
            },
        }
        payload["package_hash"] = _hash_payload(payload)
        return payload

    def _path(self, cycle_id: str) -> Path:
        return self.root / "strategy_cycle_packages" / f"{cycle_id}.json"


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _expected_package_hash(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    candidate = deepcopy(value)
    candidate.pop("package_hash", None)
    return _hash_payload(candidate)


def _package_hash_is_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    observed = str(value.get("package_hash") or "")
    expected = _expected_package_hash(value)
    return bool(observed) and hmac.compare_digest(observed, expected)


def load_latest_verified_cycle_package(path: Path) -> dict[str, Any]:
    """Load one terminal package only after its complete journal is verified."""

    rows = load_json(Path(path))
    if not rows:
        raise ValueError(f"missing strategy cycle package: {path}")
    failure = _package_chain_failure(rows)
    if failure:
        raise ValueError(
            "strategy cycle package chain is invalid"
            f"; row={failure['source_record_index']}"
            f"; reason={failure['failure_reason']}"
        )
    latest = rows[-1]
    if not isinstance(latest, dict) or latest.get("status") != "closed":
        raise ValueError(f"strategy cycle package is not terminal: {path}")
    return deepcopy(latest)


def load_verified_cycle_package_revision(path: Path, package_hash: str) -> dict[str, Any]:
    """Resolve one referenced closed revision from a valid package journal."""

    rows = load_json(Path(path))
    if not rows:
        raise ValueError(f"missing strategy cycle package: {path}")
    failure = _package_chain_failure(rows)
    if failure:
        raise ValueError(
            "strategy cycle package chain is invalid"
            f"; row={failure['source_record_index']}"
            f"; reason={failure['failure_reason']}"
        )
    expected = str(package_hash or "")
    matched = next(
        (
            row
            for row in rows
            if isinstance(row, dict)
            and hmac.compare_digest(str(row.get("package_hash") or ""), expected)
        ),
        None,
    )
    if matched is None or matched.get("status") != "closed":
        raise ValueError(f"referenced terminal strategy cycle package is missing: {package_hash}")
    return deepcopy(matched)


def _package_chain_failure(rows: list[Any]) -> dict[str, Any] | None:
    """Return the first unacknowledged hash/link failure in an append-only journal."""

    def covered(index: int, *, reason: str, observed: str, expected: str) -> bool:
        for candidate in rows[index + 1:]:
            if not _package_hash_is_valid(candidate):
                continue
            if "package_hash_mismatch" not in set(candidate.get("blockers") or []):
                continue
            integrity = candidate.get("integrity") or {}
            if (
                int(integrity.get("source_record_index", -1)) == index
                and str(integrity.get("failure_reason") or "") == reason
                and hmac.compare_digest(str(integrity.get("observed_package_hash") or ""), observed)
                and hmac.compare_digest(str(integrity.get("expected_package_hash") or ""), expected)
            ):
                return True
        return False

    valid_hashes = [_package_hash_is_valid(row) for row in rows]
    for index, row in enumerate(rows):
        if not valid_hashes[index]:
            observed = str(row.get("package_hash") or "") if isinstance(row, dict) else ""
            expected = _expected_package_hash(row)
            if covered(index, reason="package_hash_mismatch", observed=observed, expected=expected):
                continue
            return {
                "record": row,
                "source_record_index": index,
                "failure_reason": "package_hash_mismatch",
                "observed": observed,
                "expected": expected,
            }
        if index == 0 or not isinstance(row, dict):
            continue
        revision = row.get("revision") if isinstance(row.get("revision"), dict) else {}
        supersedes = revision.get("supersedes_package_hash")
        is_incident = "package_hash_mismatch" in set(row.get("blockers") or [])
        if supersedes in (None, "") and is_incident:
            continue
        if supersedes in (None, ""):
            expected = str(rows[index - 1].get("package_hash") or "")
            if covered(index, reason="supersedes_hash_mismatch", observed="", expected=expected):
                continue
            return {
                "record": row,
                "source_record_index": index,
                "failure_reason": "supersedes_hash_mismatch",
                "observed": "",
                "expected": expected,
            }
        if not valid_hashes[index - 1]:
            continue
        expected = str(rows[index - 1].get("package_hash") or "")
        observed = str(supersedes)
        if hmac.compare_digest(observed, expected):
            continue
        if covered(index, reason="supersedes_hash_mismatch", observed=observed, expected=expected):
            continue
        return {
            "record": row,
            "source_record_index": index,
            "failure_reason": "supersedes_hash_mismatch",
            "observed": observed,
            "expected": expected,
        }
    return None
