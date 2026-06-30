from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_pipeline_config
from services.journal_store import load_json, write_json


class TradeArtifactRepair:
    DATE_TOKEN = re.compile(r"(20\d{2})(\d{2})(\d{2})")

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.paper_account = load_pipeline_config().get("paper_account", {})

    def run(self, run_date: str, strategy_id: str | None = None) -> dict:
        namespaces = [self.output_root / "strategies" / strategy_id] if strategy_id else self._strategy_namespaces()
        repaired_signals = 0
        repaired_tickets = 0
        existing_repaired_signals = 0
        existing_repaired_tickets = 0
        repairs: list[dict] = []
        for namespace in namespaces:
            if not namespace.exists():
                continue
            result = self._repair_namespace(namespace, run_date)
            repaired_signals += result["summary"]["repaired_signals"]
            repaired_tickets += result["summary"]["repaired_tickets"]
            existing_repaired_signals += result["summary"]["existing_repaired_signals"]
            existing_repaired_tickets += result["summary"]["existing_repaired_tickets"]
            repairs.extend(result["repairs"])

        payload = {
            "run_date": run_date,
            "strategy_id": strategy_id or "all",
            "generated_at": self._now(),
            "status": "pass",
            "summary": {
                "strategies_scanned": len(namespaces),
                "repaired_signals": repaired_signals,
                "repaired_tickets": repaired_tickets,
                "existing_repaired_signals": existing_repaired_signals,
                "existing_repaired_tickets": existing_repaired_tickets,
                "repair_count": len(repairs),
            },
            "repairs": repairs,
            "safety": {
                "network_call_attempted": False,
                "trading_gate_changed": False,
                "repair_scope": "paper trade signal/ticket artifacts only",
            },
        }
        repair_root = self.output_root / "artifact_repairs"
        self._append_receipt(repair_root / f"{run_date}.json", payload)
        write_json(repair_root / "current.json", [payload])
        return payload

    def _strategy_namespaces(self) -> list[Path]:
        root = self.output_root / "strategies"
        if not root.exists():
            return []
        return sorted(path for path in root.iterdir() if path.is_dir())

    def _repair_namespace(self, namespace: Path, run_date: str) -> dict:
        open_trades = load_json(namespace / "paper_trades" / "current.json")
        closed_trades = load_json(namespace / "paper_trades" / "closed" / f"{run_date}.json")
        trades = [item for item in [*open_trades, *closed_trades] if isinstance(item, dict)]
        repaired_signals = 0
        repaired_tickets = 0
        repairs: list[dict] = []
        for trade in trades:
            signal_id = str(trade.get("signal_id") or "")
            ticket_id = str(trade.get("ticket_id") or "")
            if not signal_id and not ticket_id:
                continue
            target_date = self._target_date(trade, run_date)
            order = self._matching_order(namespace, target_date, trade)
            signal_path = namespace / "signals" / f"{target_date}.json"
            ticket_path = namespace / "trade_tickets" / f"{target_date}.json"
            signal_repaired = False
            ticket_repaired = False
            if signal_id and not self._row_exists(namespace / "signals", "signal_id", signal_id):
                signal_rows = load_json(signal_path)
                signal_rows = self._append_unique(signal_rows, "signal_id", self._signal_from_trade(namespace, trade, order, target_date))
                write_json(signal_path, signal_rows)
                repaired_signals += 1
                signal_repaired = True
            if ticket_id and not self._row_exists(namespace / "trade_tickets", "ticket_id", ticket_id):
                ticket_rows = load_json(ticket_path)
                ticket_rows = self._append_unique(ticket_rows, "ticket_id", self._ticket_from_trade(namespace, trade, order, target_date))
                write_json(ticket_path, ticket_rows)
                repaired_tickets += 1
                ticket_repaired = True
            if signal_repaired or ticket_repaired:
                repairs.append({
                    "strategy_id": namespace.name,
                    "target_date": target_date,
                    "trade_id": trade.get("trade_id", ""),
                    "signal_id": signal_id,
                    "ticket_id": ticket_id,
                    "order_id": trade.get("order_id", ""),
                    "repaired_signal": signal_repaired,
                    "repaired_ticket": ticket_repaired,
                    "provenance": "repaired_from_paper_trade",
                })
        existing = self._existing_repaired_artifacts(namespace)

        payload = {
            "run_date": run_date,
            "strategy_id": namespace.name,
            "generated_at": self._now(),
            "status": "pass",
            "summary": {
                "trades_scanned": len(trades),
                "repaired_signals": repaired_signals,
                "repaired_tickets": repaired_tickets,
                "existing_repaired_signals": len(existing["signals"]),
                "existing_repaired_tickets": len(existing["tickets"]),
                "repair_count": len(repairs),
            },
            "repairs": repairs,
            "existing_repaired_artifacts": existing,
        }
        self._append_receipt(namespace / "artifact_repairs" / f"{run_date}.json", payload)
        write_json(namespace / "artifact_repairs" / "current.json", [payload])
        return payload

    def _signal_from_trade(self, namespace: Path, trade: dict, order: dict, target_date: str) -> dict:
        side = str(trade.get("side") or "").lower()
        direction = "short" if side == "short" else "long"
        regime = str(trade.get("signal_regime") or trade.get("entry_reason") or "repaired_trade")
        strategy_family = self._family_from_strategy(namespace.name, regime)
        entry = trade.get("entry_price") or order.get("fill_price") or order.get("requested_price")
        stop = trade.get("stop_loss")
        target = trade.get("target")
        generated_at = trade.get("opened_at") or order.get("filled_at") or f"{target_date}T00:00:00+00:00"
        return {
            "signal_id": trade.get("signal_id", ""),
            "asset": trade.get("symbol") or "GOLD",
            "asset_class": "commodity",
            "direction": direction,
            "strength": int(float(trade.get("signal_strength") or 0)),
            "confidence": int(float(trade.get("signal_confidence") or 0)),
            "horizon": self._horizon(namespace.name, regime),
            "thesis": self._repaired_thesis(regime, direction),
            "evidence": [
                f"repaired_from_trade_id={trade.get('trade_id', '')}",
                f"entry={entry}",
                f"target={target}",
                f"stop_loss={stop}",
            ],
            "methods": ["paper-trade-artifact-repair", "risk-management"],
            "regime": regime,
            "factor_scores": {strategy_family: 100},
            "source_artifacts": self._source_artifacts(namespace, target_date, trade),
            "backtest_verdict": "repaired_provenance",
            "invalid_if": f"price breaches repaired stop loss {stop}" if stop else "",
            "generated_at": generated_at,
            "expires_at": "",
            "status": "repaired",
            "artifact_provenance": self._provenance(trade, target_date),
        }

    def _ticket_from_trade(self, namespace: Path, trade: dict, order: dict, target_date: str) -> dict:
        side = str(trade.get("side") or "").lower()
        action = "sell" if side == "short" else "buy"
        entry = trade.get("entry_price") or order.get("fill_price") or order.get("requested_price")
        stop = trade.get("stop_loss")
        target = trade.get("target")
        quantity = float(trade.get("quantity") or order.get("quantity") or 0)
        max_loss_pct = self._max_loss_pct(entry, stop, quantity)
        regime = str(trade.get("signal_regime") or "repaired_trade")
        return {
            "ticket_id": trade.get("ticket_id", ""),
            "signal_id": trade.get("signal_id", ""),
            "asset": trade.get("symbol") or "GOLD",
            "asset_class": "commodity",
            "action": action,
            "entry_zone": str(order.get("requested_price") or entry or ""),
            "stop_loss": stop,
            "targets": [target] if target is not None else [],
            "position_size_pct": 0.0,
            "max_loss_pct": max_loss_pct,
            "order_type": "limit",
            "time_in_force": "day",
            "paper_only": True,
            "trigger": f"repaired_from_trade:{trade.get('trade_id', '')}",
            "invalid_if": f"price breaches repaired stop loss {stop}" if stop else "",
            "methods": ["paper-trade-artifact-repair", "risk-management"],
            "backtest": {"verdict": "repaired_provenance"},
            "signal_regime": regime,
            "signal_strength": int(float(trade.get("signal_strength") or 0)),
            "signal_confidence": int(float(trade.get("signal_confidence") or 0)),
            "factor_scores": {self._family_from_strategy(namespace.name, regime): 100},
            "source_artifacts": self._source_artifacts(namespace, target_date, trade),
            "rationale": f"Repaired ticket artifact from paper trade {trade.get('trade_id', '')}; original trade_tickets row was missing.",
            "counter_rationale": "This is repaired provenance, not the original ticket record.",
            "manual_execution_required": True,
            "verdict": "repaired",
            "trade_quality": {
                "passes": True,
                "source": "repaired_from_paper_trade",
                "reward_to_risk": self._reward_to_risk(trade),
            },
            "artifact_provenance": self._provenance(trade, target_date),
        }

    def _matching_order(self, namespace: Path, target_date: str, trade: dict) -> dict:
        order_id = trade.get("order_id")
        ticket_id = trade.get("ticket_id")
        order_root = namespace / "paper_orders"
        paths = [order_root / f"{target_date}.json"]
        if order_root.exists():
            paths.extend(sorted(order_root.glob("*.json")))
        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            rows = load_json(path)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if (order_id and row.get("order_id") == order_id) or (ticket_id and row.get("ticket_id") == ticket_id):
                    return row
        return {}

    def _row_exists(self, root: Path, key: str, value: str) -> bool:
        if not value or not root.exists():
            return False
        for path in root.glob("*.json"):
            rows = load_json(path)
            if any(isinstance(row, dict) and row.get(key) == value for row in rows):
                return True
        return False

    def _existing_repaired_artifacts(self, namespace: Path) -> dict:
        return {
            "signals": self._existing_repaired_rows(namespace / "signals", "signal_id"),
            "tickets": self._existing_repaired_rows(namespace / "trade_tickets", "ticket_id"),
        }

    def _existing_repaired_rows(self, root: Path, id_key: str) -> list[dict]:
        if not root.exists():
            return []
        out: list[dict] = []
        for path in sorted(root.glob("*.json")):
            rows = load_json(path)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                provenance = row.get("artifact_provenance") if isinstance(row.get("artifact_provenance"), dict) else {}
                if provenance.get("status") == "repaired_from_paper_trade":
                    out.append({
                        id_key: row.get(id_key, ""),
                        "target_date": path.stem,
                        "source_trade_id": provenance.get("source_trade_id", ""),
                    })
        return out

    @staticmethod
    def _append_receipt(path: Path, payload: dict) -> None:
        rows = load_json(path)
        rows = rows if isinstance(rows, list) else []
        rows.append(payload)
        write_json(path, rows)

    @staticmethod
    def _append_unique(rows: list[dict], key: str, item: dict) -> list[dict]:
        out = [row for row in rows if isinstance(row, dict)]
        item_key = item.get(key)
        if item_key and any(row.get(key) == item_key for row in out):
            return out
        out.append(item)
        return out

    def _target_date(self, trade: dict, fallback: str) -> str:
        for key in ("signal_id", "ticket_id", "order_id"):
            dates = self._dates_from_identifier(trade.get(key))
            if dates:
                return sorted(dates)[0]
        for key in ("opened_at", "closed_at"):
            date_value = self._date_from_timestamp(trade.get(key))
            if date_value:
                return date_value
        return fallback

    @classmethod
    def _dates_from_identifier(cls, value: object) -> set[str]:
        text = str(value or "")
        out: set[str] = set()
        for match in cls.DATE_TOKEN.finditer(text):
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

    def _provenance(self, trade: dict, target_date: str) -> dict:
        return {
            "status": "repaired_from_paper_trade",
            "repaired_at": self._now(),
            "target_date": target_date,
            "source_trade_id": trade.get("trade_id", ""),
            "source_order_id": trade.get("order_id", ""),
            "source_ticket_id": trade.get("ticket_id", ""),
            "source_signal_id": trade.get("signal_id", ""),
            "warning": "Reconstructed from paper trade/order record because the original signal or ticket artifact was missing.",
        }

    @staticmethod
    def _horizon(strategy_id: str, regime: str) -> str:
        timeframe = "5m" if "_5m_" in strategy_id else "1m"
        return f"{regime}_{timeframe}" if regime else timeframe

    @staticmethod
    def _family_from_strategy(strategy_id: str, regime: str) -> str:
        if "bollinger" in strategy_id or "bollinger" in regime:
            return "bollinger_reversion"
        if "fibonacci" in strategy_id or "fibonacci" in regime:
            return "fibonacci"
        if "ema50" in strategy_id or "ema50" in regime:
            return "ema50_position"
        if "grid" in strategy_id or "grid" in regime:
            return "grid"
        if "breakout" in strategy_id or "breakout" in regime:
            return "breakout"
        return regime or "repaired_trade"

    @staticmethod
    def _repaired_thesis(regime: str, direction: str) -> str:
        templates = {
            "bollinger_reversion": "Repaired record: GOLD stretched to a Bollinger band; mean-reversion candidate.",
            "fibonacci_pullback": "Repaired record: GOLD traded near Fibonacci pullback support; continuation candidate.",
            "ema50_bounce": "Repaired record: GOLD bounced from EMA50 support; trend-position candidate.",
        }
        return templates.get(regime, f"Repaired record: GOLD {direction} trade generated under {regime or 'unknown'} regime.")

    def _source_artifacts(self, namespace: Path, target_date: str, trade: dict) -> list[str]:
        parts = namespace.parts
        try:
            strategy_index = parts.index("strategies")
            prefix = "/".join(parts[strategy_index:])
        except ValueError:
            prefix = namespace.name
        artifacts = [
            f"{prefix}/paper_trades/current.json",
            f"{prefix}/paper_orders/{target_date}.json",
        ]
        if trade.get("status") == "closed":
            artifacts.append(f"{prefix}/paper_trades/closed/{target_date}.json")
        return artifacts

    def _max_loss_pct(self, entry: object, stop: object, quantity: float) -> float:
        try:
            risk = abs(float(entry) - float(stop)) * abs(float(quantity))
        except (TypeError, ValueError):
            return 0.0
        equity = float(self.paper_account.get("starting_equity", 10000) or 10000)
        return round(risk / equity * 100, 4) if equity > 0 else 0.0

    @staticmethod
    def _reward_to_risk(trade: dict) -> float | None:
        try:
            entry = float(trade.get("entry_price"))
            stop = float(trade.get("stop_loss"))
            target = float(trade.get("target"))
        except (TypeError, ValueError):
            return None
        side = str(trade.get("side") or "long").lower()
        reward = entry - target if side == "short" else target - entry
        risk = stop - entry if side == "short" else entry - stop
        if risk <= 0:
            return None
        return round(reward / risk, 4)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
