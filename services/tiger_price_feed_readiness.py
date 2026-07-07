from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class TigerPriceFeedReadiness:
    """Artifact-only readiness gate for promoting Tiger as a price feed.

    This gate reads local Tiger feed and realtime-validation artifacts only. It
    does not open Tiger SDK clients, does not write market bars, and does not
    authorize any broker-order path.
    """

    def __init__(self, output_root: Path | None = None, *, min_imported_rows: int = 500) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))
        self.min_imported_rows = int(min_imported_rows)

    def run(self, run_date: str) -> dict[str, Any]:
        feed = self._artifact("tiger_futures_feed/current.json")
        realtime = self._artifact("tiger_realtime_validation/current.json")
        checks = [
            self._feed_import_ready(feed["payload"]),
            self._feed_window_ready(feed["payload"]),
            self._realtime_gate_ready(realtime["payload"]),
            self._realtime_safety_ready(realtime["payload"]),
        ]
        blockers = [item for item in checks if item["status"] != "pass"]
        ready = not blockers
        contract = str(feed["payload"].get("contract") or realtime["payload"].get("contract") or "MGCmain")
        payload = {
            "schema_version": "tiger-price-feed-readiness-v1",
            "run_date": run_date,
            "checked_at": self._now(),
            "provider": "tiger_openapi",
            "venue": "COMEX",
            "contract": contract,
            "status": "ready_for_price_feed" if ready else "blocked",
            "ready_for_price_feed": ready,
            "can_enable_broker_orders_from_this_gate": False,
            "checks": checks,
            "blockers": blockers,
            "evidence_paths": {
                "feed": str(feed["path"]),
                "realtime_validation": str(realtime["path"]),
                "readiness": str(self.output_root / "tiger_price_feed_readiness" / f"{run_date}.json"),
            },
            "next_commands": self._next_commands(run_date, contract),
            "summary": {
                "feed_status": str(feed["payload"].get("status") or "missing"),
                "imported_rows": feed["payload"].get("imported_rows"),
                "latest_timestamp": str(feed["payload"].get("latest_timestamp") or ""),
                "realtime_status": str(realtime["payload"].get("status") or "missing"),
                "market_hours_gate": self._gate_summary(realtime["payload"]),
            },
            "safety": {
                "artifact_only": True,
                "opens_tiger_sdk_clients": False,
                "writes_market_db": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "creates_order_control_endpoint": False,
            },
        }
        write_json(self.output_root / "tiger_price_feed_readiness" / "current.json", [payload])
        write_json(self.output_root / "tiger_price_feed_readiness" / f"{run_date}.json", [payload])
        return payload

    def _artifact(self, relative: str) -> dict[str, Any]:
        path = self.output_root / relative
        try:
            rows = load_json(path)
        except Exception as exc:  # noqa: BLE001 - artifact gates should degrade to evidence.
            return {"path": path, "payload": {}, "error": f"{type(exc).__name__}: {exc}"}
        payload = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        return {"path": path, "payload": payload, "error": "" if payload else "artifact missing or empty"}

    def _feed_import_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._check(
            "feed_import",
            str(payload.get("status") or "") == "pass" and payload.get("ready") is True,
            "Tiger futures feed import artifact is pass/ready.",
            "Tiger futures feed import artifact is missing or not ready.",
            {
                "status": payload.get("status", "missing"),
                "ready": payload.get("ready"),
                "contract": payload.get("contract", ""),
                "provider": payload.get("provider", ""),
                "checked_at": payload.get("checked_at", ""),
            },
        )

    def _feed_window_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        imported_rows = int(payload.get("imported_rows") or 0)
        return self._check(
            "feed_window",
            imported_rows >= self.min_imported_rows and bool(payload.get("latest_timestamp")),
            f"Tiger futures feed has at least {self.min_imported_rows} imported bars.",
            f"Tiger futures feed needs at least {self.min_imported_rows} imported bars.",
            {
                "imported_rows": payload.get("imported_rows"),
                "required_imported_rows": self.min_imported_rows,
                "latest_timestamp": payload.get("latest_timestamp", ""),
                "latest_price": payload.get("latest_price"),
            },
        )

    def _realtime_gate_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        gate = payload.get("market_hours_gate", {}) if isinstance(payload.get("market_hours_gate"), dict) else {}
        return self._check(
            "realtime_market_hours_gate",
            str(payload.get("status") or "") == "pass"
            and gate.get("required") is True
            and gate.get("ready_for_price_feed_promotion") is True
            and gate.get("exit_code") == 0,
            "Tiger realtime validation passed during market hours.",
            "Tiger realtime validation has not passed during market hours.",
            {
                "status": payload.get("status", "missing"),
                "checked_at": payload.get("checked_at", ""),
                "message": payload.get("message", ""),
                "market_hours_gate": self._gate_summary(payload),
            },
        )

    def _realtime_safety_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        safety = payload.get("safety", {}) if isinstance(payload.get("safety"), dict) else {}
        safe = (
            safety.get("read_only") is True
            and safety.get("writes_market_db") is False
            and safety.get("opens_trade_client") is False
            and safety.get("submits_orders") is False
        )
        return self._check(
            "realtime_safety",
            safe,
            "Tiger realtime validation stayed read-only.",
            "Tiger realtime validation safety flags need review.",
            {
                "read_only": safety.get("read_only"),
                "writes_market_db": safety.get("writes_market_db"),
                "opens_trade_client": safety.get("opens_trade_client"),
                "submits_orders": safety.get("submits_orders"),
            },
        )

    def _gate_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        gate = payload.get("market_hours_gate", {}) if isinstance(payload.get("market_hours_gate"), dict) else {}
        next_window = gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {}
        return {
            "required": gate.get("required") is True,
            "ready_for_price_feed_promotion": gate.get("ready_for_price_feed_promotion") is True,
            "market_hours_observed": gate.get("market_hours_observed") is True,
            "exit_code": gate.get("exit_code"),
            "operator_action": str(gate.get("operator_action") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
        }

    def _check(self, name: str, passed: bool, pass_summary: str, fail_summary: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "status": "pass" if passed else "fail",
            "summary": pass_summary if passed else fail_summary,
            "evidence": evidence,
        }

    def _next_commands(self, run_date: str, contract: str) -> list[str]:
        return [
            f"python3 -m pipelines.tiger_futures_feed --date {run_date} --contract {contract} --limit {self.min_imported_rows}",
            f"python3 -m pipelines.tiger_realtime_validation --date {run_date} --contract {contract} --poll-seconds 75 --require-market-hours-pass",
            f"python3 -m pipelines.tiger_price_feed_readiness --date {run_date} --json",
        ]

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
