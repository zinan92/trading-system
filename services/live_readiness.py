from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.broker_adapter import broker_preflight
from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.data_gap_doctor import DataGapDoctor
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.live_env import LiveEnvStatus
from services.oanda_account_preflight import OandaAccountPreflight
from services.official_market_data_gate import official_broker_ohlc_status


class LiveReadiness:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        broker = broker_preflight(self.output_root)
        source_identity = self._market_data_identity_for_broker(broker)
        data_source = DataSourcePreflight(
            self.output_root,
            self.market_db,
            symbol=source_identity["symbol"],
            timeframe=source_identity["timeframe"],
            write_legacy_artifacts=source_identity["write_legacy_artifacts"],
        ).run(run_date)
        live_env = LiveEnvStatus(self.output_root).run(run_date)
        oanda_account = OandaAccountPreflight(output_root=self.output_root).run(run_date)
        gaps = DataGapDoctor(
            self.output_root,
            self.market_db,
            write_legacy_artifacts=source_identity["write_legacy_artifacts"],
        ).run(run_date, symbol=source_identity["symbol"], timeframe=source_identity["timeframe"])
        checks = [
            self._official_market_data(data_source),
            self._data_gaps(gaps),
            self._execution_mode(broker),
            self._broker_provider(broker),
            self._broker_credentials(broker),
            self._live_env(live_env),
            self._broker_dry_run(broker),
            self._oanda_account(broker, oanda_account),
            self._risk_rules(),
            self._runner(),
            self._journal_review(run_date),
            self._broker_feedback(broker),
        ]
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "live_ready": status == "pass",
            "summary": self._summary(checks),
            "checks": checks,
            "next_actions": self._next_actions(checks),
        }
        write_json(self.output_root / "live_readiness" / "current.json", [payload])
        write_json(self.output_root / "live_readiness" / f"{run_date}.json", [payload])
        return payload

    def _market_data_identity_for_broker(self, broker: dict) -> dict:
        provider = str(broker.get("provider") or self.config.get("broker", {}).get("provider", ""))
        if provider == "tiger_openapi":
            feed = self.config.get("tiger_futures_feed", {}) or {}
            symbol = str(feed.get("output_symbol") or feed.get("contract") or "MGCmain")
            timeframe = str(feed.get("timeframe") or "1m")
            return {
                "provider": provider,
                "symbol": symbol,
                "timeframe": timeframe,
                "write_legacy_artifacts": False,
            }
        return {
            "provider": provider,
            "symbol": "GOLD",
            "timeframe": "5m",
            "write_legacy_artifacts": True,
        }

    def _official_market_data(self, data_source: dict) -> dict:
        ready, summary = official_broker_ohlc_status(data_source)
        return self._check("official_market_data", "pass" if ready else "fail", summary, data_source)

    def _data_gaps(self, gaps: dict) -> dict:
        symbol = str(gaps.get("symbol") or "GOLD")
        timeframe = str(gaps.get("timeframe") or "5m")
        if gaps.get("status") == "pass":
            return self._check("data_gaps", "pass", f"No hard {symbol} {timeframe} data gaps detected.", gaps)
        if gaps.get("status") == "warn":
            return self._check("data_gaps", "fail", f"Live mode requires continuous {symbol} {timeframe} bars; warning gaps must be resolved first.", gaps)
        return self._check("data_gaps", "fail", "Live mode blocked by missing or failing data gap check.", gaps)

    def _execution_mode(self, broker: dict) -> dict:
        mode = str(self.config.get("execution_mode", "paper")).lower()
        live_enabled = bool(self.config.get("live_trading_enabled", False))
        evidence = {"execution_mode": mode, "live_trading_enabled": live_enabled, "broker_preflight": broker}
        if mode == "live" and live_enabled:
            return self._check("execution_mode", "pass", "Execution config is explicitly set to live.", evidence)
        return self._check("execution_mode", "fail", "Execution is still protected by paper mode or live_trading_enabled=false.", evidence)

    def _broker_provider(self, broker: dict) -> dict:
        provider = str(broker.get("provider") or self.config.get("broker", {}).get("provider", ""))
        allowed = {"oanda_rest", "mt5_file_bridge", "binance_usdm", "tiger_openapi"}
        if broker.get("mode") != "live":
            return self._check("broker_provider", "fail", "Broker preflight is still in paper mode; live broker provider is not active.", {"provider": provider, "allowed": sorted(allowed), "broker_preflight": broker})
        if provider in allowed and broker.get("ready"):
            return self._check("broker_provider", "pass", f"Live broker provider is ready: {provider}.", broker)
        if provider in allowed:
            return self._check("broker_provider", "fail", f"Live broker provider is configured but preflight is not ready: {provider}.", broker)
        return self._check("broker_provider", "fail", "No supported live broker provider is active.", {"provider": provider, "allowed": sorted(allowed), "broker_preflight": broker})

    def _broker_credentials(self, broker: dict) -> dict:
        provider = str(broker.get("provider") or self.config.get("broker", {}).get("provider", ""))
        missing = broker.get("missing_env") or []
        if provider == "oanda_rest":
            if not missing and broker.get("ready"):
                return self._check("broker_credentials", "pass", "OANDA account credentials are present.", broker)
            return self._check("broker_credentials", "fail", "OANDA live broker credentials are missing or invalid.", broker)
        if provider == "mt5_file_bridge":
            writable = bool(broker.get("outbox_writable")) and bool(broker.get("inbox_writable"))
            status = "pass" if writable else "fail"
            return self._check("broker_credentials", status, "MT5 bridge directories are writable." if writable else "MT5 bridge directories are not fully writable.", broker)
        if provider == "binance_usdm":
            if not missing and broker.get("ready"):
                return self._check("broker_credentials", "pass", "Binance USDM API credentials are present.", broker)
            return self._check("broker_credentials", "fail", "Binance USDM live broker credentials are missing or invalid.", broker)
        if provider == "tiger_openapi":
            if not missing and broker.get("props_path_exists") and broker.get("props_path_owner_only") and broker.get("ready"):
                return self._check("broker_credentials", "pass", "Tiger OpenAPI config file is present and owner-only.", broker)
            return self._check("broker_credentials", "fail", "Tiger OpenAPI config file is missing, unsafe, or preflight is not ready.", broker)
        return self._check("broker_credentials", "fail", "No live broker credential check is available for the active provider.", broker)

    def _live_env(self, live_env: dict) -> dict:
        if live_env.get("status") == "pass":
            return self._check("live_env", "pass", "Local live env file/env vars provide required broker keys.", live_env)
        return self._check("live_env", "fail", "Local live env is missing required broker/OANDA keys.", live_env)

    def _broker_dry_run(self, broker: dict) -> dict:
        dry_run = bool(broker.get("dry_run", True))
        if broker.get("ready") and not dry_run:
            return self._check("broker_dry_run", "pass", "Broker dry_run is disabled after all other live gates pass.", broker)
        return self._check("broker_dry_run", "fail", "Broker dry_run is still enabled or broker preflight is not ready.", broker)

    def _oanda_account(self, broker: dict, oanda_account: dict) -> dict:
        provider = str(broker.get("provider") or self.config.get("broker", {}).get("provider", ""))
        if provider != "oanda_rest":
            return self._check("oanda_account", "pass", "OANDA account preflight is not required for the active broker provider.", {"provider": provider, "oanda_account": oanda_account})
        if oanda_account.get("status") == "pass" and oanda_account.get("account_ready") and oanda_account.get("instrument_ready"):
            return self._check("oanda_account", "pass", "OANDA account and XAU_USD instrument are reachable.", oanda_account)
        return self._check("oanda_account", "fail", "OANDA account or XAU_USD instrument preflight is not ready.", oanda_account)

    def _risk_rules(self) -> dict:
        rules = load_risk_rules().get("default", {})
        required = ["max_loss_pct", "daily_loss_stop_pct", "event_window_size_multiplier"]
        missing = [key for key in required if key not in rules]
        if missing:
            return self._check("risk_rules", "fail", "Core live risk rules are missing.", {"missing": missing, "rules": rules})
        return self._check("risk_rules", "pass", "Core live risk rules are configured.", {key: rules.get(key) for key in required})

    def _runner(self) -> dict:
        runner = self._load_mapping(self.output_root / "runner_status" / "current.json")
        if not runner:
            return self._check("runner", "fail", "5m runner is not running or has no current status.", {})
        interval = int(runner.get("interval_seconds", 0) or 0)
        if runner.get("state") in {"ok", "running"} and interval == 300:
            return self._check("runner", "pass", "Runner is healthy or actively running on the 5m cadence.", runner)
        return self._check("runner", "fail", "Runner must be healthy with interval_seconds=300 before live trading.", runner)

    def _journal_review(self, run_date: str) -> dict:
        required = [
            self.output_root / "journals" / f"{run_date}.md",
            self.output_root / "review_notes" / f"{run_date}.md",
            self.output_root / "reports" / f"{run_date}.md",
            self.output_root / "strategy_reviews" / f"{run_date}.json",
            self.output_root / "learning_ledger" / f"{run_date}.json",
            self.output_root / "strategy_change_proposals" / f"{run_date}.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return self._check("journal_review", "fail", "Daily journal/review/learning artifacts are missing.", {"missing": missing})
        return self._check("journal_review", "pass", "Daily journal, report, review, and learning artifacts exist.", {"count": len(required)})

    def _broker_feedback(self, broker: dict) -> dict:
        provider = str(broker.get("provider") or self.config.get("broker", {}).get("provider", ""))
        if provider == "oanda_rest":
            feed = (load_json(self.output_root / "oanda_feed" / "current.json") or [{}])[-1]
            if feed.get("status") == "pass":
                return self._check("broker_feedback", "pass", "OANDA official feed importer has successful evidence.", feed)
            return self._check("broker_feedback", "fail", "OANDA live mode requires a successful official feed import first.", feed)
        if provider == "mt5_file_bridge":
            summary = (load_json(self.output_root / "broker_receipts" / "summary_current.json") or [{}])[-1]
            smoke = (load_json(self.output_root / "mt5_bridge_smoke" / "current.json") or [{}])[-1]
            if summary and not summary.get("errors") and smoke.get("status") == "pass":
                return self._check("broker_feedback", "pass", "MT5 bridge smoke and receipt importer have successful evidence.", {"receipt_summary": summary, "smoke": smoke})
            return self._check("broker_feedback", "fail", "MT5 live mode requires bridge smoke and receipt importer evidence.", {"receipt_summary": summary, "smoke": smoke})
        if provider == "binance_usdm":
            feed = (load_json(self.output_root / "binance_usdm_feed" / "current.json") or [{}])[-1]
            if feed.get("status") == "pass" and feed.get("ready"):
                return self._check("broker_feedback", "pass", "Binance USDM venue feed has successful evidence.", feed)
            return self._check("broker_feedback", "fail", "Binance live mode requires a successful Binance USDM venue feed import first.", feed)
        if provider == "tiger_openapi":
            return self._tiger_broker_feedback()
        return self._check("broker_feedback", "fail", "No broker feedback loop is available for the active provider.", {"provider": provider})

    def _tiger_broker_feedback(self) -> dict:
        readiness = (load_json(self.output_root / "tiger_price_feed_readiness" / "current.json") or [{}])[-1]
        acceptance = (load_json(self.output_root / "tiger_price_feed_acceptance" / "current.json") or [{}])[-1]
        evidence = {
            "price_feed_readiness": self._tiger_price_feed_readiness_evidence(readiness),
            "price_feed_acceptance": self._tiger_price_feed_acceptance_evidence(acceptance),
        }
        if readiness.get("ready_for_price_feed") is True:
            return self._check(
                "broker_feedback",
                "pass",
                "Tiger OpenAPI price feed has passed readiness and can be used as broker feedback evidence.",
                evidence,
            )
        if acceptance.get("status") == "pending_market_open":
            next_window = evidence["price_feed_acceptance"]["next_trading_window"].get("start") or "the next trading window"
            return self._check(
                "broker_feedback",
                "fail",
                f"Tiger OpenAPI price feed is waiting for market-hours acceptance; rerun after {next_window}.",
                evidence,
            )
        if acceptance.get("status") == "blocked":
            return self._check(
                "broker_feedback",
                "fail",
                "Tiger OpenAPI price feed acceptance is blocked; inspect tiger_price_feed_acceptance before live readiness.",
                evidence,
            )
        return self._check(
            "broker_feedback",
            "fail",
            "Tiger live mode requires a passing tiger_price_feed_readiness artifact first.",
            evidence,
        )

    def _tiger_price_feed_readiness_evidence(self, readiness: dict) -> dict:
        return {
            "status": str(readiness.get("status") or "missing"),
            "ready_for_price_feed": readiness.get("ready_for_price_feed") is True,
            "contract": str(readiness.get("contract") or ""),
            "blocker_count": len([row for row in (readiness.get("blockers") or []) if isinstance(row, dict)]),
            "checked_at": str(readiness.get("checked_at") or ""),
            "can_enable_broker_orders_from_this_gate": readiness.get("can_enable_broker_orders_from_this_gate") is True,
        }

    def _tiger_price_feed_acceptance_evidence(self, acceptance: dict) -> dict:
        steps = acceptance.get("steps", {}) if isinstance(acceptance.get("steps"), dict) else {}
        realtime = steps.get("realtime_validation", {}) if isinstance(steps.get("realtime_validation"), dict) else {}
        gate = realtime.get("market_hours_gate", {}) if isinstance(realtime.get("market_hours_gate"), dict) else {}
        next_window = gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {}
        return {
            "status": str(acceptance.get("status") or "missing"),
            "ready_for_price_feed": acceptance.get("ready_for_price_feed") is True,
            "exit_code": acceptance.get("exit_code"),
            "blocker_count": len([row for row in (acceptance.get("blockers") or []) if isinstance(row, dict)]),
            "operator_action": str(gate.get("operator_action") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "checked_at": str(acceptance.get("checked_at") or ""),
            "can_enable_broker_orders_from_this_gate": acceptance.get("can_enable_broker_orders_from_this_gate") is True,
        }

    def _summary(self, checks: list[dict]) -> dict:
        failed = [item["name"] for item in checks if item["status"] == "fail"]
        warned = [item["name"] for item in checks if item["status"] == "warn"]
        passed = [item["name"] for item in checks if item["status"] == "pass"]
        return {"passed": len(passed), "warned": len(warned), "failed": len(failed), "failed_checks": failed, "warned_checks": warned}

    def _next_actions(self, checks: list[dict]) -> list[str]:
        by_name = {item["name"]: item for item in checks}
        actions: list[str] = []
        if by_name.get("official_market_data", {}).get("status") == "fail":
            actions.append("Connect execution-grade XAUUSD/XAUUSDT 5m bars, such as Binance USDM, then rerun python3 -m pipelines.live_readiness --date <date>.")
        if by_name.get("execution_mode", {}).get("status") == "fail":
            actions.append("Keep paper mode until all live checks pass; only then set execution_mode=live and live_trading_enabled=true.")
        if by_name.get("broker_provider", {}).get("status") == "fail":
            actions.append("Configure broker.provider as binance_usdm, oanda_rest, mt5_file_bridge, or tiger_openapi and make its preflight pass.")
        if by_name.get("broker_credentials", {}).get("status") == "fail":
            actions.append("Copy configs/live.env.template to configs/live.env, set required broker credentials, then rerun broker preflight.")
        if by_name.get("oanda_account", {}).get("status") == "fail":
            actions.append("Run python3 -m pipelines.oanda_account --date <date> and confirm OANDA account plus XAU_USD instrument are reachable.")
        if by_name.get("broker_dry_run", {}).get("status") == "fail":
            actions.append("Do not disable broker dry_run until official data, broker credentials, runner, and journal checks all pass.")
        if by_name.get("runner", {}).get("status") == "fail":
            actions.append("Start the 5m runner with --interval-seconds 300 and confirm runner_status/current.json is ok.")
        if by_name.get("journal_review", {}).get("status") == "fail":
            actions.append("Generate the daily report, journal, strategy review, learning ledger, and proposal artifacts.")
        if not actions:
            actions.append("Live readiness checks pass; final human approval is still required before real-money execution.")
        return actions

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
