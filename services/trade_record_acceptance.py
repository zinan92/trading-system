from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config, load_strategy_config
from services.datafeed_market_client import DatafeedUnavailable
from services.journal_store import load_json, write_json
from services.market_data_access import market_data_repository
from services.trade_record_card import TradeRecordCardBuilder


TRADE_RECORD_ACCEPTANCE_VERSION = "trade-record-acceptance-v1"


class TradeRecordAcceptanceAudit:
    """M1 user-facing acceptance audit for sampled trade record cards.

    The backend maturity M1 check proves cards exist at the strategy-detail
    level. This audit matches the user's manual acceptance standard: pick a
    handful of trades across strategies/dates and verify that the reader can
    understand the trade and hand-check the PnL.
    """

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None, sample_size: int = 5) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / config.get("output_root", "outputs"))
        self.market_db = Path(market_db or ROOT / config.get("local_market_db", "data/market_data.db"))
        self.sample_size = max(1, int(sample_size or 5))
        self.strategy_config = load_strategy_config()

    def run(self, run_date: str, *, persist: bool = True) -> dict:
        candidates = self._collect_candidates(run_date)
        sample = self._sample(candidates)
        rows = [self._evaluate(item) for item in sample]
        failures = [row for row in rows if row["status"] != "pass"]
        status = "pass" if rows and not failures else ("warn" if not rows else "fail")
        payload = {
            "schema_version": TRADE_RECORD_ACCEPTANCE_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "sample_size_requested": self.sample_size,
            "sample_size_actual": len(rows),
            "candidate_count": len(candidates),
            "strategy_count": len({item["strategy_id"] for item in candidates if item.get("strategy_id")}),
            "date_count": len({item["run_date"] for item in candidates if item.get("run_date")}),
            "red_line": "sampled trades must have complete cards, visible protection, and explainable PnL math",
            "failures": failures,
            "samples": rows,
        }
        if persist:
            write_json(self.output_root / "trade_record_acceptance" / "current.json", [payload])
            write_json(self.output_root / "trade_record_acceptance" / f"{run_date}.json", [payload])
        return payload

    def _collect_candidates(self, run_date: str) -> list[dict]:
        rows: list[dict] = []
        strategy_root = self.output_root / "strategies"
        if strategy_root.exists():
            for strategy_dir in sorted(path for path in strategy_root.iterdir() if path.is_dir()):
                sid = strategy_dir.name
                for path in sorted((strategy_dir / "paper_trades" / "closed").glob("*.json")):
                    trade_date = path.stem
                    if trade_date > run_date:
                        continue
                    for idx, trade in enumerate(load_json(path)):
                        if isinstance(trade, dict):
                            rows.append({"strategy_id": sid, "run_date": trade_date, "path": str(path), "index": idx, "trade": trade})
                current_path = strategy_dir / "paper_trades" / "current.json"
                for idx, trade in enumerate(load_json(current_path)):
                    if isinstance(trade, dict):
                        opened_date = self._date_from_ts(trade.get("opened_at")) or run_date
                        if opened_date <= run_date:
                            rows.append({"strategy_id": sid, "run_date": opened_date, "path": str(current_path), "index": idx, "trade": trade})
        for path in sorted((self.output_root / "paper_trades" / "closed").glob("*.json")):
            trade_date = path.stem
            if trade_date > run_date:
                continue
            for idx, trade in enumerate(load_json(path)):
                if isinstance(trade, dict):
                    sid = str(trade.get("strategy_id") or "global")
                    rows.append({"strategy_id": sid, "run_date": trade_date, "path": str(path), "index": idx, "trade": trade})
        current_path = self.output_root / "paper_trades" / "current.json"
        for idx, trade in enumerate(load_json(current_path)):
            if isinstance(trade, dict):
                sid = str(trade.get("strategy_id") or "global")
                opened_date = self._date_from_ts(trade.get("opened_at")) or run_date
                if opened_date <= run_date:
                    rows.append({"strategy_id": sid, "run_date": opened_date, "path": str(current_path), "index": idx, "trade": trade})
        return sorted(rows, key=lambda item: (item["strategy_id"], item["run_date"], self._trade_id(item)))

    def _sample(self, candidates: list[dict]) -> list[dict]:
        if len(candidates) <= self.sample_size:
            return candidates
        selected: list[dict] = []
        used_keys: set[tuple[str, str]] = set()
        for item in sorted(candidates, key=lambda row: (row["strategy_id"], row["run_date"], self._trade_id(row))):
            key = (item["strategy_id"], item["run_date"])
            if key in used_keys:
                continue
            selected.append(item)
            used_keys.add(key)
            if len(selected) >= self.sample_size:
                return selected
        for item in candidates:
            if item in selected:
                continue
            selected.append(item)
            if len(selected) >= self.sample_size:
                break
        return selected

    def _evaluate(self, item: dict) -> dict:
        trade = item["trade"]
        strategy_id = str(trade.get("strategy_id") or item["strategy_id"])
        latest_price_evidence = self._latest_price_evidence(trade)
        card = TradeRecordCardBuilder(
            run_date=item["run_date"],
            strategy_id=strategy_id,
            strategy_config=self.strategy_config.get(strategy_id, {}) if isinstance(self.strategy_config, dict) else {},
            latest_price=latest_price_evidence["price"],
        ).build({**trade, "strategy_id": strategy_id})
        card_failures = self._card_failures(card)
        pnl_check = self._pnl_check(trade, card)
        failures = card_failures + ([] if pnl_check["status"] == "pass" else [pnl_check["reason"]])
        return {
            "status": "pass" if not failures else "fail",
            "trade_id": card.get("trade_id", ""),
            "strategy_id": strategy_id,
            "run_date": item["run_date"],
            "source_path": item["path"],
            "card_status": (card.get("audit") or {}).get("status", ""),
            "protection_status": (card.get("protection") or {}).get("status", ""),
            "compliance_verdict": (card.get("compliance") or {}).get("verdict", ""),
            "pnl_check": pnl_check,
            "latest_price_evidence": latest_price_evidence,
            "failures": failures,
            "display": card.get("display", {}),
        }

    def _card_failures(self, card: dict) -> list[str]:
        failures = []
        audit = card.get("audit") or {}
        protection = card.get("protection") or {}
        compliance = card.get("compliance") or {}
        if audit.get("status") != "complete":
            failures.append(f"card_incomplete:{','.join(audit.get('missing_fields') or [])}")
        if not audit.get("red_line_passed"):
            failures.append("red_line_failed")
        if protection.get("status") in {"missing", "partial"}:
            failures.append(f"protection_{protection.get('status')}")
        if compliance.get("verdict") != "pass":
            failures.append("compliance_failed")
        return failures

    def _pnl_check(self, trade: dict, card: dict) -> dict:
        if (card.get("status") or "") != "closed":
            return {"status": "pass", "reason": "open_trade_unrealized_only"}
        side = str(card.get("side") or trade.get("side") or "").lower()
        direction = 1 if side == "long" else -1
        entry = self._number(trade.get("entry_price"))
        exit_price = self._number(trade.get("exit_price"))
        quantity = self._number(trade.get("quantity"))
        if entry is None or exit_price is None or quantity is None:
            return {"status": "fail", "reason": "missing_price_or_quantity_for_pnl_math"}
        expected_gross = round((exit_price - entry) * direction * quantity, 4)
        actual_gross = self._number(trade.get("gross_realized_pnl"))
        gross_source = "gross_realized_pnl"
        if actual_gross is None:
            realized = self._number(trade.get("realized_pnl"))
            total_cost = self._number(trade.get("total_cost")) or 0.0
            actual_gross = None if realized is None else round(realized + total_cost, 4)
            gross_source = "realized_pnl_plus_total_cost"
        tolerance = max(0.05, abs(expected_gross) * 0.003)
        gross_delta = None if actual_gross is None else round(abs(expected_gross - actual_gross), 6)
        if actual_gross is None or gross_delta is None or gross_delta > tolerance:
            return {
                "status": "fail",
                "reason": "gross_pnl_math_mismatch",
                "expected_gross": expected_gross,
                "actual_gross": actual_gross,
                "gross_source": gross_source,
                "delta": gross_delta,
                "tolerance": round(tolerance, 6),
            }
        realized = self._number(trade.get("realized_pnl"))
        total_cost = self._number(trade.get("total_cost"))
        net_delta = None
        if realized is not None and total_cost is not None:
            expected_net = round(expected_gross - total_cost, 4)
            net_delta = round(abs(expected_net - realized), 6)
            if net_delta > tolerance:
                return {
                    "status": "fail",
                    "reason": "net_pnl_math_mismatch",
                    "expected_net": expected_net,
                    "actual_net": realized,
                    "delta": net_delta,
                    "tolerance": round(tolerance, 6),
                }
        return {
            "status": "pass",
            "reason": "closed_trade_pnl_math_reconciles",
            "expected_gross": expected_gross,
            "actual_gross": actual_gross,
            "gross_source": gross_source,
            "gross_delta": gross_delta,
            "net_delta": net_delta,
            "tolerance": round(tolerance, 6),
        }

    def _trade_id(self, item: dict) -> str:
        trade = item.get("trade") or {}
        return str(trade.get("trade_id") or trade.get("order_id") or trade.get("opened_at") or item.get("index") or "")

    def _latest_price(self, trade: dict) -> float | None:
        return self._latest_price_evidence(trade)["price"]

    def _latest_price_evidence(self, trade: dict) -> dict[str, Any]:
        direct = self._number(trade.get("latest_price"))
        if direct is not None:
            return {"status": "available", "source": "trade", "price": direct}
        exit_decision = trade.get("exit_decision") if isinstance(trade.get("exit_decision"), dict) else {}
        direct = self._number(exit_decision.get("latest_price"))
        if direct is not None:
            return {"status": "available", "source": "exit_decision", "price": direct}
        symbol = str(trade.get("symbol") or "GOLD")
        try:
            store = market_data_repository(self.market_db)
            latest = store.load_latest_bar(symbol, "1m") or store.load_latest_bar(symbol, "5m")
            if not latest and symbol != "GOLD":
                latest = store.load_latest_bar("GOLD", "1m") or store.load_latest_bar("GOLD", "5m")
        except (DatafeedUnavailable, KeyError, RuntimeError, OSError) as error:
            return {
                "status": "unknown",
                "source": "datafeed",
                "price": None,
                "reason": f"{type(error).__name__}: {error}",
            }
        price = self._number(latest.get("close")) if latest else None
        return {
            "status": "available" if price is not None else "unknown",
            "source": "datafeed",
            "price": price,
            "reason": None if price is not None else "latest_price_missing",
        }

    def _date_from_ts(self, value: Any) -> str:
        parsed = self._parse_ts(value)
        return parsed.date().isoformat() if parsed else ""

    def _parse_ts(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _number(self, value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
