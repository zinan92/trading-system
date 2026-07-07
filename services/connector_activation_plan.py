from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.connector_catalog import ConnectorCatalog
from services.connector_onboarding import ConnectorOnboardingDryRun, reject_secret_payload
from services.dualtrack_config import dualtrack_config as load_dualtrack_config
from services.journal_store import load_json, write_json


ACTIVATION_PLAN_SCHEMA_VERSION = "connector-activation-plan-v1"


DUALTRACK_TIGER_MGC_PROFILE_SCHEMA_VERSION = "dualtrack-profile-preview-v1"


def config_apply_patch_digest(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    digest: list[dict[str, Any]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        row = {
            "op": str(item.get("op") or ""),
            "path": str(item.get("path") or ""),
            "current": item.get("current"),
            "planned": item.get("planned"),
            "reason": str(item.get("reason") or ""),
            "risk": str(item.get("risk") or ""),
            "writes_config": item.get("writes_config") is True,
        }
        if item.get("config_file"):
            row["config_file"] = str(item.get("config_file") or "")
        digest.append(row)
    return digest


def build_config_apply_package_id(
    *,
    connector_id: str,
    requested_roles: list[str],
    status: str,
    patch_digest: list[dict[str, Any]],
    dualtrack_patch_digest: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> str:
    payload = {
        "schema": "connector-config-apply-package-v2",
        "connector_id": connector_id,
        "requested_roles": sorted(requested_roles),
        "status": status,
        "patch_digest": patch_digest,
        "dualtrack_patch_digest": dualtrack_patch_digest,
        "warnings": [
            {
                "name": str(row.get("name") or ""),
                "summary": str(row.get("summary") or ""),
            }
            for row in warnings
            if isinstance(row, dict)
        ],
        "blockers": [
            {
                "name": str(row.get("name") or ""),
                "summary": str(row.get("summary") or ""),
            }
            for row in blockers
            if isinstance(row, dict)
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class ConnectorActivationPlan:
    """Build a config activation preview without mutating runtime config."""

    def __init__(
        self,
        output_root: Path | None = None,
        *,
        config: dict | None = None,
        dualtrack_runtime_config: dict | None = None,
        load_live_env: bool | None = None,
    ) -> None:
        self._explicit_config = config is not None
        self._load_live_env = (not self._explicit_config) if load_live_env is None else bool(load_live_env)
        self.config = config if config is not None else load_pipeline_config()
        self.dualtrack_config = dualtrack_runtime_config if dualtrack_runtime_config is not None else load_dualtrack_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))

    def evaluate(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        onboarding = ConnectorOnboardingDryRun(
            self.output_root,
            config=self.config,
            load_live_env=self._load_live_env,
        ).evaluate(payload, persist=False)
        connector_id = str(onboarding.get("connector_id") or "")
        requested_roles = [str(item) for item in onboarding.get("requested_roles", [])]
        catalog = ConnectorCatalog(
            config=self.config,
            output_root=self.output_root,
            load_live_env=self._load_live_env,
        ).snapshot()
        connector = self._connector(catalog, connector_id)
        changes = self._changes(connector_id, requested_roles)
        blockers = list(onboarding.get("blockers", []))
        if requested_roles and not changes:
            blockers.append(
                {
                    "name": "activation:unsupported_connector",
                    "status": "fail",
                    "summary": f"No activation preview is implemented for {connector_id}.",
                    "evidence": {"connector_id": connector_id, "requested_roles": requested_roles},
                }
            )
        warnings = self._warnings(connector, connector_id, requested_roles)
        activation_gate = self._activation_gate(connector_id, requested_roles, blockers, warnings)
        dualtrack_profile_preview = self._dualtrack_profile_preview(connector_id, requested_roles, blockers, warnings)
        activation_runbook = self._activation_runbook(connector_id, requested_roles, changes, blockers, warnings, activation_gate)
        config_apply_package = self._config_apply_package(connector_id, requested_roles, changes, blockers, warnings, activation_gate, activation_runbook, dualtrack_profile_preview)
        switch_audit = self._switch_audit(connector_id, requested_roles, onboarding, blockers, warnings, activation_gate, activation_runbook, config_apply_package, dualtrack_profile_preview)
        result = {
            "schema_version": ACTIVATION_PLAN_SCHEMA_VERSION,
            "checked_at": self._now(),
            "status": "preview_ready" if not blockers else "blocked",
            "connector_id": connector_id,
            "label": connector.get("label", connector_id),
            "requested_roles": requested_roles,
            "environment": str(payload.get("environment") or onboarding.get("environment") or ""),
            "onboarding_status": onboarding.get("status"),
            "config_patch_preview": changes,
            "change_count": len(changes),
            "blockers": blockers,
            "warnings": warnings,
            "activation_gate": activation_gate,
            "dualtrack_profile_preview": dualtrack_profile_preview,
            "activation_runbook": activation_runbook,
            "config_apply_package": config_apply_package,
            "switch_audit": switch_audit,
            "not_included": self._not_included(connector_id, requested_roles),
            "next_actions": self._next_actions(connector_id, requested_roles, blockers, warnings),
            "artifacts": {
                "current": str(self.output_root / "connector_activation_plan" / "current.json"),
                "connector_history": str(self.output_root / "connector_activation_plan" / f"{connector_id}.json"),
            },
            "safety": {
                "dry_run": True,
                "preview_only": True,
                "stores_credentials": False,
                "credential_values_exposed": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "creates_order_control_endpoint": False,
            },
        }
        if persist:
            self._write(result)
        return result

    def _connector(self, catalog: dict[str, Any], connector_id: str) -> dict[str, Any]:
        for connector in catalog.get("connectors", []):
            if isinstance(connector, dict) and str(connector.get("id") or "") == connector_id:
                return connector
        known = [str(item.get("id") or "") for item in catalog.get("connectors", []) if isinstance(item, dict)]
        raise ValueError(f"unknown connector_id: {connector_id}; known connectors: {', '.join(known)}")

    def _changes(self, connector_id: str, requested_roles: list[str]) -> list[dict[str, Any]]:
        if connector_id == "tiger_openapi":
            return self._tiger_changes(requested_roles)
        if connector_id == "binance_usdm":
            return self._binance_changes(requested_roles)
        if connector_id == "yahoo_chart" and "price_feed" in requested_roles:
            return [
                self._set_change(
                    "gold_5m_backfill.provider",
                    self._get(["gold_5m_backfill", "provider"]),
                    "yahoo_chart",
                    "Use Yahoo Chart as the configured public backfill source.",
                    risk="low",
                )
            ]
        return []

    def _tiger_changes(self, requested_roles: list[str]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        if "price_feed" in requested_roles:
            changes.append(
                self._set_change(
                    "tiger_futures_feed.enabled",
                    self._get(["tiger_futures_feed", "enabled"]),
                    True,
                    "Enable the Tiger futures feed pipeline; this still requires an explicit feed run.",
                    risk="low",
                )
            )
        if "broker_order" in requested_roles:
            for path, planned, reason in [
                ("broker.provider", "tiger_openapi", "Select Tiger as the active broker provider."),
                ("broker.profile", "tiger_openapi_paper", "Resolve runtime broker settings from the Tiger paper profile."),
                ("broker.environment", "paper", "Keep Tiger in paper/simulated trading environment."),
                ("broker.dry_run", True, "Keep network order submission disabled by default."),
                ("demo_trading.broker_profile", "tiger_openapi_paper", "Route the active demo strategy through the Tiger paper broker profile."),
            ]:
                changes.append(self._set_change(path, self._get(path.split(".")), planned, reason, risk="medium"))
            for path in [
                "broker.base_url",
                "broker.api_key_env",
                "broker.api_secret_env",
                "broker.allowed_symbols",
                "broker.instrument_map",
                "broker.request_dir",
                "broker.outbox_dir",
                "broker.inbox_dir",
                "broker.receipt_pattern",
            ]:
                if self._exists(path.split(".")):
                    changes.append(
                        self._remove_change(
                            path,
                            self._get(path.split(".")),
                            "Remove Binance/bridge broker overrides so the Tiger profile is not shadowed.",
                        )
                    )
        return changes

    def _binance_changes(self, requested_roles: list[str]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        if "price_feed" in requested_roles:
            changes.append(
                self._set_change(
                    "binance_usdm_1m_feed.environment",
                    self._get(["binance_usdm_1m_feed", "environment"]),
                    str(self._get(["binance_usdm_1m_feed", "environment"]) or "demo"),
                    "Keep Binance USD-M as the configured 1m feed.",
                    risk="low",
                )
            )
        if "broker_order" in requested_roles:
            changes.append(
                self._set_change(
                    "broker.provider",
                    self._get(["broker", "provider"]),
                    "binance_usdm",
                    "Select Binance USD-M as the active broker provider.",
                    risk="medium",
                )
            )
        return changes

    def _warnings(self, connector: dict[str, Any], connector_id: str, requested_roles: list[str]) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if connector_id == "tiger_openapi" and "price_feed" in requested_roles:
            price_feed = self._capability(connector, "price_feed")
            acceptance = price_feed.get("acceptance", {}) if isinstance(price_feed.get("acceptance"), dict) else {}
            if price_feed and price_feed.get("status") != "ready":
                warnings.append(
                    {
                        "name": "tiger_price_feed_acceptance_not_ready",
                        "summary": self._price_feed_acceptance_warning(acceptance),
                        "evidence": {
                            "catalog_status": str(price_feed.get("status") or "missing"),
                            "acceptance": self._acceptance_summary(acceptance),
                        },
                    }
                )
        if connector_id == "tiger_openapi" and "broker_order" in requested_roles:
            warnings.append(
                {
                    "name": "paper_network_order_not_armed",
                    "summary": "Tiger TradeClient submission remains fail-closed until an explicit operator-approved paper-order milestone changes the guarded profile flags.",
                    "evidence": {
                        "required_later_flags": [
                            "broker_profiles.tiger_openapi_paper.dry_run=false",
                            "broker_profiles.tiger_openapi_paper.network_order_submission=paper_tradeclient",
                            "broker_profiles.tiger_openapi_paper.confirm_tiger_paper_orders=true",
                        ]
                    },
                }
            )
        return warnings

    def _capability(self, connector: dict[str, Any], name: str) -> dict[str, Any]:
        for capability in connector.get("capabilities", []) or []:
            if isinstance(capability, dict) and capability.get("name") == name:
                return capability
        return {}

    def _price_feed_acceptance_warning(self, acceptance: dict[str, Any]) -> str:
        if acceptance.get("operator_summary"):
            return str(acceptance.get("operator_summary"))
        if acceptance.get("status") == "pending_market_open":
            next_window = acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {}
            start = str(next_window.get("start") or "")
            return f"Tiger price-feed activation is waiting for market-hours acceptance; rerun after {start or 'the next trading window'}."
        if acceptance.get("status") == "blocked":
            return "Tiger price-feed activation is blocked by the latest acceptance receipt."
        return "Tiger price-feed activation needs a passing acceptance receipt."

    def _acceptance_summary(self, acceptance: dict[str, Any]) -> dict[str, Any]:
        if not acceptance:
            return {"status": "missing", "ready_for_price_feed": False, "exit_code": None, "blocker_count": 0}
        next_window = acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {}
        return {
            "status": str(acceptance.get("status") or "missing"),
            "ready_for_price_feed": acceptance.get("ready_for_price_feed") is True,
            "exit_code": acceptance.get("exit_code"),
            "blocker_count": int(acceptance.get("blocker_count") or 0),
            "operator_action": str(acceptance.get("operator_action") or ""),
            "operator_status": str(acceptance.get("operator_status") or ""),
            "operator_summary": str(acceptance.get("operator_summary") or ""),
            "next_command": str(acceptance.get("next_command") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "can_enable_broker_orders_from_this_gate": acceptance.get("can_enable_broker_orders_from_this_gate") is True,
            "checked_at": str(acceptance.get("checked_at") or ""),
        }

    def _activation_gate(
        self,
        connector_id: str,
        requested_roles: list[str],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        acceptance = self._first_acceptance_context(blockers + warnings)
        if blockers:
            first = blockers[0] if blockers else {}
            return {
                "status": "blocked",
                "operator_status": str(acceptance.get("operator_status") or "blocked_by_onboarding"),
                "summary": str(acceptance.get("operator_summary") or first.get("summary") or "Connector activation is blocked."),
                "next_command": str(acceptance.get("next_command") or ""),
                "next_trading_window": acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {},
                "can_apply_config_from_this_endpoint": False,
                "can_enable_broker_orders_from_this_gate": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
            }
        if warnings:
            return {
                "status": "preview_ready_with_warnings",
                "operator_status": "operator_warning_review",
                "summary": "Config preview is ready, but warnings must be resolved or explicitly accepted in a later config-write milestone.",
                "next_command": "",
                "next_trading_window": {},
                "can_apply_config_from_this_endpoint": False,
                "can_enable_broker_orders_from_this_gate": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
            }
        return {
            "status": "preview_ready",
            "operator_status": "ready_for_manual_config_apply",
            "summary": f"{connector_id} {', '.join(requested_roles) or 'connector'} preview is ready for an explicit config-write milestone.",
            "next_command": "",
            "next_trading_window": {},
            "can_apply_config_from_this_endpoint": False,
            "can_enable_broker_orders_from_this_gate": False,
            "writes_runtime_config": False,
            "opens_network_clients": False,
            "submits_orders": False,
        }

    def _first_acceptance_context(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        for row in rows:
            if not isinstance(row, dict):
                continue
            evidence = row.get("evidence", {}) if isinstance(row.get("evidence"), dict) else {}
            acceptance = evidence.get("acceptance", {}) if isinstance(evidence.get("acceptance"), dict) else {}
            if acceptance:
                return acceptance
        return {}

    def _activation_runbook(
        self,
        connector_id: str,
        requested_roles: list[str],
        changes: list[dict[str, Any]],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        activation_gate: dict[str, Any],
    ) -> dict[str, Any]:
        if blockers:
            status = "blocked"
            first_phase = {
                "name": "resolve_activation_gate",
                "status": "blocked",
                "summary": str(activation_gate.get("summary") or "Resolve connector activation blockers before any config write."),
                "commands": [str(activation_gate.get("next_command"))] if activation_gate.get("next_command") else [],
            }
        elif warnings:
            status = "operator_review_required"
            first_phase = {
                "name": "review_warnings",
                "status": "required",
                "summary": f"Review {len(warnings)} activation warning(s) before any config write.",
                "commands": [],
            }
        else:
            status = "ready_for_separate_config_write"
            first_phase = {
                "name": "review_preview",
                "status": "required",
                "summary": f"Review {len(changes)} previewed config change(s) before opening a separate config-write milestone.",
                "commands": [],
            }
        phases = [
            self._runbook_phase(first_phase["name"], first_phase["status"], first_phase["summary"], first_phase["commands"]),
            self._runbook_phase(
                "config_apply",
                "not_included",
                "Apply config only in a separate explicit config-write milestone after operator approval.",
                [],
            ),
            self._runbook_phase(
                "post_apply_validation",
                "pending",
                "After any separate config write, rerun read-only connector and live-readiness checks before enabling automation.",
                self._post_apply_validation_commands(connector_id, requested_roles),
            ),
            self._runbook_phase(
                "rollback_boundary",
                "required_for_config_write",
                "The future config-write milestone must include a reversible patch or backup before changing runtime config.",
                [],
            ),
        ]
        return {
            "status": status,
            "phase_count": len(phases),
            "first_action": phases[0]["summary"],
            "phases": phases,
            "safety": {
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "can_apply_config_from_this_endpoint": False,
                "can_enable_broker_orders_from_this_gate": False,
            },
        }

    def _runbook_phase(self, name: str, status: str, summary: str, commands: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "status": status,
            "summary": summary,
            "commands": [command for command in commands if command],
            "writes_runtime_config": False,
            "opens_network_clients": False,
            "submits_orders": False,
        }

    def _post_apply_validation_commands(self, connector_id: str, requested_roles: list[str]) -> list[str]:
        commands = ["python3 -m pipelines.connector_catalog --json"]
        if connector_id == "tiger_openapi" and "price_feed" in requested_roles:
            commands.append(
                "python3 -m pipelines.tiger_price_feed_acceptance --date <YYYY-MM-DD> --contract MGCmain --poll-seconds 75 --json"
            )
        if "broker_order" in requested_roles:
            commands.extend(
                [
                    "python3 -m pipelines.live_readiness --date <YYYY-MM-DD> --json",
                    "python3 -m pipelines.live_switch_plan --date <YYYY-MM-DD>",
                ]
            )
        return commands

    def _dualtrack_profile_preview(
        self,
        connector_id: str,
        requested_roles: list[str],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if connector_id != "tiger_openapi" or not set(requested_roles) & {"price_feed", "broker_order"}:
            return {
                "schema_version": DUALTRACK_TIGER_MGC_PROFILE_SCHEMA_VERSION,
                "status": "not_applicable",
                "profile_id": "",
                "connector_id": connector_id,
                "requested_roles": requested_roles,
                "change_count": 0,
                "dualtrack_config_patch_preview": [],
                "safety": self._preview_safety(),
            }

        changes = self._tiger_mgc_dualtrack_changes(requested_roles)
        if blockers:
            status = "blocked"
            summary = "Tiger/MGC dualtrack profile is blocked until connector activation blockers are resolved."
        elif warnings:
            status = "operator_review_required"
            summary = "Tiger/MGC dualtrack profile is previewable, but warnings must be reviewed before any config write."
        else:
            status = "preview_ready"
            summary = "Tiger/MGC dualtrack profile is ready for a separate explicit config-write milestone."
        return {
            "schema_version": DUALTRACK_TIGER_MGC_PROFILE_SCHEMA_VERSION,
            "status": status,
            "summary": summary,
            "profile_id": "tiger_mgc_dualtrack_paper",
            "connector_id": connector_id,
            "requested_roles": requested_roles,
            "config_file": "configs/dualtrack.yaml",
            "change_count": len(changes),
            "dualtrack_config_patch_preview": changes,
            "runtime_effect": {
                "market_symbol": "MGCmain",
                "timeframe": "1m",
                "provider": "tiger_openapi:COMEX",
                "market_session": "comex_futures",
                "quantity_mode": "integer_contracts",
                "contract_multiplier": 10,
                "contracts_per_rung": 1,
                "max_machine_rungs": 2,
                "commission_per_contract_side_usd": 2.70,
                "human_fill_sync_before_close": "enabled" if "broker_order" in requested_roles else "not_requested",
                "refresh_order_sync_before_import": "read_only_tradeclient_when_applied" if "broker_order" in requested_roles else "not_requested",
                "broker_order_submission": "not_enabled_by_this_profile",
            },
            "gates": {
                "requires_tiger_price_feed_acceptance": True,
                "requires_operator_config_write": True,
                "requires_strategy_edge_approval": True,
                "can_enable_broker_orders_from_this_profile": False,
            },
            "post_apply_validation_commands": [
                "python3 -m pipelines.connector_catalog --json",
                "python3 -m pipelines.tiger_price_feed_acceptance --date <YYYY-MM-DD> --contract MGCmain --poll-seconds 75 --json",
                "python3 -m pipelines.dualtrack_cycle_runner --event pre-cycle --cycle-id <cycle_id>",
            ],
            "not_included": [
                "writing configs/dualtrack.yaml",
                "claiming the Tiger/MGC strategy has passed the lab gate",
                "enabling Tiger paper TradeClient order submission",
                "placing, previewing, cancelling, or closing broker orders",
            ],
            "safety": self._preview_safety(),
        }

    def _tiger_mgc_dualtrack_changes(self, requested_roles: list[str]) -> list[dict[str, Any]]:
        changes = [
            self._set_dualtrack_change(
                "market_data.symbol",
                self._dualtrack_get(["market_data", "symbol"]),
                "MGCmain",
                "Read dualtrack bars from the Tiger/COMEX MGC series instead of the legacy GOLD series.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "market_data.timeframe",
                self._dualtrack_get(["market_data", "timeframe"]),
                "1m",
                "Keep the dualtrack operating cadence on 1-minute bars.",
                risk="low",
            ),
            self._set_dualtrack_change(
                "market_data.provider",
                self._dualtrack_get(["market_data", "provider"]),
                "tiger_openapi:COMEX",
                "Record the intended venue identity for audit and future UI display.",
                risk="low",
            ),
            self._set_dualtrack_change(
                "market_session.enabled",
                self._dualtrack_get(["market_session", "enabled"]),
                True,
                "Enable the COMEX session mask so closed-market bars do not advance the cycle.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "market_session.venue",
                self._dualtrack_get(["market_session", "venue"]),
                "comex_futures",
                "Use the COMEX futures 23x5 trading calendar for Tiger/MGC.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "market_session.timezone",
                self._dualtrack_get(["market_session", "timezone"]),
                "America/New_York",
                "Anchor the COMEX daily maintenance break to New York time and DST.",
                risk="low",
            ),
            self._set_dualtrack_change(
                "execution_cost_model.venue",
                self._dualtrack_get(["execution_cost_model", "venue"]),
                "tiger_mgc",
                "Use the venue-specific fixed-per-contract Tiger/MGC cost model.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "execution_cost_model.quantity_mode",
                self._dualtrack_get(["execution_cost_model", "quantity_mode"]),
                "integer_contracts",
                "Model MGC as whole futures contracts, not fractional notional slices.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "execution_cost_model.contract_multiplier",
                self._dualtrack_get(["execution_cost_model", "contract_multiplier"]),
                10,
                "Use the CME Micro Gold 10-ounce contract multiplier.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "execution_cost_model.contracts_per_rung",
                self._dualtrack_get(["execution_cost_model", "contracts_per_rung"]),
                1,
                "Make each machine rung one MGC contract.",
                risk="medium",
            ),
            self._set_dualtrack_change(
                "grid.max_rungs",
                self._dualtrack_get(["grid", "max_rungs"]),
                2,
                "Cap the machine grid to two one-contract rungs for the current $10k-per-track scale.",
                risk="high",
            ),
        ]
        if "broker_order" in requested_roles:
            changes.extend(
                [
                    self._set_dualtrack_change(
                        "human_fill_sync.enabled",
                        self._dualtrack_get(["human_fill_sync", "enabled"]),
                        True,
                        "Import Tiger-observed human fills before close/scoring.",
                        risk="medium",
                    ),
                    self._set_dualtrack_change(
                        "human_fill_sync.provider",
                        self._dualtrack_get(["human_fill_sync", "provider"]),
                        "tiger_openapi",
                        "Use Tiger order-sync artifacts as the human-fill evidence source.",
                        risk="medium",
                    ),
                    self._set_dualtrack_change(
                        "human_fill_sync.run_before_close",
                        self._dualtrack_get(["human_fill_sync", "run_before_close"]),
                        True,
                        "Keep human fill import ahead of dualtrack scoring.",
                        risk="medium",
                    ),
                    self._set_dualtrack_change(
                        "human_fill_sync.refresh_order_sync_before_import",
                        self._dualtrack_get(["human_fill_sync", "refresh_order_sync_before_import"]),
                        True,
                        "Refresh Tiger filled-order evidence through the existing read-only order-sync boundary before importing.",
                        risk="medium",
                    ),
                    self._set_dualtrack_change(
                        "human_fill_sync.require_success_before_close",
                        self._dualtrack_get(["human_fill_sync", "require_success_before_close"]),
                        True,
                        "Fail closed instead of scoring if Tiger human-fill evidence is missing or blocked.",
                        risk="medium",
                    ),
                ]
            )
        return changes

    def _set_dualtrack_change(self, path: str, current: Any, planned: Any, reason: str, *, risk: str) -> dict[str, Any]:
        change = self._set_change(path, current, planned, reason, risk=risk)
        change["config_file"] = "configs/dualtrack.yaml"
        return change

    def _dualtrack_get(self, path: list[str]) -> Any:
        value: Any = self.dualtrack_config
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    def _preview_safety(self) -> dict[str, bool]:
        return {
            "preview_only": True,
            "writes_runtime_config": False,
            "opens_network_clients": False,
            "submits_orders": False,
            "creates_order_control_endpoint": False,
            "can_enable_broker_orders": False,
        }

    def _config_apply_package(
        self,
        connector_id: str,
        requested_roles: list[str],
        changes: list[dict[str, Any]],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        activation_gate: dict[str, Any],
        activation_runbook: dict[str, Any],
        dualtrack_profile_preview: dict[str, Any],
    ) -> dict[str, Any]:
        if blockers:
            status = "blocked"
            summary = str(activation_gate.get("summary") or "Resolve connector activation blockers before preparing a config write.")
        elif warnings:
            status = "operator_review_required"
            summary = "Operator must review warnings before preparing a separate config-write milestone."
        else:
            status = "ready_for_explicit_config_write"
            summary = "Config patch preview is ready for a separate explicit config-write milestone."
        paths = [str(item.get("path") or "") for item in changes if item.get("path")]
        dualtrack_changes = [
            row
            for row in (dualtrack_profile_preview.get("dualtrack_config_patch_preview") or [])
            if isinstance(row, dict)
        ]
        dualtrack_paths = [
            f"{row.get('config_file') or 'configs/dualtrack.yaml'}:{row.get('path')}"
            for row in dualtrack_changes
            if row.get("path")
        ]
        patch_digest = config_apply_patch_digest(changes)
        dualtrack_patch_digest = config_apply_patch_digest(dualtrack_changes)
        package_id = build_config_apply_package_id(
            connector_id=connector_id,
            requested_roles=requested_roles,
            status=status,
            patch_digest=patch_digest,
            dualtrack_patch_digest=dualtrack_patch_digest,
            warnings=warnings,
            blockers=blockers,
        )
        return {
            "status": status,
            "summary": summary,
            "package_id": package_id,
            "connector_id": connector_id,
            "requested_roles": requested_roles,
            "change_count": len(changes),
            "dualtrack_change_count": int(dualtrack_profile_preview.get("change_count") or 0),
            "dualtrack_profile": {
                "status": str(dualtrack_profile_preview.get("status") or ""),
                "profile_id": str(dualtrack_profile_preview.get("profile_id") or ""),
                "config_file": str(dualtrack_profile_preview.get("config_file") or ""),
                "change_count": int(dualtrack_profile_preview.get("change_count") or 0),
                "requires_strategy_edge_approval": (dualtrack_profile_preview.get("gates") or {}).get("requires_strategy_edge_approval") is True if isinstance(dualtrack_profile_preview.get("gates"), dict) else False,
                "can_enable_broker_orders_from_this_profile": (dualtrack_profile_preview.get("gates") or {}).get("can_enable_broker_orders_from_this_profile") is True if isinstance(dualtrack_profile_preview.get("gates"), dict) else False,
            },
            "patch_digest": patch_digest,
            "dualtrack_patch_digest": dualtrack_patch_digest,
            "pre_apply_checks": [
                {
                    "name": "activation_gate",
                    "status": str(activation_gate.get("status") or "missing"),
                    "summary": str(activation_gate.get("summary") or ""),
                },
                {
                    "name": "blockers",
                    "status": "pass" if not blockers else "blocked",
                    "count": len(blockers),
                },
                {
                    "name": "warnings",
                    "status": "pass" if not warnings else "review_required",
                    "count": len(warnings),
                },
            ],
            "operator_acknowledgement_required": True,
            "operator_acknowledgement": "I_UNDERSTAND_CONNECTOR_CONFIG_WRITE_IS_SEPARATE_AND_REVERSIBLE",
            "attended_apply_command": (
                "python3 -m pipelines.connector_config_apply apply "
                "--plan outputs/connector_activation_plan/current.json "
                f"--package-id {package_id} "
                "--accept-warnings "
                "--write "
                "--acknowledgement I_UNDERSTAND_CONNECTOR_CONFIG_WRITE_IS_SEPARATE_AND_REVERSIBLE "
                "--json"
            ),
            "config_write_boundary": {
                "can_apply_from_this_endpoint": False,
                "requires_separate_config_write_milestone": True,
                "requires_operator_approval": True,
                "requires_backup_before_write": True,
                "requires_package_id": True,
                "expected_package_id": package_id,
                "paths_previewed": paths,
                "dualtrack_paths_previewed": dualtrack_paths,
            },
            "rollback": {
                "required": True,
                "summary": "A future config-write milestone must capture a reversible patch or backup before mutating runtime config.",
                "paths_previewed": paths,
                "dualtrack_paths_previewed": dualtrack_paths,
            },
            "post_apply_validation_commands": self._post_apply_validation_commands(connector_id, requested_roles),
            "runbook_status": str(activation_runbook.get("status") or ""),
            "safety": {
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "stores_credentials": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders_from_this_gate": False,
            },
        }

    def _switch_audit(
        self,
        connector_id: str,
        requested_roles: list[str],
        onboarding: dict[str, Any],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        activation_gate: dict[str, Any],
        activation_runbook: dict[str, Any],
        config_apply_package: dict[str, Any],
        dualtrack_profile_preview: dict[str, Any],
    ) -> dict[str, Any]:
        if blockers:
            status = "blocked"
            next_action = str(activation_gate.get("summary") or "Resolve activation blockers before connector switching.")
        elif warnings:
            status = "operator_review_required"
            next_action = "Review activation warnings and prepare a separate reversible config-write milestone."
        else:
            status = "ready_for_explicit_config_write"
            next_action = "Open a separate explicit config-write milestone with rollback evidence before switching the connector."
        broker_order_requested = "broker_order" in requested_roles
        checks = [
            {
                "name": "onboarding",
                "status": "pass" if onboarding.get("status") == "ready_for_operator_setup" else "blocked",
                "summary": str(onboarding.get("status") or ""),
            },
            {
                "name": "activation_gate",
                "status": str(activation_gate.get("status") or "missing"),
                "summary": str(activation_gate.get("summary") or ""),
            },
            {
                "name": "activation_runbook",
                "status": str(activation_runbook.get("status") or "missing"),
                "summary": str(activation_runbook.get("first_action") or ""),
            },
            {
                "name": "config_apply_package",
                "status": str(config_apply_package.get("status") or "missing"),
                "summary": str(config_apply_package.get("summary") or ""),
            },
        ]
        if dualtrack_profile_preview.get("status") != "not_applicable":
            checks.append(
                {
                    "name": "dualtrack_profile",
                    "status": str(dualtrack_profile_preview.get("status") or "missing"),
                    "summary": str(dualtrack_profile_preview.get("summary") or ""),
                }
            )
        if broker_order_requested:
            checks.append(
                {
                    "name": "broker_order_network",
                    "status": "not_armed",
                    "summary": "Broker-order network submission remains outside connector switching and requires a later explicit paper-order milestone.",
                }
            )
        return {
            "status": status,
            "connector_id": connector_id,
            "requested_roles": requested_roles,
            "next_action_summary": next_action,
            "next_command": str(activation_gate.get("next_command") or ""),
            "check_count": len(checks),
            "checks": checks,
            "can_switch_connector_from_this_endpoint": False,
            "can_apply_config_now": False,
            "can_enable_broker_orders": False,
            "requires_separate_config_write_milestone": True,
            "requires_operator_approval": True,
            "requires_rollback_evidence": True,
            "safety": {
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "stores_credentials": False,
                "credential_values_exposed": False,
            },
        }

    def _not_included(self, connector_id: str, requested_roles: list[str]) -> list[str]:
        items = [
            "writing configs/pipeline.yaml",
            "storing API keys or credential file contents",
            "opening Tiger/Binance network clients",
            "placing, previewing, cancelling, or closing broker orders",
        ]
        if connector_id == "tiger_openapi" and "broker_order" in requested_roles:
            items.append("arming Tiger paper TradeClient network submission")
        return items

    def _next_actions(
        self,
        connector_id: str,
        requested_roles: list[str],
        blockers: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> list[str]:
        if blockers:
            return [str(item.get("summary") or "") for item in blockers if item.get("summary")]
        actions = ["Review config_patch_preview; this run did not mutate runtime config."]
        if connector_id == "tiger_openapi" and "price_feed" in requested_roles:
            actions.append("After approval, apply the feed config change and run the Tiger feed pipeline in read-only mode.")
        if connector_id == "tiger_openapi" and "broker_order" in requested_roles:
            actions.append("After approval, apply the demo broker profile change and rerun runner/profile tests before enabling paper auto execution.")
        if warnings:
            actions.append("Resolve or explicitly accept warnings before any config write milestone.")
        return actions

    def _set_change(self, path: str, current: Any, planned: Any, reason: str, *, risk: str) -> dict[str, Any]:
        return {
            "op": "set",
            "path": path,
            "current": self._safe_value(path, current),
            "planned": self._safe_value(path, planned),
            "reason": reason,
            "risk": risk,
            "writes_config": False,
        }

    def _remove_change(self, path: str, current: Any, reason: str) -> dict[str, Any]:
        return {
            "op": "remove",
            "path": path,
            "current": self._safe_value(path, current),
            "planned": None,
            "reason": reason,
            "risk": "medium",
            "writes_config": False,
        }

    def _get(self, path: list[str]) -> Any:
        value: Any = self.config
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    def _exists(self, path: list[str]) -> bool:
        value: Any = self.config
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return False
            value = value[key]
        return True

    def _safe_value(self, path: str, value: Any) -> Any:
        parts = {part.lower() for part in path.replace("[", ".").replace("]", "").split(".")}
        if parts & {"props_path", "api_key", "api_secret", "secret", "token", "license", "password"}:
            return "<redacted>" if value not in (None, "") else value
        if isinstance(value, dict):
            return {key: self._safe_value(f"{path}.{key}", child) for key, child in value.items()}
        if isinstance(value, list):
            return [self._safe_value(path, item) for item in value]
        return value

    def _write(self, payload: dict[str, Any]) -> None:
        base = self.output_root / "connector_activation_plan"
        write_json(base / "current.json", [payload])
        connector_id = payload.get("connector_id") or "unknown"
        dated = base / f"{connector_id}.json"
        rows = load_json(dated)
        rows.append(payload)
        write_json(dated, rows)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
