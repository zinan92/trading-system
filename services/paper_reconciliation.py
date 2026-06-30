from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class PaperReconciliation:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def run(self, run_date: str) -> dict:
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        known_orders = self._known_orders_through(run_date)
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        performance_rows = load_json(self.output_root / "performance" / "current.json")
        performance = performance_rows[-1] if performance_rows else {}
        equity_rows = load_json(self.output_root / "equity_curve" / "current.json")
        equity_curve = equity_rows[-1] if equity_rows else {}
        checks = [
            self._orders_have_decisions(orders, decisions),
            self._filled_orders_have_trades(orders, open_trades, closed_trades),
            self._trades_have_orders(open_trades, closed_trades, known_orders),
            self._positions_match_open_trades(open_trades, positions),
        ]
        accounting = self._accounting_invariant(performance, equity_curve)
        if accounting is not None:
            checks.append(accounting)
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "summary": {
                "orders": len(orders),
                "known_orders": len(known_orders),
                "filled_orders": sum(1 for item in orders if item.get("status") == "filled"),
                "open_trades": len(open_trades),
                "closed_trades": len(closed_trades),
                "positions": len(positions),
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
            },
            "checks": checks,
            "computed_positions": self._positions_from_open_trades(open_trades),
            "source_artifacts": {
                "paper_orders": str(self.output_root / "paper_orders" / f"{run_date}.json"),
                "paper_trades": str(self.output_root / "paper_trades" / "current.json"),
                "paper_positions": str(self.output_root / "paper_positions" / "current.json"),
                "journal_decisions": str(self.output_root / "journal_decisions" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "paper_reconciliation" / "current.json", [payload])
        write_json(self.output_root / "paper_reconciliation" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _orders_have_decisions(self, orders: list[dict], decisions: list[dict]) -> dict:
        executed = [item for item in decisions if item.get("decision_status") == "executed_paper"]
        decision_order_ids = {((item.get("paper_order") or {}).get("order_id")) for item in executed}
        missing = [item.get("order_id") for item in orders if item.get("status") == "filled" and item.get("order_id") not in decision_order_ids]
        if missing:
            return self._check("orders_have_decisions", "fail", "Filled paper orders missing executed_paper journal decisions.", {"missing_order_ids": missing})
        return self._check("orders_have_decisions", "pass", "Filled paper orders are linked to executed_paper journal decisions.", {"executed_decisions": len(executed)})

    def _filled_orders_have_trades(self, orders: list[dict], open_trades: list[dict], closed_trades: list[dict]) -> dict:
        trade_order_ids = {item.get("order_id") for item in [*open_trades, *closed_trades]}
        missing = [item.get("order_id") for item in orders if item.get("status") == "filled" and item.get("order_id") not in trade_order_ids]
        if missing:
            return self._check("filled_orders_have_trades", "fail", "Filled paper orders missing trade records.", {"missing_order_ids": missing})
        return self._check("filled_orders_have_trades", "pass", "Filled paper orders have open or closed trade records.", {"trade_order_ids": sorted([item for item in trade_order_ids if item])})

    def _trades_have_orders(self, open_trades: list[dict], closed_trades: list[dict], orders: list[dict]) -> dict:
        order_ids = {item.get("order_id") for item in orders}
        missing = [item.get("trade_id") for item in [*open_trades, *closed_trades] if item.get("order_id") not in order_ids]
        if missing:
            return self._check("trades_have_orders", "fail", "Trade records reference missing paper orders.", {"missing_trade_ids": missing})
        return self._check("trades_have_orders", "pass", "Trade records reference existing paper orders.", {"trade_count": len(open_trades) + len(closed_trades)})

    def _positions_match_open_trades(self, open_trades: list[dict], positions: dict) -> dict:
        computed = self._positions_from_open_trades(open_trades)
        mismatches = []
        for symbol, expected in computed.items():
            actual = positions.get(symbol, {})
            if not actual:
                mismatches.append({"symbol": symbol, "reason": "missing_position", "expected": expected, "actual": actual})
                continue
            for key in ["side", "quantity", "avg_price"]:
                if key == "side":
                    if str(actual.get(key, "")) != str(expected.get(key, "")):
                        mismatches.append({"symbol": symbol, "field": key, "expected": expected.get(key), "actual": actual.get(key)})
                else:
                    if abs(float(actual.get(key, 0) or 0) - float(expected.get(key, 0) or 0)) > 0.01:
                        mismatches.append({"symbol": symbol, "field": key, "expected": expected.get(key), "actual": actual.get(key)})
        extra = [symbol for symbol, item in positions.items() if symbol not in computed and float(item.get("quantity", 0) or 0) > 0]
        if extra:
            mismatches.append({"reason": "position_without_open_trades", "symbols": extra})
        if mismatches:
            return self._check("positions_match_open_trades", "fail", "Paper positions do not reconcile with open trades.", {"mismatches": mismatches, "computed_positions": computed, "positions": positions})
        return self._check("positions_match_open_trades", "pass", "Paper positions reconcile with open trades.", {"computed_positions": computed})

    def _positions_from_open_trades(self, open_trades: list[dict]) -> dict:
        grouped: dict[str, dict] = {}
        for trade in open_trades:
            if trade.get("status", "open") != "open":
                continue
            symbol = str(trade.get("symbol", "GOLD"))
            side = str(trade.get("side", "long"))
            quantity = float(trade.get("quantity", 0) or 0)
            entry = float(trade.get("entry_price", 0) or 0)
            current = grouped.setdefault(symbol, {"symbol": symbol, "side": side, "quantity": 0.0, "notional": 0.0, "sides": set()})
            current["sides"].add(side)
            current["quantity"] += quantity
            current["notional"] += quantity * entry
        result = {}
        for symbol, item in grouped.items():
            quantity = float(item["quantity"])
            sides = sorted(item.get("sides", set()))
            result[symbol] = {
                "symbol": symbol,
                "side": item["side"] if len(sides) <= 1 else "mixed",
                "sides": sides,
                "quantity": round(quantity, 6),
                "avg_price": round(float(item["notional"]) / quantity, 4) if quantity else 0.0,
            }
        return result

    def _known_orders_through(self, run_date: str) -> list[dict]:
        orders_dir = self.output_root / "paper_orders"
        if not orders_dir.exists():
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for path in sorted(orders_dir.glob("*.json")):
            if path.name == "current.json" or path.stem > run_date:
                continue
            for order in load_json(path):
                order_id = order.get("order_id")
                if order_id in seen:
                    continue
                seen.add(order_id)
                rows.append(order)
        return rows

    def _accounting_invariant(self, performance: dict, equity_curve: dict, tolerance: float = 0.01) -> dict | None:
        """Hard-check the canonical paper accounting identity:

            equity == starting_equity + realized_pnl_all + unrealized_pnl

        Execution costs are already netted into realized/unrealized, so they are
        NOT subtracted again here (subtracting them twice is the exact bug the
        dashboard NAV curve had). Also verifies the reported cost components add
        up. Returns None when there is no paper accounting data yet, so a purely
        structural reconciliation stays backward compatible.
        """
        if not performance or not equity_curve:
            return None
        summary = performance.get("summary", {}) if performance else {}
        starting = float(equity_curve.get("starting_equity", 0) or 0)
        realized = float(summary.get("realized_pnl_all", 0) or 0)
        unrealized = float(summary.get("unrealized_pnl", 0) or 0)
        open_costs = float(summary.get("open_costs", 0) or 0)
        closed_costs = float(summary.get("closed_costs", 0) or 0)
        total_costs = float(summary.get("total_execution_costs", 0) or 0)
        actual_equity = float(equity_curve.get("current_equity", starting) or starting)
        expected_equity = round(starting + realized + unrealized, 4)
        equity_drift = round(actual_equity - expected_equity, 4)
        cost_drift = round(total_costs - (open_costs + closed_costs), 4)
        evidence = {
            "definition": "equity = starting + realized_pnl_all + unrealized_pnl (costs already netted in)",
            "starting_equity": round(starting, 4),
            "realized_pnl_all": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "expected_equity": expected_equity,
            "actual_equity": round(actual_equity, 4),
            "equity_drift": equity_drift,
            "cost_components": {"open": open_costs, "closed": closed_costs, "total": total_costs},
            "cost_drift": cost_drift,
            "tolerance": tolerance,
        }
        if abs(equity_drift) > tolerance or abs(cost_drift) > tolerance:
            return self._check(
                "accounting_invariant",
                "fail",
                "Paper equity != starting + realized + unrealized, or cost components disagree.",
                evidence,
            )
        return self._check(
            "accounting_invariant",
            "pass",
            "Paper equity reconciles to starting + realized + unrealized; costs consistent.",
            evidence,
        )

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Paper Reconciliation - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Orders: {payload['summary']['orders']}",
            f"- Filled orders: {payload['summary']['filled_orders']}",
            f"- Open trades: {payload['summary']['open_trades']}",
            f"- Positions: {payload['summary']['positions']}",
            "",
            "## Checks",
        ]
        for check in payload["checks"]:
            lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")
        path = self.output_root / "paper_reconciliation" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_paper_reconciliation(run_date: str, output_root: Path) -> dict:
    return PaperReconciliation(output_root).run(run_date)
