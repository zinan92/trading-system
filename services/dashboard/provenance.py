"""Realized-trade evidence, provenance, gap grouping, demo blockers, and trade trace artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.execution_accounting import (
    executed_record_ids,
)
from services.journal_store import load_json


class ProvenanceMixin:
    def _strategy_realized_evidence(
        self,
        *,
        namespace: Path,
        source: dict,
        closed_trade_count: int,
        open_trade_count: int,
    ) -> dict:
        min_closed_trades = 5
        closed_trades = self._load_closed_trades_from_namespace(namespace)
        closed_count = len(closed_trades) if closed_trades else self._safe_int(closed_trade_count)
        open_count = self._safe_int(open_trade_count)
        realized_values = [self._safe_float(item.get("realized_pnl")) for item in closed_trades]
        realized_values = [value for value in realized_values if value is not None]
        realized_pnl = round(sum(realized_values), 4) if realized_values else (0.0 if closed_count == 0 else None)
        wins = sum(1 for value in realized_values if value > 0)
        losses = sum(1 for value in realized_values if value < 0)
        breakeven = max(0, closed_count - wins - losses)
        win_rate = round(wins / closed_count, 4) if closed_count else 0.0
        avg_pnl = round(realized_pnl / closed_count, 4) if realized_pnl is not None and closed_count else None
        gross_profit = sum(value for value in realized_values if value > 0)
        gross_loss = abs(sum(value for value in realized_values if value < 0))
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else None
        net_marked = self._safe_float(source.get("net_pnl"))
        open_marked_pnl = None
        if net_marked is not None and realized_pnl is not None:
            open_marked_pnl = round(net_marked - realized_pnl, 4)
        elif open_count > 0 and net_marked is not None:
            open_marked_pnl = round(net_marked, 4)

        if closed_count < 1 and open_count > 0:
            status = "open_pnl_only"
            tone = "warn"
            label = "open PnL only"
            action = "wait_for_close"
            reason = "Open trades exist, but no closed outcome has validated the strategy yet."
        elif closed_count < 1:
            status = "waiting_for_closed"
            tone = "warn"
            label = "waiting for closed trades"
            action = "collect_samples"
            reason = "No closed trade evidence is available yet."
        elif closed_count < min_closed_trades:
            if realized_pnl is not None and realized_pnl > 0:
                status = "thin_realized_profit"
                tone = "warn"
                label = "thin realized profit"
                action = "collect_more_closed"
                reason = f"Closed PnL is positive, but only {closed_count}/{min_closed_trades} closed trades are available."
            else:
                status = "thin_realized_loss"
                tone = "bad"
                label = "thin realized loss"
                action = "review_or_pause"
                reason = f"Closed PnL is negative with only {closed_count}/{min_closed_trades} closed trades."
        elif realized_pnl is not None and realized_pnl > 0:
            status = "realized_profitable"
            tone = "ok"
            label = "realized profitable"
            action = "review_for_promotion"
            reason = "Closed trade sample is above the minimum threshold and realized PnL is positive."
        elif realized_pnl is not None and realized_pnl < 0:
            status = "realized_losing"
            tone = "bad"
            label = "realized losing"
            action = "diagnose_or_pause"
            reason = "Closed trade sample is above the minimum threshold and realized PnL is negative."
        else:
            status = "realized_flat"
            tone = "warn"
            label = "realized flat"
            action = "collect_more_closed"
            reason = "Closed trades are available, but realized PnL is flat."

        return {
            "status": status,
            "tone": tone,
            "label": label,
            "reason": reason,
            "trader_action": action,
            "closed_trade_count": closed_count,
            "open_trade_count": open_count,
            "min_closed_trades": min_closed_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "realized_pnl": realized_pnl,
            "avg_realized_pnl": avg_pnl,
            "profit_factor": profit_factor,
            "net_marked_pnl": round(net_marked, 4) if net_marked is not None else None,
            "open_marked_pnl": open_marked_pnl,
            "uses_open_pnl": open_count > 0,
            "closed_trade_ids": [item.get("trade_id", "") for item in closed_trades[:8]],
        }

    def _realized_evidence_summary(self, strategy_rows: list[dict]) -> dict:
        rows = [row for row in strategy_rows if isinstance(row, dict)]
        evidences = [row.get("realized_evidence", {}) for row in rows]
        with_closed = [item for item in evidences if self._safe_int(item.get("closed_trade_count")) > 0]
        open_only = [item for item in evidences if item.get("status") == "open_pnl_only"]
        profitable = [item for item in evidences if item.get("status") == "realized_profitable"]
        losing = [item for item in evidences if item.get("status") == "realized_losing"]
        thin_profit = [item for item in evidences if item.get("status") == "thin_realized_profit"]
        thin_loss = [item for item in evidences if item.get("status") == "thin_realized_loss"]
        total_realized = sum(
            float(item.get("realized_pnl") or 0)
            for item in evidences
            if item.get("realized_pnl") is not None
        )
        total_open_marked = sum(
            float(item.get("open_marked_pnl") or 0)
            for item in evidences
            if item.get("open_marked_pnl") is not None
        )

        def _best_row(candidates: list[dict], reverse: bool) -> dict:
            keyed = []
            for row in rows:
                evidence = row.get("realized_evidence", {})
                pnl = self._safe_float(evidence.get("realized_pnl"))
                if evidence in candidates and pnl is not None:
                    keyed.append((pnl, row))
            if not keyed:
                return {}
            return sorted(keyed, key=lambda item: item[0], reverse=reverse)[0][1]

        best = _best_row(with_closed, True)
        worst = _best_row(with_closed, False)
        return {
            "strategy_count": len(rows),
            "with_closed_trade_strategy_count": len(with_closed),
            "open_pnl_only_strategy_count": len(open_only),
            "realized_profitable_strategy_count": len(profitable),
            "realized_losing_strategy_count": len(losing),
            "thin_realized_profit_strategy_count": len(thin_profit),
            "thin_realized_loss_strategy_count": len(thin_loss),
            "total_realized_pnl": round(total_realized, 4),
            "total_open_marked_pnl": round(total_open_marked, 4),
            "best_realized_strategy_id": best.get("strategy_id", "") if best else "",
            "best_realized_pnl": best.get("realized_evidence", {}).get("realized_pnl") if best else None,
            "worst_realized_strategy_id": worst.get("strategy_id", "") if worst else "",
            "worst_realized_pnl": worst.get("realized_evidence", {}).get("realized_pnl") if worst else None,
            "trader_warning": "separate_realized_pnl_from_open_marked_pnl",
        }

    def _trade_evidence_provenance(self, trades: list[dict]) -> dict:
        rows = [trade for trade in trades if isinstance(trade, dict)]
        repaired = []
        original = []
        missing = []
        for trade in rows:
            signal = trade.get("strategy_signal") if isinstance(trade.get("strategy_signal"), dict) else {}
            provenance = signal.get("artifact_provenance") if isinstance(signal.get("artifact_provenance"), dict) else {}
            if provenance.get("status") == "repaired_from_paper_trade":
                repaired.append(trade)
            elif signal:
                original.append(trade)
            else:
                missing.append(trade)
        return {
            "status": "has_repaired_trace" if repaired else ("missing_trace" if missing else "original_trace"),
            "trade_count": len(rows),
            "original_trace_trade_count": len(original),
            "repaired_trade_count": len(repaired),
            "missing_trace_trade_count": len(missing),
            "repaired_trade_ids": [item.get("trade_id", "") for item in repaired[:8]],
            "missing_trade_ids": [item.get("trade_id", "") for item in missing[:8]],
            "trader_note": (
                "repaired_trace_is_reviewable_but_not_original_signal_evidence"
                if repaired
                else ("missing_trace_blocks_promotion" if missing else "all_trace_evidence_original")
            ),
        }

    def _board_evidence_provenance(self, strategy_rows: list[dict]) -> dict:
        repaired_strategy_ids = []
        missing_strategy_ids = []
        original = 0
        repaired = 0
        missing = 0
        total = 0
        for row in strategy_rows:
            if not isinstance(row, dict):
                continue
            provenance = row.get("closed_loop_audit", {}).get("evidence_provenance", {})
            total += self._safe_int(provenance.get("trade_count"))
            original += self._safe_int(provenance.get("original_trace_trade_count"))
            repaired_count = self._safe_int(provenance.get("repaired_trade_count"))
            missing_count = self._safe_int(provenance.get("missing_trace_trade_count"))
            repaired += repaired_count
            missing += missing_count
            if repaired_count:
                repaired_strategy_ids.append(row.get("strategy_id", ""))
            if missing_count:
                missing_strategy_ids.append(row.get("strategy_id", ""))
        return {
            "trade_count": total,
            "original_trace_trade_count": original,
            "repaired_trade_count": repaired,
            "missing_trace_trade_count": missing,
            "repaired_strategy_ids": [item for item in repaired_strategy_ids if item],
            "missing_strategy_ids": [item for item in missing_strategy_ids if item],
            "trader_warning": "show_repaired_trace_separately_from_original_evidence",
        }

    @staticmethod
    def _load_closed_trades_from_namespace(namespace: Path) -> list[dict]:
        closed_dir = namespace / "paper_trades" / "closed"
        if not closed_dir.exists():
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for path in sorted(closed_dir.glob("*.json")):
            rows = load_json(path)
            for trade in rows if isinstance(rows, list) else []:
                if not isinstance(trade, dict):
                    continue
                tid = str(trade.get("trade_id") or f"{path.name}:{len(out)}")
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(trade)
        return out

    def _gap_groups(self, gaps: list[dict], group_by: str = "type") -> list[dict]:
        grouped: dict[str, dict] = {}
        for gap in gaps or []:
            if not isinstance(gap, dict):
                continue
            gap_type = str(gap.get("type") or "unknown_gap")
            root_cause = self._gap_root_cause(gap_type)
            key = root_cause if group_by == "root_cause" else gap_type
            item = grouped.setdefault(
                key,
                {
                    "key": key,
                    "type": gap_type if group_by == "type" else "",
                    "root_cause": root_cause,
                    "label": self._gap_group_label(key),
                    "message": gap.get("message", ""),
                    "severity": "info",
                    "count": 0,
                    "warn_count": 0,
                    "info_count": 0,
                    "gap_types": [],
                    "trade_ids": [],
                    "ticket_ids": [],
                    "signal_ids": [],
                },
            )
            item["count"] += 1
            severity = str(gap.get("severity") or "info")
            if severity == "warn":
                item["warn_count"] += 1
                item["severity"] = "warn"
            else:
                item["info_count"] += 1
            if gap_type not in item["gap_types"]:
                item["gap_types"].append(gap_type)
            for source_key, target_key in (
                ("trade_id", "trade_ids"),
                ("ticket_id", "ticket_ids"),
                ("signal_id", "signal_ids"),
            ):
                value = gap.get(source_key)
                if value and value not in item[target_key]:
                    item[target_key].append(value)

        groups = sorted(
            grouped.values(),
            key=lambda item: (item["warn_count"] == 0, -int(item["count"]), item["key"]),
        )
        for item in groups:
            item["next_action"] = self._gap_group_next_action(item["root_cause"], item["gap_types"])
            item["sample_trade_ids"] = item.pop("trade_ids")[:6]
            item["sample_ticket_ids"] = item.pop("ticket_ids")[:6]
            item["sample_signal_ids"] = item.pop("signal_ids")[:6]
        return groups

    def _board_gap_groups(self, strategy_rows: list[dict]) -> list[dict]:
        grouped: dict[str, dict] = {}
        for row in strategy_rows:
            sid = row.get("strategy_id", "")
            audit = row.get("closed_loop_audit", {}) if isinstance(row, dict) else {}
            root_groups = audit.get("root_cause_groups", []) if isinstance(audit, dict) else []
            for group in root_groups:
                if not isinstance(group, dict):
                    continue
                key = str(group.get("root_cause") or group.get("key") or "unknown_gap")
                item = grouped.setdefault(
                    key,
                    {
                        "key": key,
                        "root_cause": key,
                        "label": self._gap_group_label(key),
                        "severity": "info",
                        "count": 0,
                        "warn_count": 0,
                        "info_count": 0,
                        "gap_types": [],
                        "strategy_ids": [],
                    },
                )
                item["count"] += int(group.get("count") or 0)
                item["warn_count"] += int(group.get("warn_count") or 0)
                item["info_count"] += int(group.get("info_count") or 0)
                if item["warn_count"] > 0:
                    item["severity"] = "warn"
                if sid and sid not in item["strategy_ids"]:
                    item["strategy_ids"].append(sid)
                for gap_type in group.get("gap_types", []):
                    if gap_type and gap_type not in item["gap_types"]:
                        item["gap_types"].append(gap_type)
        groups = sorted(
            grouped.values(),
            key=lambda item: (item["warn_count"] == 0, -int(item["count"]), item["key"]),
        )
        for item in groups:
            item["strategy_count"] = len(item["strategy_ids"])
            item["next_action"] = self._gap_group_next_action(item["root_cause"], item["gap_types"])
        return groups

    def _gap_root_cause(self, gap_type: str) -> str:
        artifact_missing = {
            "missing_signal_artifact",
            "missing_ticket_artifact",
            "missing_order_artifact",
            "executed_decision_without_order_artifact",
        }
        if gap_type in artifact_missing:
            return "artifact_provenance_missing"
        if gap_type in {"filled_order_without_trade", "executed_decision_without_trade"}:
            return "order_trade_reconciliation"
        if gap_type == "missing_gate_context":
            return "risk_gate_snapshot_missing"
        if gap_type == "directional_signal_without_ticket":
            return "signal_ticket_intent_gap"
        if gap_type in {"missing_entry_reason", "missing_exit_reason"}:
            return "trade_reason_missing"
        return gap_type or "unknown_gap"

    def _gap_group_label(self, key: str) -> str:
        return {
            "artifact_provenance_missing": "artifact provenance missing",
            "order_trade_reconciliation": "order/trade reconciliation",
            "risk_gate_snapshot_missing": "risk gate snapshot missing",
            "signal_ticket_intent_gap": "signal without ticket",
            "trade_reason_missing": "trade reason missing",
        }.get(key, key.replace("_", " "))

    def _gap_group_next_action(self, root_cause: str, gap_types: list[str]) -> str:
        if root_cause == "artifact_provenance_missing":
            return "repair_same_day_signal_ticket_order_artifacts"
        if root_cause == "order_trade_reconciliation":
            return "reconcile_order_trade_records"
        if root_cause == "risk_gate_snapshot_missing":
            return "attach_risk_gate_snapshot"
        if root_cause == "signal_ticket_intent_gap":
            return "verify_no_trade_signal_handling"
        if root_cause == "trade_reason_missing":
            return "backfill_entry_exit_reasons"
        return self._closed_loop_next_action("explainability_gap", gap_types)

    def _executed_trade_count_window(self, strategy_id: str, run_date: str, days: int) -> int:
        namespace = self._strategy_namespace(strategy_id)
        try:
            end = datetime.fromisoformat(run_date).date()
        except ValueError:
            return 0
        total = 0
        for offset in range(days):
            day = end - timedelta(days=offset)
            total += self._executed_trade_count_for_namespace(namespace, day.isoformat())
        return total

    def _consecutive_inactive_days(self, strategy_id: str, run_date: str, max_days: int) -> int:
        namespace = self._strategy_namespace(strategy_id)
        try:
            end = datetime.fromisoformat(run_date).date()
        except ValueError:
            return 0
        count = 0
        for offset in range(max_days):
            day = end - timedelta(days=offset)
            if self._executed_trade_count_for_namespace(namespace, day.isoformat()) >= 1:
                break
            count += 1
        return count

    def _executed_trade_count_for_namespace(self, namespace: Path, run_date: str) -> int:
        if not namespace.exists():
            return 0
        execution_ids: set[str] = set()
        decisions = load_json(namespace / "journal_decisions" / f"{run_date}.json")
        for item in decisions:
            if item.get("decision_status") in {"executed", "executed_paper"}:
                ticket_id = str(item.get("ticket_id") or item.get("order_id") or "")
                if ticket_id:
                    execution_ids.add(ticket_id)
        for rel in ("paper_orders", "demo_order_requests", "live_order_requests"):
            rows = load_json(namespace / rel / f"{run_date}.json")
            legacy = rel == "paper_orders"
            execution_ids.update(executed_record_ids(rows, legacy_count_missing_status=legacy))
        return len(execution_ids)

    def _demo_blocker(self, namespace: Path, run_date: str) -> dict:
        reconciliation = self._latest_reconciliation(namespace)
        if reconciliation:
            if reconciliation.get("suspected_naked_position"):
                return {
                    "blocked": True,
                    "status": "suspected_naked_position",
                    "reason": "suspected naked position: " + str(reconciliation.get("escalation_action") or reconciliation.get("reason_code") or ""),
                    "system_state": reconciliation.get("system_state", "BLOCKED_NAKED_POSITION_SUSPECTED"),
                    "reason_code": reconciliation.get("reason_code", "naked_position_suspected"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if reconciliation.get("confirmation_status") == "cannot_confirm":
                return {
                    "blocked": True,
                    "status": "reconciliation_unknown",
                    "reason": f"Binance demo reconciliation cannot confirm venue state: {reconciliation.get('error')}",
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_UNKNOWN"),
                    "reason_code": reconciliation.get("reason_code", "venue_state_unknown"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if reconciliation.get("error"):
                return {
                    "blocked": True,
                    "status": "reconciliation_error",
                    "reason": f"Binance demo reconciliation failed: {reconciliation.get('error')}",
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_UNKNOWN"),
                    "reason_code": reconciliation.get("reason_code", "venue_state_unknown"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if int(reconciliation.get("drift_count") or 0) > 0:
                reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in reconciliation.get("drifts", [])})
                return {
                    "blocked": True,
                    "status": "orphan_demo_position",
                    "reason": "Binance demo reconciliation drift: " + ("; ".join(reasons) if reasons else "unknown drift"),
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_DRIFT"),
                    "reason_code": reconciliation.get("reason_code", "reconciliation_drift"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }

        request = self._latest_demo_request(namespace, run_date)
        if request:
            receipt = request.get("receipt", {}) if isinstance(request.get("receipt"), dict) else {}
            broker_response = request.get("broker_response", {}) if isinstance(request.get("broker_response"), dict) else {}
            protective_status = str(broker_response.get("protective_status") or "")
            if request.get("status") == "blocked":
                guard = request.get("guard", {}) if isinstance(request.get("guard"), dict) else {}
                return {
                    "blocked": True,
                    "status": "demo_order_blocked",
                    "reason": guard.get("block_reason") or request.get("block_reason") or "Binance demo order blocked",
                    "artifact": str(namespace / "demo_order_requests" / f"{run_date}.json"),
                    "request": request,
                }
            if protective_status in {"failed", "partial"} or receipt.get("status") == "protective_order_missing":
                emergency = broker_response.get("emergency_close", {}) if isinstance(broker_response.get("emergency_close"), dict) else {}
                local = emergency.get("local_mirror", {}) if isinstance(emergency.get("local_mirror"), dict) else {}
                if emergency.get("status") == "closed" and local.get("closed") is True:
                    return {"blocked": False, "status": "protective_failure_emergency_closed", "reason": ""}
                return {
                    "blocked": True,
                    "status": "protective_order_missing",
                    "reason": f"Binance demo protective order status is {protective_status or receipt.get('status')}",
                    "artifact": str(namespace / "demo_order_requests" / f"{run_date}.json"),
                    "request": request,
                }
        return {"blocked": False, "status": "clear", "reason": ""}

    def _latest_reconciliation(self, namespace: Path) -> dict:
        rows = load_json(namespace / "live_reconciliation" / "current.json")
        if not rows:
            return {}
        item = rows[-1]
        return item if isinstance(item, dict) else {}

    def _latest_demo_request(self, namespace: Path, run_date: str) -> dict:
        candidates = [
            namespace / "demo_order_requests" / f"{run_date}.json",
            namespace / "demo_order_requests" / "current.json",
        ]
        for path in candidates:
            rows = load_json(path)
            if rows:
                item = rows[-1]
                return item if isinstance(item, dict) else {}
        return {}

    def _trade_trace_artifacts(
        self,
        *,
        artifact_root: Path | None = None,
        run_date: str,
        trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        decisions: list[dict],
        risk_blocks: list[dict],
        orders: list[dict],
    ) -> dict:
        """Load same-day artifacts plus cross-day rows directly referenced by trades.

        Strategy detail often shows current open trades that were opened on prior
        UTC days. Auditing those trades against only `run_date` artifacts creates
        false missing_signal/missing_ticket gaps. Keep the run-date rows for
        same-day signal diagnostics, then pull only referenced historical rows.
        """
        trade_rows = [item for item in trades if isinstance(item, dict)]
        signal_ids = {item.get("signal_id") for item in trade_rows if item.get("signal_id")}
        ticket_ids = {item.get("ticket_id") for item in trade_rows if item.get("ticket_id")}
        order_ids = {item.get("order_id") for item in trade_rows if item.get("order_id")}
        root = artifact_root or self.output_root
        candidate_dates = {run_date}
        for trade in trade_rows:
            for key in ("opened_at", "closed_at"):
                date_value = self._date_from_timestamp(trade.get(key))
                if date_value:
                    candidate_dates.add(date_value)
            for key in ("signal_id", "ticket_id", "order_id"):
                candidate_dates.update(self._dates_from_identifier(trade.get(key)))

        def matching(path: Path, current: list[dict], predicate) -> list[dict]:
            rows = [item for item in current if isinstance(item, dict)]
            for date_value in sorted(candidate_dates):
                path_for_date = path / f"{date_value}.json"
                if not path_for_date.exists():
                    continue
                loaded = load_json(path_for_date)
                for item in loaded if isinstance(loaded, list) else []:
                    if isinstance(item, dict) and predicate(item, date_value):
                        rows.append(item)
            return self._dedupe_artifact_rows(rows)

        return {
            "signals": matching(
                root / "signals",
                signals if isinstance(signals, list) else [],
                lambda item, date_value: date_value == run_date or item.get("signal_id") in signal_ids,
            ),
            "tickets": matching(
                root / "trade_tickets",
                tickets if isinstance(tickets, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "decisions": matching(
                root / "journal_decisions",
                decisions if isinstance(decisions, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "risk_blocks": matching(
                root / "risk_blocks",
                risk_blocks if isinstance(risk_blocks, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "orders": matching(
                root / "paper_orders",
                orders if isinstance(orders, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("order_id") in order_ids
                or item.get("ticket_id") in ticket_ids,
            ),
        }

    @classmethod
    def _dates_from_identifier(cls, value: object) -> set[str]:
        text = str(value or "")
        out: set[str] = set()
        for match in cls._DATE_TOKEN_PATTERN.finditer(text):
            year, month, day = match.groups()
            try:
                out.add(datetime(int(year), int(month), int(day), tzinfo=timezone.utc).date().isoformat())
            except ValueError:
                continue
        return out

    @staticmethod
    def _date_from_timestamp(value: object) -> str:
        if not value:
            return ""
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _dedupe_artifact_rows(rows: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: set[tuple] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key = (
                row.get("signal_id") or "",
                row.get("ticket_id") or "",
                row.get("order_id") or "",
                row.get("trade_id") or "",
                row.get("generated_at") or row.get("created_at") or row.get("decided_at") or row.get("filled_at") or "",
            )
            if not any(key):
                key = ("row", index, repr(sorted(row.items())))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out
