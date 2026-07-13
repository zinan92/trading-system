from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from services.run_date import utc_run_date

from services.code_reload import CodeReloadGuard
from services.config_loader import ROOT, load_pipeline_config
from services.command_center import build_command_center_state
from services.connector_activation_plan import ConnectorActivationPlan
from services.connector_config_apply import ConnectorConfigApply
from services.connector_onboarding import ConnectorOnboardingDryRun
from services.dashboard_state import DashboardState
from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc, seconds_until_end
from services.dualtrack_config import dualtrack_config
from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_market_feed import DualTrackMarketFeed
from services.dualtrack_scoring import (
    DualTrackScorer,
    _trades_from_fills,
    apply_unrealized,
    filter_invalid_machine_fills,
)
from services.dualtrack_store import DualTrackPlanStore
from services.connector_catalog import ConnectorCatalog
from services.journal_store import load_json
from services.market_view_intake import MarketViewIntake
from services.replay_state import ReplayState
from services.system_state import build_system_state
from services.tiger_venue_status import TigerVenueStatus

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CYCLE_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")
_STRATEGY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9:_=-]+$")
_TIMEFRAME_PATTERN = re.compile(r"^\d+[mhdMHD]$")
_DASHBOARD_VIEWS = {"full", "trader", "ops"}
_MAX_OPEN_TRADES_PER_STRATEGY = 3
_PUBLIC_DASHBOARD_URL = "https://goldbot.park-ai-intel.com/dashboard-v4.html"
_LOCAL_GATEWAY_URL = "http://127.0.0.1:8766/dashboard-v4.html"
_CLOUDFLARED_LOG = Path("/Users/wendy/work/选题工作台/launchd-tunnel.log")
_DUALTRACK_POST_ENDPOINTS = {"/api/dualtrack/plan", "/api/dualtrack/orders", "/api/dualtrack/verdict"}
_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY = {
    "read_only": True,
    "generates_runbook": False,
    "opens_network_clients": False,
    "opens_quote_client": False,
    "opens_trade_client": False,
    "submits_orders": False,
    "writes_runtime_config": False,
    "credential_values_exposed": False,
    "raw_command_text_exposed": False,
}
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
        self._write_error(404, "not_found", "unknown POST endpoint")

    def _handle_dualtrack_current(self, query: str) -> None:
        try:
            self._write_json(200, build_dualtrack_cycle_current_response())
        except ValueError as exc:
            self._write_error(400, "invalid_dualtrack_cycle", str(exc))

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
        self._write_json(
            200,
            build_dualtrack_market_bars_response(
                symbol=symbol or None,
                timeframe=timeframe or None,
                limit=limit,
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
                result = build_dualtrack_order_post_response(payload)
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
        except BrokenPipeError:
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


def dashboard_output_root() -> Path:
    cfg = load_pipeline_config()
    return ROOT / str(cfg.get("output_root", "outputs"))


def build_system_state_response(output_root: Path | None = None, *, as_of: str | datetime | None = None) -> dict:
    return build_system_state(output_root=output_root, as_of=as_of)


def build_market_view_intake_response(payload: dict, *, output_root: Path | None = None) -> dict:
    run_date = str(payload.get("date") or payload.get("run_date") or utc_run_date())
    if not _DATE_PATTERN.match(run_date):
        raise ValueError("expected date as YYYY-MM-DD")
    raw_text = str(payload.get("raw_text") or payload.get("text") or "").strip()
    if not raw_text:
        raise ValueError("raw_text is required")
    if len(raw_text) > 20_000:
        raise ValueError("raw_text exceeds 20000 characters")
    output = Path(output_root) if output_root else dashboard_output_root()
    draft_only = _truthy(payload.get("draft_only", False))
    write_obsidian = _truthy(payload.get("write_obsidian", False))
    obsidian_env = os.environ.get("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", "").strip()
    obsidian_root = Path(obsidian_env) if obsidian_env else None
    if write_obsidian and obsidian_root is None:
        raise ValueError("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT is required when write_obsidian=true")
    intake = MarketViewIntake(output, obsidian_root=obsidian_root)
    if draft_only:
        draft = intake.draft(run_date, raw_text)
        return {
            "status": "draft",
            "draft_only": True,
            "run_date": draft.run_date,
            "draft": {
                "score": draft.score,
                "summary": draft.summary,
                "key_levels": draft.key_levels,
                "timeframes": draft.timeframes,
                "trade_plan": draft.trade_plan,
                "valid_for_hours": draft.valid_for_hours,
                "expires_at": draft.expires_at,
                "expires_if_price_moves_pct": draft.expires_if_price_moves_pct,
                "expiry_target_price": draft.expiry_target_price,
                "expire_above": draft.expire_above,
                "expire_below": draft.expire_below,
            },
        }
    market_view = intake.record(run_date, raw_text, write_obsidian=write_obsidian)
    return {
        "status": "recorded",
        "draft_only": False,
        "run_date": run_date,
        "market_view": market_view,
        "source_artifacts": {
            "json": str(output / "market_views" / f"{run_date}.json"),
            "current": str(output / "market_views" / "current.json"),
            "markdown": str(output / "market_views" / f"{run_date}.md"),
        },
    }


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


def build_dualtrack_plan_response(cycle_id: str, *, output_root: Path | None = None, as_of: str | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackPlanStore(output_root).plan_response(cycle_id, as_of=as_of)


def build_dualtrack_plan_post_response(payload: dict, *, output_root: Path | None = None) -> dict:
    lock = _truthy(payload.get("lock", True))
    now = payload.get("as_of") or payload.get("now")
    plan = DualTrackPlanStore(output_root).save_human_plan(payload, now=now, lock=lock)
    return {"status": "locked" if lock else "draft", "plan": plan}


def build_dualtrack_config_response() -> dict:
    cfg = dualtrack_config()
    return {
        "schema_version": "dualtrack-config-v1",
        "max_leverage": cfg.get("max_leverage"),
        "capital_per_track_usd": cfg.get("capital_per_track_usd"),
        "safety": {
            "read_only": True,
            "credentials_exposed": False,
        },
    }


def build_dualtrack_machine_response(cycle_id: str, *, output_root: Path | None = None, as_of: str | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackMachineRunner(output_root).machine_payload(cycle_id, as_of=as_of)


def build_dualtrack_order_post_response(payload: dict, *, output_root: Path | None = None) -> dict:
    receipt = build_execution_engine_adapter(_dualtrack_output_root(output_root)).submit_order(payload)
    if receipt.get("state") == "accepted" or receipt.get("status") == "accepted":
        return {"status": "accepted", "order": receipt}
    return {"status": "filled", "fill": receipt}


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
    if bool(market.get("is_synthetic")):
        raise ValueError("synthetic market data is forbidden")
    if market.get("status") != "ready" or market.get("fresh") is not True:
        raise ValueError("server market data is stale")
    if market.get("source_mode") != "requested_symbol":
        raise ValueError("server market source is not canonical")
    provider = str(market.get("provider") or "")
    if expected_provider and provider != expected_provider:
        raise ValueError("server market provider is not canonical")
    mark = _finite_float(market.get("latest_close"))
    if mark is None:
        raise ValueError("server market price is missing")
    now = parse_utc(received_at)
    try:
        market_ts = parse_utc(market.get("latest_timestamp"))
    except (TypeError, ValueError) as exc:
        raise ValueError("server market timestamp is invalid") from exc
    if market_ts > now + timedelta(seconds=60):
        raise ValueError("server market timestamp is in the future")
    cycle_id = str(payload.get("cycle_id") or "")
    current_window = cycle_window(now)
    if current_window.cycle_id != cycle_id:
        raise ValueError("order cycle is not current")
    if market_ts < current_window.start:
        raise ValueError("server market timestamp is outside the current cycle")
    prepared = {**payload, "ts": now.isoformat()}
    prepared["market_price"] = mark
    prepared["market_timestamp"] = market_ts.isoformat()
    prepared["market_source"] = provider
    if str(payload.get("order_type") or "market").lower() == "market":
        prepared["price"] = mark
    return prepared


def build_dualtrack_human_response(
    cycle_id: str,
    *,
    output_root: Path | None = None,
    mark_price: float | str | None = None,
    mark_source: str | None = None,
) -> dict:
    del mark_price, mark_source
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    output = _dualtrack_output_root(output_root)
    return DualTrackHumanEngine(output).human_payload(cycle_id)


def build_dualtrack_trades_response(
    cycle_id: str,
    *,
    track: str = "human",
    output_root: Path | None = None,
    as_of: str | None = None,
    mark_price: float | str | None = None,
    mark_source: str | None = None,
) -> dict:
    del mark_price, mark_source
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    normalized_track = str(track or "human").lower()
    if normalized_track not in {"human", "machine"}:
        raise ValueError("track must be human or machine")
    closed = _dualtrack_cycle_closed(cycle_id, as_of=as_of)
    output = _dualtrack_output_root(output_root)
    mark = _dualtrack_mark_price(output, cycle_id, closed=closed, as_of=as_of)
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
    mark = _dualtrack_mark_price(output, cycle_id, closed=closed, as_of=as_of)
    snapshot = build_execution_engine_adapter(output).snapshot(
        cycle_id,
        mark_price=mark["price"],
        mark_fresh=mark["fresh"],
        mark_source=mark["source"],
    )
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
        "reconciliation": latest_reconciliation,
        "shadow_cutover": latest_cutover,
        "safety": {
            "read_only": True,
            "execution_control": False,
            "machine_track_disclosed": False,
        },
    }


def build_dualtrack_attribution_response(cycle_id: str, *, output_root: Path | None = None) -> dict:
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    return DualTrackScorer(output_root).attribution_payload(cycle_id)


def build_dualtrack_ledger_response(*, output_root: Path | None = None, week: str | None = None) -> dict:
    return DualTrackScorer(output_root).ledger_payload(week=week)


def build_dualtrack_market_bars_response(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 96,
    market_db: Path | None = None,
    config: dict | None = None,
    as_of: str | None = None,
) -> dict:
    return DualTrackMarketFeed(market_db=market_db, config=config).snapshot(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        as_of=as_of,
    )


def _dualtrack_output_root(output_root: Path | None = None) -> Path:
    return Path(output_root) if output_root else ROOT / load_pipeline_config().get("output_root", "outputs")


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
) -> dict:
    if closed:
        cycle_rows = load_json(output_root / "dualtrack" / "cycles" / f"{cycle_id}.json")
        cycle = cycle_rows[-1] if cycle_rows else {}
        close_price = _finite_float(cycle.get("close_price"))
        if close_price is not None:
            return {"price": close_price, "fresh": True, "source": "cycle_close"}
    market = DualTrackMarketFeed().snapshot(symbol="GOLD", timeframe="1m", limit=1, as_of=as_of)
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
    if track == "machine":
        fills, safety = filter_invalid_machine_fills(fills)
        replay_fills = [fill for fill in fills if fill.get("execution_origin") == "recovery_replay"]
        fills = [fill for fill in fills if fill.get("execution_origin") != "recovery_replay"]
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
    enriched = apply_unrealized(trades, mark["price"], mark_fresh=mark["fresh"])
    return {"cycle_id": cycle_id, "trades": enriched, "safety": safety}


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
    if current_trades:
        return {
            "cycle_id": cycle_id,
            "reason": "current_cycle",
            "trades": current_trades,
            "summary": current_summary,
            "safety": current_safety,
        }
    for previous_cycle_id in _same_day_previous_cycles(cycle_id):
        rows = _dualtrack_trade_rows_for_cycle(output_root, previous_cycle_id, track, mark)
        if rows["trades"]:
            return {
                "cycle_id": previous_cycle_id,
                "reason": "latest_same_day",
                "trades": rows["trades"],
                "summary": _trade_summary(rows["trades"]),
                "safety": rows["safety"],
            }
    return {
        "cycle_id": cycle_id,
        "reason": "current_cycle_empty",
        "trades": [],
        "summary": current_summary,
        "safety": current_safety,
    }


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
    market_ok = (
        market.get("status") == "ready"
        and market.get("fresh") is True
        and market.get("source_mode") == "requested_symbol"
        and market.get("is_synthetic") is not True
        and (not market_config.get("provider") or market.get("provider") == market_config.get("provider"))
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


def build_connector_config_status_response(*, output_root: Path | None = None) -> dict:
    return ConnectorConfigApply(output_root=output_root).status()


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


def build_connector_price_feed_refresh_runbook_response(*, output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))
    path = root / "connector_config_apply" / "price_feed_refresh_runbook_current.json"
    rows = load_json(path)
    if rows and isinstance(rows[-1], dict):
        receipt = dict(rows[-1])
        receipt.setdefault("served_from", str(path))
        receipt["command_sequence"] = _redacted_runbook_steps(receipt.get("command_sequence"))
        receipt["status_receipt"] = _redacted_connector_status_snapshot(receipt.get("status_receipt"))
        receipt["redaction"] = {
            "raw_command_text_exposed": False,
            "credential_values_exposed": False,
        }
        receipt["endpoint_safety"] = dict(_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY)
        return receipt
    return {
        "schema_version": "connector-price-feed-refresh-runbook-v1",
        "status": "missing",
        "served_from": str(path),
        "command_sequence": [],
        "safety": {
            "read_only": True,
            "opens_network_clients": False,
            "opens_quote_client": False,
            "opens_trade_client": False,
            "submits_orders": False,
            "writes_runtime_config": False,
            "credential_values_exposed": False,
        },
        "endpoint_safety": dict(_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY),
    }


def _redacted_runbook_steps(rows: object) -> list[dict]:
    steps = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        steps.append(
            {
                "name": str(row.get("name") or ""),
                "purpose": str(row.get("purpose") or ""),
                "opens_quote_client": row.get("opens_quote_client") is True,
                "opens_trade_client": row.get("opens_trade_client") is True,
                "submits_orders": row.get("submits_orders") is True,
                "writes_runtime_config": row.get("writes_runtime_config") is True,
                "writes_market_db": row.get("writes_market_db") is True,
                "writes_plan_artifact": row.get("writes_plan_artifact") is True,
            }
        )
    return steps


def _redacted_connector_status_snapshot(status: object) -> dict:
    if not isinstance(status, dict):
        return {}
    operator_stage = status.get("operator_stage", {}) if isinstance(status.get("operator_stage"), dict) else {}
    return {
        "schema_version": str(status.get("schema_version") or ""),
        "checked_at": str(status.get("checked_at") or ""),
        "status": str(status.get("status") or ""),
        "operator_stage": {
            "stage": str(operator_stage.get("stage") or ""),
            "next_action": str(operator_stage.get("next_action") or ""),
            "runtime_switched_to_tiger_mgc": operator_stage.get("runtime_switched_to_tiger_mgc") is True,
            "price_feed_ready": operator_stage.get("price_feed_ready") is True,
            "can_switch_config_with_operator_authorization": operator_stage.get("can_switch_config_with_operator_authorization") is True,
            "can_trade_machine_track": operator_stage.get("can_trade_machine_track") is True,
            "can_submit_tiger_orders": operator_stage.get("can_submit_tiger_orders") is True,
        },
    }


def build_connector_config_apply_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    pipeline_config_path: Path | None = None,
    dualtrack_config_path: Path | None = None,
) -> dict:
    return ConnectorConfigApply(
        output_root=output_root,
        pipeline_config_path=pipeline_config_path,
        dualtrack_config_path=dualtrack_config_path,
    ).apply(payload)


def build_connector_config_rollback_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    pipeline_config_path: Path | None = None,
    dualtrack_config_path: Path | None = None,
) -> dict:
    return ConnectorConfigApply(
        output_root=output_root,
        pipeline_config_path=pipeline_config_path,
        dualtrack_config_path=dualtrack_config_path,
    ).rollback(payload)


