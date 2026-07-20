from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.broker_port import BrokerOrderRequest
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


class LiveSubmissionSafetySmoke:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        original_token = os.environ.get("OANDA_API_TOKEN")
        original_account = os.environ.get("OANDA_ACCOUNT_ID")
        blocked = False
        error = ""
        try:
            os.environ["OANDA_API_TOKEN"] = "dummy-token-for-safety-smoke"
            os.environ["OANDA_ACCOUNT_ID"] = "dummy-account-for-safety-smoke"
            adapter = build_broker_execution_port(
                BrokerBuildContext(
                    output_root=self.output_root,
                    execution_mode="live",
                    live_trading_enabled=True,
                    broker_config={
                        "provider": "oanda_rest",
                        "dry_run": False,
                        "allowed_symbols": ["GOLD", "XAUUSD"],
                    },
                    opener=self._forbidden_opener,
                )
            )
            adapter.submit_order(BrokerOrderRequest(run_date, self._ticket(), latest_price=4530.0, actual_size=0.01))
        except RuntimeError as exc:
            error = str(exc)
            blocked = "live activation gate is not real_money_ready" in error
        finally:
            self._restore_env("OANDA_API_TOKEN", original_token)
            self._restore_env("OANDA_ACCOUNT_ID", original_account)

        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if blocked else "fail",
            "blocked_by_activation_gate": blocked,
            "provider": "oanda_rest",
            "dry_run": False,
            "live_trading_enabled": True,
            "network_call_attempted": False,
            "error": error,
            "note": "Uses dummy env values only; credential values are not written and no broker request should be sent.",
        }
        write_json(self.output_root / "live_submission_safety" / "current.json", [payload])
        write_json(self.output_root / "live_submission_safety" / f"{run_date}.json", [payload])
        return payload

    def _forbidden_opener(self, *args, **kwargs):
        raise AssertionError("live submission safety smoke attempted a network call")

    def _restore_env(self, key: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _ticket(self) -> dict:
        return {
            "ticket_id": "ticket_live_submission_safety",
            "signal_id": "sig_live_submission_safety",
            "asset": "GOLD",
            "asset_class": "commodity",
            "action": "prepare_buy",
            "entry_zone": "4520-4540",
            "stop_loss": 4490,
            "targets": [4590],
            "position_size_pct": 0.1,
            "max_loss_pct": 0.05,
            "order_type": "market",
            "time_in_force": "ioc",
            "paper_only": False,
        }
