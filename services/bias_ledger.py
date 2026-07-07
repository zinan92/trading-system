from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.market_store import MarketStore
from services.market_view import MarketViewStore, infer_market_view_reference_price


NEUTRAL_MOVE_THRESHOLD_PCT = 0.30
SETTLE_BAR_TOLERANCE_MIN = 15


class BiasLedgerBlockedError(RuntimeError):
    """Raised when an expired human bias view still lacks adjudication."""


@dataclass(frozen=True)
class BiasLedgerConfig:
    output_root: Path
    market_db: Path | None
    symbol: str = "GOLD"
    neutral_move_threshold_pct: float = NEUTRAL_MOVE_THRESHOLD_PCT
    settle_bar_tolerance_min: int = SETTLE_BAR_TOLERANCE_MIN


class BiasLedger:
    def __init__(
        self,
        output_root: Path | None = None,
        market_db: Path | None = None,
        *,
        symbol: str = "GOLD",
        neutral_move_threshold_pct: float = NEUTRAL_MOVE_THRESHOLD_PCT,
        settle_bar_tolerance_min: int = SETTLE_BAR_TOLERANCE_MIN,
    ) -> None:
        config = load_pipeline_config()
        resolved_output = output_root or ROOT / str(config.get("output_root", "outputs"))
        resolved_market_db = market_db or ROOT / str(config.get("local_market_db", "data/market_data.db"))
        self.config = BiasLedgerConfig(
            output_root=Path(resolved_output),
            market_db=Path(resolved_market_db) if resolved_market_db else None,
            symbol=symbol,
            neutral_move_threshold_pct=float(neutral_move_threshold_pct),
            settle_bar_tolerance_min=int(settle_bar_tolerance_min),
        )

    @property
    def ledger_path(self) -> Path:
        return self.config.output_root / "bias_ledger" / "ledger.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.config.output_root / "bias_ledger" / "summary.json"

    def append_open_view(self, market_view: dict[str, Any]) -> dict[str, Any]:
        issued_at = _parse_ts(str(market_view.get("generated_at") or ""))
        if issued_at is None:
            raise ValueError("market_view.generated_at is required for bias ledger")
        run_date = str(market_view.get("run_date") or issued_at.date().isoformat())
        price = infer_market_view_reference_price(
            market_view,
            self.config.market_db,
            symbol=self.config.symbol,
        )
        entry = {
            "view_id": _view_id(run_date, issued_at),
            "run_date": run_date,
            "issued_at": issued_at.isoformat(),
            "bias_score": int(round(float(market_view.get("direction_score", 50)))),
            "price_at_issue": price,
            "expires_at": self._expires_at(market_view, issued_at).isoformat(),
            "status": "open",
            "verdict": None,
            "settle_price": None,
            "realized_move_pct": None,
            "brier": None,
            "adjudicated_at": None,
            "pending_reason": "missing_reference_price" if price is None else None,
        }
        self._append_event(entry)
        self.write_summary()
        return entry

    def settle_expired(self, *, as_of: str | datetime | None = None, run_date: str | None = None) -> dict[str, Any]:
        checked_at = _coerce_ts(as_of) or _utc_now()
        settled = 0
        pending = 0
        for entry in list(self.current_entries().values()):
            if run_date and entry.get("run_date") != run_date:
                continue
            if entry.get("status") != "open":
                continue
            if not self._is_expired(entry, checked_at):
                continue
            next_entry = self._settle_entry(entry, checked_at)
            self._append_event(next_entry)
            if next_entry["status"] == "adjudicated":
                settled += 1
            elif next_entry["status"] == "pending_data":
                pending += 1
        summary = self.write_summary()
        return {"settled": settled, "pending_data": pending, "summary": summary}

    def guard_before_record(self, *, as_of: str | datetime | None = None) -> None:
        checked_at = _coerce_ts(as_of) or _utc_now()
        self.settle_expired(as_of=checked_at)
        blockers = self.overdue_blockers(as_of=checked_at)
        if not blockers:
            return
        details = ", ".join(
            f"{item['view_id']}:{item.get('pending_reason') or item['status']}" for item in blockers
        )
        raise BiasLedgerBlockedError(f"bias ledger blocks new market view; resolve expired entries: {details}")

    def overdue_blockers(self, *, as_of: str | datetime | None = None) -> list[dict[str, Any]]:
        checked_at = _coerce_ts(as_of) or _utc_now()
        return [
            entry
            for entry in self.current_entries().values()
            if entry.get("status") in {"open", "pending_data"} and self._is_expired(entry, checked_at)
        ]

    def current_entries(self) -> dict[str, dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for event in self.events():
            current[str(event["view_id"])] = event
        return current

    def events(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def write_summary(self) -> dict[str, Any]:
        current = list(self.current_entries().values())
        correct = [item for item in current if item.get("verdict") == "correct"]
        wrong = [item for item in current if item.get("verdict") == "wrong"]
        brier_values = [float(item["brier"]) for item in current if item.get("brier") is not None]
        hit_denominator = len(correct) + len(wrong)
        current.sort(key=lambda item: str(item.get("issued_at") or ""))
        summary = {
            "generated_at": _utc_now().isoformat(),
            "ledger_event_count": len(self.events()),
            "total": len(current),
            "adjudicated": sum(1 for item in current if item.get("status") == "adjudicated"),
            "correct": len(correct),
            "wrong": len(wrong),
            "undecidable": sum(1 for item in current if item.get("verdict") == "undecidable"),
            "no_claim": sum(1 for item in current if item.get("verdict") == "no_claim"),
            "pending_data": sum(1 for item in current if item.get("status") == "pending_data"),
            "hit_rate": (len(correct) / hit_denominator) if hit_denominator else None,
            "mean_brier": (sum(brier_values) / len(brier_values)) if brier_values else None,
            "last_10": current[-10:],
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary

    def _settle_entry(self, entry: dict[str, Any], checked_at: datetime) -> dict[str, Any]:
        base = dict(entry)
        if entry.get("price_at_issue") is None:
            base.update({"status": "pending_data", "pending_reason": "missing_reference_price"})
            return base
        bar = self._settle_bar(str(entry["expires_at"]))
        if bar is None:
            base.update({"status": "pending_data", "pending_reason": "no_bar_within_tolerance"})
            return base
        issue_price = float(entry["price_at_issue"])
        settle_price = float(bar["close"])
        move_pct = ((settle_price - issue_price) / issue_price) * 100
        verdict, brier = self._verdict(float(entry["bias_score"]), move_pct)
        base.update(
            {
                "status": "adjudicated",
                "verdict": verdict,
                "settle_price": settle_price,
                "realized_move_pct": move_pct,
                "brier": brier,
                "adjudicated_at": checked_at.isoformat(),
                "pending_reason": None,
            }
        )
        return base

    def _settle_bar(self, expires_at: str) -> dict[str, Any] | None:
        if not self.config.market_db or not self.config.market_db.exists():
            return None
        expiry = _parse_ts(expires_at)
        if expiry is None:
            return None
        row = MarketStore(self.config.market_db).load_bar_at_or_before(self.config.symbol, "1m", expiry.isoformat())
        if not row:
            return None
        row_ts = _parse_ts(str(row.get("timestamp") or ""))
        if row_ts is None:
            return None
        age = expiry - row_ts
        if age < timedelta(0) or age > timedelta(minutes=self.config.settle_bar_tolerance_min):
            return None
        return row

    def _verdict(self, score: float, move_pct: float) -> tuple[str, float | None]:
        if abs(move_pct) < self.config.neutral_move_threshold_pct:
            return "undecidable", None
        y = 1 if move_pct > 0 else 0
        brier = ((score / 100) - y) ** 2
        if score >= 60:
            return ("correct" if y == 1 else "wrong"), brier
        if score <= 40:
            return ("correct" if y == 0 else "wrong"), brier
        return "no_claim", brier

    def _expires_at(self, market_view: dict[str, Any], issued_at: datetime) -> datetime:
        expiry = market_view.get("expiry") or {}
        explicit = _parse_ts(str(expiry.get("expires_at") or ""))
        if explicit is not None:
            return explicit
        hours = expiry.get("valid_for_hours")
        if hours is None:
            hours = MarketViewStore.DEFAULT_VALID_FOR_HOURS
        return issued_at + timedelta(hours=float(hours))

    def _is_expired(self, entry: dict[str, Any], checked_at: datetime) -> bool:
        expires_at = _parse_ts(str(entry.get("expires_at") or ""))
        return bool(expires_at and checked_at > expires_at)

    def _append_event(self, entry: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _view_id(run_date: str, issued_at: datetime) -> str:
    compact = issued_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{run_date}-{compact}"


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    except ValueError:
        return None


def _coerce_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    return _parse_ts(str(value))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
