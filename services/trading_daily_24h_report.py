from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import BJ_TZ, cycle_window
from services.journal_store import load_json, write_json
from services.strategy_cycle_package import (
    load_latest_verified_cycle_package,
    load_verified_cycle_package_revision,
)


class TradingDaily24hReportBuilder:
    """Build one Beijing-calendar-day report from terminal execution truth."""

    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))

    def build(
        self,
        *,
        now: datetime | None = None,
        report_date: str | date | None = None,
    ) -> Path:
        observed_at = _aware_utc(now or datetime.now(timezone.utc))
        target_date = self._report_date(observed_at, report_date)
        day_start = datetime.combine(target_date, time.min, tzinfo=BJ_TZ).astimezone(timezone.utc)
        day_end = day_start + timedelta(days=1)
        cycle_ids, latest_cycle_end = self._overlapping_cycles(day_start, day_end)
        if observed_at < latest_cycle_end:
            raise ValueError(
                "Beijing natural day is not terminal yet"
                f"; wait_until={latest_cycle_end.isoformat()}"
            )

        packages = [self._package(cycle_id) for cycle_id in cycle_ids]
        payload = self._aggregate(
            target_date=target_date,
            day_start=day_start,
            day_end=day_end,
            packages=packages,
            observed_at=observed_at,
        )
        artifact_path = self.output_root / "dualtrack" / "daily_reports" / f"{target_date.isoformat()}.json"
        write_json(artifact_path, [payload])
        report_path = self.output_root / "pm_reports" / f"{target_date.isoformat()}-daily-24h.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(self._markdown(payload), encoding="utf-8")
        return report_path

    def _report_date(self, observed_at: datetime, value: str | date | None) -> date:
        if value is None:
            return observed_at.astimezone(BJ_TZ).date() - timedelta(days=1)
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("report_date must be YYYY-MM-DD") from exc

    def _overlapping_cycles(self, start: datetime, end: datetime) -> tuple[list[str], datetime]:
        cursor = cycle_window(start).start
        cycle_ids: list[str] = []
        latest_end = cursor
        while cursor < end:
            window = cycle_window(cursor)
            cycle_ids.append(window.cycle_id)
            latest_end = window.end
            cursor = window.end
        if len(cycle_ids) != 3:
            raise ValueError(f"Beijing natural day requires three overlapping cycles; found {len(cycle_ids)}")
        return cycle_ids, latest_end

    def _package(self, cycle_id: str) -> dict[str, Any]:
        path = self.output_root / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
        package = load_latest_verified_cycle_package(path)
        if str(package.get("cycle_id") or "") != cycle_id:
            raise ValueError(f"cycle package identity mismatch: {cycle_id}")
        execution = package.get("execution") if isinstance(package.get("execution"), dict) else None
        if execution is None:
            raise ValueError(f"cycle package execution evidence missing: {cycle_id}")
        traceability = package.get("traceability") or {}
        if (
            traceability.get("strategy_plan_id") in (None, "")
            or traceability.get("strategy_plan_version") in (None, "")
        ):
            raise ValueError(f"cycle package StrategyPlan traceability missing: {cycle_id}")
        reconciliation = execution.get("reconciliation") or {}
        if str(reconciliation.get("status") or "") != "ok" or reconciliation.get("issues"):
            raise ValueError(f"cycle package reconciliation is not terminal: {cycle_id}")
        authoritative = _finite(execution.get("pnl", {}).get("realized"), f"{cycle_id} realized PnL")
        if "positions" not in execution or not isinstance(execution["positions"], list):
            raise ValueError(f"closed-position evidence is missing: {cycle_id}")
        if "fills" not in execution or not isinstance(execution["fills"], list):
            raise ValueError(f"fill evidence is missing: {cycle_id}")
        positions = execution["positions"]
        if not isinstance(positions, list) or any(not isinstance(row, dict) for row in positions):
            raise ValueError(f"invalid closed-position evidence: {cycle_id}")
        invalid_statuses = [
            str(row.get("status") or "")
            for row in positions
            if str(row.get("status") or "") != "closed"
        ]
        if invalid_statuses:
            raise ValueError(f"terminal cycle package contains non-closed positions: {cycle_id}")
        position_total = sum(
            _finite(row.get("realized_pnl"), f"{cycle_id} closed position realized PnL")
            for row in positions
            if str(row.get("status") or "") == "closed"
        )
        if not math.isclose(position_total, authoritative, abs_tol=1e-6):
            raise ValueError(
                f"authoritative position/PnL mismatch for {cycle_id}"
                f"; positions={position_total}; execution={authoritative}"
            )
        return package

    def _aggregate(
        self,
        *,
        target_date: date,
        day_start: datetime,
        day_end: datetime,
        packages: list[dict[str, Any]],
        observed_at: datetime,
    ) -> dict[str, Any]:
        realized = 0.0
        fills_in_day: list[dict[str, Any]] = []
        trade_ids: set[str] = set()
        position_ids: set[str] = set()
        fill_ids: set[str] = set()
        starting_cash_values: list[float] = []
        fee_values: list[float] = []
        funding_values: list[float] = []
        gross_realized_values: list[float] = []
        provenance: list[dict[str, Any]] = []

        for package in packages:
            cycle_id = str(package["cycle_id"])
            execution = package["execution"]
            account = execution.get("account") or {}
            starting_cash_values.append(_finite(account.get("starting_cash"), f"{cycle_id} starting cash"))
            if account.get("fees") not in (None, ""):
                fee_values.append(_finite(account.get("fees"), f"{cycle_id} fees"))
            if account.get("funding") not in (None, ""):
                funding_values.append(_finite(account.get("funding"), f"{cycle_id} funding"))
            package_pnl = execution.get("pnl") or {}
            if package_pnl.get("gross_realized_pnl") not in (None, ""):
                gross_realized_values.append(
                    _finite(package_pnl.get("gross_realized_pnl"), f"{cycle_id} gross realized PnL")
                )
            provenance.append({
                "cycle_id": cycle_id,
                "package_hash": str(package.get("package_hash") or ""),
                "strategy_plan_id": (package.get("traceability") or {}).get("strategy_plan_id"),
                "strategy_plan_version": (package.get("traceability") or {}).get("strategy_plan_version"),
            })
            for position in execution.get("positions") or []:
                if str(position.get("status") or "") != "closed":
                    continue
                exit_ts = _timestamp(position.get("exit_ts"), f"{cycle_id} closed position exit_ts")
                if not day_start <= exit_ts < day_end:
                    continue
                position_id = str(position.get("position_id") or position.get("trade_id") or "").strip()
                if not position_id:
                    raise ValueError(f"closed position identity missing: {cycle_id}")
                if position_id in position_ids:
                    raise ValueError(f"duplicate closed position across cycle packages: {position_id}")
                position_ids.add(position_id)
                realized += _finite(position.get("realized_pnl"), f"{cycle_id} closed position realized PnL")
            for fill in execution["fills"]:
                if not isinstance(fill, dict):
                    raise ValueError(f"invalid fill evidence: {cycle_id}")
                fill_ts = _timestamp(fill.get("ts"), f"{cycle_id} fill timestamp")
                if not day_start <= fill_ts < day_end:
                    continue
                notional = _fill_notional(fill, cycle_id=cycle_id)
                fill_id = str(fill.get("fill_id") or "").strip()
                if not fill_id:
                    raise ValueError(f"fill identity missing: {cycle_id}")
                if fill_id in fill_ids:
                    raise ValueError(f"duplicate fill across cycle packages: {fill_id}")
                fill_ids.add(fill_id)
                fills_in_day.append({**fill, "_notional": notional})
                if str(fill.get("event") or "") == "entry":
                    trade_id = str(
                        fill.get("trade_id") or fill.get("position_id") or fill.get("fill_id") or ""
                    ).strip()
                    if not trade_id:
                        raise ValueError(f"entry fill identity missing: {cycle_id}")
                    trade_ids.add(trade_id)

        base_cash = starting_cash_values[0] if starting_cash_values else 0.0
        if base_cash <= 0 or any(not math.isclose(value, base_cash, abs_tol=1e-6) for value in starting_cash_values):
            raise ValueError("cycle package starting cash is missing or inconsistent")
        total_notional = sum(float(row["_notional"]) for row in fills_in_day)
        ending_equity = base_cash + realized
        payload: dict[str, Any] = {
            "schema_version": "trading-daily-24h-v1",
            "status": "complete",
            "report_date": target_date.isoformat(),
            "timezone": "Asia/Shanghai",
            "window": {"start": day_start.isoformat(), "end": day_end.isoformat()},
            "generated_at": observed_at.replace(microsecond=0).isoformat(),
            "execution": {
                "trade_count": len(trade_ids),
                "fill_count": len(fills_in_day),
                "total_notional": round(total_notional, 8),
                "realized_pnl": round(realized, 8),
                "realized_pnl_source": "terminal_cycle_packages.execution.positions.closed",
                "fill_usage": "event_count_and_notional_only",
            },
            "nav": {
                "starting_equity": round(base_cash, 8),
                "ending_equity": round(ending_equity, 8),
                "normalized_nav": round(ending_equity / base_cash, 10),
                "daily_return_pct": round(realized / base_cash * 100, 8),
                "source": "terminal_cycle_packages",
            },
            "provenance": {
                "cycle_packages": provenance,
                "package_count": len(provenance),
            },
        }
        if len(fee_values) == len(packages):
            payload["execution"]["fees"] = round(sum(fee_values), 8)
            payload["execution"]["fees_source"] = "terminal_cycle_packages.execution.account"
        if len(funding_values) == len(packages):
            payload["execution"]["funding"] = round(sum(funding_values), 8)
            payload["execution"]["funding_source"] = "terminal_cycle_packages.execution.account"
        if len(gross_realized_values) == len(packages):
            payload["execution"]["gross_realized_pnl"] = round(sum(gross_realized_values), 8)
            payload["execution"]["gross_realized_pnl_source"] = "terminal_cycle_packages.execution.pnl"
        payload["report_hash"] = _hash_payload(payload)
        return payload

    def _markdown(self, payload: dict[str, Any]) -> str:
        execution = payload["execution"]
        nav = payload["nav"]
        package_ids = ", ".join(row["cycle_id"] for row in payload["provenance"]["cycle_packages"])
        evidence_lines = [
            (
                f"- {row['cycle_id']}｜package {row['package_hash']}｜"
                f"plan {row['strategy_plan_id']} v{row['strategy_plan_version']}"
            )
            for row in payload["provenance"]["cycle_packages"]
        ]
        return "\n".join([
            f"# 黄金交易 24 小时报告｜{payload['report_date']}",
            "",
            "## 核心结果",
            "",
            (
                f"交易 {execution['trade_count']} 笔｜成交事件 {execution['fill_count']} 个｜"
                f"名义金额 {execution['total_notional']:.2f} USD｜"
                f"已实现 PnL {execution['realized_pnl']:+.2f} USD"
            ),
            (
                f"NAV {nav['normalized_nav']:.6f}｜"
                f"当日收益率 {nav['daily_return_pct']:+.4f}%｜"
                f"权益 {nav['starting_equity']:.2f} → {nav['ending_equity']:.2f} USD"
            ),
            "",
            "## 证据",
            "",
            f"权威已实现收益来自已关闭持仓；fills 仅用于次数和名义金额。Cycle packages：{package_ids}。",
            *evidence_lines,
            f"- report hash｜{payload['report_hash']}",
            "",
        ])


