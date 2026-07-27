from __future__ import annotations
import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from services.run_date import utc_run_date
from services.accounting_projection_core import project_execution_accounting
from services.broker_adapter import PaperBrokerAdapter
from services.broker_read_model import project_broker_read_model
from services.code_reload import CodeReloadGuard
from services.paper_release_receipt import (
    PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE,
    PaperServiceBootGate,
)
from services.cloud_service_boot import CloudPaperServiceBootGate
from services.config_loader import ROOT, load_pipeline_config
from services.command_center import build_command_center_state
from services.connector_activation_plan import ConnectorActivationPlan
from services.connector_onboarding import ConnectorOnboardingDryRun
from services.dashboard_state import DashboardState
from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc, seconds_until_end
from services.dualtrack_config import dualtrack_config
from services.execution_plugin_composition import build_configured_execution_engine_adapter
from services.grid_lifecycle_evidence import build_grid_lifecycle_evidence
from services.production_accounting import normalize_nautilus_snapshot_for_accounting
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_market_feed import DualTrackMarketFeed
from services.dualtrack_scoring import (
    DualTrackScorer,
    _trades_from_fills,
    apply_unrealized,
    filter_invalid_machine_fills,
)
from services.dualtrack_store import DualTrackPlanStore
from services.strategy_control_plane import (
    StrategyControlPlane,
    last_paper_execution_market_event,
    production_mutation_lock,
    resolve_paper_safe_action_pricing,
    settle_paper_safe_action_commands,
)
from services.risk_port import (
    action_class_for_command,
    assert_matching_risk_decision,
    build_manual_order_risk_request,
    build_paper_safe_action_market_gate,
    normalize_manual_order_command,
    require_risk_permission,
)
from services.strategy_recommendation import StrategyRecommendationService
from services.strategy_shadow import load_strategy_shadow_runs, load_strategy_shadow_runs_for_cycles
from services.strategy_shadow_promotion import evaluate_grid_shadow_promotion
from services.safe_repair_queue import SafeRepairQueue
from services.strategy_cycle_package import StrategyCyclePackager
from services.connector_catalog import ConnectorCatalog
from services.journal_store import load_json
from services.production_accounting import build_production_accounting_history
from services.replay_state import ReplayState
from services.tiger_venue_status import TigerVenueStatus
from services.trading_system_read_model import (
    project_market_read_model,
    project_trading_system_read_model,
)
from services.trading_daily_24h_report import load_daily_report_rows
from services.cloud_daily_self_review import load_daily_self_review
from pipelines.cloud_health import build_cloud_health

from services.contracts.common import _CYCLE_ID_PATTERN, _DATE_PATTERN, _truthy  # noqa: F401 — re-exported for backward compatibility
from services.contracts.system import build_market_view_intake_response, build_system_state_response, dashboard_output_root  # noqa: F401 — re-exported for backward compatibility
from services.contracts.dualtrack import _dualtrack_output_root, build_dualtrack_attribution_response, build_dualtrack_human_response, build_dualtrack_ledger_response, build_dualtrack_machine_response, build_dualtrack_plan_post_response, build_dualtrack_plan_response, build_dualtrack_verdict_post_response  # noqa: F401 — re-exported for backward compatibility
from services.contracts.connector_control import _CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY, _redacted_connector_status_snapshot, _redacted_runbook_steps, build_connector_config_apply_response, build_connector_config_rollback_response, build_connector_config_status_response, build_connector_price_feed_refresh_runbook_response  # noqa: F401 — re-exported for backward compatibility
from services.contracts.trader_ops_payload import _compact_backend_maturity, _compact_collector_run, _compact_collector_runs, _compact_daily_trade_samples, _compact_data_health, _compact_evening_review, _compact_frequency_evidence, _compact_strategy_daily_reviews, _compact_strategy_frequency, _compact_strategy_promotion_gate, _compact_trade_reviews, _compact_trading_plan, _trim_maturity_evidence, compact_ops_payload, compact_trader_payload  # noqa: F401 — re-exported for backward compatibility
from services.contracts.strategy_payload import _compact_artifact_provenance, _compact_bar, _compact_decision, _compact_decision_snapshot, _compact_exit_decision, _compact_explainability_summary, _compact_nav_curve, _compact_order, _compact_risk_block, _compact_signal, _compact_strategy_detail, _compact_trade, _compact_trade_record_card, compact_strategy_payload  # noqa: F401 — re-exported for backward compatibility

_STRATEGY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9:_=-]+$")


_TIMEFRAME_PATTERN = re.compile(r"^\d+[mhdMHD]$")


_DASHBOARD_DATAFEED_TIMEOUT_SECONDS = 2.0


_DASHBOARD_RECENT_ACTIVITY_LIMIT = 80


_DASHBOARD_VIEWS = {"full", "trader", "ops"}


_MAX_OPEN_TRADES_PER_STRATEGY = 3


_PUBLIC_DASHBOARD_URL = "https://goldbot.park-ai-intel.com/dashboard-v5.html"


_LOCAL_GATEWAY_URL = "http://127.0.0.1:8766/dashboard-v5.html"


_CLOUDFLARED_LOG = Path("/Users/wendy/work/选题工作台/launchd-tunnel.log")


_DUALTRACK_POST_ENDPOINTS = {"/api/dualtrack/plan", "/api/dualtrack/orders", "/api/dualtrack/verdict"}


_CONNECTOR_ATTENDED_SWITCH_REVIEW_ENDPOINT_SAFETY = {
    "read_only": True,
    "generates_authorization": False,
    "runs_config_apply": False,
    "opens_network_clients": False,
    "opens_quote_client": False,
    "opens_trade_client": False,
    "submits_orders": False,
    "writes_runtime_config": False,
    "credential_values_exposed": False,
    "raw_acknowledgement_exposed": False,
    "attended_apply_command_exposed": False,
}


_TIGER_PAPER_ORDER_REFRESH_RUNBOOK_ENDPOINT_SAFETY = {
    "read_only": True,
    "generates_runbook": False,
    "opens_network_clients": False,
    "opens_quote_client": False,
    "opens_trade_client": False,
    "submits_orders": False,
    "cancels_orders": False,
    "closes_positions": False,
    "writes_runtime_config": False,
    "credential_values_exposed": False,
    "raw_command_text_exposed": False,
}


class DashboardHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self._should_disable_static_cache():
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _should_disable_static_cache(self) -> bool:
        path = urlparse(self.path).path
        return path in {
            "/dashboard-v2.html",
            "/dashboard-v3.html",
            "/dashboard.html",
            "/dashboard-v4.html",
            "/dashboard-v5.html",
            "/command-center.html",
            "/dashboard-dualtrack-split.html",
            "/dashboard-dualtrack-v5.html",
            "/dashboard-dualtrack-replay.html",
            "/dashboard-replay.html",
            "/dashboard-replay-v4.html",
            "/ops-dashboard.html",
            "/assets/shell.js",
            "/assets/shell.css",
            "/packages/standard-kline/standard-kline.js",
        }

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._redirect("command-center.html")
            return
        if parsed.path == "/dashboard-v5.html":
            self._serve_static_alias("/dashboard-gridmind.html")
            return
        if parsed.path == "/api/command-center-state":
            self._handle_command_center_state_api()
            return
        if parsed.path == "/api/dashboard":
            self._handle_dashboard_api(parsed.query)
            return
        if parsed.path == "/api/replay":
            self._handle_replay_api(parsed.query)
            return
        if parsed.path == "/api/system/status":
            self._handle_system_status_api(parsed.query)
            return
        if parsed.path == "/api/system-state":
            self._handle_system_state_api()
            return
        if parsed.path == "/api/trader/overview":
            self._handle_trader_overview_api(parsed.query)
            return
        if parsed.path == "/api/ops/status":
            self._handle_ops_status_api(parsed.query)
            return
        if parsed.path == "/api/public-access-health":
            self._handle_public_access_health()
            return
        if parsed.path == "/api/dualtrack/cycle/current":
            self._handle_dualtrack_current(parsed.query)
            return
        if parsed.path == "/api/strategy-console/current":
            self._handle_strategy_console_current(parsed.query)
            return
        if parsed.path == "/api/trading-system/read-model":
            self._handle_trading_system_read_model(parsed.query)
            return
        if parsed.path == "/api/trading-system/ai-evaluation-receipt":
            self._handle_ai_evaluation_receipt(parsed.query)
            return
        if parsed.path == "/api/trading-system/daily-self-review":
            self._handle_daily_self_review(parsed.query)
            return
        if parsed.path == "/api/trading-system/cloud-health":
            self._handle_cloud_health()
            return
        if parsed.path == "/api/dualtrack/config":
            self._handle_dualtrack_config_get()
            return
        if parsed.path.startswith("/api/dualtrack/trades/"):
            self._handle_dualtrack_trades_get(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/dualtrack/execution/"):
            self._handle_dualtrack_execution_get(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/dualtrack/plan/"):
            self._handle_dualtrack_plan_get(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/dualtrack/machine/"):
            self._handle_dualtrack_machine_get(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/dualtrack/human/"):
            self._handle_dualtrack_human_get(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/dualtrack/attribution/"):
            self._handle_dualtrack_attribution_get(parsed.path)
            return
        if parsed.path == "/api/dualtrack/ledger":
            self._handle_dualtrack_ledger_get(parsed.query)
            return
        if parsed.path == "/api/dualtrack/market/bars":
            self._handle_dualtrack_market_bars_get(parsed.query)
            return
        if parsed.path == "/api/dualtrack/runtime/status":
            self._handle_dualtrack_runtime_status_get(parsed.query)
            return
        if parsed.path == "/api/dualtrack/venue/tiger":
            self._handle_dualtrack_tiger_venue_get()
            return
        if parsed.path == "/api/dualtrack/venue/tiger/paper-order-refresh-runbook":
            self._handle_tiger_paper_order_refresh_runbook_get()
            return
        if parsed.path == "/api/connectors/catalog":
            self._handle_connector_catalog_get()
            return
        if parsed.path == "/api/connectors/config/status":
            self._handle_connector_config_status_get()
            return
        if parsed.path == "/api/connectors/config/attended-switch-review":
            self._handle_connector_attended_switch_review_get()
            return
        if parsed.path == "/api/connectors/config/price-feed-refresh-runbook":
            self._handle_connector_price_feed_refresh_runbook_get()
            return
        super().do_GET()

    def _serve_static_alias(self, path: str) -> None:
        original_path = self.path
        try:
            self.path = path
            super().do_GET()
        finally:
            self.path = original_path

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/market-view/intake":
            self._handle_market_view_intake_api()
            return
        if parsed.path == "/api/connectors/onboarding/dry-run":
            self._handle_connector_onboarding_dry_run()
            return
        if parsed.path == "/api/connectors/activation/plan":
            self._handle_connector_activation_plan()
            return
        if parsed.path == "/api/connectors/config/apply":
            self._handle_connector_config_apply()
            return
        if parsed.path == "/api/connectors/config/rollback":
            self._handle_connector_config_rollback()
            return
        if parsed.path in _DUALTRACK_POST_ENDPOINTS:
            if not _dualtrack_mutation_request_allowed(
                str(self.headers.get("Host") or ""),
                str(self.headers.get("Origin") or ""),
            ):
                self._write_error(403, "dualtrack_origin_blocked", "dualtrack writes require the same local origin")
                return
            self._handle_dualtrack_post(parsed.path)
            return
        if parsed.path == "/api/strategy-console/control":
            if not _dualtrack_mutation_request_allowed(str(self.headers.get("Host") or ""), str(self.headers.get("Origin") or "")):
                self._write_error(403, "strategy_console_origin_blocked", "strategy-console writes require the same local origin")
                return
            self._handle_strategy_console_control()
            return
        self._write_error(404, "not_found", "unknown POST endpoint")

    def _handle_dualtrack_current(self, query: str) -> None:
        try:
            self._write_json(200, build_dualtrack_cycle_current_response())
        except ValueError as exc:
            self._write_error(400, "invalid_dualtrack_cycle", str(exc))

    def _handle_strategy_console_current(self, query: str) -> None:
        params = parse_qs(query)
        try:
            self._write_json(200, build_strategy_console_current_response(as_of=(params.get("as_of") or [None])[0]))
        except ValueError as exc:
            self._write_error(400, "strategy_console_unavailable", str(exc))

    def _handle_trading_system_read_model(self, query: str) -> None:
        params = parse_qs(query)
        try:
            self._write_json(
                200,
                build_trading_system_read_model_response(
                    as_of=(params.get("as_of") or [None])[0],
                ),
            )
        except ValueError as exc:
            self._write_error(400, "trading_system_read_model_unavailable", str(exc))

    def _handle_ai_evaluation_receipt(self, query: str) -> None:
        params = parse_qs(query)
        evaluation_id = str((params.get("evaluation_id") or [""])[0]).strip()
        if not evaluation_id or not _SYMBOL_PATTERN.match(evaluation_id):
            self._write_error(400, "invalid_evaluation_id", "evaluation_id contains invalid characters")
            return
        try:
            self._write_json(200, build_ai_evaluation_receipt_response(evaluation_id))
        except ValueError as exc:
            self._write_error(404, "ai_evaluation_receipt_not_found", str(exc))

    def _handle_daily_self_review(self, query: str) -> None:
        params = parse_qs(query)
        report_date = str((params.get("date") or [""])[0]).strip() or None
        if report_date and not _DATE_PATTERN.match(report_date):
            self._write_error(400, "invalid_review_date", "date must be YYYY-MM-DD")
            return
        try:
            self._write_json(
                200,
                build_daily_self_review_response(report_date=report_date),
            )
        except ValueError as exc:
            self._write_error(404, "daily_self_review_not_found", str(exc))

    def _handle_cloud_health(self) -> None:
        try:
            self._write_json(200, build_cloud_health(persist=True))
        except Exception as exc:  # noqa: BLE001 - preserve classified failure.
            self._write_json(
                503,
                {
                    "schema_version": "cloud-paper-health-v1",
                    "runtime_mode": "cloud",
                    "paper_only": True,
                    "status": "blocked",
                    "incidents": [
                        {
                            "stage": "cloud_health",
                            "code": "cloud_health_unavailable",
                            "summary": f"Cloud health could not be built: {type(exc).__name__}",
                            "next_action": "Inspect the Cloud health service and latest receipt.",
                        }
                    ],
                    "control_actions_executed": 0,
                    "secrets_included": False,
                },
            )

    def _handle_strategy_console_control(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=64_000)
            self._write_json(200, build_strategy_console_control_response(payload, actor=self._control_actor()))
        except ValueError as exc:
            self._write_error(400, "invalid_strategy_console_control", str(exc))

    def _control_actor(self) -> dict:
        # The gateway asserts this header only after validating the Cloudflare
        # Access JWT, and never forwards client-supplied headers. The server is
        # loopback-bound, so a request without it is a local operator.
        email = str(self.headers.get("X-Goldbot-Actor-Email") or "").strip().lower()
        return {
            "email": email or None,
            "transport": "public_gateway" if email else "local",
            "client": self.client_address[0] if self.client_address else None,
        }

    def _handle_dualtrack_plan_get(self, path: str, query: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        self._write_json(200, build_dualtrack_plan_response(cycle_id))

    def _handle_dualtrack_config_get(self) -> None:
        self._write_json(200, build_dualtrack_config_response())

    def _handle_dualtrack_trades_get(self, path: str, query: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        params = parse_qs(query)
        track = (params.get("track") or ["human"])[0]
        try:
            self._write_json(200, build_dualtrack_trades_response(cycle_id, track=track))
        except PermissionError as exc:
            self._write_error(403, "dualtrack_trades_hidden", str(exc))
        except ValueError as exc:
            self._write_error(400, "dualtrack_trades_invalid", str(exc))

    def _handle_dualtrack_execution_get(self, path: str, query: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        params = parse_qs(query)
        try:
            self._write_json(
                200,
                build_dualtrack_execution_response(
                    cycle_id,
                    as_of=(params.get("as_of") or [None])[0],
                ),
            )
        except ValueError as exc:
            self._write_error(400, "dualtrack_execution_invalid", str(exc))

    def _handle_dualtrack_machine_get(self, path: str, query: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        self._write_json(200, build_dualtrack_machine_response(cycle_id))

    def _handle_dualtrack_human_get(self, path: str, query: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        self._write_json(200, build_dualtrack_human_response(cycle_id))

    def _handle_dualtrack_attribution_get(self, path: str) -> None:
        cycle_id = path.rsplit("/", 1)[-1]
        if not _CYCLE_ID_PATTERN.match(cycle_id):
            self._write_error(400, "invalid_cycle_id", "expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
            return
        try:
            self._write_json(200, build_dualtrack_attribution_response(cycle_id))
        except ValueError as exc:
            self._write_error(404, "dualtrack_attribution_unavailable", str(exc))

    def _handle_dualtrack_ledger_get(self, query: str) -> None:
        params = parse_qs(query)
        self._write_json(200, build_dualtrack_ledger_response(week=params.get("week", [None])[0]))

    def _handle_dualtrack_market_bars_get(self, query: str) -> None:
        params = parse_qs(query)
        symbol = (params.get("symbol") or [""])[0].strip()
        timeframe = (params.get("timeframe") or [""])[0].strip()
        end = (params.get("end") or [""])[0].strip()
        if symbol and not _SYMBOL_PATTERN.match(symbol):
            self._write_error(400, "invalid_symbol", "symbol contains unsupported characters")
            return
        if timeframe and not _TIMEFRAME_PATTERN.match(timeframe):
            self._write_error(400, "invalid_timeframe", "expected timeframe like 1m, 5m, 1h, or 1d")
            return
        try:
            limit = int((params.get("limit") or ["96"])[0])
        except ValueError:
            self._write_error(400, "invalid_limit", "limit must be an integer")
            return
        if end:
            try:
                parse_utc(end)
            except (TypeError, ValueError):
                self._write_error(400, "invalid_end", "end must be an ISO-8601 timestamp")
                return
        self._write_json(
            200,
            build_dualtrack_market_bars_response(
                symbol=symbol or None,
                timeframe=timeframe or None,
                limit=limit,
                end=end or None,
                config=_dashboard_market_read_config(),
            ),
        )

    def _handle_dualtrack_tiger_venue_get(self) -> None:
        self._write_json(200, build_dualtrack_tiger_venue_response())

    def _handle_tiger_paper_order_refresh_runbook_get(self) -> None:
        self._write_json(200, build_tiger_paper_order_refresh_runbook_response())

    def _handle_dualtrack_runtime_status_get(self, query: str) -> None:
        params = parse_qs(query)
        self._write_json(200, build_dualtrack_runtime_status_response(as_of=(params.get("as_of") or [None])[0]))

    def _handle_connector_catalog_get(self) -> None:
        self._write_json(200, build_connector_catalog_response())

    def _handle_connector_config_status_get(self) -> None:
        self._write_json(200, build_connector_config_status_response())

    def _handle_connector_attended_switch_review_get(self) -> None:
        self._write_json(200, build_connector_attended_switch_review_response())

    def _handle_connector_price_feed_refresh_runbook_get(self) -> None:
        self._write_json(200, build_connector_price_feed_refresh_runbook_response())

    def _handle_connector_onboarding_dry_run(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=32_000)
            self._write_json(200, build_connector_onboarding_dry_run_response(payload))
        except ValueError as exc:
            self._write_error(400, "invalid_connector_onboarding_dry_run", str(exc))

    def _handle_connector_activation_plan(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=32_000)
            self._write_json(200, build_connector_activation_plan_response(payload))
        except ValueError as exc:
            self._write_error(400, "invalid_connector_activation_plan", str(exc))

    def _handle_connector_config_apply(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=256_000)
            self._write_json(200, build_connector_config_apply_response(payload))
        except ValueError as exc:
            self._write_error(400, "invalid_connector_config_apply", str(exc))

    def _handle_connector_config_rollback(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=128_000)
            self._write_json(200, build_connector_config_rollback_response(payload))
        except ValueError as exc:
            self._write_error(400, "invalid_connector_config_rollback", str(exc))

    def _handle_dualtrack_post(self, path: str) -> None:
        try:
            payload = self._read_json_body(max_bytes=64_000)
            if path == "/api/dualtrack/plan":
                result = build_dualtrack_plan_post_response(payload)
            elif path == "/api/dualtrack/orders":
                received_at = parse_utc(None)
                cfg = dualtrack_config()
                market_data = cfg.get("market_data") if isinstance(cfg.get("market_data"), dict) else {}
                market = DualTrackMarketFeed().snapshot(
                    symbol=str(market_data.get("symbol") or "GOLD"),
                    timeframe=str(market_data.get("timeframe") or "1m"),
                    limit=1,
                    as_of=received_at.isoformat(),
                )
                payload = _prepare_dualtrack_network_order(
                    payload,
                    market=market,
                    received_at=received_at,
                    expected_provider=str(market_data.get("provider") or ""),
                )
                result = build_dualtrack_order_post_response(
                    payload,
                    enforce_risk=True,
                    market=market,
                )
            else:
                result = build_dualtrack_verdict_post_response(payload)
            self._write_json(200, result)
        except ValueError as exc:
            self._write_error(400, "invalid_dualtrack_request", str(exc))

    def _handle_market_view_intake_api(self) -> None:
        try:
            payload = self._read_json_body(max_bytes=64_000)
            result = build_market_view_intake_response(payload)
            self._write_json(200, result)
        except ValueError as exc:
            self._write_error(400, "invalid_market_view_intake", str(exc))
        except Exception as exc:  # noqa: BLE001 — local write endpoint must fail closed.
            self.log_error("market view intake failed: %s", exc)
            self._write_error(500, "market_view_intake_failed", "internal error recording market view")

    def _read_json_body(self, *, max_bytes: int) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            raise ValueError("request body is required")
        if length > max_bytes:
            raise ValueError(f"request body exceeds {max_bytes} bytes")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _handle_replay_api(self, query: str) -> None:
        params = parse_qs(query)
        run_date = params.get("date", [utc_run_date()])[0]
        if not _DATE_PATTERN.match(run_date):
            self._write_error(400, "invalid_date", "expected YYYY-MM-DD")
            return
        strategy_id = params.get("strategy", [""])[0]
        if strategy_id and not _STRATEGY_PATTERN.match(strategy_id):
            self._write_error(400, "invalid_strategy", "strategy id must match [A-Za-z0-9_-]+")
            return
        symbol = params.get("symbol", ["GOLD"])[0]
        if not _SYMBOL_PATTERN.match(symbol):
            self._write_error(400, "invalid_symbol", "symbol contains invalid characters")
            return
        raw_timeframes = params.get("timeframes", ["1d,4h,15m,5m,1m"])[0]
        timeframes = [item.strip() for item in raw_timeframes.split(",") if item.strip()]
        if any(not _TIMEFRAME_PATTERN.match(item) for item in timeframes):
            self._write_error(400, "invalid_timeframes", "timeframes must look like 1m, 5m, 15m, 4h, or 1d")
            return
        cursor = params.get("cursor", [None])[0]
        try:
            limit = int(params.get("limit", ["0"])[0] or 0)
        except ValueError:
            self._write_error(400, "invalid_limit", "limit must be an integer")
            return
        trade_id = params.get("trade_id", params.get("review_trade_id", [""]))[0].strip()
        if trade_id and not _SYMBOL_PATTERN.match(trade_id):
            self._write_error(400, "invalid_trade_id", "trade id contains invalid characters")
            return
        try:
            state = ReplayState()
            payload = state.snapshot(
                run_date,
                cursor=cursor,
                symbol=symbol,
                timeframes=timeframes,
                strategy_id=strategy_id,
                limit=limit or None,
                trade_id=trade_id,
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except ValueError as exc:
            self._write_error(400, "invalid_replay_request", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — replay endpoint must stay defensive.
            self.log_error("replay snapshot failed: %s", exc)
            self._write_error(500, "replay_snapshot_failed", "internal error generating replay snapshot")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _handle_dashboard_api(self, query: str) -> None:
        params = parse_qs(query)
        run_date = params.get("date", [utc_run_date()])[0]
        if not _DATE_PATTERN.match(run_date):
            self._write_error(400, "invalid_date", "expected YYYY-MM-DD")
            return
        strategy_id = params.get("strategy", [None])[0]
        if strategy_id is not None and not _STRATEGY_PATTERN.match(strategy_id):
            # User-controlled path component — reject anything but [A-Za-z0-9_-].
            self._write_error(400, "invalid_strategy", "strategy id must match [A-Za-z0-9_-]+")
            return
        view = params.get("view", ["full"])[0]
        if view not in _DASHBOARD_VIEWS:
            self._write_error(400, "invalid_view", "view must be full, trader, or ops")
            return
        try:
            if strategy_id:
                base = DashboardState()
                state = DashboardState(output_root=base.output_root / "strategies" / strategy_id, market_db=base.market_db)
            else:
                state = DashboardState()
            payload = state.snapshot(run_date)
            if view == "trader":
                payload = compact_strategy_payload(payload) if strategy_id else compact_trader_payload(payload)
            elif view == "ops":
                payload = compact_strategy_payload(payload) if strategy_id else compact_ops_payload(payload)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — defensive: never crash the handler.
            self.log_error("dashboard snapshot failed: %s", exc)
            self._write_error(500, "snapshot_failed", "internal error generating dashboard snapshot")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _handle_system_status_api(self, query: str) -> None:
        self._handle_contract_api(query, build_system_status_contract)

    def _handle_system_state_api(self) -> None:
        self._write_json(200, build_system_state_response())

    def _handle_command_center_state_api(self) -> None:
        self._write_json(200, build_command_center_state())

    def _handle_trader_overview_api(self, query: str) -> None:
        self._handle_contract_api(query, build_trader_overview_contract)

    def _handle_ops_status_api(self, query: str) -> None:
        self._handle_contract_api(query, build_ops_status_contract)

    def _handle_contract_api(self, query: str, builder) -> None:
        parsed = self._dashboard_contract_request(query)
        if parsed is None:
            return
        run_date, strategy_id = parsed
        try:
            state = DashboardState()
            payload = state.snapshot(run_date)
            body = json.dumps(builder(payload, strategy_id=strategy_id), ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — contract endpoints must fail closed.
            self.log_error("dashboard contract failed: %s", exc)
            self._write_error(500, "contract_failed", "internal error generating dashboard contract")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _dashboard_contract_request(self, query: str) -> tuple[str, str] | None:
        params = parse_qs(query)
        run_date = params.get("date", [utc_run_date()])[0]
        if not _DATE_PATTERN.match(run_date):
            self._write_error(400, "invalid_date", "expected YYYY-MM-DD")
            return None
        strategy_id = params.get("strategy", [""])[0]
        if strategy_id and not _STRATEGY_PATTERN.match(strategy_id):
            self._write_error(400, "invalid_strategy", "strategy id must match [A-Za-z0-9_-]+")
            return None
        return run_date, strategy_id

    def _handle_public_access_health(self) -> None:
        try:
            payload = build_public_access_health()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — health endpoint must stay defensive.
            self.log_error("public access health failed: %s", exc)
            self._write_error(500, "public_access_health_failed", "internal error generating public access health")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _write_error(self, status: int, code: str, message: str) -> None:
        body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _redirect(self, target: str) -> None:
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def build_dualtrack_cycle_current_response(*, output_root: Path | None = None, as_of: str | None = None) -> dict:
    cfg = dualtrack_config()
    deadline = int(cfg.get("plan_lock_deadline_min_before_cycle", 0))
    window = cycle_window(as_of, lock_deadline_min_before_cycle=deadline)
    store = DualTrackPlanStore(output_root, config=cfg)
    effective = store.machine_plan(window.cycle_id)
    effective_status = {
        "has_effective_plan": effective is not None,
        "machine_stands_down": effective is None,
    }
    if effective is not None:
        effective_status["author"] = effective.get("effective_author", "")
    return {
        **window.to_dict(),
        "countdown_seconds": seconds_until_end(as_of, lock_deadline_min_before_cycle=deadline),
        "effective_plan_status": effective_status,
    }


def build_strategy_console_current_response(*, output_root: Path | None = None, as_of: str | None = None) -> dict:
    """Read model for the one-production-strategy console.

    Legacy dual-track endpoints remain available for historical investigation;
    execution-engine shadow status is deliberately not a strategy shadow.
    """
    return _assemble_strategy_console_snapshot(output_root=output_root, as_of=as_of)


def _current_strategy_risk_decision(output: Path, source: dict) -> dict | None:
    plan = source.get("production_plan") if isinstance(source.get("production_plan"), dict) else {}
    runtime = source.get("runtime") if isinstance(source.get("runtime"), dict) else {}
    if str(plan.get("strategy_type") or "grid").lower() == "dca":
        cycle = source.get("cycle") if isinstance(source.get("cycle"), dict) else {}
        cycle_id = str(cycle.get("cycle_id") or plan.get("cycle_id") or "")
        # The control plane owns DCA decisions beneath its strategy-control
        # root.  ``output`` here is the configured output root, while the
        # control plane writes beneath its DualTrack subtree.  Older fixtures
        # may still place the same artifact directly below the configured root.
        # Prefer the writer's current location; only consult the legacy path
        # when no current artifact exists.  A present-but-mismatched decision
        # is deliberately still projected as missing/historical by the read
        # model rather than silently sourcing a different artifact.
        current_path = (
            output / "dualtrack" / "strategy_control" / "dca_risk_decisions" / f"{cycle_id}.json"
        )
        legacy_path = output / "dca_risk_decisions" / f"{cycle_id}.json"
        current_rows = load_json(current_path)
        risk_row_sets = [current_rows] if current_rows else [load_json(legacy_path)]
    else:
        risk_rows = load_json(output / "dualtrack" / "risk_decisions" / "current.json")
    expected_risk_id = str(runtime.get("risk_decision_id") or "")
    if str(plan.get("strategy_type") or "grid").lower() != "dca":
        risk_row_sets = [load_json(output / "dualtrack" / "risk_decisions" / "current.json")]
    for risk_rows in risk_row_sets:
        matching = [
            row
            for row in risk_rows
            if isinstance(row, dict)
            and (not expected_risk_id or str(row.get("decision_id") or "") == expected_risk_id)
        ]
        if matching:
            return matching[-1]
    return None


def build_trading_system_read_model_response(
    *,
    output_root: Path | None = None,
    as_of: str | None = None,
) -> dict:
    """Stable operator projection over one observational console snapshot."""

    output = _dualtrack_output_root(output_root)
    source = _assemble_strategy_console_snapshot(output_root=output, as_of=as_of)
    risk = _current_strategy_risk_decision(output, source)
    broker_adapter = PaperBrokerAdapter(output)
    broker = project_broker_read_model(
        broker_adapter,
        strategy_id="production_grid",
    )
    payload = project_trading_system_read_model(
        source,
        risk_decision=risk,
        broker=broker,
        generated_at=parse_utc(as_of).isoformat(),
    ).to_dict()
    return _compact_dashboard_read_model_payload(payload)


def build_daily_self_review_response(
    *,
    output_root: Path | None = None,
    report_date: str | None = None,
) -> dict[str, Any]:
    output = _dualtrack_output_root(output_root)
    return load_daily_self_review(output, report_date=report_date)


def build_ai_evaluation_receipt_response(
    evaluation_id: str,
    *,
    output_root: Path | None = None,
    as_of: str | None = None,
) -> dict:
    """Return one archived AI receipt on demand, never as polling payload."""

    output = _dualtrack_output_root(output_root)
    cycle = build_dualtrack_cycle_current_response(output_root=output, as_of=as_of)
    cycle_id = str(cycle.get("cycle_id") or "")
    control = StrategyControlPlane(output).read_model(cycle_id, as_of=as_of)
    for proposal in control.get("proposals") or []:
        if not isinstance(proposal, dict):
            continue
        receipt = proposal.get("evaluation_receipt") or {}
        if str(receipt.get("evaluation_id") or "") != evaluation_id:
            continue
        return {
            "schema_version": "dashboard-ai-evaluation-receipt-v1",
            "evaluation_id": evaluation_id,
            "proposal": {
                key: proposal.get(key)
                for key in (
                    "proposal_id",
                    "cycle_id",
                    "source",
                    "created_at",
                    "direction",
                    "style",
                    "range",
                    "key_levels",
                    "grid",
                    "signal",
                    "tp_sl",
                    "rationale",
                    "analysis",
                    "prompt_contract",
                    "evaluation_receipt",
                )
            },
        }
    raise ValueError("evaluation receipt does not belong to the current cycle")


def _compact_dashboard_read_model_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove polling duplicates; canonical artifacts remain on disk and on-demand."""

    compact = json.loads(json.dumps(payload))
    strategy = compact.get("strategy") or {}
    strategy["proposals"] = [
        _compact_dashboard_proposal(row)
        for row in strategy.get("proposals") or []
        if isinstance(row, dict)
    ]
    strategy["proposal_diff"] = {
        "status": "available_on_demand",
        "reason": "omitted_from_polling_payload",
    }

    execution = compact.get("execution") or {}
    for key in ("accounting", "current_accounting"):
        execution[key] = _compact_dashboard_accounting(execution.get(key))
    execution["orders"] = _bounded_dashboard_rows(
        execution.get("orders"),
        active=lambda row: bool(row.get("is_open") or row.get("is_accepted")),
    )
    execution["positions"] = _bounded_dashboard_rows(
        execution.get("positions"),
        active=lambda row: str(row.get("status") or "") == "open",
    )
    for key in ("trades", "fills"):
        execution[key] = _bounded_dashboard_rows(execution.get(key))
    execution["open_orders"] = [
        row for row in execution.get("orders") or [] if isinstance(row, dict) and row.get("is_open")
    ]
    execution["accepted_orders"] = [
        row for row in execution.get("orders") or [] if isinstance(row, dict) and row.get("is_accepted")
    ]
    execution["open_positions"] = [
        row for row in execution.get("positions") or [] if isinstance(row, dict) and row.get("status") == "open"
    ]

    review = compact.get("review") or {}
    review["cycle_packages"] = [
        _compact_dashboard_review_package(row)
        for row in review.get("cycle_packages") or []
        if isinstance(row, dict)
    ]
    return compact


def _compact_dashboard_accounting(snapshot: Any) -> dict[str, Any]:
    """Retain trust and aggregate facts; lists are projected separately above."""

    if not isinstance(snapshot, dict):
        return {}
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"orders", "positions", "trades", "fills"}
    }


def _bounded_dashboard_rows(value: Any, *, active=None) -> list[Any]:
    """Keep recent history plus every active row; never hide live exposure."""

    rows = value if isinstance(value, list) else []
    if len(rows) <= _DASHBOARD_RECENT_ACTIVITY_LIMIT:
        return rows
    recent = rows[-_DASHBOARD_RECENT_ACTIVITY_LIMIT:]
    active_rows = [row for row in rows if isinstance(row, dict) and active and active(row)]
    seen = {json.dumps(row, sort_keys=True, default=str) for row in recent}
    return recent + [
        row for row in active_rows
        if json.dumps(row, sort_keys=True, default=str) not in seen
    ]


def _compact_dashboard_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Keep trend-card facts while moving verbose input/output to a lazy receipt."""

    receipt = proposal.get("evaluation_receipt") or {}
    receipt_summary = {
        key: receipt.get(key)
        for key in ("schema_version", "evaluation_id", "cycle_id", "status", "evaluated_at", "archive", "effects")
        if key in receipt
    }
    return {
        key: proposal.get(key)
        for key in (
            "schema_version",
            "cycle_id",
            "source",
            "created_at",
            "direction",
            "style",
            "range",
            "key_levels",
            "grid",
            "signal",
            "tp_sl",
            "risk_budget",
            "intraday_rules",
            "legacy_status",
            "legacy",
            "rationale",
            "evidence_used",
            "analysis",
            "preview_id",
            "proposal_id",
        )
        if key in proposal
    } | ({"evaluation_receipt": receipt_summary} if receipt_summary else {})


def _compact_dashboard_review_package(package: dict[str, Any]) -> dict[str, Any]:
    """Keep 12-hour review comparability without re-sending raw AI receipts."""

    compact = json.loads(json.dumps(package))
    proposals = compact.get("proposals") or []
    compact["proposals"] = [
        _compact_dashboard_proposal(row)
        for row in proposals
        if isinstance(row, dict)
    ]
    return compact


def _dashboard_market_read_config() -> dict:
    """Bound a read-only Dashboard market request without changing tick policy."""

    config = dict(load_pipeline_config())
    datafeed = dict(config.get("datafeed") or {})
    try:
        configured_timeout = float(datafeed.get("timeout_seconds", 10.0))
    except (TypeError, ValueError):
        configured_timeout = _DASHBOARD_DATAFEED_TIMEOUT_SECONDS
    datafeed["timeout_seconds"] = max(
        0.1,
        min(configured_timeout, _DASHBOARD_DATAFEED_TIMEOUT_SECONDS),
    )
    datafeed["live_request_attempts"] = 1
    config["datafeed"] = datafeed
    return config


def _assemble_strategy_console_snapshot(
    *,
    output_root: Path | None = None,
    as_of: str | None = None,
) -> dict:
    """Assemble all compatibility sources from one request-scoped market read."""

    output = _dualtrack_output_root(output_root)
    cycle = build_dualtrack_cycle_current_response(output_root=output, as_of=as_of)
    cycle_id = str(cycle["cycle_id"])
    control = StrategyControlPlane(output).read_model(cycle_id, as_of=as_of)
    market = build_dualtrack_market_bars_response(
        limit=240,
        as_of=as_of,
        config=_dashboard_market_read_config(),
    )
    trades = build_dualtrack_trades_response(
        cycle_id,
        track="human",
        output_root=output,
        as_of=as_of,
        market_snapshot=market,
    )
    execution = build_dualtrack_execution_response(
        cycle_id,
        output_root=output,
        as_of=as_of,
        market_snapshot=market,
    )
    production_history = build_strategy_console_production_history(
        output_root=output,
        mark_price=market.get("latest_close"),
        mark_fresh=bool(market.get("fresh")),
        authoritative_engine=str(execution.get("engine") or "legacy_paper"),
    )
    production_account = {
        **production_history["account"],
        "exposure": (execution.get("account") or {}).get("exposure", 0),
        "margin": (execution.get("account") or {}).get("margin", 0),
        "slippage": (execution.get("account") or {}).get("slippage", 0),
    }
    ledger = build_dualtrack_ledger_response(output_root=output)
    raw_cycle_packages = StrategyCyclePackager(output).list_verified_packages(limit=12)
    review_cycle_id = _latest_review_cycle_id(ledger, raw_cycle_packages)
    cycle_packages = _compact_review_packages(raw_cycle_packages, review_cycle_id)
    shadows = (
        [_compact_strategy_shadow(row) for row in load_strategy_shadow_runs(output, review_cycle_id)]
        if review_cycle_id
        else []
    )
    closed_cycle_ids = [
        str(package.get("cycle_id") or "")
        for package in raw_cycle_packages
        if package.get("status") == "closed" and package.get("cycle_id")
    ]
    shadow_promotion = evaluate_grid_shadow_promotion(
        load_strategy_shadow_runs_for_cycles(output, closed_cycle_ids)
    )
    safe_repair_queue = SafeRepairQueue(output).read_model()
    cloud_health = {}
    try:
        rows = load_json(output / "cloud" / "health" / "current.json")
        if rows and isinstance(rows[-1], dict):
            cloud_health = dict(rows[-1])
    except (OSError, ValueError):
        cloud_health = {}
    return {
        "schema_version": "strategy-production-console-v1",
        "cycle": cycle,
        **control,
        "market": market,
        "production_execution": {
            **execution,
            "account": production_account,
            "pnl": production_history["pnl"],
            "trades": production_history["trades"],
            "fills": production_history["fills"],
            "trade_summary": production_history["summary"],
            "production_history_accounting_snapshot": production_history.get("accounting_snapshot", {}),
            "current_cycle_fills": execution.get("fills", []),
            "current_cycle_trades": (
                execution.get("positions", [])
                if str(execution.get("engine") or "") == "nautilus_paper"
                else trades.get("trades", [])
            ),
            "history_contract": production_history["history_contract"],
        },
        "ledger": ledger,
        "cycle_packages": cycle_packages,
        "review_cycle_id": review_cycle_id,
        "daily_reports": build_strategy_console_daily_reports_response(output_root=output),
        "strategy_shadows": shadows,
        "strategy_shadow_promotion": shadow_promotion,
        "safe_repair_queue": safe_repair_queue,
        "cloud_health": cloud_health,
        "execution_shadow": execution.get("shadow_cutover", {}),
        "safety": {
            "one_production_strategy": True,
            "strategy_shadow_separate_from_execution_shadow": True,
            "new_entries_fail_closed": not bool(control.get("production_plan")),
        },
        "ui_capabilities": {
            "manual_order": True,
            "manual_close": True,
            "market_timeframes": ["1m", "5m", "15m", "30m", "1h", "4h"],
            "start_stop_production": True,
            "parameter_mutation": True,
            "cancel_order": True,
            "reset_statistics": True,
            "production_grid_orders": True,
            "strategy_preview": True,
            "runtime_actual_state": True,
        },
    }


def _latest_review_cycle_id(ledger: dict[str, Any], packages: list[dict[str, Any]]) -> str:
    """Choose one closed cycle with review evidence; never mix cycles."""

    closed = {
        str(row.get("cycle_id") or "")
        for row in packages
        if isinstance(row, dict) and row.get("status") == "closed" and row.get("cycle_id")
    }
    for review in ledger.get("recent_reviews") or []:
        cycle_id = str((review or {}).get("cycle_id") or "")
        if cycle_id in closed:
            return cycle_id
    return next(
        (
            str(row.get("cycle_id") or "")
            for row in packages
            if isinstance(row, dict) and row.get("status") == "closed" and row.get("cycle_id")
        ),
        "",
    )


def _compact_review_packages(
    packages: list[dict[str, Any]],
    selected_cycle_id: str,
) -> list[dict[str, Any]]:
    """Project polling-safe review evidence without replay event payloads."""

    result: list[dict[str, Any]] = []
    for package in packages:
        cycle_id = str(package.get("cycle_id") or "")
        summary = {
            key: package.get(key)
            for key in ("schema_version", "cycle_id", "status", "blockers", "packaged_at", "window", "package_hash")
            if key in package
        }
        if cycle_id == selected_cycle_id:
            execution = package.get("execution") or {}
            summary.update({
                "strategy_plan": package.get("strategy_plan"),
                "proposals": package.get("proposals") or [],
                "execution": {
                    "engine": execution.get("engine"),
                    "order_count": len(execution.get("orders") or []),
                    "fill_count": len(execution.get("fills") or []),
                    "position_count": len(execution.get("positions") or []),
                    "pnl": execution.get("pnl") or {},
                    "reconciliation": execution.get("reconciliation") or {},
                },
                "review": package.get("review") or {},
                "strategy_shadows": [
                    _compact_strategy_shadow(row)
                    for row in package.get("strategy_shadows") or []
                    if isinstance(row, dict)
                ],
                "traceability": package.get("traceability") or {},
            })
        result.append(summary)
    return result


def _compact_strategy_shadow(row: dict[str, Any]) -> dict[str, Any]:
    """Keep comparison lineage and metrics; omit replay orders and market events."""

    scenario = row.get("scenario") or {}
    compact_scenario = {
        "plan_identity": scenario.get("plan_identity") or {},
        "evaluation_window": scenario.get("evaluation_window") or {},
        "contracts": scenario.get("contracts") or {},
        "hashes": scenario.get("hashes") or {},
    }
    return {
        key: value
        for key, value in {
            "schema_version": row.get("schema_version"),
            "status": row.get("status"),
            "blockers": row.get("blockers") or [],
            "cycle_id": row.get("cycle_id"),
            "variant_id": row.get("variant_id"),
            "scenario_id": row.get("scenario_id"),
            "input_hash": row.get("input_hash"),
            "plan": row.get("plan") or {},
            "scenario": compact_scenario,
            "metrics": row.get("metrics") or {},
            "review": row.get("review") or {},
            "safety": row.get("safety") or {},
        }.items()
        if value not in (None, "")
    }


def build_strategy_console_daily_reports_response(
    *,
    output_root: Path | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    output = _dualtrack_output_root(output_root)
    rows = load_daily_report_rows(output, limit=limit)
    return {
        "schema_version": "strategy-daily-reports-v1",
        "reports": rows,
        "latest": rows[0] if rows else None,
        "source": "terminal_cycle_packages",
    }


def build_strategy_console_production_history(
    *,
    output_root: Path | None = None,
    mark_price: float | None = None,
    mark_fresh: bool = False,
    limit: int = 200,
    authoritative_engine: str = "legacy_paper",
) -> dict:
    """Return the compatibility view derived from canonical accounting truth."""

    output = _dualtrack_output_root(output_root)
    starting_cash = float(dualtrack_config().get("capital_per_track_usd") or 0.0)
    return build_production_accounting_history(
        output_root=output,
        mark_price=mark_price,
        mark_fresh=mark_fresh,
        starting_cash=starting_cash,
        authoritative_engine=authoritative_engine,
        limit=limit,
    )


def build_strategy_console_control_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    market: dict | None = None,
    account: dict | None = None,
    recommendation_provider=None,
    actor: dict | None = None,
) -> dict:
    output = _dualtrack_output_root(output_root)
    cycle_id = str(payload.get("cycle_id") or cycle_window(payload.get("as_of")).cycle_id)
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    action = str(payload.get("action") or "").lower()
    safe_control = action in {"stop", "cancel_all"}
    # The chart selector is display-only. Production planning always receives
    # the fixed 1m execution tape. Grid geometry needs only D1/4H; the AI
    # recommendation path separately requires D1/4H/1H/15m.
    if market is not None:
        trusted_market = dict(market)
    elif action == "cancel_all":
        trusted_market = {}
    elif action == "stop":
        try:
            trusted_market = dict(build_dualtrack_market_bars_response(
                timeframe="1m",
                limit=240,
                as_of=payload.get("as_of"),
            ))
        except Exception as exc:
            trusted_market = {
                "schema_version": "dualtrack-market-bars-v1",
                "status": "blocked",
                "fresh": False,
                "is_synthetic": False,
                "provider": "",
                "source_mode": "unavailable",
                "symbol": "GOLD",
                "timeframe": "1m",
                "latest_close": None,
                "latest_timestamp": "",
                "bars": [],
                "access_issues": [f"{type(exc).__name__}: {exc}"],
            }
    else:
        trusted_market = dict(build_dualtrack_market_bars_response(
            timeframe="1m",
            limit=240,
            as_of=payload.get("as_of"),
        ))
    if not safe_control and not isinstance(trusted_market.get("strategy_timeframes"), dict):
        required = ("1d", "4h") if action != "refresh_recommendation" else ("1d", "4h", "1h", "15m")
        trusted_market["strategy_timeframes"] = build_strategy_timeframes_response(
            as_of=payload.get("as_of"),
            timeframes=required,
        )
    trusted_account = account
    if trusted_account is None and not safe_control:
        history = build_strategy_console_production_history(
            output_root=output,
            mark_price=trusted_market.get("latest_close"),
            mark_fresh=bool(trusted_market.get("fresh")),
        )
        trusted_account = {
            **dict(history.get("account") or {}),
            "accounting_snapshot": dict(history.get("accounting_snapshot") or {}),
        }
    plane = StrategyControlPlane(output)
    if action == "refresh_recommendation":
        contexts = dict(trusted_market.get("strategy_timeframes") or {})
        if not all(timeframe in contexts for timeframe in ("1d", "4h", "1h", "15m")):
            contexts.update(build_strategy_timeframes_response(as_of=payload.get("as_of")))
            trusted_market["strategy_timeframes"] = contexts
        before = plane.active_plan(cycle_id) or plane.ensure_compatible_active_plan(cycle_id, as_of=payload.get("as_of"))
        review_files = sorted((output / "dualtrack" / "reviews").glob("*_machine.json"))
        review_rows = load_json(review_files[-1]) if review_files else []
        review = review_rows[-1] if review_rows else {}
        try:
            recommendation_service = StrategyRecommendationService(
                output,
                decision_provider=recommendation_provider,
            )
            recommendation = recommendation_service.recommend(
                cycle_id,
                strategy_timeframes=contexts,
                current_plan=before or {},
                account=trusted_account or {},
                review=review,
                now=payload.get("as_of"),
            )
        except (OSError, RuntimeError) as exc:
            # Return a structured fail-closed API error instead of dropping the
            # browser connection. Production state remains untouched.
            raise ValueError(f"AI recommendation unavailable: {exc}") from exc
        preview = plane.preview(
            cycle_id,
            {"direction": recommendation["direction"], "style": recommendation["style"]},
            market=trusted_market,
            account=trusted_account,
        )
        proposal = plane.upsert_proposal({
            "proposal_id": f"proposal-{recommendation['evaluation_receipt']['evaluation_id']}",
            "cycle_id": cycle_id,
            "source": "ai",
            "created_at": recommendation["created_at"],
            "direction": recommendation["direction"],
            "style": recommendation["style"],
            "range": preview["range"],
            "key_levels": recommendation["key_levels"] or [preview["range"]["low"], preview["range"]["high"]],
            "grid": {**preview["grid"], "orders": preview["orders"]},
            "signal": recommendation["signal"],
            "tp_sl": {
                "mode": "per_grid",
                "take_profit": "next_grid_level",
                "stop_loss": "one_grid_beyond_range",
                "r_multiple": 1.0,
            },
            "risk_budget": preview["risk"],
            "intraday_rules": [
                {"if": "1m closes outside range for 3 consecutive bars", "then": "exit_only_and_replan"},
            ],
            "rationale": recommendation["rationale"],
            "evidence_used": recommendation["evidence_used"],
            "analysis": recommendation["analysis"],
            "strategy_type": recommendation["strategy_type"],
            "framework": recommendation["framework"],
            "prompt_contract": recommendation["prompt_contract"],
            "evaluation_receipt": recommendation["evaluation_receipt"],
            "preview_id": preview["preview_id"],
        }, now=recommendation["created_at"])
        after = plane.active_plan(cycle_id)
        unchanged = (before or {}).get("strategy_plan_id") == (after or {}).get("strategy_plan_id")
        finalized_receipt = recommendation_service.persist_receipt(
            recommendation["evaluation_receipt"],
            effects={
                "proposal_id": proposal["proposal_id"],
                "preview_id": preview["preview_id"],
                "production_plan_unchanged": unchanged,
                "orders_created": 0,
            },
        )
        recommendation["evaluation_receipt"] = finalized_receipt
        proposal["evaluation_receipt"] = finalized_receipt
        proposal = plane.upsert_proposal(proposal, now=recommendation["created_at"])
        return {
            "action": action,
            "recommendation": recommendation,
            "proposal": proposal,
            "preview": preview,
            "production_plan_unchanged": unchanged,
        }
    return plane.control(
        cycle_id,
        action,
        payload,
        market=trusted_market,
        account=trusted_account,
        now=payload.get("as_of"),
        actor=actor,
    )


def build_strategy_timeframes_response(
    *,
    as_of: str | None = None,
    market_db: Path | None = None,
    config: dict | None = None,
    timeframes: tuple[str, ...] = ("1d", "4h", "1h", "15m"),
) -> dict[str, dict[str, Any]]:
    """Build fixed, completed strategy bars; never follows the chart timeframe."""
    checked_at = parse_utc(as_of)
    feed = DualTrackMarketFeed(market_db=market_db, config=config)
    # D1 position is a first-class strategy input. Request up to 300 raw bars
    # so the separate completed-calendar position window can retain 200 bars.
    specs = {"1d": 300, "4h": 64, "1h": 96, "15m": 160}
    seconds = {"1d": 86_400, "4h": 14_400, "1h": 3_600, "15m": 900}
    minimum = {"1d": 15, "4h": 15, "1h": 15, "15m": 50}
    result: dict[str, dict[str, Any]] = {}
    unknown = set(timeframes) - set(specs)
    if unknown:
        raise ValueError(f"unsupported strategy timeframes: {sorted(unknown)}")
    for timeframe in timeframes:
        limit = specs[timeframe]
        snapshot = feed.snapshot(symbol="GOLD", timeframe=timeframe, limit=limit, as_of=as_of)
        trusted = snapshot.get("status") in {"ready", "derived"} and snapshot.get("is_synthetic") is False
        # A single upstream connection reset must not make the operator's selected
        # grid mode disagree with the chart. Retry only the exact same trusted
        # source/timeframe once; synthetic data is never retried or accepted.
        if not trusted and snapshot.get("is_synthetic") is False:
            snapshot = feed.snapshot(symbol="GOLD", timeframe=timeframe, limit=limit, as_of=as_of)
            trusted = snapshot.get("status") in {"ready", "derived"} and snapshot.get("is_synthetic") is False
        if not trusted:
            issues = snapshot.get("access_issues") or []
            detail = f": {issues[0]}" if issues else ""
            raise ValueError(f"strategy timeframe {timeframe} is unavailable or untrusted{detail}")
        completed: list[dict[str, Any]] = []
        completed_daily_calendar: list[dict[str, Any]] = []
        for bar in snapshot.get("bars") or []:
            started = parse_utc(str(bar.get("timestamp") or ""))
            if started + timedelta(seconds=seconds[timeframe]) > checked_at:
                continue
            if timeframe == "1d":
                # Position is measured from the exchange's completed calendar
                # daily bars. The Grid D1 ATR keeps its established
                # weekday-only contract below, so a position-window extension
                # cannot alter existing Range geometry.
                completed_daily_calendar.append(dict(bar))
            if timeframe == "1d" and started.weekday() >= 5:
                continue
            completed.append(dict(bar))
        if len(completed) < minimum[timeframe]:
            raise ValueError(f"strategy timeframe {timeframe} has insufficient completed bars")
        result[timeframe] = {
            "timeframe": timeframe,
            "provider": snapshot.get("provider"),
            "is_synthetic": False,
            "status": snapshot.get("status"),
            "fresh": snapshot.get("fresh"),
            "bars": completed,
            "bar_count": len(completed),
            "latest_timestamp": completed[-1].get("timestamp"),
            "completed_only": True,
            "weekends_excluded": timeframe == "1d",
            "long_term_position_bars": completed_daily_calendar if timeframe == "1d" else [],
            "long_term_position_completed_only": timeframe == "1d",
            "long_term_position_weekends_excluded": False if timeframe == "1d" else None,
        }
    return result


def build_dualtrack_config_response() -> dict:
    cfg = dualtrack_config()
    cadence = cfg.get("cycle_cadence") if isinstance(cfg.get("cycle_cadence"), dict) else {}
    planner = cfg.get("machine_planner") if isinstance(cfg.get("machine_planner"), dict) else {}
    reassessment = planner.get("range_reassessment") if isinstance(planner.get("range_reassessment"), dict) else {}
    return {
        "schema_version": "dualtrack-config-v1",
        "max_leverage": cfg.get("max_leverage"),
        "capital_per_track_usd": cfg.get("capital_per_track_usd"),
        "cycle_cadence": {
            "decision_hours": int(cadence.get("hours", 12)),
            "windows_cst": ["09:00-21:00", "21:00-09:00"],
            "accounting_day_hours": int(cadence.get("accounting_day_hours", 24)),
            "restored_at_cst": cadence.get("restored_at_cst"),
        },
        "machine_rules": {
            "range_reassessment": {
                "enabled": bool(reassessment.get("enabled", True)),
                "confirmation_timeframe": str(reassessment.get("confirmation_timeframe") or "1m"),
                "confirm_closes": max(2, int(reassessment.get("confirm_closes", 3))),
                "cooldown_minutes": max(0, int(reassessment.get("cooldown_minutes", 60))),
                "failure_retry_minutes": max(1, int(reassessment.get("failure_retry_minutes", 5))),
                "max_replans_per_window": max(1, int(reassessment.get("max_replans_per_cycle", 2))),
                "minimum_remaining_minutes": max(0, int(reassessment.get("minimum_remaining_minutes", 30))),
            },
            "neutral_mode": "bilateral_grid",
            "untrusted_market_action": "stand_down",
            "missing_plan_action": "stand_down",
            "open_position_on_breach_action": "exit_only_until_flat",
        },
        "safety": {
            "read_only": True,
            "credentials_exposed": False,
        },
    }


def build_dualtrack_order_post_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    enforce_risk: bool = False,
    market: dict | None = None,
    account: dict | None = None,
) -> dict:
    root = _dualtrack_output_root(output_root)
    with production_mutation_lock(root):
        return _build_dualtrack_order_post_response_locked(
            payload,
            output_root=root,
            enforce_risk=enforce_risk,
            market=market,
            account=account,
        )


def _build_dualtrack_order_post_response_locked(
    payload: dict,
    *,
    output_root: Path | None,
    enforce_risk: bool,
    market: dict | None,
    account: dict | None,
) -> dict:
    root = _dualtrack_output_root(output_root)
    command = dict(payload)
    prepared_safe_action_market_gate = (
        dict(command.get("safe_action_market_gate") or {})
        if isinstance(command.get("safe_action_market_gate"), dict)
        else None
    )
    safe_action_market_gate: dict | None = None
    position_cycle_id = str(command.get("position_cycle_id") or "")
    if position_cycle_id:
        request_cycle_id = str(command.get("cycle_id") or "")
        if not _CYCLE_ID_PATTERN.match(position_cycle_id):
            raise ValueError("position_cycle_id must be YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
        if str(command.get("event") or "").lower() not in {"exit", "stop", "target", "flatten"}:
            raise ValueError("position_cycle_id is only valid for closing an existing position")
        if not command.get("trade_id"):
            raise ValueError("cross-cycle position close requires trade_id")
        if cycle_window_from_id(position_cycle_id).start > cycle_window_from_id(request_cycle_id).start:
            raise ValueError("position_cycle_id cannot be later than the request cycle")
        command["request_cycle_id"] = request_cycle_id
        command["cycle_id"] = position_cycle_id
    event = str(command.get("event") or "entry").lower()
    plane = StrategyControlPlane(root)
    production_plan = (
        plane.active_plan(str(command.get("cycle_id") or ""))
        if enforce_risk
        else plane.ensure_compatible_active_plan(str(command.get("cycle_id") or ""))
    )
    strict_production = str(command.get("source") or "") == "strategy_production_console"
    if event == "entry" and strict_production and not production_plan:
        raise ValueError("no valid StrategyPlan: new entries are fail-closed")
    if event == "entry" and strict_production and plane.runtime_state(str(command.get("cycle_id") or ""))["desired_state"] != "running":
        raise ValueError("production strategy is stopped")
    if production_plan:
        command["strategy_plan_id"] = production_plan["strategy_plan_id"]
        command["strategy_plan_version"] = production_plan["version"]
    cfg = plane.config
    if enforce_risk:
        command = normalize_manual_order_command(command, config=cfg)
        command.pop("safe_action_market_gate", None)
    elif isinstance(command.get("safe_action_market_gate"), dict):
        safe_action_market_gate = dict(command["safe_action_market_gate"])
    adapter = build_configured_execution_engine_adapter(root)
    risk_decision_payload: dict | None = None
    if enforce_risk:
        if not isinstance(market, dict):
            raise ValueError("server-validated market is required for order risk")
        action_class = action_class_for_command(command)
        if action_class == "cancel":
            safe_action_market_gate = build_paper_safe_action_market_gate(
                action_class,
                market,
                pricing_source="not_required",
            )
            command["safe_action_market_gate"] = safe_action_market_gate
        elif action_class == "reduce_only":
            command_cycle_id = str(command.get("cycle_id") or "")
            execution_snapshot = adapter.snapshot(command_cycle_id)
            pricing = resolve_paper_safe_action_pricing(
                market,
                execution_snapshot,
                requested_at=str(command.get("requested_at") or command.get("ts") or ""),
                command=command,
                engine_name=str(getattr(adapter, "name", "legacy_paper")),
                last_market_event=last_paper_execution_market_event(adapter, command_cycle_id),
                allow_market_mark=_prepared_safe_action_market_mark_allowed(
                    prepared_safe_action_market_gate,
                    market,
                ),
            )
            command.update({
                "ts": pricing["command_timestamp"],
                "price": pricing["price"],
                "market_price": pricing["price"],
                "market_timestamp": pricing["pricing_timestamp"],
                "market_source": pricing["pricing_provider"],
                "market_fresh": pricing["pricing_source"] == "fresh_server_mark",
            })
            command = normalize_manual_order_command(command, config=cfg)
            execution_price = float(command["price"])
            safe_action_market_gate = build_paper_safe_action_market_gate(
                action_class,
                market,
                pricing_source=pricing["pricing_source"],
                pricing_price=execution_price,
                pricing_timestamp=pricing["pricing_timestamp"],
                pricing_provider=pricing["pricing_provider"],
            )
            command["safe_action_market_gate"] = safe_action_market_gate
        trusted_account = account
        if trusted_account is None and action_class in {"cancel", "reduce_only"}:
            trusted_account = {}
        elif trusted_account is None:
            history = build_strategy_console_production_history(
                output_root=root,
                mark_price=market.get("latest_close"),
                mark_fresh=bool(market.get("fresh")),
                authoritative_engine=str(getattr(adapter, "name", "legacy_paper")),
            )
            trusted_account = {
                **dict(history.get("account") or {}),
                "accounting_snapshot": dict(history.get("accounting_snapshot") or {}),
            }

        def risk_request():
            cycle_id = str(command.get("cycle_id") or "")
            return build_manual_order_risk_request(
                checked_at=str(command.get("ts") or parse_utc(None).isoformat()),
                command=command,
                account_context=trusted_account or {},
                market=market,
                execution_snapshot=adapter.snapshot(cycle_id),
                execution_reconciliation=adapter.reconcile(cycle_id),
                config=cfg,
                policy=plane.risk_port.resolve_policy(cfg),
                evaluator=plane.risk_port.evaluator_metadata(),
            )

        risk_port = plane.risk_port
        initial_request = risk_request()
        initial_decision = risk_port.evaluate(initial_request)
        plane.risk_store.persist(initial_decision)
        require_risk_permission(initial_decision)
        risk_decision_payload = assert_matching_risk_decision(
            risk_port,
            initial_decision,
            risk_request(),
        ).to_dict()
    if action_class_for_command(command) == "cancel":
        target_order_id = str(command.get("cancel_order_id") or command.get("order_id") or "")
        if not target_order_id:
            raise ValueError("cancel_order_id is required")
        cycle_id = str(command.get("cycle_id") or "")
        cancellation = adapter.cancel_orders(
            cycle_id,
            order_ids=[target_order_id],
            ts=str(command.get("requested_at") or command.get("ts") or ""),
            reason="operator_manual_cancel",
        )
        settlement = settle_paper_safe_action_commands(adapter, cycle_id)
        snapshot = adapter.snapshot(cycle_id)
        if any(
            str(row.get("order_id") or "") == target_order_id
            and str(row.get("state") or "").lower() == "accepted"
            for row in snapshot.get("orders") or []
        ):
            raise ValueError("paper cancel left the target order accepted")
        reconciliation = adapter.reconcile(cycle_id)
        if reconciliation.get("status") != "ok":
            raise ValueError("paper ledger reconciliation failed")
        result = {
            "status": "cancelled" if cancellation.get("cancelled_order_count") else "idempotent",
            "cancellation": cancellation,
            "execution_event": settlement,
            "reconciliation": reconciliation,
        }
        if risk_decision_payload is not None:
            result["risk_decision"] = risk_decision_payload
        if safe_action_market_gate is not None:
            result["safe_action_market_gate"] = safe_action_market_gate
        return result
    immediate_nautilus_event = (
        str(getattr(adapter, "name", "")) == "nautilus_paper"
        and (
            str(command.get("order_type") or "market").lower() == "market"
            or event in {"exit", "stop", "target", "flatten"}
        )
    )
    if immediate_nautilus_event:
        trusted_price = _finite_float(command.get("market_price"))
        if trusted_price is None or not command.get("market_timestamp") or not command.get("market_source"):
            raise ValueError("Nautilus market order requires a server-validated market event")
    receipt = adapter.submit_order(command)
    stale_safe_action = (
        safe_action_market_gate is not None
        and safe_action_market_gate.get("action_class") == "reduce_only"
        and safe_action_market_gate.get("pricing_source") != "fresh_server_mark"
    )
    if stale_safe_action:
        settle_paper_safe_action_commands(adapter, str(command.get("cycle_id") or ""))
    if receipt.get("state") == "accepted" or receipt.get("status") == "accepted":
        if immediate_nautilus_event:
            if not stale_safe_action:
                cfg = dualtrack_config()
                nautilus = ((cfg.get("execution_shadow") or {}).get("nautilus") or {})
                market_price = float(command["market_price"])
                adapter.process_market_event({
                    "schema_version": "dualtrack-market-event-v1",
                    "event_id": f"dashboard-order:{receipt.get('order_id')}:{command.get('ts')}",
                    "cycle_id": str(command.get("cycle_id") or ""),
                    "ts_event": str(command.get("ts") or ""),
                    "event_started_at": str(command.get("market_timestamp") or ""),
                    "source": str(command.get("market_source") or ""),
                    "provider": str(command.get("market_source") or ""),
                    "instrument_id": str(nautilus.get("execution_instrument_id") or command.get("symbol") or "GOLD"),
                    "symbol": str(command.get("symbol") or "GOLD"),
                    "timeframe": "1m",
                    "open": market_price,
                    "high": market_price,
                    "low": market_price,
                    "close": market_price,
                    "price": market_price,
                    "fresh": True,
                    "is_synthetic": False,
                })
            snapshot = adapter.snapshot(str(command.get("cycle_id") or ""))
            fill = next((
                row for row in reversed(snapshot.get("fills") or [])
                if str(row.get("order_id") or "") == str(receipt.get("order_id") or "")
            ), None)
            if fill is not None:
                result = {"status": "filled", "fill": fill}
                if risk_decision_payload is not None:
                    result["risk_decision"] = risk_decision_payload
                if safe_action_market_gate is not None:
                    result["safe_action_market_gate"] = safe_action_market_gate
                return result
            if stale_safe_action:
                raise ValueError("paper safe action did not fill against the last trusted market event")
        result = {"status": "accepted", "order": receipt}
        if risk_decision_payload is not None:
            result["risk_decision"] = risk_decision_payload
        if safe_action_market_gate is not None:
            result["safe_action_market_gate"] = safe_action_market_gate
        return result
    if str(getattr(adapter, "name", "")) == "legacy_paper":
        DualTrackScorer(root).rebuild_ledgers()
    result = {"status": "filled", "fill": receipt}
    if risk_decision_payload is not None:
        result["risk_decision"] = risk_decision_payload
    if safe_action_market_gate is not None:
        result["safe_action_market_gate"] = safe_action_market_gate
    return result


def _dualtrack_mutation_request_allowed(host: str, origin: str) -> bool:
    host_value = str(host or "").strip()
    try:
        target = urlparse(f"http://{host_value}")
    except ValueError:
        return False
    if target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    origin_value = str(origin or "").strip()
    if not origin_value:
        return True
    try:
        source = urlparse(origin_value)
        source_port = source.port or (80 if source.scheme == "http" else None)
        target_port = target.port or 80
    except ValueError:
        return False
    return (
        source.scheme == "http"
        and source.hostname == target.hostname
        and source_port == target_port
    )


def _prepare_dualtrack_network_order(
    payload: dict,
    *,
    market: dict,
    received_at: str | datetime,
    expected_provider: str = "",
) -> dict:
    action_class = action_class_for_command(payload)
    now = parse_utc(received_at)
    cycle_id = str(payload.get("cycle_id") or "")
    current_window = cycle_window(now)
    if current_window.cycle_id != cycle_id:
        raise ValueError("order cycle is not current")
    prepared = {
        key: value
        for key, value in payload.items()
        if key not in {
            "action_class",
            "market_fresh",
            "market_price",
            "market_source",
            "market_timestamp",
            "requested_at",
            "safe_action_market_gate",
        }
    }
    prepared["requested_at"] = now.isoformat()
    if action_class == "cancel":
        target_order_id = str(payload.get("cancel_order_id") or payload.get("order_id") or "").strip()
        if not target_order_id:
            raise ValueError("cancel_order_id is required")
        prepared["cancel_order_id"] = target_order_id
        prepared["ts"] = now.isoformat()
        prepared["market_fresh"] = market.get("fresh") is True
        prepared["safe_action_market_gate"] = build_paper_safe_action_market_gate(
            "cancel",
            market,
            pricing_source="not_required",
        )
        return prepared
    if action_class == "reduce_only":
        if market.get("is_synthetic") is True:
            raise ValueError("synthetic market data is forbidden")
        provider = str(market.get("provider") or "")
        mark = _finite_float(market.get("latest_close"))
        try:
            market_ts = (
                parse_utc(market.get("latest_timestamp"))
                if market.get("latest_timestamp") not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            market_ts = None
        canonical_source = market.get("source_mode") in {"requested_symbol", provider}
        canonical_provider = not expected_provider or provider == expected_provider
        usable_mark = (
            market.get("is_synthetic") is False
            and
            canonical_source
            and canonical_provider
            and bool(provider)
            and mark is not None
            and mark > 0
            and market_ts is not None
            and market_ts <= now + timedelta(seconds=60)
        )
        prepared["ts"] = now.isoformat()
        prepared["market_fresh"] = market.get("fresh") is True
        if usable_mark:
            fresh_server_mark = (
                market.get("status") in {"ready", "derived"}
                and market.get("fresh") is True
            )
            prepared.update({
                "price": mark,
                "market_price": mark,
                "market_timestamp": market_ts.isoformat(),
                "market_source": provider,
            })
            prepared["safe_action_market_gate"] = build_paper_safe_action_market_gate(
                action_class,
                market,
                pricing_source=(
                    "fresh_server_mark" if fresh_server_mark else "last_known_server_mark"
                ),
                pricing_price=mark,
                pricing_timestamp=market_ts.isoformat(),
                pricing_provider=provider,
            )
        else:
            prepared.pop("price", None)
            prepared["safe_action_market_gate"] = build_paper_safe_action_market_gate(
                action_class,
                market,
                pricing_source="paper_execution_fallback_required",
            )
        return prepared
    if market.get("is_synthetic") is not False:
        raise ValueError("synthetic market data is forbidden")
    if market.get("status") != "ready" or market.get("fresh") is not True:
        raise ValueError("server market data is stale")
    provider = str(market.get("provider") or "")
    if market.get("source_mode") not in {"requested_symbol", provider}:
        raise ValueError("server market source is not canonical")
    if expected_provider and provider != expected_provider:
        raise ValueError("server market provider is not canonical")
    mark = _finite_float(market.get("latest_close"))
    if mark is None or mark <= 0:
        raise ValueError("server market price is missing")
    try:
        market_ts = parse_utc(market.get("latest_timestamp"))
    except (TypeError, ValueError) as exc:
        raise ValueError("server market timestamp is invalid") from exc
    if market_ts > now + timedelta(seconds=60):
        raise ValueError("server market timestamp is in the future")
    if market_ts < current_window.start:
        raise ValueError("server market timestamp is outside the current cycle")
    prepared["ts"] = now.isoformat()
    prepared["market_price"] = mark
    prepared["market_timestamp"] = market_ts.isoformat()
    prepared["market_source"] = provider
    prepared["market_fresh"] = market.get("fresh") is True
    if str(payload.get("order_type") or "market").lower() == "market":
        prepared["price"] = mark
    return prepared


def _prepared_safe_action_market_mark_allowed(
    gate: dict | None,
    market: dict,
) -> bool:
    """Accept a mark only when the gateway-bound evidence still matches it."""

    if not isinstance(gate, dict) or market.get("is_synthetic") is not False:
        return False
    pricing_source = str(gate.get("pricing_source") or "")
    if pricing_source not in {"fresh_server_mark", "last_known_server_mark"}:
        return False
    provider = str(market.get("provider") or "")
    mark = _finite_float(market.get("latest_close"))
    gate_price = _finite_float(gate.get("pricing_price"))
    if not provider or mark is None or mark <= 0 or gate_price != mark:
        return False
    try:
        market_timestamp = parse_utc(market.get("latest_timestamp")).isoformat()
        gate_timestamp = parse_utc(gate.get("pricing_timestamp")).isoformat()
    except (TypeError, ValueError):
        return False
    return (
        gate.get("schema_version") == "paper-safe-action-market-gate-v1"
        and gate.get("scope") == "paper_only"
        and gate.get("action_class") == "reduce_only"
        and gate.get("entry_market_gate_applies") is False
        and gate.get("pricing_required") is True
        and gate.get("market_is_synthetic") is False
        and str(gate.get("market_status") or "") == str(market.get("status") or "missing")
        and gate.get("market_fresh") is (market.get("fresh") is True)
        and str(gate.get("market_provider") or "") == provider
        and str(gate.get("pricing_provider") or "") == provider
        and gate_timestamp == market_timestamp
    )


def build_dualtrack_trades_response(
    cycle_id: str,
    *,
    track: str = "human",
    output_root: Path | None = None,
    as_of: str | None = None,
    mark_price: float | str | None = None,
    mark_source: str | None = None,
    market_snapshot: dict | None = None,
) -> dict:
    del mark_price, mark_source
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    normalized_track = str(track or "human").lower()
    if normalized_track not in {"human", "machine"}:
        raise ValueError("track must be human or machine")
    closed = _dualtrack_cycle_closed(cycle_id, as_of=as_of)
    output = _dualtrack_output_root(output_root)
    mark = _dualtrack_mark_price(
        output,
        cycle_id,
        closed=closed,
        as_of=as_of,
        market_snapshot=market_snapshot,
    )
    rows = _dualtrack_trade_rows_for_cycle(output, cycle_id, normalized_track, mark)
    enriched = rows["trades"]
    summary = _trade_summary(enriched)
    display = _dualtrack_display_trade_rows(
        output,
        cycle_id,
        normalized_track,
        mark,
        current_trades=enriched,
        current_summary=summary,
        current_safety=rows["safety"],
    )
    return {
        "schema_version": "dualtrack-trades-v1",
        "cycle_id": cycle_id,
        "track": normalized_track,
        "status": "closed" if closed else "mid",
        "blind": False,
        "mark_price": mark["price"],
        "mark_fresh": mark["fresh"],
        "mark_source": mark["source"],
        "trades": enriched,
        "summary": summary,
        "recovery_replay_trades": rows["recovery_replay_trades"],
        "recovery_replay_summary": _trade_summary(rows["recovery_replay_trades"]),
        "display_cycle_id": display["cycle_id"],
        "display_reason": display["reason"],
        "display_trades": display["trades"],
        "display_summary": display["summary"],
        "display_safety": display["safety"],
        "safety": {
            "read_only": True,
            "machine_mid_order_rows_hidden": False,
            **rows["safety"],
        },
    }


def build_dualtrack_execution_response(
    cycle_id: str,
    *,
    output_root: Path | None = None,
    as_of: str | None = None,
    market_snapshot: dict | None = None,
) -> dict:
    """Read-only execution/accounting view for the human paper track.

    The machine track intentionally stays on its blind-safe endpoint during an
    open cycle.  This surface is for operational accounting, not a new machine
    intervention or disclosure channel.
    """

    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    output = _dualtrack_output_root(output_root)
    closed = _dualtrack_cycle_closed(cycle_id, as_of=as_of)
    mark = _dualtrack_mark_price(
        output,
        cycle_id,
        closed=closed,
        as_of=as_of,
        market_snapshot=market_snapshot,
    )
    adapter = build_configured_execution_engine_adapter(output)
    snapshot = adapter.snapshot(
        cycle_id,
        mark_price=mark["price"],
        mark_fresh=mark["fresh"],
        mark_source=mark["source"],
    )
    accounting_source = (
        normalize_nautilus_snapshot_for_accounting(output, snapshot)
        if str(snapshot.get("engine") or "") == "nautilus_paper"
        else snapshot
    )
    accounting_snapshot = project_execution_accounting(accounting_source).to_dict()
    reconciliation = adapter.reconcile(cycle_id)
    reconciliation_rows = load_json(output / "dualtrack" / "reconciliation" / f"{cycle_id}.json")
    latest_reconciliation = reconciliation_rows[-1] if reconciliation_rows else {
        "status": "missing",
        "reason": "shadow_reconciliation_not_run",
    }
    cutover_rows = load_json(output / "dualtrack" / "cutover" / "shadow_gate_current.json")
    latest_cutover = cutover_rows[-1] if cutover_rows else {
        "status": "missing",
        "blocker": "shadow_cutover_gate_not_run",
    }
    return {
        **snapshot,
        "accounting_snapshot": accounting_snapshot,
        "reconciliation": reconciliation,
        "grid_lifecycle": build_grid_lifecycle_evidence(
            output,
            cycle_id=cycle_id,
            execution_snapshot=snapshot,
            reconciliation=reconciliation,
        ),
        "execution_shadow_reconciliation": latest_reconciliation,
        "shadow_cutover": latest_cutover,
        "safety": {
            "read_only": True,
            "execution_control": False,
            "machine_track_disclosed": False,
        },
    }


def build_dualtrack_market_bars_response(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 96,
    market_db: Path | None = None,
    config: dict | None = None,
    as_of: str | None = None,
    end: str | None = None,
) -> dict:
    return project_market_read_model(
        DualTrackMarketFeed(market_db=market_db, config=config).snapshot(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            as_of=as_of,
            end=end,
        )
    )


def _dualtrack_cycle_closed(cycle_id: str, *, as_of: str | None = None) -> bool:
    window = cycle_window_from_id(cycle_id)
    return parse_utc(as_of) >= window.end


def _finite_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _dualtrack_mark_price(
    output_root: Path,
    cycle_id: str,
    *,
    closed: bool,
    as_of: str | None = None,
    market_snapshot: dict | None = None,
) -> dict:
    if closed:
        cycle_rows = load_json(output_root / "dualtrack" / "cycles" / f"{cycle_id}.json")
        cycle = cycle_rows[-1] if cycle_rows else {}
        close_price = _finite_float(cycle.get("close_price"))
        if close_price is not None:
            return {"price": close_price, "fresh": True, "source": "cycle_close"}
    market = market_snapshot or DualTrackMarketFeed().snapshot(
        symbol="GOLD",
        timeframe="1m",
        limit=1,
        as_of=as_of,
    )
    latest_bar = (market.get("bars") or [{}])[-1] if isinstance(market.get("bars"), list) else {}
    price = _finite_float(market.get("latest_close"))
    if price is None:
        price = _finite_float(latest_bar.get("close"))
    return {
        "price": price,
        "fresh": bool(market.get("fresh")) and price is not None,
        "source": str(market.get("source_mode") or market.get("provider") or "market"),
    }


def _dualtrack_trade_rows_for_cycle(output_root: Path, cycle_id: str, track: str, mark: dict) -> dict:
    fills = load_json(output_root / "dualtrack" / "fills" / f"{cycle_id}_{track}.json")
    safety: dict[str, Any] = {}
    replay_trades: list[dict] = []
    if track == "machine":
        fills, safety = filter_invalid_machine_fills(fills)
        replay_fills = [fill for fill in fills if fill.get("execution_origin") == "recovery_replay"]
        fills = [fill for fill in fills if fill.get("execution_origin") != "recovery_replay"]
        replay_trades = apply_unrealized(
            _trades_from_fills(replay_fills, track=track),
            mark["price"],
            mark_fresh=mark["fresh"],
        )
        replay_trades = [{**trade, "source_cycle_id": cycle_id} for trade in replay_trades]
        safety = {
            **safety,
            "recovery_replay_fill_count": len(replay_fills),
            "recovery_replay_realized_pnl": round(
                sum(float(fill.get("realized_pnl") or 0.0) for fill in replay_fills),
                8,
            ),
            "recovery_replay_excluded_from_paper_pnl": True,
        }
    trades = _trades_from_fills(fills, track=track)
    enriched = [
        {**trade, "source_cycle_id": cycle_id}
        for trade in apply_unrealized(trades, mark["price"], mark_fresh=mark["fresh"])
    ]
    return {
        "cycle_id": cycle_id,
        "trades": enriched,
        "recovery_replay_trades": replay_trades,
        "safety": safety,
    }


def _dualtrack_display_trade_rows(
    output_root: Path,
    cycle_id: str,
    track: str,
    mark: dict,
    *,
    current_trades: list[dict],
    current_summary: dict,
    current_safety: dict,
) -> dict:
    carried_open = _prior_open_trade_rows(output_root, cycle_id, track, mark)
    if current_trades:
        existing_ids = {str(trade.get("trade_id") or "") for trade in current_trades}
        carried_open = [trade for trade in carried_open if str(trade.get("trade_id") or "") not in existing_ids]
        display_trades = [*current_trades, *carried_open]
        return {
            "cycle_id": cycle_id,
            "reason": "current_plus_carried_open" if carried_open else "current_cycle",
            "trades": display_trades,
            "summary": _trade_summary(display_trades),
            "safety": current_safety,
        }
    for previous_cycle_id in _same_day_previous_cycles(cycle_id):
        rows = _dualtrack_trade_rows_for_cycle(output_root, previous_cycle_id, track, mark)
        if rows["trades"]:
            existing_ids = {str(trade.get("trade_id") or "") for trade in rows["trades"]}
            older_open = [trade for trade in carried_open if str(trade.get("trade_id") or "") not in existing_ids]
            display_trades = [*rows["trades"], *older_open]
            return {
                "cycle_id": previous_cycle_id,
                "reason": "latest_same_day_plus_carried_open" if older_open else "latest_same_day",
                "trades": display_trades,
                "summary": _trade_summary(display_trades),
                "safety": rows["safety"],
            }
    if carried_open:
        return {
            "cycle_id": str(carried_open[0].get("source_cycle_id") or cycle_id),
            "reason": "carried_open_positions",
            "trades": carried_open,
            "summary": _trade_summary(carried_open),
            "safety": current_safety,
        }
    return {
        "cycle_id": cycle_id,
        "reason": "current_cycle_empty",
        "trades": [],
        "summary": current_summary,
        "safety": current_safety,
    }


def _prior_open_trade_rows(output_root: Path, cycle_id: str, track: str, mark: dict) -> list[dict]:
    current_start = cycle_window_from_id(cycle_id).start
    suffix = f"_{track}.json"
    carried: list[dict] = []
    fills_dir = output_root / "dualtrack" / "fills"
    if not fills_dir.exists():
        return carried
    for path in sorted(fills_dir.glob(f"*{suffix}"), reverse=True):
        prior_cycle_id = path.name.removesuffix(suffix)
        if prior_cycle_id == cycle_id:
            continue
        try:
            if cycle_window_from_id(prior_cycle_id).start >= current_start:
                continue
        except ValueError:
            continue
        rows = _dualtrack_trade_rows_for_cycle(output_root, prior_cycle_id, track, mark)
        carried.extend(trade for trade in rows["trades"] if str(trade.get("status") or "open") == "open")
    return carried


def _same_day_previous_cycles(cycle_id: str) -> list[str]:
    date_part, kind = cycle_id.rsplit("_", 1)
    if kind == "NIGHT":
        return [f"{date_part}_DAY"]
    return []


def _trade_summary(trades: list[dict]) -> dict:
    open_trades = [trade for trade in trades if trade.get("status") == "open"]
    closed_trades = [trade for trade in trades if trade.get("status") == "closed"]
    realized = sum(float(trade.get("realized_pnl") or 0.0) for trade in trades)
    unrealized_values = [trade.get("unrealized_pnl") for trade in open_trades]
    unrealized_known = all(value is not None for value in unrealized_values)
    unrealized = sum(float(value or 0.0) for value in unrealized_values) if unrealized_known else None
    return {
        "trade_count": len(trades),
        "open_trade_count": len(open_trades),
        "closed_trade_count": len(closed_trades),
        "realized_pnl": round(realized, 8),
        "unrealized_pnl": None if unrealized is None else round(unrealized, 8),
    }


def build_dualtrack_runtime_status_response(*, output_root: Path | None = None, as_of: str | None = None) -> dict:
    cfg = dualtrack_config()
    deadline = int(cfg.get("plan_lock_deadline_min_before_cycle", 0))
    now = parse_utc(as_of)
    window = cycle_window(now, lock_deadline_min_before_cycle=deadline)
    output = Path(output_root) if output_root else ROOT / load_pipeline_config().get("output_root", "outputs")
    store = DualTrackPlanStore(output, config=cfg)
    machine = DualTrackMachineRunner(output, config=cfg).machine_payload(window.cycle_id, as_of=now)
    market_config = cfg.get("market_data") if isinstance(cfg.get("market_data"), dict) else {}
    market = DualTrackMarketFeed().snapshot(
        symbol=str(market_config.get("symbol") or "GOLD"),
        timeframe=str(market_config.get("timeframe") or "1m"),
        limit=5,
        as_of=now.isoformat(),
    )
    runner_rows = _json_rows(output / "dualtrack" / "runner" / f"{window.cycle_id}.json")
    latest_runner = runner_rows[-1] if runner_rows else {}
    runner_ts = latest_runner.get("ts")
    runner_age = _age_seconds(runner_ts, now) if runner_ts else None
    runner_max_age = 600
    runner_ok = runner_age is not None and runner_age <= runner_max_age
    cycle_rows = _json_rows(output / "dualtrack" / "cycles" / f"{window.cycle_id}.json")
    cycle_state = cycle_rows[-1] if cycle_rows else {}
    reassessment_rows = _json_rows(output / "dualtrack" / "reassessment" / f"{window.cycle_id}.json")
    range_reassessment = reassessment_rows[-1] if reassessment_rows else {}
    attribution_rows = _json_rows(output / "dualtrack" / "attribution" / f"{window.cycle_id}.json")
    ledger_rows = _json_rows(output / "dualtrack" / "ledger" / "daily" / f"{window.cycle_id.split('_', 1)[0]}.json")
    human_fills = _json_rows(output / "dualtrack" / "fills" / f"{window.cycle_id}_human.json")
    machine_fills = _paper_execution_fills(_json_rows(output / "dualtrack" / "fills" / f"{window.cycle_id}_machine.json"))
    previous_window = cycle_window(window.start - timedelta(seconds=1), lock_deadline_min_before_cycle=deadline)
    previous_attribution_rows = _json_rows(output / "dualtrack" / "attribution" / f"{previous_window.cycle_id}.json")
    previous_ledger_rows = _json_rows(output / "dualtrack" / "ledger" / "daily" / f"{previous_window.cycle_id.split('_', 1)[0]}.json")
    previous_human_fills = _json_rows(output / "dualtrack" / "fills" / f"{previous_window.cycle_id}_human.json")
    previous_machine_fills = _paper_execution_fills(
        _json_rows(output / "dualtrack" / "fills" / f"{previous_window.cycle_id}_machine.json")
    )
    effective = store.machine_plan(window.cycle_id)
    closed = now >= window.end
    market_provider = str(market.get("provider") or "")
    market_source_mode = str(market.get("source_mode") or "")
    market_ok = (
        market.get("status") == "ready"
        and market.get("fresh") is True
        and bool(market_provider)
        and market_source_mode in {"requested_symbol", market_provider}
        and market.get("is_synthetic") is not True
        and (not market_config.get("provider") or market_provider == market_config.get("provider"))
    )
    stood_down = bool(cycle_state.get("machine_stood_down", effective is None))
    machine_layers = list(machine.get("layers") or cycle_state.get("layers") or [])
    invalid_machine_fill_count = _machine_invalid_fill_count(machine_layers)
    sample_ok = bool(effective) and market_ok and runner_ok and not stood_down and invalid_machine_fill_count == 0
    checks = [
        _runtime_check("market", market_ok, "行情新鲜", "行情过期或展示种子", {
            "source_mode": market.get("source_mode"),
            "provider": market.get("provider"),
            "latest_timestamp": market.get("latest_timestamp"),
            "age_minutes": market.get("age_minutes"),
        }),
        _runtime_check("runner", runner_ok, "live tick 正常", "live tick 心跳过期或缺失", {
            "latest_ts": runner_ts,
            "age_seconds": runner_age,
            "max_age_seconds": runner_max_age,
            "event": latest_runner.get("event"),
        }),
        _runtime_check("effective_plan", bool(effective), "机器 AI 作战单存在", "机器 AI 作战单缺失，机器轨应站下", {
            "author": (effective or {}).get("effective_author", ""),
            "decision_mode": (effective or {}).get("decision_mode", ""),
            "degraded": bool((effective or {}).get("degraded")),
        }),
        _runtime_check("machine", not stood_down, "机器轨未站下", "机器轨站下", {
            "layers": machine_layers,
        }),
        _runtime_quality_check(
            "machine_fill_quality",
            invalid_machine_fill_count == 0,
            "机器轨成交几何正常",
            "机器轨模拟出现已过滤的无效成交，禁止作为切换样本",
            {"invalid_machine_fill_count": invalid_machine_fill_count, "layers": machine_layers},
        ),
    ]
    if closed:
        checks.append(_runtime_check("close", bool(attribution_rows), "收盘归因已生成", "收盘归因缺失", {
            "attribution_available": bool(attribution_rows),
            "ledger_available": bool(ledger_rows),
        }))
    status = "blocked" if any(item["status"] == "blocked" for item in checks) else "warn" if any(item["status"] == "warn" for item in checks) else "ok"
    next_tick_due_at = None
    if runner_ts:
        next_tick_due_at = (parse_utc(runner_ts) + timedelta(seconds=60)).isoformat()
    return {
        "schema_version": "dualtrack-runtime-status-v1",
        "status": status,
        "checked_at": now.isoformat(),
        "cycle_id": window.cycle_id,
        "closed": closed,
        "checks": checks,
        "market": {
            "status": market.get("status"),
            "source_mode": market.get("source_mode"),
            "symbol": market.get("symbol"),
            "timeframe": market.get("timeframe"),
            "provider": market.get("provider"),
            "fresh": bool(market.get("fresh")),
            "latest_timestamp": market.get("latest_timestamp"),
            "age_minutes": market.get("age_minutes"),
        },
        "runner": {
            "latest_ts": runner_ts,
            "event": latest_runner.get("event", ""),
            "age_seconds": runner_age,
            "max_age_seconds": runner_max_age,
            "next_tick_due_at": next_tick_due_at,
            "bar_count": (latest_runner.get("detail") or {}).get("bar_count"),
        },
        "machine_range_observation": dict(cycle_state.get("range_observation") or {}),
        "machine_range_reassessment": dict(range_reassessment),
        "sample": {
            "valid_now": sample_ok,
            "has_effective_plan": effective is not None,
            "effective_author": (effective or {}).get("effective_author", ""),
            "machine_stood_down": stood_down,
            "machine_pnl": round(float(machine.get("realized_pnl", 0.0)) + float(machine.get("unrealized_pnl", 0.0)), 8),
            "human_fill_count": len(human_fills),
            "machine_fill_count": len(machine_fills),
            "invalid_machine_fill_count": invalid_machine_fill_count,
            "machine_fills_hidden": False,
        },
        "closeout": {
            "attribution_available": bool(attribution_rows),
            "ledger_available": bool(ledger_rows),
            "replay_url": f"dashboard-dualtrack-replay.html?layout=dualtrack&cycle={window.cycle_id}",
        },
        "previous_closeout": {
            "cycle_id": previous_window.cycle_id,
            "attribution_available": bool(previous_attribution_rows),
            "ledger_available": bool(previous_ledger_rows),
            "replay_url": f"dashboard-dualtrack-replay.html?layout=dualtrack&cycle={previous_window.cycle_id}",
            "human_fill_count": len(previous_human_fills),
            "machine_fill_count": len(previous_machine_fills),
            "machine_fills_hidden": False,
        },
    }


def build_dualtrack_tiger_venue_response(*, output_root: Path | None = None) -> dict:
    return TigerVenueStatus(output_root).snapshot()


def build_tiger_paper_order_refresh_runbook_response(*, output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))
    path = root / "tiger_paper_order_readiness" / "refresh_runbook_current.json"
    rows = load_json(path)
    venue = build_dualtrack_tiger_venue_response(output_root=root)
    summary = venue.get("paper_order_refresh_runbook", {}) if isinstance(venue.get("paper_order_refresh_runbook"), dict) else {}
    if not rows or not isinstance(rows[-1], dict):
        return {
            "schema_version": "tiger-paper-order-refresh-runbook-api-v1",
            "status": "missing",
            "served_from": str(path),
            "command_steps": [],
            "command_count": 0,
            "matches_current_readiness": False,
            "redaction": {
                "raw_command_text_exposed": False,
                "credential_values_exposed": False,
            },
            "endpoint_safety": dict(_TIGER_PAPER_ORDER_REFRESH_RUNBOOK_ENDPOINT_SAFETY),
        }
    receipt = rows[-1]
    generation = receipt.get("generation_safety", {}) if isinstance(receipt.get("generation_safety"), dict) else {}
    sequence = receipt.get("command_sequence_safety", {}) if isinstance(receipt.get("command_sequence_safety"), dict) else {}
    steps = []
    for row in (receipt.get("command_sequence") or []):
        if not isinstance(row, dict):
            continue
        steps.append(
            {
                "name": str(row.get("name") or ""),
                "label": str(row.get("label") or ""),
                "opens_trade_client": row.get("opens_trade_client") is True,
                "opens_trade_client_mode": str(row.get("opens_trade_client_mode") or ""),
                "submits_orders": row.get("submits_orders") is True,
                "writes_runtime_config": row.get("writes_runtime_config") is True,
            }
        )
    return {
        "schema_version": "tiger-paper-order-refresh-runbook-api-v1",
        "status": str(receipt.get("status") or "missing"),
        "served_from": str(path),
        "runbook_id": str(receipt.get("runbook_id") or ""),
        "checked_at": str(receipt.get("checked_at") or ""),
        "run_date": str(receipt.get("run_date") or ""),
        "readiness_status": str(receipt.get("readiness_status") or ""),
        "readiness_checked_at": str(receipt.get("readiness_checked_at") or ""),
        "matches_current_readiness": summary.get("matches_current_readiness") is True,
        "source_readiness_checked_at": str(summary.get("source_readiness_checked_at") or ""),
        "current_readiness_checked_at": str(summary.get("current_readiness_checked_at") or ""),
        "blocker_count": int(receipt.get("blocker_count") or 0),
        "stale_evidence_count": int(receipt.get("stale_evidence_count") or 0),
        "blocker_names": [str(name) for name in (receipt.get("blocker_names") or []) if name],
        "command_count": len(steps),
        "command_steps": steps,
        "generation_safety": {
            "artifact_only": generation.get("artifact_only") is True,
            "opens_quote_client": generation.get("opens_quote_client") is True,
            "opens_trade_client": generation.get("opens_trade_client") is True,
            "submits_orders": generation.get("submits_orders") is True,
            "cancels_orders": generation.get("cancels_orders") is True,
            "closes_positions": generation.get("closes_positions") is True,
            "writes_runtime_config": generation.get("writes_runtime_config") is True,
            "credential_values_exposed": generation.get("credential_values_exposed") is True,
        },
        "command_sequence_safety": {
            "opens_trade_client_read_only": sequence.get("opens_trade_client_read_only") is True,
            "submits_orders": sequence.get("submits_orders") is True,
            "writes_runtime_config": sequence.get("writes_runtime_config") is True,
        },
        "redaction": {
            "raw_command_text_exposed": False,
            "credential_values_exposed": False,
        },
        "endpoint_safety": dict(_TIGER_PAPER_ORDER_REFRESH_RUNBOOK_ENDPOINT_SAFETY),
    }


def build_connector_catalog_response() -> dict:
    return ConnectorCatalog().snapshot()


def build_connector_onboarding_dry_run_response(payload: dict, *, output_root: Path | None = None) -> dict:
    return ConnectorOnboardingDryRun(output_root=output_root).evaluate(payload)


def build_connector_activation_plan_response(payload: dict, *, output_root: Path | None = None) -> dict:
    return ConnectorActivationPlan(output_root=output_root).evaluate(payload)


def build_connector_attended_switch_review_response(*, output_root: Path | None = None) -> dict:
    status = build_connector_config_status_response(output_root=output_root)
    stage = status.get("operator_stage", {}) if isinstance(status.get("operator_stage"), dict) else {}
    review = stage.get("attended_switch_review", {}) if isinstance(stage.get("attended_switch_review"), dict) else {}
    latest_check = status.get("latest_check", {}) if isinstance(status.get("latest_check"), dict) else {}
    latest_authorization = status.get("latest_authorization", {}) if isinstance(status.get("latest_authorization"), dict) else {}
    latest_audit = status.get("latest_readiness_audit", {}) if isinstance(status.get("latest_readiness_audit"), dict) else {}
    current_runtime = status.get("current_runtime", {}) if isinstance(status.get("current_runtime"), dict) else {}
    return {
        "schema_version": "connector-attended-switch-review-api-v1",
        "status": str(review.get("status") or "missing"),
        "checked_at": str(status.get("checked_at") or ""),
        "operator_stage": str(stage.get("stage") or ""),
        "stage_summary": str(stage.get("summary") or ""),
        "next_action": str(stage.get("next_action") or ""),
        "package_id": str(review.get("package_id") or latest_audit.get("package_id") or latest_authorization.get("package_id") or latest_check.get("package_id") or ""),
        "authorization_id": str(review.get("authorization_id") or latest_authorization.get("authorization_id") or ""),
        "audit_id": str(review.get("audit_id") or latest_audit.get("audit_id") or ""),
        "can_switch_config_with_operator_authorization": review.get("can_switch_config_with_operator_authorization") is True,
        "runtime_switched_to_tiger_mgc": stage.get("runtime_switched_to_tiger_mgc") is True,
        "current_broker_provider": str(current_runtime.get("broker_provider") or ""),
        "current_dualtrack_symbol": str(current_runtime.get("dualtrack_symbol") or ""),
        "can_trade_machine_track": stage.get("can_trade_machine_track") is True,
        "can_submit_tiger_orders": stage.get("can_submit_tiger_orders") is True,
        "requires_operator_command": review.get("requires_operator_command") is True,
        "requires_warning_acceptance": review.get("requires_warning_acceptance") is True,
        "requires_acknowledgement": review.get("requires_acknowledgement") is True,
        "requires_package_id": review.get("requires_package_id") is True,
        "rollback_required": review.get("rollback_required") is True,
        "post_apply_validation_count": int(review.get("post_apply_validation_count") or 0),
        "current_runtime": {
            "broker_provider": str(current_runtime.get("broker_provider") or ""),
            "dualtrack_symbol": str(current_runtime.get("dualtrack_symbol") or ""),
            "runtime_switched_to_tiger_mgc": stage.get("runtime_switched_to_tiger_mgc") is True,
        },
        "after_switch_gates": {
            "can_trade_machine_track": review.get("can_trade_machine_track_after_switch") is True,
            "can_submit_tiger_orders": review.get("can_submit_tiger_orders_after_switch") is True,
            "not_authorized": list(review.get("not_authorized_after_switch") or []),
        },
        "evidence": {
            "write_check_status": str(latest_check.get("status") or "missing"),
            "authorization_status": str(latest_authorization.get("status") or "missing"),
            "final_readiness_audit_status": str(latest_audit.get("status") or "missing"),
            "final_readiness_audit_checked_at": str(latest_audit.get("checked_at") or ""),
            "authorization_markdown": str(review.get("authorization_markdown") or ""),
            "final_readiness_audit_markdown": str(review.get("final_readiness_audit_markdown") or ""),
        },
        "redaction": {
            "raw_acknowledgement_exposed": False,
            "attended_apply_command_exposed": False,
            "credential_values_exposed": False,
        },
        "endpoint_safety": dict(_CONNECTOR_ATTENDED_SWITCH_REVIEW_ENDPOINT_SAFETY),
    }


def build_system_status_contract(payload: dict, *, strategy_id: str = "") -> dict:
    health = _system_health_model(payload)
    permission = _trade_permission(payload, health, strategy_id=strategy_id)
    return {
        "contract": _contract_header("system-status-v1", payload),
        "run_date": payload.get("run_date", ""),
        "strategy_id": permission.get("strategy_id", ""),
        "status": permission.get("status", ""),
        "headline": permission.get("headline", ""),
        "headline_zh": permission.get("headline_zh", ""),
        "trade_permission": permission,
        "health": health,
        "diagnostics": {
            "ops_status_query": "/api/ops/status",
            "full_diagnostics_query": "/api/dashboard?view=full",
        },
    }


def build_trader_overview_contract(payload: dict, *, strategy_id: str = "") -> dict:
    system = build_system_status_contract(payload, strategy_id=strategy_id)
    selected_id = system["trade_permission"].get("strategy_id", "")
    strategy = _selected_strategy_row(payload, selected_id)
    detail = payload.get("strategy_detail") if isinstance(payload.get("strategy_detail"), dict) else {}
    return {
        "contract": _contract_header("trader-overview-v1", payload),
        "run_date": payload.get("run_date", ""),
        "strategy_id": selected_id,
        "latest": payload.get("latest", {}),
        "latest_quote": payload.get("latest_quote", {}),
        "market": {
            "market_view_status": _market_view_for_trader(payload.get("market_view_status", {})),
            "ohlc_quality": _ohlc_quality_for_trader(payload.get("ohlc_quality", {})),
            "market_data_gate": _market_data_gate_for_trader(payload.get("market_data_gate", {})),
            "data_provenance": _data_provenance_for_trader(payload.get("data_provenance", {})),
        },
        "trade_permission": system["trade_permission"],
        "system_health": system["health"],
        "current_strategy": _strategy_overview_row(strategy),
        "nav": {
            "strategy_points": _compact_nav_points(strategy.get("nav_points", [])),
            "gold_points": _compact_gold_nav_points(((payload.get("performance_board") or {}).get("gold_nav") or {}).get("points", [])),
            "quality": strategy.get("nav_quality", {}),
        },
        "position": strategy.get("position", {}),
        "today": {
            "daily_trade_samples": _daily_trade_samples_for_trader(payload.get("daily_trade_samples", {})),
            "trade_reviews": _trade_reviews_for_trader(payload.get("trade_reviews", {})),
            "review_loop": _review_loop_for_trader(payload.get("review_loop", {})),
        },
        "trades": {
            "open": [_compact_trade(row) for row in (detail.get("open_trades") or []) if isinstance(row, dict)][:8],
            "closed_recent": [_compact_trade(row) for row in (detail.get("closed_trades") or []) if isinstance(row, dict)][-12:],
            "record_cards": [
                _compact_trade_record_card(row)
                for row in (detail.get("trade_record_cards") or [])
                if isinstance(row, dict)
            ][-12:],
        },
        "replay": {
            "api": "/api/replay",
            "dashboard_url": "/dashboard-replay-v4.html",
            "default_strategy": selected_id,
        },
    }


def build_ops_status_contract(payload: dict, *, strategy_id: str = "") -> dict:
    system = build_system_status_contract(payload, strategy_id=strategy_id)
    dashboard_health = payload.get("dashboard_health", {}) if isinstance(payload.get("dashboard_health"), dict) else {}
    health_checks = [
        _ops_check(row)
        for row in dashboard_health.get("checks", [])
        if isinstance(row, dict)
    ]
    return {
        "contract": _contract_header("ops-status-v1", payload),
        "run_date": payload.get("run_date", ""),
        "status": _ops_overall_status(system["trade_permission"], health_checks),
        "system": system,
        "runner": {
            "runner": payload.get("runner", {}),
            "system_vitals": payload.get("system_vitals", {}),
            "bot_supervisor": payload.get("bot_supervisor", {}),
            "bot_checkpoint": payload.get("bot_checkpoint", {}),
            "schedule": payload.get("schedule", {}),
            "schedule_status": payload.get("schedule_status", {}),
            "schedule_install_plan": payload.get("schedule_install_plan", {}),
            "schedule_install": payload.get("schedule_install", {}),
            "schedule_rollback_plan": payload.get("schedule_rollback_plan", {}),
            "schedule_rollback": payload.get("schedule_rollback", {}),
            "schedule_post_install_verify": payload.get("schedule_post_install_verify", {}),
            "schedule_takeover_package": payload.get("schedule_takeover_package", {}),
            "schedule_takeover_package_check": payload.get("schedule_takeover_package_check", {}),
        },
        "data": {
            "market_data_gate": payload.get("market_data_gate", {}),
            "ohlc_quality": payload.get("ohlc_quality", {}),
            "data_health": _compact_data_health(payload.get("data_health", {})) if isinstance(payload.get("data_health"), dict) else {},
            "data_gaps": payload.get("data_gaps", {}),
            "data_gap_repair": payload.get("data_gap_repair", {}),
            "data_integrity": payload.get("data_integrity", {}),
            "data_archive": payload.get("data_archive", {}),
            "market_db": payload.get("market_db", {}),
            "source_contracts": payload.get("source_contracts", {}),
        },
        "execution": {
            "live_reconciliation": payload.get("live_reconciliation", {}),
            "legacy_live_reconciliation": payload.get("legacy_live_reconciliation", {}),
            "live_submission_safety": payload.get("live_submission_safety", {}),
            "live_broker_preflight": payload.get("live_broker_preflight", {}),
            "paper_reconciliation": payload.get("paper_reconciliation", {}),
            "paper_trade_attribution": payload.get("paper_trade_attribution", {}),
            "paper_exit_monitor": payload.get("paper_exit_monitor", {}),
            "risk_monitor": payload.get("risk_monitor", {}),
        },
        "alerts": payload.get("alerts", {}),
        "backend": {
            "dashboard_health": {
                "status": dashboard_health.get("status", ""),
                "health_attention": dashboard_health.get("health_attention", []),
                "checks": health_checks,
            },
            "backend_maturity": _compact_backend_maturity(payload.get("backend_maturity", {}))
            if isinstance(payload.get("backend_maturity"), dict)
            else {},
            "operation_runbook": payload.get("operation_runbook", {}),
        },
        "diagnostics": {
            "full_diagnostics_query": "/api/dashboard?view=full",
            "legacy_ops_query": "/api/dashboard?view=ops",
        },
    }


def _contract_header(schema_version: str, payload: dict) -> dict:
    source = payload.get("contract", {}) if isinstance(payload.get("contract"), dict) else {}
    return {
        "schema_version": schema_version,
        "generated_at": _utc_now(),
        "source_schema_version": source.get("schema_version", ""),
        "source_generated_at": source.get("generated_at", ""),
    }


def _system_health_model(payload: dict) -> dict:
    system_vitals = payload.get("system_vitals", {}) if isinstance(payload.get("system_vitals"), dict) else {}
    always_on = system_vitals.get("always_on", {}) if isinstance(system_vitals.get("always_on"), dict) else {}
    vitals = {
        str(row.get("name")): row
        for row in system_vitals.get("vitals", [])
        if isinstance(row, dict) and row.get("name")
    }
    rows = []
    for key in ("data_feed", "strategy_evaluation", "runner_liveness", "execution_blocker", "tp_sl_coverage"):
        vital = vitals.get(key, {})
        status = str(vital.get("status") or "warn")
        rows.append(
            {
                "key": key,
                "status": status if status in {"up", "warn", "down"} else "warn",
                "message": vital.get("message", "vital missing"),
                "detail": vital.get("detail", {}) if isinstance(vital.get("detail"), dict) else {},
                "suppressed_down": False,
            }
        )
    if always_on.get("blocks_new_orders"):
        rows.append(
            {
                "key": "always_on",
                "status": "down",
                "message": "always-on critical heartbeat stale or missing",
                "detail": always_on,
                "suppressed_down": False,
            }
        )
    elif always_on.get("status") == "DEGRADED":
        rows.append(
            {
                "key": "always_on",
                "status": "warn",
                "message": "always-on non-critical heartbeat degraded",
                "detail": always_on,
                "suppressed_down": False,
            }
        )
    hard_down_rows = [row for row in rows if row["status"] == "down"]
    warn_rows = [row for row in rows if row["status"] == "warn"]
    return {
        "overall": "down" if hard_down_rows else "warn" if warn_rows else "up",
        "source_overall": system_vitals.get("overall", ""),
        "checked_at": system_vitals.get("checked_at", ""),
        "always_on": always_on,
        "rows": rows,
        "hard_down_rows": hard_down_rows,
        "warn_rows": warn_rows,
    }


def _trade_permission(payload: dict, health: dict, *, strategy_id: str = "") -> dict:
    selected_id = _active_strategy_id(payload, strategy_id)
    strategy = _selected_strategy_row(payload, selected_id)
    hard_down = list(health.get("hard_down_rows", []) or [])
    warn_rows = list(health.get("warn_rows", []) or [])
    demo_blocker = (payload.get("performance_board") or {}).get("active_demo_blocker", {}) if isinstance(payload.get("performance_board"), dict) else {}
    money_guardrails = payload.get("live_money_guardrails", {}) if isinstance(payload.get("live_money_guardrails"), dict) else {}
    open_count = _int_or(strategy.get("open_trades"), 0)
    if open_count == 0:
        detail = payload.get("strategy_detail") if isinstance(payload.get("strategy_detail"), dict) else {}
        open_count = len([row for row in detail.get("open_trades", []) if isinstance(row, dict)])
    today_count = _int_or(strategy.get("today_trade_count"), 0)
    daily_execution = strategy.get("daily_execution", {}) if isinstance(strategy.get("daily_execution"), dict) else {}
    if today_count == 0:
        today_count = _int_or(daily_execution.get("executed_trade_count"), 0)
    open_limit = _effective_open_trade_limit(selected_id)

    blockers: list[dict] = []
    primary: dict = {}
    status = "READY_TO_TRADE"
    headline = "System ready; wait for the next valid signal."
    headline_zh = "系统可交易；等待下一张合格信号。"

    if money_guardrails.get("status") == "BLOCKED_OPERATOR_HALT":
        primary = _blocker_from_live_money_guardrail(money_guardrails)
        blockers.append(primary)
        status = primary["status"]
        headline = "No new exposure: operator HALT is active."
        headline_zh = "现在不开新仓：人工 HALT 已生效。"
    elif hard_down:
        primary = _blocker_from_health_row(hard_down[0])
        blockers.extend(_blocker_from_health_row(row) for row in hard_down)
        status = primary["status"]
        headline = "No new exposure: system blocker."
        headline_zh = "现在不开新仓：系统异常。"
    elif isinstance(demo_blocker, dict) and demo_blocker.get("blocked"):
        primary = _blocker_from_demo_blocker(demo_blocker)
        blockers.append(primary)
        status = primary["status"]
        headline = "No new exposure: execution reconciliation blocker."
        headline_zh = "现在不开新仓：执行/对账阻塞。"
    elif str(money_guardrails.get("status") or "").startswith("BLOCKED_"):
        primary = _blocker_from_live_money_guardrail(money_guardrails)
        blockers.append(primary)
        status = primary["status"]
        headline = "No new exposure: live money guardrail block."
        headline_zh = "现在不开新仓：真钱资金护栏阻断。"
    elif open_count >= open_limit["limit"]:
        primary = {
            "status": "PAUSED_POSITION_LIMIT",
            "code": "position_limit",
            "source": open_limit["source"],
            "message": f"{open_count} open trades reaches the effective strategy limit {open_limit['limit']}",
        }
        blockers.append(primary)
        status = "PAUSED_POSITION_LIMIT"
        headline = "No new exposure: position limit reached."
        headline_zh = "现在不开新仓：持仓已满。"
    elif warn_rows:
        primary = _blocker_from_health_row(warn_rows[0], degraded=True)
        blockers.extend(_blocker_from_health_row(row, degraded=True) for row in warn_rows)
        status = "DEGRADED"
        headline = "System degraded: non-critical observability needs review."
        headline_zh = "系统降级：非关键观测面需复核，但不阻断新信号。"

    return {
        "status": status,
        "headline": headline,
        "headline_zh": headline_zh,
        "strategy_id": selected_id,
        "primary_blocker": primary,
        "blockers": blockers,
        "is_system_blocker": status.startswith("BLOCKED_") and status != "BLOCKED_STRATEGY",
        "is_strategy_blocker": status == "BLOCKED_STRATEGY",
        "is_position_limit": status == "PAUSED_POSITION_LIMIT",
        "allows_new_order_if_signal": status in {"READY_TO_TRADE", "DEGRADED"},
        "open_trade_count": open_count,
        "open_trade_limit": open_limit["limit"],
        "open_trade_limit_source": open_limit["source"],
        "requires_flat_before_entry": open_limit["requires_flat_before_entry"],
        "today_executed_trade_count": today_count,
        "live_money_guardrails": {
            "status": money_guardrails.get("status", ""),
            "allows_new_order": money_guardrails.get("allows_new_order"),
            "limits": money_guardrails.get("limits", {}) if isinstance(money_guardrails.get("limits"), dict) else {},
        },
        "always_on": health.get("always_on", {}) if isinstance(health.get("always_on"), dict) else {},
    }


def _effective_open_trade_limit(strategy_id: str) -> dict:
    try:
        demo = (load_pipeline_config().get("demo_trading", {}) or {})
    except Exception:  # noqa: BLE001 - dashboard status should degrade gracefully if config is unreadable.
        demo = {}
    requires_flat = (
        bool(demo.get("enabled") is True)
        and str(demo.get("active_strategy_id", "")) == str(strategy_id or "")
        and bool(demo.get("require_flat_before_entry", False))
    )
    if requires_flat:
        return {
            "limit": 1,
            "source": "pipeline.demo_trading.require_flat_before_entry",
            "requires_flat_before_entry": True,
        }
    return {
        "limit": _MAX_OPEN_TRADES_PER_STRATEGY,
        "source": "performance_board.strategies[].open_trades",
        "requires_flat_before_entry": False,
    }


def _blocker_from_health_row(row: dict, *, degraded: bool = False) -> dict:
    key = row.get("key", "")
    code = key or "system_health"
    if degraded:
        status = "DEGRADED"
    elif key == "data_feed":
        status = "BLOCKED_DATA_STALE"
    elif key == "execution_blocker":
        detail = row.get("detail", {}) if isinstance(row.get("detail"), dict) else {}
        status = str(detail.get("system_state") or "BLOCKED_RECONCILIATION")
        code = str(detail.get("reason_code") or key or "execution_blocker")
    elif key == "tp_sl_coverage":
        status = "BLOCKED_PROTECTION_MISSING"
    elif key in {"runner_liveness", "strategy_evaluation", "always_on"}:
        status = "BLOCKED_ALWAYS_ON_STALE"
        detail = row.get("detail", {}) if isinstance(row.get("detail"), dict) else {}
        critical = detail.get("critical_blockers") if isinstance(detail.get("critical_blockers"), list) else []
        first = critical[0] if critical and isinstance(critical[0], dict) else {}
        code = str(first.get("name") or key or "always_on")
    else:
        status = "BLOCKED_SYSTEM_DOWN"
    return {
        "status": status,
        "code": code,
        "source": f"system_vitals.{key}" if key else "system_vitals",
        "message": row.get("message", ""),
    }


def _blocker_from_demo_blocker(blocker: dict) -> dict:
    if blocker.get("system_state"):
        status = str(blocker.get("system_state"))
        return {
            "status": status,
            "code": blocker.get("reason_code") or blocker.get("status") or "active_demo_blocker",
            "source": "performance_board.active_demo_blocker",
            "message": blocker.get("reason") or blocker.get("message") or "active demo strategy has an execution blocker",
        }
    text = " ".join(str(blocker.get(key) or "") for key in ("status", "reason", "message")).lower()
    status = "BLOCKED_PROTECTION_MISSING" if any(token in text for token in ("protect", "tp", "sl", "止盈", "止损", "护单")) else "BLOCKED_RECONCILIATION"
    return {
        "status": status,
        "code": blocker.get("status") or "active_demo_blocker",
        "source": "performance_board.active_demo_blocker",
        "message": blocker.get("reason") or blocker.get("message") or "active demo strategy has an execution blocker",
    }


def _blocker_from_live_money_guardrail(guardrail: dict) -> dict:
    blocker = guardrail.get("primary_blocker", {}) if isinstance(guardrail.get("primary_blocker"), dict) else {}
    return {
        "status": blocker.get("status") or guardrail.get("status") or "BLOCKED_MONEY_GUARDRAIL",
        "code": blocker.get("code") or guardrail.get("reason_code") or "live_money_guardrail",
        "source": blocker.get("source") or "live_money_guardrails.current",
        "message": blocker.get("message") or guardrail.get("headline") or "live money guardrail blocks new orders",
    }


def _active_strategy_id(payload: dict, requested: str = "") -> str:
    if requested:
        return requested
    board = payload.get("performance_board", {}) if isinstance(payload.get("performance_board"), dict) else {}
    if board.get("active_strategy_id"):
        return str(board["active_strategy_id"])
    if payload.get("strategy_id"):
        return str(payload["strategy_id"])
    samples = payload.get("daily_trade_samples", {}) if isinstance(payload.get("daily_trade_samples"), dict) else {}
    if samples.get("active_strategy_id"):
        return str(samples["active_strategy_id"])
    rows = board.get("strategies", []) if isinstance(board.get("strategies"), list) else []
    return str(rows[0].get("strategy_id") or "") if rows and isinstance(rows[0], dict) else ""


def _selected_strategy_row(payload: dict, strategy_id: str) -> dict:
    board = payload.get("performance_board", {}) if isinstance(payload.get("performance_board"), dict) else {}
    rows = board.get("strategies", []) if isinstance(board.get("strategies"), list) else []
    for row in rows:
        if isinstance(row, dict) and str(row.get("strategy_id") or "") == strategy_id:
            return row
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _strategy_overview_row(row: dict) -> dict:
    keep = (
        "strategy_id",
        "rank",
        "engine",
        "timeframe",
        "classification",
        "execution_profile",
        "status",
        "today_trade_count",
        "trade_count_7d",
        "inactive_days",
        "sample_status",
        "starting_equity",
        "current_equity",
        "return_pct",
        "gold_return_pct",
        "vs_gold_pct",
        "max_drawdown_pct",
        "win_rate",
        "profit_factor",
        "net_pnl",
        "closed_trades",
        "open_trades",
        "performance_confidence",
        "position",
    )
    return {key: row.get(key) for key in keep if key in row}


def _compact_nav_points(points: object) -> list[dict]:
    rows = [row for row in (points or []) if isinstance(row, dict)]
    return [
        {key: row.get(key) for key in ("timestamp", "run_date", "equity", "return_pct") if key in row}
        for row in rows[-600:]
    ]


def _compact_gold_nav_points(points: object) -> list[dict]:
    rows = [row for row in (points or []) if isinstance(row, dict)]
    return [
        {key: row.get(key) for key in ("timestamp", "close", "index") if key in row}
        for row in rows[-600:]
    ]


def _market_view_for_trader(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = ("status", "direction_bias", "filter_effect", "operator_message", "summary", "trade_plan", "expires_at")
    return {key: value.get(key) for key in keep if key in value}


def _ohlc_quality_for_trader(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = ("status", "provider", "truth_level", "promotion_ready", "trust_label", "action", "official_rows")
    return {key: value.get(key) for key in keep if key in value}


def _market_data_gate_for_trader(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = ("mode", "trader_label", "trader_summary", "trader_action", "promotion_ready", "official_rows", "blockers")
    return {key: value.get(key) for key in keep if key in value}


def _data_provenance_for_trader(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = ("mode", "allows_paper", "allows_live", "latest_provider", "latest_truth_level", "latest_is_public")
    return {key: value.get(key) for key in keep if key in value}


def _daily_trade_samples_for_trader(value: object) -> dict:
    return _compact_daily_trade_samples(value) if isinstance(value, dict) else {}


def _trade_reviews_for_trader(value: object) -> dict:
    return _compact_trade_reviews(value) if isinstance(value, dict) else {}


def _review_loop_for_trader(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = ("status", "run_date", "active_strategy_id", "today_focus", "next_actions")
    compact = {key: value.get(key) for key in keep if key in value}
    for key in ("morning_plan", "evening_review", "strategy_experiment", "strategy_improvement_plan"):
        if isinstance(value.get(key), dict):
            compact[key] = {
                sub_key: value[key].get(sub_key)
                for sub_key in ("status", "summary", "next_action", "action")
                if sub_key in value[key]
            }
    return compact


def _ops_check(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in ("name", "status", "message", "strategy_id", "strategy_ids", "mode", "blockers", "generated_at")
        if key in row
    }


def _ops_overall_status(permission: dict, checks: list[dict]) -> str:
    if str(permission.get("status", "")).startswith("BLOCKED_"):
        return "fail"
    statuses = {str(row.get("status") or "") for row in checks}
    if statuses & {"fail", "error", "down", "block"}:
        return "fail"
    if permission.get("status") in {"DEGRADED", "PAUSED_POSITION_LIMIT"} or statuses & {"warn", "degraded"}:
        return "warn"
    return "ok"


def _int_or(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_public_access_health(
    public_url: Optional[str] = None,
    local_url: Optional[str] = None,
    timeout: float = 4.0,
    log_path: Optional[Path] = None,
) -> dict:
    public_url = public_url or os.environ.get("GOLDBOT_PUBLIC_DASHBOARD_URL", _PUBLIC_DASHBOARD_URL)
    local_url = local_url or os.environ.get("GOLDBOT_LOCAL_GATEWAY_URL", _LOCAL_GATEWAY_URL)
    log_path = log_path or Path(os.environ.get("GOLDBOT_CLOUDFLARED_LOG", str(_CLOUDFLARED_LOG))).expanduser()
    local = _probe_http(local_url, timeout)
    public = _probe_http(public_url, timeout)
    tunnel = _cloudflared_process_summary()
    log = _cloudflared_log_summary(log_path)
    local_ok = _http_ok(local)
    public_ok = _http_ok(public)
    deployment_timeout = max(timeout, 8.0)
    deployment = _deployment_feature_summary(public_url, deployment_timeout) if public_ok else {
        "status": "skip",
        "reason": "public dashboard is not reachable",
        "checks": [],
        "missing_features": [],
    }
    status = "ok" if public_ok else "fail"
    diagnosis = "public_access_ok"
    action = "Public domain is serving the trader dashboard."
    if not local_ok:
        diagnosis = "local_gateway_failed"
        action = "Start or restart the local gateway on 127.0.0.1:8766 before checking Cloudflare."
    elif not public_ok:
        diagnosis = "public_tunnel_failed"
        action = "Local dashboard is healthy; fix Cloudflare tunnel egress or connector routing."
        latest_text = "\n".join(log.get("latest_errors", []))
        if any(token in latest_text for token in ("QUIC", "7844", "HTTP/2", "TLS handshake", "no route to host")):
            diagnosis = "cloudflare_edge_unreachable"
            action = "Allow outbound Cloudflare Tunnel traffic on port 7844 or run the connector from a network that can reach Cloudflare edge."
    elif deployment.get("status") != "ok":
        status = "warn"
        if deployment.get("reason") == "ops_access_forbidden":
            diagnosis = "public_ops_protected"
            action = (
                "Trader dashboard is public; OPS dashboard returns 403. "
                "Verify the Cloudflare Access or route policy, or use the local OPS dashboard."
            )
        elif deployment.get("reason") == "ops_probe_failed":
            diagnosis = "public_ops_probe_failed"
            action = (
                "Trader dashboard is public; OPS dashboard could not be fetched for feature checks. "
                "Check the public OPS route, Cloudflare tunnel latency, or use the local OPS dashboard."
            )
        elif deployment.get("reason") == "trader_probe_failed":
            diagnosis = "public_trader_probe_failed"
            action = (
                "Public domain responded to the health probe, but the trader HTML could not be fetched "
                "for feature checks. Check public route latency or restart the public gateway."
            )
        else:
            diagnosis = "public_deployment_stale"
            missing = ", ".join(deployment.get("missing_features", [])[:4]) or "feature fingerprint"
            action = f"Redeploy dashboard static files or restart the public gateway; public HTML is missing {missing}."
    return {
        "status": status,
        "checked_at": _utc_now(),
        "diagnosis": diagnosis,
        "operator_action": action,
        "public_url": public_url,
        "local_url": local_url,
        "local_gateway": local,
        "public_domain": public,
        "deployment_features": deployment,
        "cloudflared": tunnel,
        "cloudflared_log": log,
    }


def _deployment_feature_summary(public_url: str, timeout: float) -> dict:
    is_v4 = urlparse(public_url).path.endswith("/dashboard-v4.html")
    is_v5 = urlparse(public_url).path.endswith("/dashboard-v5.html")
    if is_v5:
        trader_checks = {
            "dashboard_v5_title": "Extended 网格交易机器人",
            "strategy_console_api": "/api/strategy-console/current",
            "market_bars_api": "/api/dualtrack/market/bars",
            "standard_kline": "StandardKline.StandardKlineChart",
            "strategy_shadows": "Strategy Shadows",
            "production_plan_traceability": "strategy_plan_version",
            "runtime_actual_state": "actual_state",
            "access_session_api": "/api/auth/session",
            "authenticated_control_gate": "ACCESS_CONTROLLED",
            "access_control_label": "已登录 · 可控制",
            "tokens_css": "assets/tokens.css",
        }
    elif is_v4:
        trader_checks = {
            "dashboard_v4_title": "GoldBot Trader Console V4",
            "replay_v4_route": 'new URL("dashboard-replay-v4.html", window.location.href)',
            "trade_record_cards": "Trade record cards",
            "signal_to_ticket_funnel": "信号漏斗 · 为什么不开仓",
            "tp_sl_protection": "TP/SL protection",
            "trade_replay_lightweight_charts": "TradingView Lightweight Charts",
            "nav_detail_preload": "navDetailPreloadIds",
            "display_strategy_comparison": "function strategyDisplayComparison",
            "gold_cadence_nav_sampling": "gold_ohlc_cadence_mtm_excess",
            "nav_chart_before_controls": 'data-priority="chart-before-controls"',
            "nav_edge_tape": 'id="navEdgeTape"',
            "nav_method_details": 'id="navMethodDetails"',
            "nav_series_click_replay": "strategy_series_to_gold_ohlc_replay",
            "replay_source_timeframe_scope": "Current replay scope keeps the source timeframe by default",
            "replay_display_scope_contract": "displayScope",
            "replay_marker_contract": 'markerSurface:"Gold OHLC"',
        }
    else:
        trader_checks = {
            "trade_replay_lightweight_charts": "TradingView Lightweight Charts",
            "localized_strategy_short_names": 'shortZh:"突破"',
            "daily_loop_exception_replay": "loopExceptionActions",
            "solid_gold_baseline": 'navGoldBaselineMode = "solid_series"',
            "today_traded_strategy_edge": "Default view prioritizes strategies that really traded today",
            "nav_detail_preload": "navDetailPreloadIds",
            "display_strategy_comparison": "function strategyDisplayComparison",
            "gold_cadence_nav_sampling": "gold_ohlc_cadence_mtm_excess",
            "nav_end_labels": "function navEndLabel",
            "chart_first_overview": 'data-layout="chart-first-overview"',
            "nav_chart_before_controls": 'data-priority="chart-before-controls"',
            "nav_edge_tape": 'id="navEdgeTape"',
            "nav_method_details": 'id="navMethodDetails"',
            "nav_series_click_replay": "strategy_series_to_gold_ohlc_replay",
            "trader_evidence_summary": "Evidence summary",
            "replay_source_timeframe_scope": "Current replay scope keeps the source timeframe by default",
            "replay_display_scope_contract": "displayScope",
            "replay_marker_contract": 'markerSurface:"Gold OHLC"',
            "promotion_dossier": "Promotion Dossier",
        }
    ops_checks = {
        "ops_command_copy": "opsCommandBlock",
        "ops_copy_buttons": "data-copy-command",
        "ops_incident_queue": "OPS Incident Queue",
        "ops_shift_brief": "ops-shift-brief",
        "ops_console_title": "GoldBot OPS Console",
        "ops_backend_signals": 'id="backend-signals"',
    }
    if is_v4 or is_v5:
        replay_checks = {
            "replay_v4_body": '<body class="replay-v4">',
            "replay_v4_backlink": 'new URL("dashboard-v4.html", window.location.href)',
            "replay_lightweight_charts": "lightweight-charts.standalone.production.js",
            "replay_api_client": "/api/replay",
            "replay_trade_record": "这笔交易 · 病历",
            "replay_lifecycle": "这笔的生命周期",
            "replay_gate_funnel": "信号 → 出票 · 五道关口",
        }
    else:
        replay_checks = {
            "replay_page_title": "GoldBot Replay",
            "replay_lightweight_charts": "lightweight-charts.standalone.production.js",
            "replay_api_client": "/api/replay",
            "replay_trader_focus": "Trader Focus",
        }
    trader = _probe_html_features(public_url, timeout, trader_checks)
    trader_vendor_filename = "packages/standard-kline/standard-kline.js" if is_v5 else "data/vendor/echarts.min.js"
    trader_vendor_name = "trader_vendor_standard_kline" if is_v5 else "trader_vendor_echarts"
    trader_vendor_url = _sibling_dashboard_url(public_url, trader_vendor_filename)
    if not is_v5:
        trader_vendor_url += "?v=20260627-gateway"
    trader_vendor = _probe_http(trader_vendor_url, timeout)
    replay_filename = "dashboard-replay-v4.html"
    replay_url = _sibling_dashboard_url(public_url, replay_filename)
    replay = _probe_html_features(replay_url, timeout, replay_checks)
    replay_vendor_url = _sibling_dashboard_url(public_url, "data/vendor/lightweight-charts.standalone.production.js")
    replay_vendor = _probe_http(replay_vendor_url, timeout)
    ops_url = _sibling_dashboard_url(public_url, "ops-dashboard.html")
    ops = _probe_html_features(ops_url, timeout, ops_checks)
    ops_protected = int(ops.get("status_code") or 0) in {401, 403}
    ops_report = dict(ops)
    if ops_protected:
        ops_report["protected"] = True
    checks = [
        *[
            {"surface": "trader", "name": item["name"], "ok": item["ok"]}
            for item in trader.get("checks", [])
        ],
        {"surface": "trader", "name": trader_vendor_name, "ok": _http_ok(trader_vendor)},
        *[
            {"surface": "replay", "name": item["name"], "ok": item["ok"]}
            for item in replay.get("checks", [])
        ],
        {"surface": "replay", "name": "replay_vendor_lightweight_charts", "ok": _http_ok(replay_vendor)},
        *[
            {"surface": "ops", "name": item["name"], "ok": item["ok"]}
            for item in ops.get("checks", [])
        ],
    ]
    if not trader.get("ok"):
        reason = "trader_probe_failed"
    elif not _http_ok(trader_vendor):
        reason = "trader_vendor_probe_failed"
    elif not replay.get("ok"):
        reason = "replay_probe_failed"
    elif not _http_ok(replay_vendor):
        reason = "replay_vendor_probe_failed"
    elif ops_protected:
        reason = "ops_access_forbidden"
    elif not ops.get("ok"):
        reason = "ops_probe_failed"
    else:
        reason = "ok"
    missing = [
        f"{item['surface']}:{item['name']}"
        for item in checks
        if (
            not item.get("ok")
            and not (reason == "trader_probe_failed" and item.get("surface") == "trader")
            and not (reason == "trader_vendor_probe_failed" and item.get("name") == trader_vendor_name)
            and not (reason == "replay_probe_failed" and item.get("surface") == "replay")
            and not (reason == "replay_vendor_probe_failed" and item.get("name") == "replay_vendor_lightweight_charts")
            and not (reason in {"ops_access_forbidden", "ops_probe_failed"} and item.get("surface") == "ops")
        )
    ]
    if reason == "ok" and missing:
        reason = "feature_fingerprint_missing"
    status = "ok" if reason == "ok" else "warn"
    return {
        "status": status,
        "reason": reason,
        "ops_access": "protected" if ops_protected else "public",
        "trader_url": public_url,
        "trader_vendor_url": trader_vendor_url,
        "replay_url": replay_url,
        "replay_vendor_url": replay_vendor_url,
        "ops_url": ops_url,
        "trader": trader,
        "trader_vendor": trader_vendor,
        "replay": replay,
        "replay_vendor": replay_vendor,
        "ops": ops_report,
        "checks": checks,
        "missing_features": missing,
    }


def _sibling_dashboard_url(url: str, filename: str) -> str:
    parsed = urlparse(url)
    base_path = parsed.path.rsplit("/", 1)[0] if "/" in parsed.path else ""
    path = f"{base_path}/{filename}" if base_path else f"/{filename}"
    return parsed._replace(path=path, query="", fragment="").geturl()


def _probe_html_features(url: str, timeout: float, features: dict[str, str]) -> dict:
    started = time.perf_counter()
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 GoldBotDashboardHealth/1.0", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed dashboard health URLs.
            body = response.read(1_500_000).decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 0) or 0)
            headers = response.headers
            checks = [{"name": name, "ok": token in body} for name, token in features.items()]
            return {
                "ok": 200 <= status_code < 400,
                "status_code": status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "cache_control": headers.get("Cache-Control", ""),
                "cf_cache_status": headers.get("cf-cache-status", ""),
                "content_length": len(body),
                "checks": checks,
            }
    except Exception as exc:  # noqa: BLE001 - deployment freshness should degrade gracefully.
        return {
            "ok": False,
            "status_code": int(getattr(exc, "code", 0) or 0) or None,
            "reason": _exception_reason(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "checks": [{"name": name, "ok": False} for name in features],
        }


def _probe_http(url: str, timeout: float) -> dict:
    started = time.perf_counter()
    request = Request(url, headers={"User-Agent": "GoldBotDashboardHealth/1.0", "Range": "bytes=0-256"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed dashboard health URLs.
            response.read(256)
            status_code = int(getattr(response, "status", 0) or 0)
            return {
                "ok": 200 <= status_code < 400,
                "status_code": status_code,
                "reason": getattr(response, "reason", ""),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
    except Exception as exc:  # noqa: BLE001 - expose concise health failure.
        status_code = int(getattr(exc, "code", 0) or 0)
        return {
            "ok": False,
            "status_code": status_code or None,
            "reason": _exception_reason(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }


def _http_ok(result: dict) -> bool:
    code = result.get("status_code")
    return bool(result.get("ok")) or (isinstance(code, int) and 200 <= code < 400)


def _exception_reason(exc: Exception) -> str:
    reason = getattr(exc, "reason", "") or str(exc) or exc.__class__.__name__
    return reason if isinstance(reason, str) else str(reason)


def _cloudflared_process_summary() -> dict:
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "cloudflared"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception as exc:  # noqa: BLE001 - health should not raise.
        return {"running": False, "error": str(exc), "processes": []}
    processes = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and "cloudflared" in line
    ]
    return {"running": bool(processes), "processes": processes[:4]}


def _cloudflared_log_summary(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "path": str(path), "latest_errors": []}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]
    except OSError as exc:
        return {"exists": True, "path": str(path), "error": str(exc), "latest_errors": []}
    interesting = [
        line
        for line in lines
        if any(token in line for token in (" ERR ", "precheck", "Registered tunnel connection", "CONNECTIVITY PRE-CHECKS"))
    ]
    return {
        "exists": True,
        "path": str(path),
        "latest_errors": interesting[-8:],
    }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_rows(path: Path) -> list[dict]:
    try:
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _paper_execution_fills(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("execution_origin") != "recovery_replay"]


def _age_seconds(ts: str, now) -> int:
    return max(0, int((now - parse_utc(ts)).total_seconds()))


def _runtime_check(name: str, ok: bool, ok_message: str, blocked_message: str, evidence: dict) -> dict:
    return {
        "name": name,
        "status": "ok" if ok else "blocked",
        "message": ok_message if ok else blocked_message,
        "evidence": evidence,
    }


def _runtime_quality_check(name: str, clean: bool, ok_message: str, warning_message: str, evidence: dict) -> dict:
    return {
        "name": name,
        "status": "ok" if clean else "warn",
        "message": ok_message if clean else warning_message,
        "evidence": evidence,
    }


def _machine_invalid_fill_count(layers: list[Any]) -> int:
    total = 0
    for layer in layers:
        text = str(layer or "")
        if not text.startswith("invalid_fills:"):
            continue
        try:
            count = int(text.rsplit(":", 1)[-1])
        except ValueError:
            continue
        total += max(0, count)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Trading OS dashboard and local JSON API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    boot = (
        CloudPaperServiceBootGate().verify("dashboard")
        if os.getenv("GRIDMIND_RUNTIME_MODE") == "cloud"
        else PaperServiceBootGate().verify("dashboard")
    )
    if not boot.get("ok"):
        print(
            f"[paper-boot] dashboard blocked: {boot.get('blocker') or 'unknown'}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard server: http://{args.host}:{args.port}/dashboard-v4.html")
    print(f"Dashboard API: http://{args.host}:{args.port}/api/dashboard?date={utc_run_date()}")
    _start_code_reload_watcher()
    server.serve_forever()


def _start_code_reload_watcher(interval_seconds: int = 30) -> None:
    """Exit when the on-disk Python changes so launchd (KeepAlive) respawns this
    daemon with fresh code — no more serving stale code after an edit."""
    guard = CodeReloadGuard([ROOT / "services", ROOT / "pipelines"])

    def _watch() -> None:
        while True:
            time.sleep(interval_seconds)
            if guard.changed():
                print("[code-reload] source changed on disk — exiting so launchd respawns with fresh code", flush=True)
                os._exit(0)

    threading.Thread(target=_watch, daemon=True, name="code-reload-watcher").start()


if __name__ == "__main__":
    main()
