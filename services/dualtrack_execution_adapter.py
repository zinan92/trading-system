from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_scoring import _trades_from_fills, apply_unrealized


@runtime_checkable
class ExecutionEngineAdapter(Protocol):
    name: str

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        ...

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        ...

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        ...

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        ...


class LegacyPaperExecutionAdapter:
    name = "legacy_paper"

    def __init__(self, output_root: Path, *, config: dict[str, Any] | None = None) -> None:
        self.output_root = Path(output_root)
        self.engine = DualTrackHumanEngine(self.output_root, config=config)

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        return self.engine.submit_order(command)

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if bool(event.get("is_synthetic")):
            raise ValueError("synthetic market data is forbidden")
        if event.get("fresh") is not True:
            raise ValueError("market event is stale")
        source = str(event.get("source") or "")
        if not source:
            raise ValueError("market event source is required")
        return self.engine.sweep_protective_exits(
            str(event.get("cycle_id") or ""),
            mark_price=event.get("price"),
            ts=event.get("ts_event"),
            source=source,
        )

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        payload = self.engine.human_payload(cycle_id)
        fills = list(payload.get("fills") or [])
        positions = apply_unrealized(
            _trades_from_fills(fills, track="human"),
            mark_price,
            mark_fresh=mark_fresh,
        )
        open_positions = [position for position in positions if position.get("status") == "open"]
        unrealized_values = [position.get("unrealized_pnl") for position in open_positions]
        unrealized = (
            sum(float(value or 0.0) for value in unrealized_values)
            if all(value is not None for value in unrealized_values)
            else None
        )
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": [],
            "fills": fills,
            "positions": positions,
            "account": dict(payload.get("account") or {}),
            "pnl": {
                "realized": round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8),
                "unrealized": None if unrealized is None else round(unrealized, 8),
            },
            "mark": {
                "price": mark_price,
                "fresh": bool(mark_fresh),
                "source": mark_source,
            },
            "capabilities": {
                "native_order_lifecycle": False,
                "protective_orders": "compatibility_sweep",
                "restart_reconciliation": "local_ledger",
            },
        }

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(cycle_id)
        fills = snapshot["fills"]
        positions = snapshot["positions"]
        issues: list[dict[str, Any]] = []
        fill_ids = [str(fill.get("fill_id") or "") for fill in fills]
        duplicate_ids = sorted({fill_id for fill_id in fill_ids if fill_id and fill_ids.count(fill_id) > 1})
        if duplicate_ids:
            issues.append({"code": "duplicate_fill_id", "fill_ids": duplicate_ids})
        matched_exit_ids = {
            str(exit_fill.get("fill_id") or "")
            for position in positions
            for exit_fill in position.get("exit_fills") or []
        }
        orphan_exit_ids = [
            str(fill.get("fill_id") or "")
            for fill in fills
            if str(fill.get("event") or "") in {"exit", "stop", "target", "flatten"}
            and str(fill.get("fill_id") or "") not in matched_exit_ids
        ]
        if orphan_exit_ids:
            issues.append({"code": "orphan_exit_fill", "fill_ids": orphan_exit_ids})
        negative_positions = [
            str(position.get("trade_id") or "")
            for position in positions
            if float(position.get("remaining_units") or 0.0) < 0
        ]
        if negative_positions:
            issues.append({"code": "negative_position_units", "trade_ids": negative_positions})
        account_realized = snapshot["account"].get("realized_pnl")
        if account_realized not in (None, "") and abs(float(account_realized) - float(snapshot["pnl"]["realized"])) > 1e-6:
            issues.append({
                "code": "account_realized_pnl_mismatch",
                "account": float(account_realized),
                "fills": float(snapshot["pnl"]["realized"]),
            })
        return {
            "schema_version": "dualtrack-execution-reconciliation-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "status": "ok" if not issues else "drift",
            "issues": issues,
            "counts": {
                "fills": len(fills),
                "positions": len(positions),
                "open_positions": sum(1 for position in positions if position.get("status") == "open"),
            },
        }


def build_execution_engine_adapter(
    output_root: Path,
    *,
    engine: str = "legacy_paper",
    config: dict[str, Any] | None = None,
) -> ExecutionEngineAdapter:
    normalized = str(engine or "legacy_paper").strip().lower()
    if normalized == "legacy_paper":
        return LegacyPaperExecutionAdapter(output_root, config=config)
    if normalized == "nautilus":
        raise RuntimeError("Nautilus adapter spike is not enabled")
    raise ValueError(f"unknown execution engine: {engine}")