def build_dualtrack_verdict_post_response(payload: dict, *, output_root: Path | None = None) -> dict:
    cycle_id = str(payload.get("cycle_id") or "")
    if not _CYCLE_ID_PATTERN.match(cycle_id):
        raise ValueError("expected YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    verdict = DualTrackScorer(output_root).record_verdict(cycle_id, str(payload.get("note") or ""))
    return {"status": "recorded", "verdict": verdict}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def compact_trader_payload(payload: dict) -> dict:
    """Return only the reader-facing data needed for dashboard-v3 first paint.

    The full `/api/dashboard` response remains unchanged for OPS and debugging.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "data_provenance",
        "market_view_status",
        "ohlc_quality",
        "market_data_gate",
        "performance_board",
        "review_loop",
        "dashboard_health",
        "system_vitals",
        "backend_maturity",
        "strategy_frequency",
        "strategy_daily_reviews",
        "daily_trade_samples",
        "trade_reviews",
        "strategy_promotion_gate",
        "live_reconciliation",
        "legacy_live_reconciliation",
        "live_submission_safety",
        "alerts",
        "operation_runbook",
        "source_contracts",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    if isinstance(payload.get("backend_maturity"), dict):
        compact["backend_maturity"] = _compact_backend_maturity(payload["backend_maturity"])
    if isinstance(payload.get("strategy_frequency"), dict):
        compact["strategy_frequency"] = _compact_strategy_frequency(payload["strategy_frequency"])
    if isinstance(payload.get("strategy_daily_reviews"), dict):
        compact["strategy_daily_reviews"] = _compact_strategy_daily_reviews(payload["strategy_daily_reviews"])
    if isinstance(payload.get("daily_trade_samples"), dict):
        compact["daily_trade_samples"] = _compact_daily_trade_samples(payload["daily_trade_samples"])
    if isinstance(payload.get("trade_reviews"), dict):
        compact["trade_reviews"] = _compact_trade_reviews(payload["trade_reviews"])
    if isinstance(payload.get("strategy_promotion_gate"), dict):
        compact["strategy_promotion_gate"] = _compact_strategy_promotion_gate(payload["strategy_promotion_gate"])
    return compact


def _compact_backend_maturity(maturity: dict) -> dict:
    checks = []
    for check in maturity.get("checks", []) or []:
        if not isinstance(check, dict):
            continue
        checks.append(
            {
                "name": check.get("name", ""),
                "status": check.get("status", ""),
                "summary": check.get("summary", ""),
                "evidence": _trim_maturity_evidence(check.get("evidence", {})),
            }
        )
    return {
        "schema_version": maturity.get("schema_version", ""),
        "run_date": maturity.get("run_date", ""),
        "generated_at": maturity.get("generated_at", ""),
        "status": maturity.get("status", ""),
        "summary": maturity.get("summary", {}),
        "checks": checks,
    }


def _trim_maturity_evidence(evidence: object) -> object:
    if not isinstance(evidence, dict):
        return evidence
    trimmed = dict(evidence)
    if isinstance(trimmed.get("board"), dict):
        board = trimmed["board"]
        trimmed["board"] = {
            "stage_counts": board.get("stage_counts", {}),
            "effective_strategy_count": board.get("effective_strategy_count"),
            "needs_attention_count": board.get("needs_attention_count"),
            "bottlenecks": list(board.get("bottlenecks", []) or [])[:8],
        }
    if isinstance(trimmed.get("labels"), dict):
        labels = trimmed["labels"]
        trimmed["labels"] = dict(list(labels.items())[:16])
    return trimmed


def _compact_strategy_frequency(frequency: dict) -> dict:
    strategies = []
    for row in frequency.get("strategies", []) or []:
        if not isinstance(row, dict):
            continue
        classification = row.get("classification", {}) if isinstance(row.get("classification"), dict) else {}
        attribution = row.get("attribution", {}) if isinstance(row.get("attribution"), dict) else {}
        strategies.append(
            {
                "strategy_id": row.get("strategy_id", ""),
                "timeframe": row.get("timeframe", ""),
                "stage": row.get("stage", ""),
                "stage_label": row.get("stage_label", ""),
                "reason": row.get("reason", ""),
                "primary_reason": row.get("primary_reason", ""),
                "limiting_reason": row.get("limiting_reason", ""),
                "executed_trade_count": row.get("executed_trade_count", 0),
                "min_daily_executed_trades": row.get("min_daily_executed_trades"),
                "signal_count": row.get("signal_count", 0),
                "candidate_count": row.get("candidate_count", 0),
                "ticket_count": row.get("ticket_count", 0),
                "sample_statuses": list(row.get("sample_statuses", []) or [])[:20],
                "recommendation": row.get("recommendation", {}) if isinstance(row.get("recommendation"), dict) else {},
                "classification": {
                    "family": classification.get("family", ""),
                    "family_label": classification.get("family_label", ""),
                    "style": classification.get("style", ""),
                    "style_label": classification.get("style_label", ""),
                    "expected_trades_per_day_min": classification.get("expected_trades_per_day_min"),
                    "expected_trades_per_day_max": classification.get("expected_trades_per_day_max"),
                    "role": classification.get("role", ""),
                },
                "attribution": {
                    "primary_reason": attribution.get("primary_reason", ""),
                    "limiting_reason": attribution.get("limiting_reason", ""),
                    "limiting_reason_label": attribution.get("limiting_reason_label", ""),
                    "reason_counts": attribution.get("reason_counts", {}),
                    "evidence": _compact_frequency_evidence(attribution.get("evidence", [])),
                },
            }
        )
    return {
        "schema_version": frequency.get("schema_version", ""),
        "run_date": frequency.get("run_date", ""),
        "generated_at": frequency.get("generated_at", ""),
        "status": frequency.get("status", ""),
        "summary": frequency.get("summary", {}),
        "strategies": strategies,
    }


def _compact_frequency_evidence(evidence: object) -> list[dict]:
    if not isinstance(evidence, list):
        return []
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "reason": item.get("reason", ""),
                "reason_label": item.get("reason_label", ""),
                "detail": item.get("detail", ""),
                "sample_id": item.get("sample_id", ""),
                "signal_id": item.get("signal_id", ""),
                "signal_generated_at": item.get("signal_generated_at", ""),
                "decision_cursor": item.get("decision_cursor", ""),
                "ticket_id": item.get("ticket_id", ""),
                "execution_status": item.get("execution_status", ""),
            }
        )
        if len(rows) >= 5:
            break
    return rows


def _compact_daily_trade_samples(samples: dict) -> dict:
    summary = samples.get("summary", {}) if isinstance(samples.get("summary"), dict) else {}
    requirements = samples.get("sample_requirements", {}) if isinstance(samples.get("sample_requirements"), dict) else {}
    keep_summary = [
        "observation_count",
        "candidate_count",
        "ticket_count",
        "quality_pass_count",
        "executed_count",
        "executed_trade_sample_count",
        "no_signal_count",
        "blocked_count",
        "candidate_without_ticket_count",
        "pending_review_count",
        "paper_order_count",
        "demo_order_count",
        "live_order_count",
        "below_minimum",
        "target_range_met",
        "target_range",
        "minimum_executed_trades",
        "funnel",
    ]
    keep_requirements = [
        "effective_leverage",
        "min_target_equity_return_pct",
        "min_target_price_move_pct",
        "min_reward_to_risk",
        "daily_min_trade_samples",
        "daily_target_trade_samples_low",
        "daily_target_trade_samples_high",
    ]
    return {
        "run_date": samples.get("run_date", ""),
        "generated_at": samples.get("generated_at", ""),
        "status": samples.get("status", ""),
        "active_strategy_id": samples.get("active_strategy_id", ""),
        "summary": {key: summary.get(key) for key in keep_summary if key in summary},
        "sample_requirements": {key: requirements.get(key) for key in keep_requirements if key in requirements},
    }


def _compact_strategy_daily_reviews(reviews: dict) -> dict:
    rows = []
    for row in reviews.get("strategies", []) or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "strategy_id": row.get("strategy_id", ""),
                "family": row.get("family", ""),
                "style": row.get("style", ""),
                "timeframe": row.get("timeframe", ""),
                "pm_verdict": row.get("pm_verdict", ""),
                "frequency": row.get("frequency", {}),
                "pnl": row.get("pnl", {}),
                "attribution": row.get("attribution", {}),
                "tp_sl": row.get("tp_sl", {}),
                "pm_action": row.get("pm_action", ""),
                "pm_summary": row.get("pm_summary", ""),
                "review_priority": row.get("review_priority", 0),
                "review_priority_label": row.get("review_priority_label", ""),
                "review_question": row.get("review_question", ""),
                "next_review_cursor": row.get("next_review_cursor", ""),
                "replay_context": row.get("replay_context", {}),
                "sample_quality": row.get("sample_quality", {}),
            }
        )
    return {
        "schema_version": reviews.get("schema_version", ""),
        "run_date": reviews.get("run_date", ""),
        "generated_at": reviews.get("generated_at", ""),
        "strategy_count": reviews.get("strategy_count", len(rows)),
        "status_counts": reviews.get("status_counts", {}),
        "strategies": rows,
    }


def _compact_trade_reviews(reviews: dict) -> dict:
    return {
        "run_date": reviews.get("run_date", ""),
        "generated_at": reviews.get("generated_at", ""),
        "active_strategy_id": reviews.get("active_strategy_id", ""),
        "status": reviews.get("status", ""),
        "summary": reviews.get("summary", {}),
    }


def _compact_strategy_promotion_gate(gate: dict) -> dict:
    return {
        "run_date": gate.get("run_date", ""),
        "generated_at": gate.get("generated_at", ""),
        "status": gate.get("status", ""),
        "strategy_id": gate.get("strategy_id", ""),
        "promotion_allowed": gate.get("promotion_allowed", False),
        "auto_apply": gate.get("auto_apply", False),
        "paper_only": gate.get("paper_only", True),
        "live_config_change_allowed": gate.get("live_config_change_allowed", False),
        "closed_trade_count": gate.get("closed_trade_count"),
        "official_rows": gate.get("official_rows"),
        "data_truth_level": gate.get("data_truth_level", ""),
        "candidate": gate.get("candidate", {}),
        "blockers": list(gate.get("blockers", []) or [])[:8],
    }


def compact_ops_payload(payload: dict) -> dict:
    """Return an OPS first-paint payload.

    The full snapshot is intentionally still available as `view=full`. The OPS
    console should not wait on multi-MB review/detail artifacts before it can
    answer whether local services, data gates, schedules, and public access are
    alive.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "manifest",
        "signals",
        "backtests",
        "tickets",
        "orders",
        "positions",
        "decisions",
        "pending",
        "review",
        "journal",
        "risk",
        "risk_blocks",
        "open_trades",
        "closed_trades",
        "paper_execution_blocks",
        "performance",
        "paper_performance",
        "paper_exit_monitor",
        "paper_exit_decisions",
        "paper_risk_action_plan",
        "equity_curve",
        "paper_reconciliation",
        "paper_trade_attribution",
        "daily_review",
        "operation_runbook",
        "schedule",
        "schedule_status",
        "schedule_install_plan",
        "schedule_install",
        "schedule_rollback_plan",
        "schedule_rollback",
        "schedule_post_install_verify",
        "schedule_takeover_package",
        "schedule_takeover_package_check",
        "runner",
        "system_vitals",
        "bot_supervisor",
        "bot_checkpoint",
        "market_db",
        "data_provenance",
        "ohlc_quality",
        "market_data_gate",
        "strategy_config",
        "risk_rules",
        "broker_preflight",
        "data_source_preflight",
        "data_source_lineage",
        "data_trust",
        "official_feed_receipt",
        "official_feed_onboarding",
        "broker_feed_doctor",
        "broker_feed",
        "broker_feed_smoke",
        "oanda_feed",
        "oanda_account",
        "binance_usdm_feed",
        "broker_receipts",
        "broker_receipt_summary",
        "mt5_bridge_smoke",
        "live_order_requests",
        "mock_runtime",
        "mock_uat",
        "live_readiness",
        "live_env",
        "live_activation",
        "live_approval",
        "live_submission_safety",
        "live_broker_preflight",
        "live_dry_run_drill",
        "live_switch_plan",
        "live_cutover",
        "strategy_review",
        "strategy_snapshot",
        "learning_ledger",
        "strategy_change_proposal",
        "strategy_learning_actions",
        "strategy_experiments",
        "strategy_improvement_plan",
        "strategy_promotion_gate",
        "strategy_guardrails",
        "risk_monitor",
        "paper_auto_approval_gate",
        "data_quality",
        "data_gaps",
        "data_gap_repair",
        "data_archive",
        "data_integrity",
        "health",
        "audit",
        "doctor",
        "dashboard_health",
        "alerts",
        "live_reconciliation",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    if isinstance(payload.get("data_health"), dict):
        compact["data_health"] = _compact_data_health(payload["data_health"])
    compact["collector_runs"] = _compact_collector_runs(payload.get("collector_runs", []))
    bars = [bar for bar in payload.get("bars", []) if isinstance(bar, dict)]
    compact["bars"] = [_compact_bar(bar) for bar in bars[-500:]]
    compact["ops_payload"] = {
        "mode": "compact",
        "bars_returned": len(compact["bars"]),
        "bars_total": len(bars),
        "truncated_sections": [
            "bars",
            "evening_review",
            "trading_plan",
            "strategy_detail",
            "nav_curve_intraday",
            "performance_board",
            "collector_runs",
            "data_health",
        ],
        "full_diagnostics_query": "/api/dashboard?view=full",
    }
    if isinstance(payload.get("evening_review"), dict):
        compact["evening_review"] = _compact_evening_review(payload["evening_review"])
    if isinstance(payload.get("trading_plan"), dict):
        compact["trading_plan"] = _compact_trading_plan(payload["trading_plan"])
    return compact


def _compact_data_health(data_health: dict) -> dict:
    keep = (
        "run_date",
        "checked_at",
        "symbol",
        "timeframe",
        "status",
        "summary",
        "providers",
        "degenerate",
        "misaligned",
        "provider_conflicts",
        "recommendations",
    )
    compact = {key: data_health.get(key) for key in keep if key in data_health}
    compact["gaps"] = list(data_health.get("gaps", []) or [])[:6]
    compact["issues"] = list(data_health.get("issues", []) or [])[:8]
    compact["suspicious_price_jumps"] = list(data_health.get("suspicious_price_jumps", []) or [])[:8]
    return compact


def _compact_collector_runs(runs: list) -> dict:
    rows = [row for row in (runs or []) if isinstance(row, dict)]
    latest_by_key: dict[tuple[str, str], dict] = {}
    status_counts: dict[str, int] = {}
    stale_count = 0
    for row in rows:
        status = str(row.get("fetch_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "stale":
            stale_count += 1
        key = (str(row.get("symbol") or ""), str(row.get("timeframe") or ""))
        current = latest_by_key.get(key)
        if not current or str(row.get("collected_at") or "") > str(current.get("collected_at") or ""):
            latest_by_key[key] = row
    latest = sorted(
        latest_by_key.values(),
        key=lambda row: str(row.get("collected_at") or ""),
        reverse=True,
    )
    return {
        "total_runs": len(rows),
        "latest_count": len(latest),
        "stale_count": stale_count,
        "status_counts": status_counts,
        "latest": [_compact_collector_run(row) for row in latest[:12]],
    }


def _compact_collector_run(row: dict) -> dict:
    keep = (
        "collected_at",
        "symbol",
        "timeframe",
        "timestamp",
        "close",
        "provider",
        "quality_flags",
        "record_type",
        "fetch_status",
        "bar_age_seconds",
        "stored_rows_seen",
    )
    return {key: row.get(key) for key in keep if key in row}


def _compact_evening_review(review: dict) -> dict:
    keep = (
        "run_date",
        "generated_at",
        "status",
        "active_strategy_id",
        "adherence",
        "attribution",
        "performance",
        "notes",
    )
    compact = {key: review.get(key) for key in keep if key in review}
    compact["improvement_queue"] = list(review.get("improvement_queue", []) or [])[:6]
    compact["trade_reviews"] = list(review.get("trade_reviews", []) or [])[:6]
    compact["hypotheses"] = list(review.get("hypotheses", []) or [])[:6]
    return compact


def _compact_trading_plan(plan: dict) -> dict:
    keep = (
        "run_date",
        "generated_at",
        "plan_type",
        "active_strategy_id",
        "timeframe",
        "status",
        "mode",
        "decision",
        "allowed_to_trade",
        "blockers",
        "strategy_profile",
        "broker_plan",
        "data_plan",
        "risk_envelope",
        "trade_plan",
        "today_focus",
    )
    compact = {key: plan.get(key) for key in keep if key in plan}
    compact["signals"] = list(plan.get("signals", []) or [])[:8]
    compact["tickets"] = list(plan.get("tickets", []) or [])[:8]
    return compact


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
    if is_v4:
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
    if is_v4:
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
    trader_vendor_url = _sibling_dashboard_url(public_url, "data/vendor/echarts.min.js") + "?v=20260627-gateway"
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
        {"surface": "trader", "name": "trader_vendor_echarts", "ok": _http_ok(trader_vendor)},
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
            and not (reason == "trader_vendor_probe_failed" and item.get("name") == "trader_vendor_echarts")
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


def compact_strategy_payload(payload: dict) -> dict:
    """Return the trader-facing single-strategy replay payload.

    Full strategy snapshots carry OPS/debug artifacts that can push public
    responses past several MB. The replay view needs Gold OHLC, trades, orders,
    and concise evidence only; `/api/dashboard?strategy=<id>` remains full.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "data_provenance",
        "ohlc_quality",
        "market_data_gate",
        "dashboard_health",
        "system_vitals",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    detail = payload.get("strategy_detail")
    if isinstance(detail, dict):
        compact["strategy_detail"] = _compact_strategy_detail(detail)
    return compact


def _compact_strategy_detail(detail: dict) -> dict:
    return {
        "strategy_id": detail.get("strategy_id", ""),
        "timeframe": detail.get("timeframe", ""),
        "classification": detail.get("classification", {}),
        "summary": detail.get("summary", {}),
        "bars": [_compact_bar(bar) for bar in detail.get("bars", []) if isinstance(bar, dict)],
        "replay_ohlc": detail.get("replay_ohlc", {}),
        "ohlc_quality": detail.get("ohlc_quality", {}),
        "nav_points": detail.get("nav_points", []),
        "nav_quality": detail.get("nav_quality", {}),
        "nav_curve_intraday": _compact_nav_curve(detail.get("nav_curve_intraday", {})),
        "orders": [_compact_order(order) for order in detail.get("orders", []) if isinstance(order, dict)],
        "open_orders": [_compact_order(order) for order in detail.get("open_orders", []) if isinstance(order, dict)],
        "open_trades": [_compact_trade(trade) for trade in detail.get("open_trades", []) if isinstance(trade, dict)],
        "closed_trades": [_compact_trade(trade) for trade in detail.get("closed_trades", []) if isinstance(trade, dict)],
        "trades": [_compact_trade(trade) for trade in detail.get("trades", []) if isinstance(trade, dict)],
        "trade_record_cards": [
            _compact_trade_record_card(card)
            for card in detail.get("trade_record_cards", [])
            if isinstance(card, dict)
        ],
        "trade_record_audit": detail.get("trade_record_audit", {}),
        "strategy_book": detail.get("strategy_book", {}),
        "edge_judgment": detail.get("edge_judgment", {}),
        "explainability_status": detail.get("explainability_status", ""),
        "explainability_summary": _compact_explainability_summary(detail),
        "performance_confidence": detail.get("performance_confidence", {}),
        "unrealized_pnl": detail.get("unrealized_pnl", 0),
        "entry_reason": detail.get("entry_reason", ""),
        "exit_reason": detail.get("exit_reason", ""),
        "strategy_signal": _compact_signal(detail.get("strategy_signal", {})),
        "latest_decision_snapshot": _compact_decision_snapshot(detail.get("latest_decision_snapshot", {})),
        "latest_go_decision_snapshot": _compact_decision_snapshot(detail.get("latest_go_decision_snapshot", {})),
        "decision_snapshot_summary": detail.get("decision_snapshot_summary", {}),
        "risk_block": _compact_risk_block(detail.get("risk_block", {})),
    }


def _compact_decision_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    plan = snapshot.get("execution_plan") if isinstance(snapshot.get("execution_plan"), dict) else {}
    signal = snapshot.get("signal") if isinstance(snapshot.get("signal"), dict) else {}
    return {
        "strategy_id": snapshot.get("strategy_id", ""),
        "bar_timestamp": snapshot.get("bar_timestamp", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "final_decision": snapshot.get("final_decision", ""),
        "signal": {
            "direction": signal.get("direction", ""),
            "confidence": signal.get("confidence", ""),
            "strength": signal.get("strength", ""),
            "regime": signal.get("regime", ""),
        },
        "execution_plan": {
            "ticket_id": plan.get("ticket_id", ""),
            "entry_zone": plan.get("entry_zone", ""),
            "take_profit": plan.get("take_profit"),
            "stop_loss": plan.get("stop_loss"),
            "target_equity_return_pct": plan.get("target_equity_return_pct"),
        },
        "no_go_reason": snapshot.get("no_go_reason", ""),
    }


def _compact_explainability_summary(detail: dict) -> dict:
    groups = detail.get("explainability_root_cause_groups") or detail.get("explainability_gap_groups") or []
    compact_groups = []
    total = 0
    warn_total = 0
    info_total = 0
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        count = int(group.get("count") or 0)
        warn_count = int(group.get("warn_count") or (count if group.get("severity") == "warn" else 0) or 0)
        info_count = int(group.get("info_count") or max(0, count - warn_count) or 0)
        total += count
        warn_total += warn_count
        info_total += info_count
        compact_groups.append({
            "root_cause": group.get("root_cause") or group.get("key") or "",
            "label": group.get("label") or group.get("key") or "",
            "severity": group.get("severity") or ("warn" if warn_count else "info"),
            "count": count,
            "warn_count": warn_count,
            "info_count": info_count,
            "next_action": group.get("next_action") or "",
            "gap_types": list(group.get("gap_types", []) or [])[:4],
            "sample_trade_ids": list(group.get("sample_trade_ids", []) or [])[:3],
            "sample_ticket_ids": list(group.get("sample_ticket_ids", []) or [])[:3],
            "sample_signal_ids": list(group.get("sample_signal_ids", []) or [])[:3],
        })
    compact_groups = sorted(
        compact_groups,
        key=lambda item: (0 if item["severity"] == "warn" else 1, -int(item["count"] or 0), item["root_cause"]),
    )[:4]
    status = detail.get("explainability_status") or ("warn" if warn_total else "ok")
    return {
        "status": status,
        "verdict": "not_promotion_ready" if warn_total else "reviewable",
        "gap_count": total,
        "warn_count": warn_total,
        "info_count": info_total,
        "root_cause_count": len(compact_groups),
        "groups": compact_groups,
    }


def _compact_nav_curve(curve: dict) -> dict:
    if not isinstance(curve, dict):
        return {}
    return {
        "status": curve.get("status", ""),
        "source": curve.get("source", ""),
        "reason": curve.get("reason", ""),
        "starting_equity": curve.get("starting_equity"),
        "current_equity": curve.get("current_equity"),
        "current_drawdown_pct": curve.get("current_drawdown_pct"),
        "max_drawdown_pct": curve.get("max_drawdown_pct"),
        "point_count": curve.get("point_count", 0),
        "points": [
            {
                key: point.get(key)
                for key in (
                    "timestamp",
                    "close",
                    "equity",
                    "unrealized_pnl",
                    "realized_pnl",
                    "active_trade_count",
                    "drawdown_pct",
                )
                if key in point
            }
            for point in curve.get("points", [])
            if isinstance(point, dict)
        ],
    }


def _compact_bar(bar: dict) -> dict:
    return {
        key: bar.get(key)
        for key in ("timestamp", "open", "high", "low", "close", "provider", "quality_flags")
        if key in bar
    }


def _compact_order(order: dict) -> dict:
    keep = (
        "order_id",
        "ticket_id",
        "status",
        "requested_price",
        "fill_price",
        "quantity",
        "filled_at",
        "rejection_reason",
        "total_cost",
    )
    return {key: order.get(key) for key in keep if key in order}


def _compact_trade(trade: dict) -> dict:
    keep = (
        "trade_id",
        "order_id",
        "ticket_id",
        "signal_id",
        "signal_regime",
        "signal_strength",
        "signal_confidence",
        "symbol",
        "side",
        "status",
        "quantity",
        "entry_price",
        "requested_entry_price",
        "stop_loss",
        "target",
        "opened_at",
        "closed_at",
        "exit_price",
        "realized_pnl",
        "unrealized_pnl",
        "quality_flags",
        "entry_reason",
        "exit_reason",
    )
    compact = {key: trade.get(key) for key in keep if key in trade}
    if isinstance(trade.get("strategy_signal"), dict):
        compact["strategy_signal"] = _compact_signal(trade["strategy_signal"])
    if isinstance(trade.get("risk_block"), dict):
        compact["risk_block"] = _compact_risk_block(trade["risk_block"])
    if isinstance(trade.get("decision"), dict):
        compact["decision"] = _compact_decision(trade["decision"])
    if isinstance(trade.get("exit_decision"), dict):
        compact["exit_decision"] = _compact_exit_decision(trade["exit_decision"])
    if isinstance(trade.get("record_card"), dict):
        compact["record_card"] = _compact_trade_record_card(trade["record_card"])
    return compact


def _compact_trade_record_card(card: dict) -> dict:
    return {
        "schema_version": card.get("schema_version", ""),
        "run_date": card.get("run_date", ""),
        "trade_id": card.get("trade_id", ""),
        "strategy_id": card.get("strategy_id", ""),
        "trader_id": card.get("trader_id", ""),
        "portfolio_id": card.get("portfolio_id", ""),
        "strategy_family": card.get("strategy_family", ""),
        "strategy_variant": card.get("strategy_variant", ""),
        "status": card.get("status", ""),
        "symbol": card.get("symbol", ""),
        "side": card.get("side", ""),
        "quantity": card.get("quantity"),
        "entry": card.get("entry", {}),
        "exit": card.get("exit", {}),
        "protection": card.get("protection", {}),
        "pnl": card.get("pnl", {}),
        "compliance": card.get("compliance", {}),
        "audit": card.get("audit", {}),
        "display": card.get("display", {}),
    }


def _compact_signal(signal: dict) -> dict:
    if not isinstance(signal, dict):
        return {}
    keep = (
        "signal_id",
        "asset",
        "direction",
        "strength",
        "confidence",
        "horizon",
        "thesis",
        "evidence",
        "methods",
        "regime",
        "factor_scores",
        "backtest_verdict",
        "invalid_if",
        "generated_at",
        "expires_at",
        "status",
        "artifact_provenance",
    )
    compact = {key: signal.get(key) for key in keep if key in signal and key != "artifact_provenance"}
    if isinstance(signal.get("artifact_provenance"), dict):
        compact["artifact_provenance"] = _compact_artifact_provenance(signal["artifact_provenance"])
    return compact


def _compact_artifact_provenance(provenance: dict) -> dict:
    keep = (
        "status",
        "repaired_at",
        "target_date",
        "source_trade_id",
        "source_order_id",
        "source_ticket_id",
        "source_signal_id",
        "warning",
    )
    return {key: provenance.get(key) for key in keep if key in provenance}


def _compact_risk_block(block: dict) -> dict:
    if not isinstance(block, dict):
        return {}
    keep = ("status", "blocked", "reason", "message", "checks", "generated_at")
    return {key: block.get(key) for key in keep if key in block}


def _compact_decision(decision: dict) -> dict:
    if not isinstance(decision, dict):
        return {}
    compact = {}
    if "risk_snapshot" in decision:
        compact["risk_snapshot"] = decision.get("risk_snapshot")
    if "decision" in decision:
        compact["decision"] = decision.get("decision")
    if "reason" in decision:
        compact["reason"] = decision.get("reason")
    return compact


def _compact_exit_decision(decision: dict) -> dict:
    if not isinstance(decision, dict):
        return {}
    keep = (
        "status",
        "required_user_action",
        "latest_price",
        "unrealized_pnl",
        "reason",
        "generated_at",
    )
    return {key: decision.get(key) for key in keep if key in decision}


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
