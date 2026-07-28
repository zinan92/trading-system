"""Read-only, fail-closed preflight for the Linux Paper runtime."""

from __future__ import annotations

import os
import platform
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from services.config_loader import (
    ROOT,
    load_dualtrack_config,
    load_pipeline_config,
)
from services.datafeed_market_client import DatafeedMarketClient
from services.journal_store import write_json
from services.paper_release_receipt import current_source_attestation


class CloudPaperPreflight:
    """Prove cloud dependencies without invoking a Paper control path."""

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        pipeline_config: dict[str, Any] | None = None,
        dualtrack_config: dict[str, Any] | None = None,
        datafeed_client: DatafeedMarketClient | None = None,
        platform_name: str | None = None,
        source_attestation: Callable[[], dict[str, Any]] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        self.pipeline = load_pipeline_config() if pipeline_config is None else pipeline_config
        self.dualtrack = (
            load_dualtrack_config() if dualtrack_config is None else dualtrack_config
        )
        self.output_root = Path(
            output_root
            or self.environment.get("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
            or ROOT / str(self.pipeline.get("output_root", "outputs"))
        )
        datafeed_url = str(
            self.environment.get("TRADING_ORCHESTRATOR_DATAFEED_URL")
            or (self.pipeline.get("datafeed") or {}).get("base_url")
            or "http://127.0.0.1:8100"
        ).rstrip("/")
        self.datafeed_url = datafeed_url
        self.datafeed_client = datafeed_client or DatafeedMarketClient(
            base_url=datafeed_url,
            timeout_seconds=float((self.pipeline.get("datafeed") or {}).get("timeout_seconds", 10)),
        )
        self.platform_name = platform_name or platform.system()
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation(ROOT)
        )
        self.command_runner = command_runner or subprocess.run
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self) -> dict[str, Any]:
        checks = [
            self._check("linux_os", self._linux_os),
            self._check("source_attestation", self._source),
            self._check("paper_only", self._paper_only),
            self._check("loopback_ports", self._loopback_ports),
            self._check("persistent_paths", self._persistent_paths),
            self._check("datafeed_health", self._datafeed_health),
            self._check("latest_execution_venue_candle", self._latest_candle),
            self._check("historical_execution_venue_candles", self._historical_candles),
            self._check("nautilus_runtime", self._nautilus_runtime),
        ]
        failed = [row for row in checks if row["status"] != "pass"]
        payload = {
            "schema_version": "cloud-paper-preflight-v1",
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "blocked" if failed else "pass",
            "paper_only": True,
            "control_actions_executed": 0,
            "checks": checks,
            "failed_check_ids": [row["id"] for row in failed],
            "next_action": (
                failed[0]["next_action"]
                if failed
                else "Cloud Paper dependencies are ready for service installation; no strategy was started."
            ),
        }
        receipt_path = self.output_root / "cloud" / "preflight" / "current.json"
        try:
            write_json(receipt_path, [payload])
        except Exception as exc:  # noqa: BLE001 - preserve a receipt outside broken storage.
            row = {
                "id": "receipt_write",
                "status": "blocked",
                "reason": "receipt_write_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "next_action": "Repair the persistent output root before installing services.",
            }
            payload["checks"].append(row)
            payload["failed_check_ids"].append("receipt_write")
            payload["status"] = "blocked"
            payload["next_action"] = row["next_action"]
            fallback = Path(
                self.environment.get("GRIDMIND_PREFLIGHT_FALLBACK_ROOT")
                or "/tmp/gridmind-cloud-preflight"
            ) / "current.json"
            write_json(fallback, [payload])
            payload["receipt_fallback"] = str(fallback)
        return payload

    def _check(self, check_id: str, probe: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            detail = probe()
            return {
                "id": check_id,
                "status": "pass",
                "reason": "",
                "next_action": "",
                **detail,
            }
        except Exception as exc:  # noqa: BLE001 - every failure must become a receipt.
            return {
                "id": check_id,
                "status": "blocked",
                "reason": f"{check_id}_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "next_action": self._next_action(check_id),
            }

    def _linux_os(self) -> dict[str, Any]:
        if self.platform_name != "Linux":
            raise RuntimeError(f"expected Linux; observed {self.platform_name}")
        return {"observed": self.platform_name}

    def _source(self) -> dict[str, Any]:
        attestation = self.source_attestation()
        if not attestation.get("tracked_tree_clean"):
            raise RuntimeError("tracked source tree is dirty")
        sha = str(attestation.get("source_sha") or "")
        tree = str(attestation.get("source_tree_sha") or "")
        if len(sha) != 40 or len(tree) != 40:
            raise RuntimeError("source SHA or tree SHA is invalid")
        return {"source_sha": sha, "source_tree_sha": tree, "tracked_tree_clean": True}

    def _paper_only(self) -> dict[str, Any]:
        engine = self.dualtrack.get("execution_engine") or {}
        violations: list[str] = []
        if str(self.pipeline.get("execution_mode") or "").lower() != "paper":
            violations.append("execution_mode")
        if self.pipeline.get("live_trading_enabled") is not False:
            violations.append("live_trading_enabled")
        if engine.get("real_money_eligible") is not False:
            violations.append("real_money_eligible")
        if str(engine.get("authoritative") or "") != "nautilus_paper":
            violations.append("authoritative_engine")
        if violations:
            raise RuntimeError(f"Paper-only invariants failed: {','.join(violations)}")
        return {
            "execution_mode": "paper",
            "live_trading_enabled": False,
            "real_money_eligible": False,
            "authoritative_engine": "nautilus_paper",
        }

    def _loopback_ports(self) -> dict[str, Any]:
        dashboard_url = str(
            self.environment.get("TRADING_ORCHESTRATOR_DASHBOARD_URL")
            or "http://127.0.0.1:8765"
        )
        for label, value in (
            ("datafeed", self.datafeed_url),
            ("dashboard", dashboard_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise RuntimeError(f"{label} must bind through a loopback URL")
        return {"datafeed_url": self.datafeed_url, "dashboard_url": dashboard_url}

    def _persistent_paths(self) -> dict[str, Any]:
        datafeed_db = Path(
            self.environment.get("KLINE_DB_PATH") or "/var/lib/gridmind/datafeed/kline.db"
        )
        env_path = Path(
            self.environment.get("TRADING_ORCHESTRATOR_LIVE_ENV")
            or "/etc/gridmind/paper.env"
        )
        for directory in (self.output_root, datafeed_db.parent):
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".gridmind-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        if not env_path.is_file():
            raise RuntimeError(f"Paper environment file is missing: {env_path}")
        if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
            raise RuntimeError(f"Paper environment file must not be group/world readable: {env_path}")
        return {
            "output_root": str(self.output_root),
            "datafeed_db": str(datafeed_db),
            "paper_env": str(env_path),
        }

    def _datafeed_health(self) -> dict[str, Any]:
        payload = self.datafeed_client.health()
        if payload.get("status") != "ok":
            raise RuntimeError(f"datafeed status is {payload.get('status')!r}")
        storage = payload.get("storage")
        if isinstance(storage, dict):
            if storage.get("status") != "ok":
                raise RuntimeError(f"datafeed storage status is {storage.get('status')!r}")
            return {
                "service_status": "ok",
                "storage_status": "ok",
                "storage_health_source": "datafeed_endpoint",
            }
        datafeed_db = Path(
            self.environment.get("KLINE_DB_PATH")
            or "/var/lib/gridmind/datafeed/kline.db"
        )
        if not datafeed_db.is_file():
            raise RuntimeError("legacy datafeed health omitted storage and owner DB is missing")
        with sqlite3.connect(f"file:{datafeed_db}?mode=ro", uri=True) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"datafeed owner DB quick_check is {quick_check!r}")
        return {
            "service_status": "ok",
            "storage_status": "ok",
            "storage_health_source": "owner_sqlite_quick_check",
        }

    def _latest_candle(self) -> dict[str, Any]:
        payload = self.datafeed_client.candles(
            asset_class="commodity",
            ticker="GOLD",
            timeframe="1m",
            limit=2,
            source="binance_usdm_futures",
            cache_policy="bypass",
            quality="strict",
            require_execution_venue=True,
        )
        return self._validate_candles(payload, require_fresh=True)

    def _historical_candles(self) -> dict[str, Any]:
        payload = self.datafeed_client.candles(
            asset_class="commodity",
            ticker="GOLD",
            timeframe="1m",
            limit=240,
            source="binance_usdm_futures",
            cache_policy="allow",
            quality="standard",
            require_execution_venue=True,
        )
        return self._validate_candles(payload, require_fresh=False)

    def _validate_candles(
        self,
        payload: dict[str, Any],
        *,
        require_fresh: bool,
    ) -> dict[str, Any]:
        candles = payload.get("candles")
        if not isinstance(candles, list) or not candles:
            raise RuntimeError("trusted candle response is empty")
        selected_source = payload.get("selected_source") or payload.get("source_mode")
        if selected_source != "binance_usdm_futures":
            raise RuntimeError("execution-venue source mismatch")
        if payload.get("execution_venue") is not True or payload.get("is_synthetic") is not False:
            raise RuntimeError("response is not a non-synthetic execution venue")
        if require_fresh and payload.get("fresh") is not True:
            raise RuntimeError("latest candle is not fresh")
        return {
            "candle_count": len(candles),
            "selected_source": selected_source,
            "fresh": payload.get("fresh"),
            "latest_timestamp": payload.get("latest_timestamp"),
        }

    def _nautilus_runtime(self) -> dict[str, Any]:
        runtime = str(
            self.environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON")
            or (self.dualtrack.get("execution_engine") or {}).get("shadow_runtime_path")
            or ""
        ).strip()
        if not runtime:
            raise RuntimeError("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON is missing")
        result = self.command_runner(
            [
                runtime,
                "-c",
                (
                    "import json, sys; import nautilus_trader; "
                    "print(json.dumps({'version': list(sys.version_info[:3])}))"
                ),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Nautilus import failed: {str(result.stderr or '')[-300:]}")
        return {"interpreter": runtime, "import": "nautilus_trader"}

    @staticmethod
    def _next_action(check_id: str) -> str:
        actions = {
            "linux_os": "Run this preflight on the target Linux host.",
            "source_attestation": "Deploy a clean committed SHA before installing services.",
            "paper_only": "Restore Paper-only flags; do not continue with live-capable configuration.",
            "loopback_ports": "Bind datafeed and Dashboard to loopback-only URLs.",
            "persistent_paths": "Create writable persistent output and datafeed directories.",
            "datafeed_health": "Start or repair the independent datafeed and its owner storage.",
            "latest_execution_venue_candle": "Restore fresh Binance USD-M execution-venue access.",
            "historical_execution_venue_candles": "Backfill or restore trusted historical GOLD candles.",
            "nautilus_runtime": "Build the dedicated Nautilus runtime and verify its interpreter path.",
        }
        return actions[check_id]