def load_daily_report_rows(output_root: Path, *, limit: int = 30) -> list[dict[str, Any]]:
    folder = Path(output_root) / "dualtrack" / "daily_reports"
    if not folder.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        values = load_json(path)
        if values and isinstance(values[-1], dict):
            row = values[-1]
            observed = str(row.get("report_hash") or "")
            candidate = dict(row)
            candidate.pop("report_hash", None)
            expected = _hash_payload(candidate)
            if not observed or observed != expected:
                raise ValueError(f"daily report hash is invalid: {path}")
            provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
            packages = provenance.get("cycle_packages")
            if not isinstance(packages, list) or not packages:
                raise ValueError(f"daily report package provenance is missing: {path}")
            for reference in packages:
                if not isinstance(reference, dict):
                    raise ValueError(f"daily report package provenance is invalid: {path}")
                cycle_id = str(reference.get("cycle_id") or "")
                package_hash = str(reference.get("package_hash") or "")
                if not cycle_id or not package_hash:
                    raise ValueError(f"daily report package provenance is invalid: {path}")
                package_path = (
                    Path(output_root)
                    / "dualtrack"
                    / "strategy_cycle_packages"
                    / f"{cycle_id}.json"
                )
                load_verified_cycle_package_revision(package_path, package_hash)
            rows.append(row)
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def _fill_notional(fill: dict[str, Any], *, cycle_id: str) -> float:
    if fill.get("notional") not in (None, ""):
        value = _finite(fill.get("notional"), f"{cycle_id} fill notional")
    else:
        price = _finite(fill.get("price"), f"{cycle_id} fill price")
        quantity_value = next(
            (fill.get(key) for key in ("quantity", "pnl_units", "units", "contracts") if fill.get(key) not in (None, "")),
            None,
        )
        quantity = _finite(quantity_value, f"{cycle_id} fill quantity")
        if price <= 0 or quantity <= 0:
            raise ValueError(f"{cycle_id} fill price and quantity must be positive")
        value = price * quantity
    if value <= 0:
        raise ValueError(f"{cycle_id} fill notional must be positive")
    return value


def _finite(value: Any, label: str) -> float:
    if value in (None, ""):
        raise ValueError(f"{label} is missing")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _timestamp(value: Any, label: str) -> datetime:
    if value in (None, ""):
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must include timezone")
    return value.astimezone(timezone.utc)


def _hash_payload(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
