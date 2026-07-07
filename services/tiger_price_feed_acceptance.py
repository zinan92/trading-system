from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT, load_pipeline_config
from services.connector_catalog import ConnectorCatalog
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.tiger_price_feed_readiness import TigerPriceFeedReadiness
from services.tiger_realtime_validation import run_tiger_realtime_validation


class TigerPriceFeedAcceptance:
    """One-command acceptance receipt for Tiger price-feed promotion.

    This orchestration may refresh the read-only realtime validation artifact,
    then evaluates the artifact-only readiness gate and connector catalog. It
    never imports TradeClient, submits orders, or writes market bars.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        output_root: Path | None = None,
        *,
        quote_client: Any | None = None,
        sleeper: Callable[[float], None] | None = None,
        realtime_runner: Callable[..., dict[str, Any]] | None = None,
        market_db: Path | None = None,
    ) -> None:
        self._explicit_config = config is not None
        self.config = config if config is not None else load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))
        self.market_db = Path(market_db) if market_db else Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / self.config.get("local_market_db", "data/market_data.db"))))
        self.quote_client = quote_client
        self.sleeper = sleeper
        self.realtime_runner = realtime_runner or run_tiger_realtime_validation

    def run(
        self,
        run_date: str,
        *,
        contract: str | None = None,
        output_symbol: str | None = None,
        run_realtime: bool = True,
        as_of: str | datetime | None = None,
        trading_date: str | None = None,
        poll_seconds: float = 75.0,
        max_lag_seconds: float = 180.0,
        limit: int = 5,
        min_imported_rows: int = 500,
    ) -> dict[str, Any]:
        realtime = self._refresh_or_load_realtime(
            run_date,
            contract=contract,
            output_symbol=output_symbol,
            run_realtime=run_realtime,
            as_of=as_of,
            trading_date=trading_date,
            poll_seconds=poll_seconds,
            max_lag_seconds=max_lag_seconds,
            limit=limit,
        )
        readiness = TigerPriceFeedReadiness(self.output_root, min_imported_rows=min_imported_rows).run(run_date)
        catalog = ConnectorCatalog(
            config=self.config,
            output_root=self.output_root,
            load_live_env=not self._explicit_config,
        ).snapshot()
        write_json(self.output_root / "connector_catalog" / "current.json", [catalog])
        write_json(self.output_root / "connector_catalog" / f"{run_date}.json", [catalog])
        resolved_contract = self._contract(contract, readiness, realtime)
        data_source_preflight = self._refresh_data_source_preflight(
            run_date,
            contract=resolved_contract,
            output_symbol=output_symbol,
        )

        tiger_capabilities = self._tiger_capabilities(catalog)
        price_feed_capability = tiger_capabilities.get("price_feed", {})
        status = self._status(realtime, readiness, price_feed_capability)
        exit_code = self._exit_code(status)
        checked_at = self._now()
        operator_reference_time = self._operator_reference_time(
            as_of=as_of,
            run_realtime=run_realtime,
            realtime=realtime,
            checked_at=checked_at,
        )
        steps = {
            "realtime_validation": self._realtime_step(realtime, run_realtime),
            "price_feed_readiness": self._readiness_step(readiness),
            "connector_catalog": self._catalog_step(catalog, tiger_capabilities),
            "data_source_preflight": self._data_source_preflight_step(data_source_preflight),
        }
        next_commands = self._next_commands(run_date, resolved_contract)
        payload = {
            "schema_version": "tiger-price-feed-acceptance-v1",
            "run_date": run_date,
            "checked_at": checked_at,
            "provider": "tiger_openapi",
            "venue": "COMEX",
            "contract": resolved_contract,
            "status": status,
            "exit_code": exit_code,
            "ready_for_price_feed": status == "accepted",
            "can_enable_broker_orders_from_this_gate": False,
            "operator_next_action": self._operator_next_action(status, steps["realtime_validation"], operator_reference_time, next_commands),
            "steps": steps,
            "blockers": self._blockers(readiness, price_feed_capability),
            "evidence_paths": {
                "realtime_validation": str(self.output_root / "tiger_realtime_validation" / "current.json"),
                "price_feed_readiness": str(self.output_root / "tiger_price_feed_readiness" / "current.json"),
                "connector_catalog": str(self.output_root / "connector_catalog" / "current.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / self._source_key(data_source_preflight) / "current.json"),
                "acceptance": str(self.output_root / "tiger_price_feed_acceptance" / f"{run_date}.json"),
            },
            "next_commands": next_commands,
            "safety": {
                "read_only": True,
                "opens_quote_client": run_realtime and self.quote_client is None,
                "opens_trade_client": False,
                "opens_order_clients": False,
                "submits_orders": False,
                "writes_market_db": False,
                "writes_acceptance_artifact": True,
                "refreshes_price_feed_readiness_artifact": True,
                "refreshes_connector_catalog_artifact": True,
                "refreshes_data_source_preflight_artifact": True,
                "credential_values_exposed": False,
            },
        }
        write_json(self.output_root / "tiger_price_feed_acceptance" / "current.json", [payload])
        write_json(self.output_root / "tiger_price_feed_acceptance" / f"{run_date}.json", [payload])
        return payload

    def plan(
        self,
        run_date: str,
        *,
        contract: str | None = None,
        output_symbol: str | None = None,
        poll_seconds: float = 75.0,
        max_lag_seconds: float = 180.0,
        limit: int = 5,
        min_imported_rows: int = 500,
    ) -> dict[str, Any]:
        """Write a local operator plan without opening Tiger SDK clients.

        This is a preflight for humans: it describes the next acceptance
        sequence and safety boundary, but it does not refresh realtime
        validation, catalog, readiness, data-source preflight, or runtime
        config artifacts.
        """
        realtime = self._latest("tiger_realtime_validation")
        readiness = self._latest("tiger_price_feed_readiness")
        acceptance = self._latest("tiger_price_feed_acceptance")
        resolved_contract = self._contract(contract, readiness, realtime)
        resolved_output_symbol = self._output_symbol(output_symbol, resolved_contract)
        checked_at = self._now()
        commands = self._plan_commands(run_date, resolved_contract, poll_seconds)
        payload = {
            "schema_version": "tiger-price-feed-acceptance-plan-v1",
            "run_date": run_date,
            "checked_at": checked_at,
            "provider": "tiger_openapi",
            "venue": "COMEX",
            "contract": resolved_contract,
            "output_symbol": resolved_output_symbol,
            "timeframe": "1m",
            "status": "plan_ready",
            "exit_code": 0,
            "ready_for_price_feed": False,
            "can_enable_broker_orders_from_this_gate": False,
            "operator_next_action": {
                "status": "review_plan_then_run_acceptance",
                "summary": "Review the plan-only safety boundary, then run the Tiger price-feed acceptance command when you want to refresh market-hours evidence.",
                "next_command": commands[0]["command"] if commands else "",
            },
            "parameters": {
                "poll_seconds": poll_seconds,
                "max_lag_seconds": max_lag_seconds,
                "limit": limit,
                "min_imported_rows": min_imported_rows,
            },
            "latest_evidence": {
                "acceptance": self._artifact_snapshot(acceptance),
                "readiness": self._artifact_snapshot(readiness),
                "realtime_validation": self._artifact_snapshot(realtime),
            },
            "evidence_paths": {
                "plan": str(self.output_root / "tiger_price_feed_acceptance_plan" / f"{run_date}.json"),
                "acceptance": str(self.output_root / "tiger_price_feed_acceptance" / "current.json"),
                "realtime_validation": str(self.output_root / "tiger_realtime_validation" / "current.json"),
                "price_feed_readiness": str(self.output_root / "tiger_price_feed_readiness" / "current.json"),
            },
            "next_commands": [row["command"] for row in commands],
            "command_sequence": commands,
            "safety": {
                "plan_only": True,
                "read_only": True,
                "opens_quote_client": False,
                "opens_trade_client": False,
                "opens_order_clients": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_plan_artifact": True,
                "writes_acceptance_artifact": False,
                "refreshes_realtime_validation_artifact": False,
                "refreshes_price_feed_readiness_artifact": False,
                "refreshes_connector_catalog_artifact": False,
                "refreshes_data_source_preflight_artifact": False,
                "credential_values_exposed": False,
            },
        }
        write_json(self.output_root / "tiger_price_feed_acceptance_plan" / "current.json", [payload])
        write_json(self.output_root / "tiger_price_feed_acceptance_plan" / f"{run_date}.json", [payload])
        return payload

    def _refresh_or_load_realtime(
        self,
        run_date: str,
        *,
        contract: str | None,
        output_symbol: str | None,
        run_realtime: bool,
        as_of: str | datetime | None,
        trading_date: str | None,
        poll_seconds: float,
        max_lag_seconds: float,
        limit: int,
    ) -> dict[str, Any]:
        if not run_realtime:
            return self._latest("tiger_realtime_validation")

        overrides: dict[str, Any] = {}
        if contract:
            overrides["contract"] = contract
            overrides.setdefault("output_symbol", contract)
        if output_symbol:
            overrides["output_symbol"] = output_symbol
        try:
            return self.realtime_runner(
                run_date,
                output_root=self.output_root,
                config=overrides,
                quote_client=self.quote_client,
                as_of=as_of,
                trading_date=trading_date,
                poll_seconds=poll_seconds,
                max_lag_seconds=max_lag_seconds,
                limit=limit,
                require_market_hours_pass=True,
                sleeper=self.sleeper,
            )
        except Exception as exc:  # noqa: BLE001 - acceptance should still produce a receipt.
            payload = self._realtime_exception_payload(run_date, contract, output_symbol, exc)
            write_json(self.output_root / "tiger_realtime_validation" / "current.json", [payload])
            write_json(self.output_root / "tiger_realtime_validation" / f"{run_date}.json", [payload])
            return payload

    def _latest(self, artifact: str) -> dict[str, Any]:
        rows = load_json(self.output_root / artifact / "current.json")
        return rows[-1] if rows and isinstance(rows[-1], dict) else {}

    def _realtime_exception_payload(
        self,
        run_date: str,
        contract: str | None,
        output_symbol: str | None,
        exc: Exception,
    ) -> dict[str, Any]:
        feed = self.config.get("tiger_futures_feed", {}) or {}
        return {
            "schema_version": "tiger-realtime-validation-v1",
            "run_date": run_date,
            "provider": str(feed.get("provider") or "tiger_openapi:COMEX"),
            "contract": str(contract or feed.get("contract") or "MGCmain"),
            "output_symbol": str(output_symbol or contract or feed.get("output_symbol") or feed.get("contract") or "MGCmain"),
            "timeframe": str(feed.get("timeframe") or "1m"),
            "checked_at": self._now(),
            "status": "fail",
            "message": f"Tiger realtime acceptance runner raised {type(exc).__name__}; inspect local runtime logs.",
            "market_hours_gate": {
                "required": True,
                "ready_for_price_feed_promotion": False,
                "market_hours_observed": False,
                "exit_code": 2,
                "operator_action": "review_realtime_validation_exception",
                "next_trading_window": {"start": "", "end": "", "trading_date": ""},
            },
            "safety": {
                "read_only": True,
                "writes_market_db": False,
                "opens_quote_client": self.quote_client is None,
                "opens_trade_client": False,
                "opens_order_clients": False,
                "submits_orders": False,
            },
        }

    def _refresh_data_source_preflight(
        self,
        run_date: str,
        *,
        contract: str,
        output_symbol: str | None,
    ) -> dict[str, Any]:
        feed = self.config.get("tiger_futures_feed", {}) or {}
        symbol = str(output_symbol or feed.get("output_symbol") or contract or feed.get("contract") or "MGCmain")
        timeframe = str(feed.get("timeframe") or "1m")
        try:
            return DataSourcePreflight(
                output_root=self.output_root,
                market_db=self.market_db,
                symbol=symbol,
                timeframe=timeframe,
                write_legacy_artifacts=False,
            ).run(run_date)
        except Exception as exc:  # noqa: BLE001 - acceptance should still produce an operator receipt.
            payload = {
                "schema_version": "data-source-preflight-exception-v1",
                "run_date": run_date,
                "checked_at": self._now(),
                "symbol": symbol,
                "timeframe": timeframe,
                "source_key": f"{symbol}_{timeframe}",
                "status": "error",
                "ready_for_paper": False,
                "ready_for_live": False,
                "live_data_mode": "not_live_ready",
                "message": f"DataSourcePreflight raised {type(exc).__name__}; inspect local runtime logs.",
                "error": f"{type(exc).__name__}: {exc}",
                "can_enable_broker_orders_from_this_gate": False,
            }
            scoped = self.output_root / "data_source_preflight" / str(payload["source_key"])
            write_json(scoped / "current.json", [payload])
            write_json(scoped / f"{run_date}.json", [payload])
            return payload

    def _status(self, realtime: dict[str, Any], readiness: dict[str, Any], price_feed_capability: dict[str, Any]) -> str:
        if readiness.get("ready_for_price_feed") is True and price_feed_capability.get("status") == "ready":
            return "accepted"
        gate = realtime.get("market_hours_gate", {}) if isinstance(realtime.get("market_hours_gate"), dict) else {}
        if realtime.get("status") == "pending_market_open" or gate.get("exit_code") == 75:
            return "pending_market_open"
        for blocker in readiness.get("blockers", []) or []:
            if isinstance(blocker, dict) and blocker.get("name") == "realtime_market_hours_gate":
                blocker_gate = blocker.get("evidence", {}).get("market_hours_gate", {}) if isinstance(blocker.get("evidence"), dict) else {}
                if blocker_gate.get("exit_code") == 75:
                    return "pending_market_open"
        return "blocked"

    def _exit_code(self, status: str) -> int:
        if status == "accepted":
            return 0
        if status == "pending_market_open":
            return 75
        return 2

    def _blockers(self, readiness: dict[str, Any], price_feed_capability: dict[str, Any]) -> list[dict[str, Any]]:
        blockers = [item for item in readiness.get("blockers", []) or [] if isinstance(item, dict)]
        if price_feed_capability and price_feed_capability.get("status") != "ready":
            blockers.append(
                {
                    "name": "connector_catalog_price_feed",
                    "status": str(price_feed_capability.get("status") or "missing"),
                    "summary": str(price_feed_capability.get("summary") or "Tiger connector price feed is not ready."),
                }
            )
        return blockers

    def _realtime_step(self, realtime: dict[str, Any], run_realtime: bool) -> dict[str, Any]:
        gate = realtime.get("market_hours_gate", {}) if isinstance(realtime.get("market_hours_gate"), dict) else {}
        return {
            "ran_this_acceptance": run_realtime,
            "status": str(realtime.get("status") or "missing"),
            "message": str(realtime.get("message") or ""),
            "checked_at": str(realtime.get("checked_at") or ""),
            "market_hours_gate": {
                "required": gate.get("required") is True,
                "ready_for_price_feed_promotion": gate.get("ready_for_price_feed_promotion") is True,
                "market_hours_observed": gate.get("market_hours_observed") is True,
                "exit_code": gate.get("exit_code"),
                "operator_action": str(gate.get("operator_action") or ""),
                "next_trading_window": gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {},
            },
        }

    def _readiness_step(self, readiness: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": str(readiness.get("status") or "missing"),
            "ready_for_price_feed": readiness.get("ready_for_price_feed") is True,
            "blocker_count": len([item for item in readiness.get("blockers", []) or [] if isinstance(item, dict)]),
            "summary": readiness.get("summary", {}) if isinstance(readiness.get("summary"), dict) else {},
        }

    def _catalog_step(self, catalog: dict[str, Any], tiger_capabilities: dict[str, dict[str, Any]]) -> dict[str, Any]:
        summary = catalog.get("summary", {}) if isinstance(catalog.get("summary"), dict) else {}
        return {
            "schema_version": str(catalog.get("schema_version") or ""),
            "ready_price_feed_count": summary.get("ready_price_feed_count"),
            "ready_broker_count": summary.get("ready_broker_count"),
            "tiger_price_feed_status": str((tiger_capabilities.get("price_feed") or {}).get("status") or "missing"),
            "tiger_broker_order_status": str((tiger_capabilities.get("broker_order") or {}).get("status") or "missing"),
        }

    def _data_source_preflight_step(self, data_source_preflight: dict[str, Any]) -> dict[str, Any]:
        gate = data_source_preflight.get("execution_venue_readiness_gate", {}) if isinstance(data_source_preflight.get("execution_venue_readiness_gate"), dict) else {}
        return {
            "status": str(data_source_preflight.get("status") or "missing"),
            "source_key": self._source_key(data_source_preflight),
            "ready_for_paper": data_source_preflight.get("ready_for_paper") is True,
            "ready_for_live": data_source_preflight.get("ready_for_live") is True,
            "latest_provider": str(data_source_preflight.get("latest_provider") or ""),
            "latest_timestamp": str(data_source_preflight.get("latest_timestamp") or ""),
            "live_data_mode": str(data_source_preflight.get("live_data_mode") or ""),
            "gate_status": str(gate.get("status") or "missing"),
            "gate_allows_live": gate.get("allows_live") is True,
            "message": str(data_source_preflight.get("message") or ""),
            "can_enable_broker_orders_from_this_gate": False,
        }

    def _operator_reference_time(
        self,
        *,
        as_of: str | datetime | None,
        run_realtime: bool,
        realtime: dict[str, Any],
        checked_at: str,
    ) -> str:
        if as_of is not None:
            return as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)
        if not run_realtime and realtime.get("checked_at"):
            return str(realtime.get("checked_at") or checked_at)
        return checked_at

    def _source_key(self, data_source_preflight: dict[str, Any]) -> str:
        source_key = str(data_source_preflight.get("source_key") or "")
        if source_key:
            return source_key
        symbol = str(data_source_preflight.get("symbol") or "MGCmain")
        timeframe = str(data_source_preflight.get("timeframe") or "1m")
        return f"{symbol}_{timeframe}"

    def _output_symbol(self, output_symbol: str | None, contract: str) -> str:
        feed = self.config.get("tiger_futures_feed", {}) or {}
        return str(output_symbol or feed.get("output_symbol") or contract or feed.get("contract") or "MGCmain")

    def _artifact_snapshot(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": str(artifact.get("status") or "missing"),
            "checked_at": str(artifact.get("checked_at") or ""),
            "run_date": str(artifact.get("run_date") or ""),
            "contract": str(artifact.get("contract") or ""),
            "ready_for_price_feed": artifact.get("ready_for_price_feed") is True,
        }

    def _operator_next_action(self, status: str, realtime_step: dict[str, Any], checked_at: str, next_commands: list[str]) -> dict[str, Any]:
        if status == "accepted":
            return {
                "status": "accepted",
                "summary": "Tiger price feed accepted.",
                "next_command": "",
            }
        if status == "blocked":
            return {
                "status": "blocked",
                "summary": "Tiger price-feed acceptance is blocked; inspect blockers before retrying.",
                "next_command": next_commands[0] if next_commands else "",
            }
        if status != "pending_market_open":
            return {
                "status": status,
                "summary": f"Tiger price-feed acceptance status is {status}.",
                "next_command": next_commands[0] if next_commands else "",
            }

        gate = realtime_step.get("market_hours_gate", {}) if isinstance(realtime_step.get("market_hours_gate"), dict) else {}
        next_window = gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {}
        now = self._parse_time(checked_at)
        start = self._parse_time(str(next_window.get("start") or ""))
        end = self._parse_time(str(next_window.get("end") or ""))
        if start and now and now < start:
            return {
                "status": "waiting_market_open",
                "summary": f"Wait until {start.isoformat()} to rerun Tiger price-feed acceptance.",
                "next_command": next_commands[0] if next_commands else "",
                "next_trading_window": self._next_window_summary(next_window),
            }
        if start and now and (end is None or now <= end):
            return {
                "status": "rerun_acceptance_now",
                "summary": "COMEX window is open; rerun Tiger price-feed acceptance now.",
                "next_command": next_commands[0] if next_commands else "",
                "next_trading_window": self._next_window_summary(next_window),
            }
        if end and now and now > end:
            return {
                "status": "window_expired",
                "summary": "The recorded COMEX validation window has expired; rerun acceptance to compute the next window.",
                "next_command": next_commands[0] if next_commands else "",
                "next_trading_window": self._next_window_summary(next_window),
            }
        return {
            "status": "pending_market_open",
            "summary": "Tiger price-feed acceptance is waiting for a COMEX market-hours validation window.",
            "next_command": next_commands[0] if next_commands else "",
            "next_trading_window": self._next_window_summary(next_window),
        }

    def _next_window_summary(self, next_window: dict[str, Any]) -> dict[str, str]:
        return {
            "start": str(next_window.get("start") or ""),
            "end": str(next_window.get("end") or ""),
            "trading_date": str(next_window.get("trading_date") or ""),
        }

    def _parse_time(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    def _tiger_capabilities(self, catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
        for connector in catalog.get("connectors", []) or []:
            if isinstance(connector, dict) and connector.get("id") == "tiger_openapi":
                return {
                    str(capability.get("name") or ""): capability
                    for capability in connector.get("capabilities", []) or []
                    if isinstance(capability, dict)
                }
        return {}

    def _contract(self, contract: str | None, readiness: dict[str, Any], realtime: dict[str, Any]) -> str:
        return str(contract or readiness.get("contract") or realtime.get("contract") or "MGCmain")

    def _next_commands(self, run_date: str, contract: str) -> list[str]:
        return [
            f"python3 -m pipelines.tiger_price_feed_acceptance --date {run_date} --contract {contract} --poll-seconds 75 --json",
            f"python3 -m pipelines.tiger_realtime_validation --date {run_date} --contract {contract} --poll-seconds 75 --require-market-hours-pass",
            f"python3 -m pipelines.tiger_price_feed_readiness --date {run_date} --json",
        ]

    def _plan_commands(self, run_date: str, contract: str, poll_seconds: float) -> list[dict[str, Any]]:
        poll = self._format_seconds(poll_seconds)
        return [
            {
                "name": "refresh_tiger_price_feed_acceptance",
                "command": f"python3 -m pipelines.tiger_price_feed_acceptance --date {run_date} --contract {contract} --poll-seconds {poll} --json",
                "purpose": "Refresh Tiger/COMEX market-hours price-feed evidence.",
                "opens_quote_client": True,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_acceptance_artifact": True,
            },
            {
                "name": "refresh_final_readiness_audit",
                "command": "python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json",
                "purpose": "Recompute the attended-switch Go/No-Go result after fresh price-feed evidence.",
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_acceptance_artifact": False,
            },
            {
                "name": "show_connector_switch_status",
                "command": "python3 -m pipelines.connector_config_apply status --json",
                "purpose": "Confirm whether operator_stage has returned to ready_for_attended_config_switch.",
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_acceptance_artifact": False,
            },
        ]

    def _format_seconds(self, value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
