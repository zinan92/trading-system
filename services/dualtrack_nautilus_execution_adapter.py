from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT
from services.dualtrack_execution_contract import canonical_market_event
from services.dualtrack_shadow_input import build_shadow_input
from services.journal_store import load_json, write_json


ReplayExecutor = Callable[[Path, Path, Path], dict[str, Any]]


class NautilusExecutionAdapter:
    """Event-sourced, paper-only Nautilus adapter with durable normalized state."""

    name = "nautilus_paper"

    def __init__(
        self,
        output_root: Path,
        *,
        nautilus_python: str | Path,
        preflight_path: str | Path | None = None,
        replay_executor: ReplayExecutor | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "nautilus_paper"
        self.nautilus_python = Path(nautilus_python)
        self.preflight_path = Path(
            preflight_path
            or self.output_root / "dualtrack" / "nautilus" / "instrument_preflight.json"
        )
        self._replay_executor = replay_executor or self._subprocess_replay
        self._validate_runtime()

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        normalized = _canonical_command(command)
        cycle_id = normalized["cycle_id"]
        path = self._commands_path(cycle_id)
        rows = load_json(path)
        command_id = normalized["command_id"]
        existing = next((row for row in rows if row.get("command_id") == command_id), None)
        if existing:
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
            write_json(path, rows)
        snapshot = self._replay(cycle_id)
        processed_rows.append({"cycle_id": cycle_id, "event_id": event_id})
        write_json(self._processed_events_path(cycle_id), processed_rows)
        return {
            "status": "replayed",
            "event_id": event_id,
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
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": [_order_receipt(row) for row in commands],
            "fills": [],
            "positions": [],
            "account": {"margin": 0.0, "exposure": 0.0, "slippage": 0.0},
            "pnl": {"realized": 0.0, "unrealized": 0.0},
            "mark": {"price": None, "fresh": False, "source": ""},
            "capabilities": {
                "native_order_lifecycle": True,
                "paper_only": True,
                "event_sourced_restart": True,
                "browser_mark_ignored": True,
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
            },
        }

    def _replay(self, cycle_id: str) -> dict[str, Any]:
        events = load_json(self._events_path(cycle_id))
        commands = load_json(self._commands_path(cycle_id))
        bundle = build_shadow_input(
            cycle_id=cycle_id,
            authoritative_snapshot={"cycle_id": cycle_id, "engine": self.name, "fills": []},
            market_events=events,
            commands=commands,
        )
        input_path = self.root / "inputs" / f"{cycle_id}.json"
        output_path = self.root / "replays" / f"{cycle_id}.json"
        write_json(input_path, [bundle])
        snapshot = self._replay_executor(self.preflight_path, input_path, output_path)
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
            },
        }
        self._persist_snapshot(cycle_id, normalized)
        return normalized

    def _persist_snapshot(self, cycle_id: str, snapshot: dict[str, Any]) -> None:
        write_json(self._orders_path(cycle_id), list(snapshot.get("orders") or []))
        write_json(self._fills_path(cycle_id), list(snapshot.get("fills") or []))
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
        if self._replay_executor == self._subprocess_replay and not self.nautilus_python.exists():
            raise RuntimeError("isolated Nautilus Python runtime is missing")

    def _commands_path(self, cycle_id: str) -> Path:
        return self.root / "commands" / f"{cycle_id}.json"

    def _events_path(self, cycle_id: str) -> Path:
        return self.root / "events" / f"{cycle_id}.json"

    def _processed_events_path(self, cycle_id: str) -> Path:
        return self.root / "processed_events" / f"{cycle_id}.json"

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
    if event not in {"entry", "exit", "stop", "target", "flatten"}:
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
    return {
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
