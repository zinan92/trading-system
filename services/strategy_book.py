from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import write_json


STRATEGY_BOOK_VERSION = "strategy-book-v1"


class StrategyBook:
    """M2 per-strategy accounting book.

    This is the PM/trader source of truth for one strategy's paper portfolio:
    identity, NAV accounting, positions, exposure, reconciliation, and audit.
    It is intentionally defensive: unreadable artifacts become book failures,
    not stale or invented numbers.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        strategy_id: str,
        strategy_config: dict | None = None,
        starting_equity: float = 10_000.0,
    ) -> None:
        self.output_root = Path(output_root)
        self.strategy_id = strategy_id or self.output_root.name
        self.strategy_config = strategy_config or {}
        self.starting_equity = float(starting_equity or self.strategy_config.get("starting_equity", 10_000.0) or 10_000.0)
        self.read_errors: list[dict] = []

    def build(self, run_date: str, persist: bool = True) -> dict:
        performance = self._latest_mapping(self.output_root / "performance" / "current.json")
        equity = self._latest_mapping(self.output_root / "equity_curve" / "current.json")
        positions = self._mapping(self.output_root / "paper_positions" / "current.json")
        reconciliation = self._latest_mapping(self.output_root / "paper_reconciliation" / "current.json")
        open_trades = self._list(self.output_root / "paper_trades" / "current.json")
        closed_today = self._list(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        closed_all = self._closed_all()
        summary = performance.get("summary", {}) if isinstance(performance, dict) else {}
        accounting = self._accounting(summary, equity)
        position_summary = self._positions(positions, open_trades)
        audit = self._audit(accounting, position_summary, reconciliation)
        payload = {
            "schema_version": STRATEGY_BOOK_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "identity": self._identity(),
            "accounting": accounting,
            "positions": position_summary,
            "trades": {
                "open_trade_count": len(open_trades),
                "closed_today_count": len(closed_today),
                "closed_all_count": len(closed_all),
                "open_trade_ids": [str(item.get("trade_id", "")) for item in open_trades if isinstance(item, dict)][:12],
                "closed_today_trade_ids": [str(item.get("trade_id", "")) for item in closed_today if isinstance(item, dict)][:12],
            },
            "reconciliation": {
                "status": reconciliation.get("status", "missing") if isinstance(reconciliation, dict) else "missing",
                "summary": reconciliation.get("summary", {}) if isinstance(reconciliation, dict) else {},
                "failed_checks": [
                    item
                    for item in (reconciliation.get("checks", []) if isinstance(reconciliation, dict) else [])
                    if isinstance(item, dict) and item.get("status") == "fail"
                ][:8],
            },
            "audit": audit,
            "source_artifacts": {
                "performance": str(self.output_root / "performance" / "current.json"),
                "equity_curve": str(self.output_root / "equity_curve" / "current.json"),
                "positions": str(self.output_root / "paper_positions" / "current.json"),
                "open_trades": str(self.output_root / "paper_trades" / "current.json"),
                "reconciliation": str(self.output_root / "paper_reconciliation" / "current.json"),
            },
        }
        if persist:
            write_json(self.output_root / "strategy_book" / "current.json", [payload])
            write_json(self.output_root / "strategy_book" / f"{run_date}.json", [payload])
        return payload

    def _identity(self) -> dict:
        classification = self.strategy_config.get("classification", {}) if isinstance(self.strategy_config, dict) else {}
        family = str(classification.get("family") or "unclassified_family")
        variant = str(self.strategy_config.get("strategy_variant") or classification.get("style") or "base_variant")
        return {
            "strategy_id": self.strategy_id,
            "trader_id": str(self.strategy_config.get("trader_id") or classification.get("trader_id") or f"trader_{family}"),
            "portfolio_id": str(self.strategy_config.get("portfolio_id") or classification.get("portfolio_id") or f"portfolio_{self.strategy_id}"),
            "strategy_family": family,
            "strategy_variant": variant,
            "timeframe": str(self.strategy_config.get("timeframe", "5m")),
            "engine": str(self.strategy_config.get("engine", "ma")),
        }

    def _accounting(self, summary: dict, equity: dict) -> dict:
        starting = self._float(equity.get("starting_equity"), self.starting_equity)
        realized = self._float(summary.get("realized_pnl_all"), 0.0)
        unrealized = self._float(summary.get("unrealized_pnl"), 0.0)
        current = self._float(equity.get("current_equity"), starting)
        expected = round(starting + realized + unrealized, 4)
        drift = round(current - expected, 4)
        return {
            "starting_equity": round(starting, 4),
            "realized_pnl_all": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "expected_equity": expected,
            "current_equity": round(current, 4),
            "equity_drift": drift,
            "current_drawdown_pct": self._float(equity.get("current_drawdown_pct"), 0.0),
            "max_drawdown_pct": self._float(equity.get("max_drawdown_pct"), 0.0),
            "point_count": int(equity.get("point_count") or len(equity.get("points", []) or []) or 0),
            "status": "pass" if abs(drift) <= 0.01 and not self.read_errors else "fail",
            "definition": "current_equity = starting_equity + realized_pnl_all + unrealized_pnl",
        }

    def _positions(self, positions: dict, open_trades: list[dict]) -> dict:
        rows = [item for item in positions.values() if isinstance(item, dict)]
        gross_notional = 0.0
        gross_quantity = 0.0
        unrealized = 0.0
        for item in rows:
            quantity = self._float(item.get("quantity"), 0.0)
            avg = self._float(item.get("avg_price"), 0.0)
            gross_quantity += abs(quantity)
            gross_notional += abs(quantity * avg)
            unrealized += self._float(item.get("unrealized_pnl"), 0.0)
        open_count = len([item for item in open_trades if isinstance(item, dict) and str(item.get("status", "open")).lower() == "open"])
        return {
            "position_count": len(rows),
            "open_trade_count": open_count,
            "gross_quantity": round(gross_quantity, 6),
            "gross_notional": round(gross_notional, 4),
            "unrealized_pnl": round(unrealized, 4),
            "status": "flat" if not rows and open_count == 0 else ("pass" if len(rows) > 0 or open_count == 0 else "fail"),
            "positions": positions,
        }

    def _audit(self, accounting: dict, positions: dict, reconciliation: dict) -> dict:
        failures = []
        if self.read_errors:
            failures.append("artifact_read_error")
        if accounting.get("status") != "pass":
            failures.append("accounting_invariant")
        if positions.get("status") == "fail":
            failures.append("positions_missing_for_open_trades")
        if reconciliation and reconciliation.get("status") == "fail":
            failures.append("paper_reconciliation_failed")
        return {
            "status": "pass" if not failures else "fail",
            "failures": failures,
            "read_errors": self.read_errors,
            "red_line": "NAV and positions must reconcile to trades; corrupt artifacts must not produce a green book",
        }

    def _latest_mapping(self, path: Path) -> dict:
        data = self._read(path)
        if isinstance(data, list):
            item = data[-1] if data else {}
            return item if isinstance(item, dict) else {}
        return data if isinstance(data, dict) else {}

    def _mapping(self, path: Path) -> dict:
        data = self._read(path)
        return data if isinstance(data, dict) else {}

    def _list(self, path: Path) -> list[dict]:
        data = self._read(path)
        return data if isinstance(data, list) else []

    def _read(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.read_errors.append({"path": str(path), "error": str(exc)})
            return None

    def _closed_all(self) -> list[dict]:
        closed_dir = self.output_root / "paper_trades" / "closed"
        if not closed_dir.exists():
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for path in sorted(closed_dir.glob("*.json")):
            for item in self._list(path):
                key = str(item.get("trade_id") or item.get("order_id") or repr(item))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(item)
        return rows

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
