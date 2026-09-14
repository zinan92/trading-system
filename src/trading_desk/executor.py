"""Approve -> preview -> human-pressed execute, for Hyperliquid Testnet grids only.

Nothing here runs on a timer or page load. `preview` never places orders. `execute` is
only reachable from the 执行 button Park presses, and refuses while any grid is live.
"""
from __future__ import annotations

import json
import plistlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config

LIVE_COORDINATOR_STATES = {"grid_running", "grid_paused_range", "grid_blocked", "dca_running", "candidate_selected", "stop_requested"}
RETRYABLE = ("market_price_mismatch", "market_fact_unavailable", "market_observation_mismatch", "market_bbo_inconsistent", "mid_outside_bbo")


class ExecutionRefused(Exception):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def strategy_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("kind") != "new" or plan.get("direction") not in {"long", "short"}:
        raise ExecutionRefused("这份计划不需要执行（沿用现有网格或观望）。")
    low, high = plan["range"]
    return {
        "direction": plan["direction"],
        "range": {"low": round(float(low), 1), "high": round(float(high), 1)},
        "grid": {"count": len(plan["rungs"]), "notional_per_grid": float(plan.get("notional_per_rung") or 19), "notional_mode": "manual"},
        "hard_stop": round(float(plan["hard_stop"]), 1),
    }


class Executor:
    def __init__(self, config: Config, fetch: Callable[[str, dict[str, Any] | None], Any],
                 run: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
        self.config = config
        self.fetch = fetch
        self.run = run or subprocess.run

    # ---- read ----------------------------------------------------------------
    def coordinator(self) -> dict[str, Any]:
        path = self.config.paper_output / "testnet_automation" / "current.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "unknown"}
        data = data[-1] if isinstance(data, list) and data else data
        return data if isinstance(data, dict) else {"status": "unknown"}

    # ---- preview (no orders) ---------------------------------------------------
    def preview(self, asset: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        if asset["kind"] != "hl_testnet":
            raise ExecutionRefused("黄金纸面盘暂不支持从交易台执行，只能记录判断。")
        strategy = strategy_from_plan(plan)
        body = {"venue_profile_id": asset["venue_profile_id"], "instrument_id": asset["instrument_id"], "strategy_family": "grid", "strategy": strategy}
        response = self.fetch(f"{self.config.dashboard_url}/api/dashboard-control/preview", body)
        preview = (response or {}).get("preview") or {}
        inner = preview.get("preview") or {}
        return {
            "strategy": strategy,
            "execution_ready": preview.get("execution_ready") is True,
            "blockers": preview.get("blockers") or [],
            "preview_digest": preview.get("preview_digest"),
            "range": inner.get("range"),
            "orders": [{"price": o.get("price"), "notional": o.get("notional")} for o in inner.get("orders") or []],
            "max_loss": (inner.get("risk") or {}).get("max_loss"),
        }

    # ---- execute (only from the human-pressed button) --------------------------
    def execute(self, asset: dict[str, Any], plan: dict[str, Any], *, shown_max_loss: float | None, judgment_id: int) -> dict[str, Any]:
        coordinator = self.coordinator()
        if coordinator.get("status") in LIVE_COORDINATOR_STATES:
            raise ExecutionRefused("测试盘上已经有网格在跑。交易台暂不支持从前端停止网格，请先让执行员按流程停掉当前网格。",
                                   {"coordinator_status": coordinator.get("status")})
        if coordinator.get("status") == "unknown":
            raise ExecutionRefused("读不到交易后台状态，为安全起见不执行。")
        fresh = self.preview(asset, plan)
        if not fresh["execution_ready"] or not fresh["preview_digest"]:
            raise ExecutionRefused("预览没有通过风险检查，没有下单。", {"blockers": fresh["blockers"]})
        if shown_max_loss is not None and fresh["max_loss"] is not None and float(fresh["max_loss"]) > float(shown_max_loss) * 1.05:
            raise ExecutionRefused("价格变动后最多亏损变大了，请看新的预览后重新按执行。", {"preview": fresh})
        digest = fresh["preview_digest"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        confirmation = {"preview_digest": digest, "acknowledged": True, "operator_id": "park",
                        "statement": f"Park 在交易台亲自按下「执行」：判断 #{judgment_id}，{asset['key']} {plan['direction']} 网格，{stamp}"}
        confirmed = self.fetch(f"{self.config.dashboard_url}/api/dashboard-control/confirm", {"preview_digest": digest, "confirmation": confirmation})
        confirmed = (confirmed or {}).get("confirmation", confirmed) or {}
        if confirmed.get("status") != "confirmed" or not confirmed.get("activation_id"):
            raise ExecutionRefused("确认没有通过，没有下单。", {"confirmation": {k: confirmed.get(k) for k in ("status", "blockers")}})
        receipts = self._drive(confirmed["activation_id"], approval_id=f"park-desk-{asset['key'].lower()}-{stamp}", tag=stamp)
        final = receipts[-1] if receipts else {}
        return {"preview": fresh, "activation_id": confirmed["activation_id"], "attempts": receipts,
                "status": final.get("status"), "started": final.get("status") == "grid_running"}

    def _drive(self, activation_id: str, *, approval_id: str, tag: str) -> list[dict[str, Any]]:
        env = self._driver_env()
        receipts_dir = self.config.db_path.parent / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for attempt in range(1, 5):
            receipt = receipts_dir / f"{tag}-{attempt}.json"
            args = [str(self.config.nautilus_python), "-m", "pipelines.testnet_proof_driver",
                    "--output-root", str(self.config.paper_output), "--activation-id", activation_id,
                    "--approved-by", "park", "--approval-id", approval_id,
                    "--secret-file", env["HYPERLIQUID_TESTNET_SECRET_FILE"], "--account-address", env["HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS"],
                    "--runtime-id", env["HYPERLIQUID_TESTNET_RUNTIME_ID"], "--release-sha", env["TRADING_ORCHESTRATOR_RELEASE_SHA"],
                    "--receipt", str(receipt)]
            self.run(args, cwd=str(self.config.trading_system_checkout), env=env, capture_output=True, text=True, timeout=240)
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {"status": "no_receipt"}
            summary = {"attempt": attempt, "status": data.get("status"), "reason_code": data.get("reason_code"),
                       "lifecycle_status": (data.get("result") or {}).get("lifecycle_status")}
            results.append(summary)
            text = json.dumps(summary)
            if not (summary["status"] == "no_receipt" or any(code in text for code in RETRYABLE)):
                break
        return results

    def _driver_env(self) -> dict[str, str]:
        with self.config.dashboard_plist.open("rb") as handle:
            env = dict(plistlib.load(handle).get("EnvironmentVariables") or {})
        try:
            for line in self.config.control_script.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^export (TRADING_ORCHESTRATOR_NAUTILUS\w*)=(.*)$", line.strip())
                if match:
                    env[match.group(1)] = match.group(2).strip().strip('"').strip("'")
        except OSError:
            pass
        env["PYTHONPATH"] = f"{self.config.trading_system_checkout}:{self.config.standard_broker_src}"
        env.setdefault("HOME", str(Path.home()))
        missing = [k for k in ("HYPERLIQUID_TESTNET_SECRET_FILE", "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS", "HYPERLIQUID_TESTNET_RUNTIME_ID", "TRADING_ORCHESTRATOR_RELEASE_SHA") if not env.get(k)]
        if missing:
            raise ExecutionRefused("执行环境配置不完整，没有下单。", {"missing": missing})
        return env
