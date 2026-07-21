"""Durable 12-hour production-strategy evidence packages."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dualtrack_clock import cycle_window_from_id
from services.dualtrack_execution_adapter import build_configured_execution_engine_adapter
from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane
from services.strategy_shadow import StrategyShadowRunner


class StrategyCyclePackager:
    def __init__(self, output_root: Path, *, config: dict | None = None, adapter=None) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack"
        self.config = config
        self.adapter = adapter

    def package(self, cycle_id: str, *, now: str | None = None) -> dict[str, Any]:
        path = self.root / "strategy_cycle_packages" / f"{cycle_id}.json"
        existing = load_json(path)
        if existing:
            repaired = self._repair_existing_package(existing[-1], cycle_id=cycle_id, now=now)
            if repaired is not None:
                write_json(path, [*existing, repaired])
                return repaired
            return existing[-1]

        if self.adapter is None:
            self.adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)

        window = cycle_window_from_id(cycle_id)
        control = StrategyControlPlane(self.output_root)
        plan = control.active_plan(cycle_id) or control.latest_plan(cycle_id)
        snapshot = self.adapter.snapshot(cycle_id)
        reconciliation = self.adapter.reconcile(cycle_id)
        orders = [dict(row) for row in snapshot.get("orders") or []]
        fills = [dict(row) for row in snapshot.get("fills") or []]
        positions = [dict(row) for row in snapshot.get("positions") or []]
        pnl = dict(snapshot.get("pnl") or {})
        accepted = [row for row in orders if str(row.get("state") or "").lower() == "accepted"]
        open_positions = [row for row in positions if str(row.get("status") or "").lower() == "open"]
        production_review = self._production_review(
            cycle_id,
            fills=fills,
            orders=orders,
            accepted=accepted,
            open_positions=open_positions,
            reconciliation=reconciliation,
            pnl=pnl,
        )
        market_evidence = self._market_evidence(cycle_id)
        shadow_generation = self._ensure_strategy_shadows(
            cycle_id,
            plan=plan,
            proposals=control.proposals(cycle_id),
            market_events=market_evidence.pop("events"),
        )
        strategy_shadows = self._strategy_shadows(cycle_id)
        packaged_at = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        terminal = (
            not accepted
            and not open_positions
            and str(reconciliation.get("status") or "").lower() == "ok"
        )
        payload = {
            "schema_version": "strategy-cycle-package-v1",
            "cycle_id": cycle_id,
            "status": "closed" if terminal else "blocked",
            "packaged_at": packaged_at,
            "window": window.to_dict(),
            "strategy_plan": plan,
            "proposals": control.proposals(cycle_id),
            "execution": {
                "engine": snapshot.get("engine") or getattr(self.adapter, "name", ""),
                "orders": orders,
                "fills": fills,
                "positions": positions,
                "account": dict(snapshot.get("account") or {}),
                "pnl": pnl,
                "reconciliation": reconciliation,
            },
            "review": production_review,
            "strategy_shadows": strategy_shadows,
            "shadow_generation": shadow_generation,
            "market_evidence": market_evidence,
            "traceability": {
                "strategy_plan_id": (plan or {}).get("strategy_plan_id"),
                "strategy_plan_version": (plan or {}).get("version"),
                "orders_linked": all(row.get("strategy_plan_id") not in (None, "") for row in orders),
                "fills_linked": all(row.get("strategy_plan_id") not in (None, "") for row in fills),
                "production_ledger_immutable": True,
                "strategy_shadow_separate_from_execution_shadow": True,
            },
            "safety": {
                "real_orders": False,
                "writes_production_ledger": False,
                "historical_records_preserved": True,
            },
        }
        payload["package_hash"] = _hash_payload(payload)
        write_json(path, [payload])
        return payload

    def list_packages(self, *, limit: int = 12) -> list[dict[str, Any]]:
        folder = self.root / "strategy_cycle_packages"
        if not folder.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            values = load_json(path)
            if values and isinstance(values[-1], dict):
                rows.append(values[-1])
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def _strategy_shadows(self, cycle_id: str) -> list[dict[str, Any]]:
        folder = self.root / "strategy_shadows"
        if not folder.exists():
            return []
        return [
            rows[-1]
            for path in sorted(folder.glob(f"{cycle_id}_*.json"))
            if (rows := load_json(path)) and isinstance(rows[-1], dict)
        ]

    def _market_evidence(self, cycle_id: str) -> dict[str, Any]:
        path = self.root / "nautilus_authoritative" / "events" / f"{cycle_id}.json"
        rows = [row for row in load_json(path) if isinstance(row, dict)]
        return {
            "event_count": len(rows),
            "first_event_at": (rows[0].get("event_started_at") or rows[0].get("ts_event")) if rows else None,
            "last_event_at": (rows[-1].get("event_started_at") or rows[-1].get("ts_event")) if rows else None,
            "event_hash": _hash_payload(rows),
            "source_path": str(path),
            "events": rows,
        }

    def _ensure_strategy_shadows(
        self,
        cycle_id: str,
        *,
        plan: dict[str, Any] | None,
        proposals: list[dict[str, Any]],
        market_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        by_timestamp: dict[str, dict[str, Any]] = {}
        for row in market_events:
            timestamp = str(row.get("event_started_at") or row.get("timestamp") or "")
            close = row.get("close") if row.get("close") is not None else row.get("price")
            if not timestamp or close is None or not all(row.get(key) is not None for key in ("open", "high", "low")):
                continue
            by_timestamp[timestamp] = {
                "timestamp": timestamp,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": close,
            }
        events = [by_timestamp[key] for key in sorted(by_timestamp)]
        variants: list[tuple[str, dict[str, Any]]] = []
        if plan:
            variants.append(("production", plan))
        for proposal in proposals:
            source = str(proposal.get("source") or "")
            if source in {"human", "ai"}:
                variants.append((source, proposal))
        if not events:
            return {"status": "skipped", "reason": "market_event_history_missing", "generated": [], "errors": []}
        generated: list[str] = []
        errors: list[dict[str, str]] = []
        runner = StrategyShadowRunner(self.output_root)
        seen: set[str] = set()
        for variant_id, variant_plan in variants:
            if variant_id in seen:
                continue
            seen.add(variant_id)
            try:
                runner.run(
                    cycle_id=cycle_id,
                    variant_id=variant_id,
                    plan=variant_plan,
                    market_events=events,
                )
                generated.append(variant_id)
            except Exception as exc:  # Shadow failures must never unwind production-cycle closure.
                errors.append({"variant_id": variant_id, "error": f"{type(exc).__name__}: {exc}"})
        return {
            "status": "complete" if generated and not errors else "partial" if generated else "blocked",
            "generated": generated,
            "errors": errors,
            "market_event_count": len(events),
            "future_function": False,
        }

    def _production_review(
        self,
        cycle_id: str,
        *,
        fills: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
        reconciliation: dict[str, Any],
        pnl: dict[str, Any],
    ) -> dict[str, Any]:
        if pnl.get("realized") is not None:
            realized = round(float(pnl["realized"]), 8)
            realized_source = "execution.pnl.realized"
        else:
            realized = round(sum(float(row.get("realized_pnl") or 0.0) for row in fills), 8)
            realized_source = "fill.realized_pnl_compatibility_fallback"
        legacy_rows = load_json(self.root / "reviews" / f"{cycle_id}_machine.json")
        legacy_review = legacy_rows[-1] if legacy_rows and isinstance(legacy_rows[-1], dict) else {}
        compatibility = self._legacy_review_compatibility(legacy_review)
        return {
            "schema_version": "production-cycle-review-v2",
            "cycle_id": cycle_id,
            "status": "complete" if not accepted and not open_positions and reconciliation.get("status") == "ok" else "blocked",
            "realized_pnl": realized,
            "realized_pnl_source": realized_source,
            "fill_count": len(fills),
            "order_count": len(orders),
            "accepted_order_count_at_close": len(accepted),
            "open_position_count_at_close": len(open_positions),
            "reconciliation_status": reconciliation.get("status"),
            **compatibility,
            "legacy_review_source_disclosed": bool(legacy_review),
        }

    def _legacy_review_compatibility(self, legacy_review: dict[str, Any]) -> dict[str, Any]:
        direction = dict(legacy_review.get("direction_review") or legacy_review.get("direction_grade") or {})
        key_levels = dict(legacy_review.get("key_level_review") or legacy_review.get("key_level_grade") or {})
        signal = dict(legacy_review.get("signal_review") or legacy_review.get("signal_grade") or {})
        tp_sl = dict(legacy_review.get("tpsl_review") or legacy_review.get("tp_sl_grade") or {})
        iteration = dict(legacy_review.get("next_iteration") or legacy_review.get("review_change") or {})
        next_change: dict[str, Any] = {}
        if direction.get("verdict") == "adjust":
            next_change = {
                "dimension": "direction",
                "summary": (
                    f"计划 {direction.get('decision') or '--'} 与本周期实际 "
                    f"{direction.get('realized_regime') or '--'} 不一致；下一版仅重新评估方向，其他参数保持不变。"
                ),
                "auto_apply": False,
                "validation_rule": iteration.get("validation_rule") or "需经后续完整周期验证",
                "source": "legacy_review_compatibility",
            }
        elif iteration:
            next_change = {
                "dimension": iteration.get("dimension"),
                "summary": iteration.get("change") or iteration.get("summary") or "下一版本继续收集证据",
                "auto_apply": False,
                "validation_rule": iteration.get("validation_rule"),
                "source": "legacy_review_compatibility",
            }
        return {
            "direction_grade": direction,
            "key_level_grade": key_levels,
            "signal_grade": signal,
            "tp_sl_grade": tp_sl,
            "next_version_change": next_change,
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
        restored_plan = False
        if not isinstance(repaired.get("strategy_plan"), dict):
            archived_plan = StrategyControlPlane(self.output_root).latest_plan(cycle_id)
            if archived_plan:
                repaired["strategy_plan"] = archived_plan
                traceability = dict(repaired.get("traceability") or {})
                traceability.update({
                    "strategy_plan_id": archived_plan.get("strategy_plan_id"),
                    "strategy_plan_version": archived_plan.get("version"),
                })
                repaired["traceability"] = traceability
                reasons.append("restore_archived_strategy_plan_link")
                restored_plan = True
        execution = package.get("execution") if isinstance(package.get("execution"), dict) else {}
        pnl = execution.get("pnl") if isinstance(execution.get("pnl"), dict) else {}
        review = package.get("review") if isinstance(package.get("review"), dict) else {}
        if pnl.get("realized") is not None:
            authoritative = round(float(pnl["realized"]), 8)
            recorded = round(float(review.get("realized_pnl") or 0.0), 8)
            if authoritative != recorded:
                repaired_review = dict(repaired.get("review") or {})
                repaired_review.update({
                    "schema_version": "production-cycle-review-v2",
                    "realized_pnl": authoritative,
                    "realized_pnl_source": "execution.pnl.realized",
                })
                repaired["review"] = repaired_review
                reasons.append("repair_review_realized_pnl_source")

        repaired_review = dict(repaired.get("review") or {})
        if not repaired_review.get("next_version_change"):
            legacy_rows = load_json(self.root / "reviews" / f"{cycle_id}_machine.json")
            legacy_review = legacy_rows[-1] if legacy_rows and isinstance(legacy_rows[-1], dict) else {}
            compatibility = self._legacy_review_compatibility(legacy_review)
            if compatibility.get("next_version_change"):
                repaired_review.update(compatibility)
                repaired_review["legacy_review_source_disclosed"] = True
                repaired["review"] = repaired_review
                reasons.append("map_legacy_review_to_production_cycle_schema")

        shadow_generation_existing = repaired.get("shadow_generation") if isinstance(repaired.get("shadow_generation"), dict) else {}
        needs_shadow_backfill = (
            "shadow_generation" not in repaired
            or restored_plan
            or (
                not repaired.get("strategy_shadows")
                and shadow_generation_existing.get("reason") == "market_event_history_missing"
                and int((repaired.get("market_evidence") or {}).get("event_count") or 0) > 0
                and shadow_generation_existing.get("backfill_attempted") is not True
            )
        )
        if repaired.get("status") == "closed" and needs_shadow_backfill:
            market_evidence = self._market_evidence(cycle_id)
            events = market_evidence.pop("events")
            shadow_generation = self._ensure_strategy_shadows(
                cycle_id,
                plan=repaired.get("strategy_plan") if isinstance(repaired.get("strategy_plan"), dict) else None,
                proposals=[row for row in repaired.get("proposals") or [] if isinstance(row, dict)],
                market_events=events,
            )
            shadow_generation["backfill_attempted"] = True
            repaired["shadow_generation"] = shadow_generation
            repaired["strategy_shadows"] = self._strategy_shadows(cycle_id)
            repaired["market_evidence"] = market_evidence
            if "backfill_strategy_shadow_evidence" not in reasons:
                reasons.append("backfill_strategy_shadow_evidence")

        if not reasons:
            return None
        previous_hash = str(repaired.pop("package_hash", "") or "")
        repaired["revision"] = {
            "reason": "+".join(reasons),
            "reasons": reasons,
            "revised_at": now or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "supersedes_package_hash": previous_hash,
            "original_record_preserved": True,
        }
        repaired["package_hash"] = _hash_payload(repaired)
        return repaired


def _hash_payload(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
