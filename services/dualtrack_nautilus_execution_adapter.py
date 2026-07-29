from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT
from services.dualtrack_execution_contract import canonical_market_event, normalize_execution_command
from services.dualtrack_config import dualtrack_config
from services.dualtrack_grid_core import GridLineLifecycle
from services.dualtrack_shadow_input import build_shadow_input
from services.journal_store import load_json, write_json
from services.risk_port import action_class_for_command, build_paper_safe_action_market_gate


ReplayExecutor = Callable[[Path, Path, Path], dict[str, Any]]
REPLAY_VERSION = "dualtrack-nautilus-replay-v8"


class NautilusExecutionAdapter:
    """Event-sourced, paper-only Nautilus adapter with durable normalized state."""

    name = "nautilus_paper"

    def __init__(
        self,
        output_root: Path,
        *,
        nautilus_python: str | Path,
        storage_namespace: str = "nautilus_paper",
        preflight_path: str | Path | None = None,
        replay_executor: ReplayExecutor | None = None,
        defer_replay: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        namespace = str(storage_namespace or "").strip()
        if not namespace or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in namespace):
            raise ValueError("Nautilus storage namespace is invalid")
        self.storage_namespace = namespace
        self.root = self.output_root / "dualtrack" / namespace
        self.nautilus_python = Path(nautilus_python)
        self.preflight_path = Path(
            preflight_path
            or self.output_root / "dualtrack" / "nautilus" / "instrument_preflight.json"
        )
        self._replay_executor = replay_executor or self._subprocess_replay
        self.defer_replay = bool(defer_replay)
        self.config = dict(config or dualtrack_config())
        self._validate_runtime()

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        normalized = _canonical_command(self._prepare_command(command))
        cycle_id = normalized["cycle_id"]
        path = self._commands_path(cycle_id)
        rows = load_json(path)
        command_id = normalized["command_id"]
        existing = next((row for row in rows if row.get("command_id") == command_id), None)
        if existing:
            incoming_authoritative_id = str(normalized["command"].get("authoritative_order_id") or "")
            existing_command = existing.get("command") if isinstance(existing.get("command"), dict) else {}
            if incoming_authoritative_id and not existing_command.get("authoritative_order_id"):
                existing["command"] = {**existing_command, "authoritative_order_id": incoming_authoritative_id}
                write_json(path, rows)
            return _order_receipt(existing)
        row = {
            "schema_version": "dualtrack-shadow-command-v1",
            "command_id": command_id,
            "cycle_id": cycle_id,
            "command": normalized["command"],
        }
        rows.append(row)
        write_json(path, rows)
        self._persist_accepted_orders(cycle_id, rows)
        return _order_receipt(row)

    def _prepare_command(self, command: dict[str, Any]) -> dict[str, Any]:
        prepared = normalize_execution_command(command, self.config)
        event = str(prepared.get("event") or "entry").lower()
        if event not in {"exit", "stop", "target", "flatten"}:
            return prepared
        cycle_id = str(prepared.get("cycle_id") or "")
        target = self._resolve_open_position(cycle_id, prepared)
        if target is None:
            raise ValueError("Nautilus close command could not resolve exactly one open position")
        target_side = str(target.get("side") or "").lower()
        if target_side not in {"buy", "long", "sell", "short"}:
            raise ValueError("Nautilus target position side is invalid")
        expected_side = "sell" if target_side in {"buy", "long"} else "buy"
        if str(prepared.get("side") or "").lower() != expected_side:
            raise ValueError("Nautilus close side does not reduce the target position")
        prepared["target_position_id"] = str(target.get("position_id") or "")
        prepared["target_command_id"] = str(target.get("trade_id") or "")
        remaining_before = float(target.get("remaining_units") or 0.0)
        if prepared.get("quantity") in (None, "") and prepared.get("contracts") in (None, ""):
            prepared["quantity"] = remaining_before
        requested = float(prepared.get("quantity") or prepared.get("contracts") or 0.0)
        if requested <= 0 or requested > remaining_before + 1e-9:
            raise ValueError("Nautilus close quantity exceeds the target position")
        prepared["target_remaining_before"] = remaining_before
        prepared["target_remaining_after"] = max(0.0, remaining_before - requested)
        return prepared

    def _resolve_open_position(self, cycle_id: str, command: dict[str, Any]) -> dict[str, Any] | None:
        positions = [
            row
            for row in self.snapshot(cycle_id).get("positions") or []
            if str(row.get("status") or "") == "open"
        ]
        trade_ids = {
            str(command.get("trade_id") or ""),
            str(command.get("target_command_id") or ""),
        } - {""}
        position_ids = {
            str(command.get("position_id") or ""),
            str(command.get("target_position_id") or ""),
        } - {""}
        exact = []
        if trade_ids:
            exact = [row for row in positions if str(row.get("trade_id") or "") in trade_ids]
        if not exact and position_ids:
            exact = [row for row in positions if str(row.get("position_id") or "") in position_ids]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None

        plan_id = str(command.get("strategy_plan_id") or "")
        target_side = str(command.get("target_position_side") or "").lower()
        entry_price = command.get("target_entry_price")
        candidates = positions
        if plan_id:
            candidates = [row for row in candidates if str(row.get("strategy_plan_id") or "") == plan_id]
        if target_side:
            candidates = [row for row in candidates if str(row.get("side") or "").lower() == target_side]
        if entry_price not in (None, ""):
            expected = float(entry_price)
            candidates = [
                row for row in candidates
                if abs(float(row.get("entry_price") or 0.0) - expected) <= max(1e-8, abs(expected) * 1e-8)
            ]
        return candidates[0] if len(candidates) == 1 else None

    def cancel_orders(
        self,
        cycle_id: str,
        *,
        order_ids: list[str] | None = None,
        strategy_plan_id: str | None = None,
        ts: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        requested_ids = {str(value) for value in (order_ids or []) if str(value)}
        rows = load_json(self._commands_path(cycle_id))
        accepted_ids = {
            str(row.get("order_id") or "")
            for row in self.snapshot(cycle_id).get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        }
        candidates: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            command = row.get("command") if isinstance(row.get("command"), dict) else {}
            if str(command.get("event") or "entry").lower() == "cancel":
                continue
            authoritative_id = str(command.get("authoritative_order_id") or "")
            command_id = str(row.get("command_id") or "")
            if command_id not in accepted_ids:
                continue
            match_id = authoritative_id or command_id
            if requested_ids and authoritative_id not in requested_ids and command_id not in requested_ids:
                continue
            if strategy_plan_id and str(command.get("strategy_plan_id") or "") != str(strategy_plan_id):
                continue
            candidates.append((row, match_id))

        cancelled_ids: list[str] = []
        changed = False
        timestamp = str(ts or "").strip()
        if not timestamp:
            raise ValueError("Nautilus cancellation timestamp is required")
        existing_ids = {str(row.get("command_id") or "") for row in rows}
        for source_row, match_id in candidates:
            source_command = dict(source_row.get("command") or {})
            target_command_id = str(source_row.get("command_id") or "")
            cancel_source_id = f"nautilus-cancel:{target_command_id}:{timestamp}:{reason}"
            cancel_payload = {
                **source_command,
                "event": "cancel",
                "ts": timestamp,
                "cancel_order_id": target_command_id,
                "authoritative_order_id": match_id,
                "reason": str(reason or ""),
                "source_fill_id": cancel_source_id,
                "safe_action_market_gate": build_paper_safe_action_market_gate(
                    action_class_for_command({"event": "cancel"}),
                    None,
                    pricing_source="not_required",
                ),
            }
            normalized = _canonical_command(cancel_payload)
            if normalized["command_id"] not in existing_ids:
                rows.append({
                    "schema_version": "dualtrack-shadow-command-v1",
                    "command_id": normalized["command_id"],
                    "cycle_id": cycle_id,
                    "command": normalized["command"],
                })
                existing_ids.add(normalized["command_id"])
                changed = True
            cancelled_ids.append(match_id)
        if changed:
            write_json(self._commands_path(cycle_id), rows)
        return {
            "status": "cancelled" if cancelled_ids else "idempotent",
            "cycle_id": cycle_id,
            "cancelled_order_ids": cancelled_ids,
            "cancelled_order_count": len(cancelled_ids),
        }

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        normalized = canonical_market_event(event)
        if not normalized.get("provider"):
            raise ValueError("Nautilus market event provider is required")
        if not normalized.get("instrument_id"):
            raise ValueError("Nautilus market event instrument_id is required")
        cycle_id = normalized["cycle_id"]
        path = self._events_path(cycle_id)
        rows = load_json(path)
        event_id = normalized["event_id"]
        processed_rows = load_json(self._processed_events_path(cycle_id))
        processed_ids = {str(row.get("event_id") or "") for row in processed_rows}
        if event_id in processed_ids:
            return {
                "status": "idempotent",
                "event_id": event_id,
                "snapshot": self.snapshot(cycle_id),
            }
        if not any(row.get("event_id") == event_id for row in rows):
            rows.append(normalized)
            rows.sort(key=_market_event_sort_key)
            write_json(path, rows)
        if self.defer_replay:
            return {
                "status": "queued",
                "event_id": event_id,
                "pending_event_count": sum(
                    1 for row in rows if str(row.get("event_id") or "") not in processed_ids
                ),
            }
        result = self.flush(cycle_id)
        return {**result, "event_id": event_id}

    def last_market_event(self, cycle_id: str) -> dict[str, Any] | None:
        """Return the latest persisted canonical paper event without replaying it."""

        rows = load_json(self._events_path(cycle_id))
        if not rows:
            return None
        return dict(max(rows, key=_market_event_sort_key))

    def flush(self, cycle_id: str) -> dict[str, Any]:
        return self._flush(cycle_id, reject_pending_market_events=False)

    def flush_commands(self, cycle_id: str) -> dict[str, Any]:
        """Replay persisted commands only when no market event is awaiting replay.

        Control-plane mutations must never acknowledge a market event as a side
        effect.  The replay still rebuilds from the immutable event history, but
        it uses the exact event snapshot checked here; an event appended later
        remains pending for the normal market-event path.
        """

        return self._flush(cycle_id, reject_pending_market_events=True)

    def _flush(
        self,
        cycle_id: str,
        *,
        reject_pending_market_events: bool,
    ) -> dict[str, Any]:
        rows = load_json(self._events_path(cycle_id))
        processed_rows = load_json(self._processed_events_path(cycle_id))
        processed_ids = {str(row.get("event_id") or "") for row in processed_rows}
        pending_ids = [
            str(row.get("event_id") or "")
            for row in rows
            if str(row.get("event_id") or "") not in processed_ids
        ]
        if reject_pending_market_events and pending_ids:
            raise RuntimeError(
                "Nautilus command flush blocked by pending market events"
                f"; pending_event_ids={pending_ids}"
            )
        commands = load_json(self._commands_path(cycle_id))
        processed_commands = load_json(self._processed_commands_path(cycle_id))
        processed_command_ids = {str(row.get("command_id") or "") for row in processed_commands}
        pending_command_ids = [
            str(row.get("command_id") or "")
            for row in commands
            if str(row.get("command_id") or "") not in processed_command_ids
        ]
        current_snapshot = self.snapshot(cycle_id)
        current_version = str((current_snapshot.get("capabilities") or {}).get("replay_version") or "")
        requires_rebuild = bool(rows or commands) and current_version != REPLAY_VERSION
        if not pending_ids and not pending_command_ids and not requires_rebuild:
            return {
                "status": "idempotent",
                "processed_event_count": 0,
                "processed_command_count": 0,
                "snapshot": self.snapshot(cycle_id),
            }
        replay_rows, late_event_ids, execution_watermark = _execution_replay_events(
            rows,
            processed_rows,
        )
        snapshot = self._replay(
            cycle_id,
            events=replay_rows,
            commands=commands,
        )
        # Rearm appends next-generation grid commands during replay; re-read
        # the durable command log so they enter the processed bookkeeping.
        commands = load_json(self._commands_path(cycle_id))
        processed_by_id = {str(row.get("event_id") or ""): row for row in processed_rows}
        for persisted in rows:
            persisted_id = str(persisted.get("event_id") or "")
            if persisted_id and persisted_id not in processed_by_id:
                processed_row = {
                    "cycle_id": cycle_id,
                    "event_id": persisted_id,
                    "disposition": (
                        "late_ignored"
                        if persisted_id in late_event_ids
                        else "accepted"
                    ),
                    "ts_event": str(persisted.get("ts_event") or ""),
                }
                if persisted_id in late_event_ids:
                    processed_row.update(
                        {
                            "reason": "ts_event_not_after_execution_watermark",
                            "execution_watermark": execution_watermark,
                        }
                    )
                processed_rows.append(processed_row)
                processed_by_id[persisted_id] = processed_rows[-1]
        write_json(self._processed_events_path(cycle_id), processed_rows)
        processed_command_by_id = {
            str(row.get("command_id") or ""): row for row in processed_commands
        }
        for command in commands:
            command_id = str(command.get("command_id") or "")
            if command_id and command_id not in processed_command_by_id:
                processed_commands.append({"cycle_id": cycle_id, "command_id": command_id})
                processed_command_by_id[command_id] = processed_commands[-1]
        write_json(self._processed_commands_path(cycle_id), processed_commands)
        return {
            "status": "replayed",
            "replay_version": REPLAY_VERSION,
            "revision_rebuild": requires_rebuild,
            "processed_event_count": len(pending_ids),
            "processed_command_count": len(pending_command_ids),
            "orders": len(snapshot["orders"]),
            "fills": len(snapshot["fills"]),
            "positions": len(snapshot["positions"]),
            "snapshot": snapshot,
        }

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        rows = load_json(self._snapshot_path(cycle_id))
        if rows and isinstance(rows[-1], dict):
            return dict(rows[-1])
        commands = load_json(self._commands_path(cycle_id))
        visible_commands = [
            row
            for row in commands
            if str((row.get("command") or {}).get("event") or "entry").lower() != "cancel"
        ]
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": [_order_receipt(row) for row in visible_commands],
            "fills": [],
            "positions": [],
            "account": {
                "starting_cash": float(self.config["capital_per_track_usd"]),
                "realized_pnl": 0.0,
                "ending_cash": float(self.config["capital_per_track_usd"]),
                "equity": float(self.config["capital_per_track_usd"]),
                "margin": 0.0,
                "exposure": 0.0,
                "slippage": 0.0,
                "fees": 0.0,
                "funding": 0.0,
            },
            "pnl": {"realized": 0.0, "unrealized": 0.0},
            "mark": {"price": None, "fresh": False, "source": ""},
            "capabilities": {
                "native_order_lifecycle": True,
                "paper_only": True,
                "event_sourced_restart": True,
                "browser_mark_ignored": True,
                "replay_version": REPLAY_VERSION,
            },
        }

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(cycle_id)
        issues: list[dict[str, Any]] = []
        for field, path in (
            ("orders", self._orders_path(cycle_id)),
            ("fills", self._fills_path(cycle_id)),
            ("positions", self._positions_path(cycle_id)),
        ):
            persisted = load_json(path)
            expected = list(snapshot.get(field) or [])
            if persisted != expected:
                issues.append({
                    "code": f"persisted_{field}_mismatch",
                    "snapshot_count": len(expected),
                    "persisted_count": len(persisted),
                })
        fill_ids = [str(row.get("fill_id") or "") for row in snapshot.get("fills") or []]
        duplicates = sorted({fill_id for fill_id in fill_ids if fill_id and fill_ids.count(fill_id) > 1})
        if duplicates:
            issues.append({"code": "duplicate_fill_id", "fill_ids": duplicates})
        account = dict(snapshot.get("account") or {})
        pnl = dict(snapshot.get("pnl") or {})
        _check_account_identity(
            issues,
            account,
            "ending_cash",
            _sum_if_present(account, "starting_cash", "realized_pnl"),
        )
        _check_account_identity(
            issues,
            account,
            "equity",
            _sum_if_present(account, "ending_cash", None, extra=pnl.get("unrealized")),
        )
        if account.get("fees") not in (None, ""):
            fill_fees = round(sum(float(row.get("cost") or 0.0) for row in snapshot.get("fills") or []), 8)
            _check_account_identity(issues, account, "fees", fill_fees)
        if account.get("margin") not in (None, "") and account.get("exposure") not in (None, ""):
            expected_margin = round(float(account["exposure"]) / float(self.config["max_leverage"]), 8)
            _check_account_identity(issues, account, "margin", expected_margin)
        for position in snapshot.get("positions") or []:
            if position.get("status") != "open":
                continue
            if not position.get("trade_id") or not position.get("position_id"):
                issues.append({"code": "open_position_identity_missing"})
            if position.get("strategy_plan_id") in (None, ""):
                issues.append({"code": "open_position_strategy_plan_missing", "trade_id": position.get("trade_id")})
        return {
            "schema_version": "dualtrack-execution-reconciliation-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "status": "ok" if not issues else "drift",
            "issues": issues,
            "counts": {
                "orders": len(snapshot.get("orders") or []),
                "fills": len(snapshot.get("fills") or []),
                "positions": len(snapshot.get("positions") or []),
                "open_positions": sum(
                    1 for row in snapshot.get("positions") or [] if row.get("status") == "open"
                ),
            },
        }

    def _replay(
        self,
        cycle_id: str,
        *,
        events: list[dict[str, Any]] | None = None,
        commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        event_rows = events if events is not None else load_json(self._events_path(cycle_id))
        command_rows = (
            commands if commands is not None else load_json(self._commands_path(cycle_id))
        )
        events = sorted(event_rows, key=_market_event_sort_key)
        commands = sorted(command_rows, key=_command_sort_key)
        bundle = build_shadow_input(
            cycle_id=cycle_id,
            authoritative_snapshot={"cycle_id": cycle_id, "engine": self.name, "fills": []},
            market_events=events,
            commands=commands,
            execution_settings={
                "starting_cash": float(self.config["capital_per_track_usd"]),
                "max_leverage": float(self.config["max_leverage"]),
            },
        )
        input_path = self.root / "inputs" / f"{cycle_id}.json"
        output_path = self.root / "replays" / f"{cycle_id}.json"
        snapshot: dict[str, Any] | None = None
        max_passes = max(1, len(events) + len(commands) + 1)
        for _pass in range(max_passes):
            bundle = build_shadow_input(
                cycle_id=cycle_id,
                authoritative_snapshot={"cycle_id": cycle_id, "engine": self.name, "fills": []},
                market_events=events,
                commands=commands,
                execution_settings={
                    "starting_cash": float(self.config["capital_per_track_usd"]),
                    "max_leverage": float(self.config["max_leverage"]),
                },
            )
            write_json(input_path, [bundle])
            snapshot = self._replay_executor(self.preflight_path, input_path, output_path)
            generated = self._next_grid_rearm_commands(cycle_id, commands, snapshot)
            if not generated:
                break
            commands = sorted([*commands, *generated], key=_command_sort_key)
            write_json(self._commands_path(cycle_id), commands)
        else:
            raise RuntimeError("Nautilus grid rearm replay did not converge")
        if snapshot is None:
            raise RuntimeError("Nautilus replay did not return a snapshot")
        if snapshot.get("schema_version") != "dualtrack-execution-v1":
            raise RuntimeError("Nautilus replay returned unsupported snapshot schema")
        if str(snapshot.get("cycle_id") or "") != cycle_id:
            raise RuntimeError("Nautilus replay returned the wrong cycle")
        normalized = {
            **snapshot,
            "engine": self.name,
            "capabilities": {
                **dict(snapshot.get("capabilities") or {}),
                "paper_only": True,
                "event_sourced_restart": True,
                "browser_mark_ignored": True,
                "replay_version": REPLAY_VERSION,
            },
        }
        if self.storage_namespace == "nautilus_authoritative":
            lifecycle = self._persist_grid_lifecycle(cycle_id, commands, snapshot)
            normalized["rearms"] = lifecycle["rearms"]
            normalized["grid_lifecycle"] = lifecycle
            normalized["capabilities"]["grid_rearm_after_target"] = True
            self._merge_future_accepted_orders(normalized, commands, events)
        self._persist_snapshot(cycle_id, normalized)
        return normalized

    def _next_grid_rearm_commands(
        self,
        cycle_id: str,
        commands: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self.storage_namespace != "nautilus_authoritative":
            return []
        command_by_id = {
            str(row.get("command_id") or ""): dict(row.get("command") or {})
            for row in commands
        }
        lines: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
        for command_id, command in command_by_id.items():
            if not _grid_rearm_entry(command):
                continue
            root_id = str(command.get("grid_line_id") or command_id)
            generation = int(command.get("grid_generation") or 1)
            lines.setdefault(root_id, []).append((generation, command_id, command))

        fills = [dict(row) for row in snapshot.get("fills") or []]
        generated: list[dict[str, Any]] = []
        for root_id, generations in sorted(lines.items()):
            generation, command_id, command = max(generations, key=lambda row: row[0])
            entry_quantity = sum(
                float(fill.get("quantity") or 0.0)
                for fill in fills
                if str(fill.get("trade_id") or "") == command_id
                and str(fill.get("event") or "") == "entry"
            )
            target_fills = [
                fill
                for fill in fills
                if str(fill.get("trade_id") or "") == command_id
                and str(fill.get("event") or "") == "target"
            ]
            target_quantity = sum(float(fill.get("quantity") or 0.0) for fill in target_fills)
            if entry_quantity <= 0 or target_quantity + 1e-9 < entry_quantity:
                continue
            target_at = max((str(fill.get("ts") or "") for fill in target_fills), default="")
            if not target_at or self._grid_plan_cancelled_after(commands, command, target_at):
                continue
            next_generation = generation + 1
            if any(item[0] == next_generation for item in generations):
                continue
            payload = {
                **command,
                "cycle_id": cycle_id,
                "ts": _after_fill_timestamp(target_at),
                "source_fill_id": f"nautilus-grid-rearm:{root_id}:generation:{next_generation}",
                "grid_line_id": root_id,
                "grid_generation": next_generation,
                "grid_rearm_enabled": True,
                "rearm_of_order_id": command_id,
            }
            normalized = _canonical_command(payload)
            generated.append({
                "schema_version": "dualtrack-shadow-command-v1",
                "command_id": normalized["command_id"],
                "cycle_id": cycle_id,
                "command": normalized["command"],
            })
        return generated

    @staticmethod
    def _grid_plan_cancelled_after(
        commands: list[dict[str, Any]],
        entry_command: dict[str, Any],
        target_at: str,
    ) -> bool:
        plan_id = str(entry_command.get("strategy_plan_id") or "")
        for row in commands:
            command = row.get("command") if isinstance(row.get("command"), dict) else {}
            if str(command.get("event") or "").lower() != "cancel":
                continue
            if plan_id and str(command.get("strategy_plan_id") or "") != plan_id:
                continue
            if str(command.get("ts") or "") >= target_at:
                return True
        return False

    def _persist_grid_lifecycle(
        self,
        cycle_id: str,
        commands: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        command_by_id = {
            str(row.get("command_id") or ""): dict(row.get("command") or {})
            for row in commands
        }
        grouped: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
        for command_id, command in command_by_id.items():
            if not _grid_rearm_entry(command):
                continue
            root_id = str(command.get("grid_line_id") or command_id)
            grouped.setdefault(root_id, []).append(
                (int(command.get("grid_generation") or 1), command_id, command)
            )
        fills = [dict(row) for row in snapshot.get("fills") or []]
        orders = {
            str(row.get("order_id") or ""): dict(row)
            for row in snapshot.get("orders") or []
        }
        rows: list[dict[str, Any]] = []
        for root_id, generations in sorted(grouped.items()):
            first = min(generations, key=lambda row: row[0])
            lifecycle = GridLineLifecycle(
                line_id=root_id,
                armed_at=str(first[2].get("ts") or ""),
                requested_quantity=float(
                    first[2].get("quantity") or first[2].get("contracts") or 0.0
                ),
            )
            for generation, command_id, command in sorted(generations):
                if lifecycle.generation != generation:
                    raise RuntimeError("Nautilus grid lifecycle generation is discontinuous")
                line_fills = sorted(
                    (
                        fill for fill in fills
                        if str(fill.get("trade_id") or "") == command_id
                    ),
                    key=lambda fill: (
                        str(fill.get("ts") or ""),
                        str(fill.get("fill_id") or ""),
                    ),
                )
                for fill in line_fills:
                    event = str(fill.get("event") or "")
                    if event == "entry":
                        lifecycle.apply_entry_fill(
                            fill_id=str(fill.get("fill_id") or ""),
                            quantity=float(fill.get("quantity") or 0.0),
                            at=str(fill.get("ts") or ""),
                        )
                    elif event in {"target", "stop", "exit", "flatten"}:
                        lifecycle.apply_close_fill(
                            fill_id=str(fill.get("fill_id") or ""),
                            quantity=float(fill.get("quantity") or 0.0),
                            at=str(fill.get("ts") or ""),
                            rearm=event == "target" and any(
                                next_generation == generation + 1
                                for next_generation, _next_id, _next_command in generations
                            ),
                            terminal_state="stopped" if event == "stop" else "closed",
                        )
                order = orders.get(command_id) or {}
                if (
                    str(order.get("state") or "") in {"canceled", "cancelled"}
                    and lifecycle.active
                    and lifecycle.open_quantity <= 1e-9
                ):
                    lifecycle.cancel(
                        at=str(
                            order.get("cancelled_at")
                            or order.get("ts")
                            or command.get("ts")
                            or ""
                        ),
                        reason="authoritative_order_cancelled",
                    )
            rows.extend(lifecycle.transitions)
        rows.sort(
            key=lambda row: (
                str(row.get("at") or ""),
                str(row.get("line_id") or ""),
                int(row.get("sequence") or 0),
            )
        )
        path = self.output_root / "dualtrack" / "grid_lifecycle" / f"{cycle_id}_nautilus.json"
        write_json(path, rows)
        return {
            "schema_version": "dualtrack-grid-lifecycle-summary-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "line_count": len(grouped),
            "rearms": sum(row.get("event") == "close_fill_confirmed_rearm" for row in rows),
            "audit_path": str(path),
        }

    @staticmethod
    def _merge_future_accepted_orders(
        snapshot: dict[str, Any],
        commands: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> None:
        if not events:
            return
        last_event_at = max(str(event.get("ts_event") or "") for event in events)
        orders = list(snapshot.get("orders") or [])
        known_ids = {str(order.get("order_id") or "") for order in orders}
        for row in commands:
            command_id = str(row.get("command_id") or "")
            command = row.get("command") if isinstance(row.get("command"), dict) else {}
            if (
                command_id
                and command_id not in known_ids
                and _grid_rearm_entry(command)
                and str(command.get("ts") or "") > last_event_at
            ):
                orders.append(_order_receipt(row))
                known_ids.add(command_id)
        snapshot["orders"] = orders

    def _persist_snapshot(self, cycle_id: str, snapshot: dict[str, Any]) -> None:
        previous_fills = load_json(self._fills_path(cycle_id))
        next_fills = list(snapshot.get("fills") or [])
        if previous_fills:
            previous_by_id = {
                _fill_business_key(row): row
                for row in previous_fills
                if isinstance(row, dict) and _fill_business_key(row)
            }
            next_by_id = {
                _fill_business_key(row): row
                for row in next_fills
                if isinstance(row, dict) and _fill_business_key(row)
            }
            missing = sorted(set(previous_by_id) - set(next_by_id))
            changed = sorted(
                fill_id
                for fill_id in set(previous_by_id) & set(next_by_id)
                if _fill_business_value(previous_by_id[fill_id])
                != _fill_business_value(next_by_id[fill_id])
            )
            if missing or changed or len(previous_by_id) != len(previous_fills):
                raise RuntimeError(
                    "immutable fill history regressed"
                    f"; missing={missing}; changed={changed}"
                )
        write_json(self._orders_path(cycle_id), list(snapshot.get("orders") or []))
        write_json(self._fills_path(cycle_id), next_fills)
        write_json(self._positions_path(cycle_id), list(snapshot.get("positions") or []))
        write_json(self.root / "accounts" / f"{cycle_id}.json", [dict(snapshot.get("account") or {})])
        write_json(self._snapshot_path(cycle_id), [snapshot])

    def _persist_accepted_orders(self, cycle_id: str, commands: list[dict[str, Any]]) -> None:
        receipts = [_order_receipt(row) for row in commands]
        snapshot_rows = load_json(self._snapshot_path(cycle_id))
        if snapshot_rows and isinstance(snapshot_rows[-1], dict):
            snapshot = dict(snapshot_rows[-1])
            orders = list(snapshot.get("orders") or [])
            known_ids = {str(order.get("order_id") or "") for order in orders}
            orders.extend(order for order in receipts if str(order.get("order_id") or "") not in known_ids)
            snapshot["orders"] = orders
            self._persist_snapshot(cycle_id, snapshot)
            return
        write_json(self._orders_path(cycle_id), receipts)

    def _subprocess_replay(self, preflight_path: Path, input_path: Path, output_path: Path) -> dict[str, Any]:
        script = ROOT / "spikes" / "dualtrack_nautilus_shadow_replay.py"
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(ROOT) if not existing else f"{ROOT}{os.pathsep}{existing}"
        result = subprocess.run(
            [
                str(self.nautilus_python),
                str(script),
                "--preflight",
                str(preflight_path),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Nautilus replay failed: {result.stderr[-1000:]}")
        rows = load_json(output_path)
        if not rows or not isinstance(rows[-1], dict):
            raise RuntimeError("Nautilus replay did not persist a snapshot")
        return dict(rows[-1])

    def _validate_runtime(self) -> None:
        preflight = load_json(self.preflight_path)
        artifact = preflight[-1] if preflight else {}
        if artifact.get("status") != "ready_for_paper_shadow":
            raise RuntimeError("Nautilus instrument preflight is not ready for paper shadow")
        fee_model = artifact.get("fee_model") or {}
        if fee_model.get("real_money_eligible") is not False:
            raise RuntimeError("Nautilus fee model must be explicitly paper-only")
        if fee_model.get("mode") != "account_observed":
            raise RuntimeError("Nautilus persistent paper adapter requires account-observed fees")
        for field in ("maker_fee_rate", "taker_fee_rate", "funding_rate", "funding_time", "observed_at"):
            if fee_model.get(field) in (None, ""):
                raise RuntimeError(f"Nautilus account cost evidence is missing {field}")
        configured_fees = self.config.get("paper_fee_model") if isinstance(self.config.get("paper_fee_model"), dict) else {}
        if configured_fees:
            for field in ("maker_fee_rate", "taker_fee_rate"):
                if float(configured_fees.get(field, -1)) != float(fee_model[field]):
                    raise RuntimeError(f"legacy and Nautilus paper fee models differ for {field}")
        if self._replay_executor == self._subprocess_replay and not self.nautilus_python.exists():
            raise RuntimeError("isolated Nautilus Python runtime is missing")

    def _commands_path(self, cycle_id: str) -> Path:
        return self.root / "commands" / f"{cycle_id}.json"

    def _events_path(self, cycle_id: str) -> Path:
        return self.root / "events" / f"{cycle_id}.json"

    def _processed_events_path(self, cycle_id: str) -> Path:
        return self.root / "processed_events" / f"{cycle_id}.json"

    def _processed_commands_path(self, cycle_id: str) -> Path:
        return self.root / "processed_commands" / f"{cycle_id}.json"

    def _orders_path(self, cycle_id: str) -> Path:
        return self.root / "orders" / f"{cycle_id}.json"

    def _fills_path(self, cycle_id: str) -> Path:
        return self.root / "fills" / f"{cycle_id}.json"

    def _positions_path(self, cycle_id: str) -> Path:
        return self.root / "positions" / f"{cycle_id}.json"

    def _snapshot_path(self, cycle_id: str) -> Path:
        return self.root / "snapshots" / f"{cycle_id}.json"


def _canonical_command(command: dict[str, Any]) -> dict[str, Any]:
    cycle_id = str(command.get("cycle_id") or "").strip()
    timestamp = str(command.get("ts") or "").strip()
    side = str(command.get("side") or "").lower()
    event = str(command.get("event") or "entry").lower()
    order_type = str(command.get("order_type") or "market").lower()
    price = float(command.get("price") or 0.0)
    quantity_value = command.get("quantity", command.get("contracts"))
    quantity = float(quantity_value) if quantity_value not in (None, "") else (
        float(command.get("notional") or 0.0) / price if price > 0 else 0.0
    )
    if not cycle_id or not timestamp:
        raise ValueError("Nautilus order requires cycle_id and timestamp")
    if side not in {"buy", "sell"}:
        raise ValueError("Nautilus order side must be buy or sell")
    if event not in {"entry", "exit", "stop", "target", "flatten", "cancel"}:
        raise ValueError("Nautilus order event is unsupported")
    if order_type not in {"market", "limit"}:
        raise ValueError("Nautilus order type must be market or limit")
    if price <= 0 or quantity <= 0:
        raise ValueError("Nautilus order price and quantity must be positive")
    safe = {
        key: value
        for key, value in command.items()
        if key not in {"authorization", "credential", "secret", "token", "password"}
    }
    safe.update({
        "cycle_id": cycle_id,
        "ts": timestamp,
        "side": side,
        "event": event,
        "order_type": order_type,
        "price": price,
        "quantity": quantity,
    })
    source_id = str(command.get("source_fill_id") or command.get("external_fill_id") or "")
    identity = source_id or json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "command_id": f"nautilus-command-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
        "cycle_id": cycle_id,
        "command": safe,
    }


def _order_receipt(row: dict[str, Any]) -> dict[str, Any]:
    command = dict(row.get("command") or {})
    receipt = {
        "schema_version": "dualtrack-nautilus-order-receipt-v1",
        "order_id": str(row.get("command_id") or ""),
        "cycle_id": str(row.get("cycle_id") or command.get("cycle_id") or ""),
        "state": "accepted",
        "side": str(command.get("side") or ""),
        "event": str(command.get("event") or "entry"),
        "order_type": str(command.get("order_type") or "market"),
        "price": float(command.get("price") or 0.0),
        "quantity": float(command.get("quantity") or 0.0),
        "engine": "nautilus_paper",
    }
    for field in ("ts", "sl", "tp", "strategy_plan_id", "strategy_plan_version"):
        if command.get(field) not in (None, ""):
            receipt[field] = command[field]
    receipt["requested_price"] = float(command.get("requested_price") or command.get("price") or 0.0)
    receipt["requested_quantity"] = float(
        command.get("requested_quantity") or command.get("quantity") or 0.0
    )
    return receipt


def _market_event_sort_key(row: dict[str, Any]) -> str:
    return str(row.get("ts_event") or "")


def _execution_replay_events(
    events: list[dict[str, Any]],
    processed_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], str]:
    """Keep late-arriving candles as audit facts without revising execution history."""

    event_by_id = {
        str(row.get("event_id") or ""): row
        for row in events
        if str(row.get("event_id") or "")
    }
    ignored_ids = {
        str(row.get("event_id") or "")
        for row in processed_events
        if str(row.get("disposition") or "") == "late_ignored"
    }
    accepted_processed = [
        event_by_id[event_id]
        for event_id in (
            str(row.get("event_id") or "")
            for row in processed_events
            if str(row.get("disposition") or "") != "late_ignored"
        )
        if event_id in event_by_id
    ]
    watermark = max(
        (_market_event_sort_key(row) for row in accepted_processed),
        default="",
    )
    processed_ids = {
        str(row.get("event_id") or "")
        for row in processed_events
        if str(row.get("event_id") or "")
    }
    late_ids = {
        event_id
        for event_id, event in event_by_id.items()
        if event_id not in processed_ids
        and bool(watermark)
        and _market_event_sort_key(event) <= watermark
    }
    replay_events = [
        row
        for row in events
        if str(row.get("event_id") or "") not in ignored_ids | late_ids
    ]
    return replay_events, late_ids, watermark


def _command_sort_key(row: dict[str, Any]) -> str:
    command = row.get("command") if isinstance(row.get("command"), dict) else {}
    return str(command.get("ts") or "")


def _grid_rearm_entry(command: dict[str, Any]) -> bool:
    if str(command.get("event") or "entry").lower() != "entry":
        return False
    if str(command.get("order_type") or "market").lower() != "limit":
        return False
    if command.get("sl") in (None, "") or command.get("tp") in (None, ""):
        return False
    if command.get("grid_rearm_enabled") is True:
        return bool(str(command.get("grid_line_id") or ""))
    return (
        str(command.get("source") or "") == "strategy_production_console"
        and str(command.get("source_fill_id") or "").startswith("strategy-grid:")
        and bool(str(command.get("strategy_plan_id") or ""))
    )


def _after_fill_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed + timedelta(microseconds=1)).isoformat()


def _fill_business_key(row: dict[str, Any]) -> str:
    """Return the stable fill identity exposed by the execution contract."""
    return str(row.get("order_id") or row.get("fill_id") or "")


def _fill_business_value(row: dict[str, Any]) -> dict[str, Any]:
    """Ignore replay-local IDs while protecting every economic fill field."""
    return {key: value for key, value in row.items() if key != "fill_id"}


def _sum_if_present(
    values: dict[str, Any],
    first: str,
    second: str | None,
    *,
    extra: Any = None,
) -> float | None:
    operands = [values.get(first), values.get(second) if second else extra]
    if any(value in (None, "") for value in operands):
        return None
    return round(sum(float(value) for value in operands), 8)


def _check_account_identity(
    issues: list[dict[str, Any]],
    account: dict[str, Any],
    field: str,
    expected: float | None,
) -> None:
    if expected is None or account.get(field) in (None, ""):
        return
    actual = round(float(account[field]), 8)
    if abs(actual - expected) > 1e-8:
        issues.append({
            "code": f"account_{field}_mismatch",
            "expected": expected,
            "actual": actual,
        })
