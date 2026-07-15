from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.datafeed_market_client import DatafeedMarketClient


def _is_trading_at(timestamp: datetime, sessions: dict) -> bool:
    for window in sessions.get("trading_windows", []):
        start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        if start <= timestamp.astimezone(timezone.utc) < end:
            return True
    return False


class TigerRealtimeValidation:
    """Read-only market-hours validator for Tiger futures bars.

    This intentionally polls QuoteClient futures bars only. It does not write to
    the market DB and does not import or open Tiger TradeClient/order APIs.
    """

    def __init__(
        self,
        config: dict | None = None,
        quote_client: Any | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or load_pipeline_config().get("tiger_futures_feed", {})
        self.quote_client = quote_client
        self.sleeper = sleeper or time.sleep

    def run(
        self,
        *,
        as_of: str | datetime | None = None,
        trading_date: str | None = None,
        poll_seconds: float = 75.0,
        max_lag_seconds: float = 180.0,
        limit: int = 5,
    ) -> dict:
        now = self._parse_as_of(as_of)
        if self.quote_client is None:
            return self._run_datafeed(
                now=now,
                trading_date=trading_date,
                poll_seconds=poll_seconds,
                max_lag_seconds=max_lag_seconds,
                limit=limit,
            )
        from services.market_store import MarketStore
        from services.tiger_futures_feed import TigerFuturesFeedClient

        client = TigerFuturesFeedClient(MarketStore(Path(":memory:")), self.config, quote_client=self.quote_client)
        preflight = client.preflight()
        base = self._base_payload(client, now, poll_seconds, max_lag_seconds)
        if not preflight.get("ready"):
            return {
                **base,
                "status": "fail",
                "message": preflight.get("message", "Tiger OpenAPI preflight failed."),
                "preflight": self._redacted_preflight(preflight),
            }

        sessions = self._session_payloads(client, now=now, trading_date=trading_date)
        active = next((item for item in sessions if item["is_trading_now"]), None)
        if active is None:
            return {
                **base,
                "status": "pending_market_open",
                "message": "COMEX futures session is not trading at validation time; rerun during the next trading window.",
                "preflight": self._redacted_preflight(preflight),
                "sessions": sessions,
                "next_trading_window": self._next_trading_window(sessions, now),
            }

        first = self._fetch_latest(client, limit=limit)
        if poll_seconds > 0:
            self.sleeper(float(poll_seconds))
        second = self._fetch_latest(client, limit=limit)
        return self._market_open_payload(
            base=base,
            preflight=preflight,
            sessions=sessions,
            active_session=active,
            first=first,
            second=second,
            now=now,
            max_lag_seconds=max_lag_seconds,
        )

    def _run_datafeed(
        self,
        *,
        now: datetime,
        trading_date: str | None,
        poll_seconds: float,
        max_lag_seconds: float,
        limit: int,
    ) -> dict:
        pipeline = load_pipeline_config()
        datafeed = pipeline.get("datafeed", {}) or {}
        route = (datafeed.get("instrument_routes", {}) or {}).get("MGCmain", {})
        client = DatafeedMarketClient(base_url=str(datafeed.get("base_url") or "http://127.0.0.1:8100"))
        source = str(route.get("source") or "tiger_openapi_comex")
        ticker = str(route.get("ticker") or "MGCmain")
        asset_class = str(route.get("asset_class") or "commodity")
        base = {
            "schema_version": "tiger-realtime-validation-v1",
            "provider": source,
            "contract": ticker,
            "output_symbol": "MGCmain",
            "timeframe": "1m",
            "checked_at": now.isoformat(),
            "poll_seconds": float(poll_seconds),
            "max_lag_seconds": float(max_lag_seconds),
            "safety": {"read_only": True, "writes_market_db": False, "opens_quote_client": False, "opens_trade_client": False, "opens_order_clients": False, "submits_orders": False},
            "market_data_backend": "datafeed",
        }
        sessions = []
        for candidate in self._candidate_trading_dates(now, trading_date):
            try:
                receipt = client.sessions(asset_class=asset_class, ticker=ticker, source=source, trading_date=candidate)
                error = ""
            except Exception as exc:
                receipt = {"windows": [], "trading_windows": []}
                error = f"{type(exc).__name__}: {exc}"
            sessions.append({"trading_date": candidate, "timezone": receipt.get("timezone", ""), "is_trading_now": _is_trading_at(now, receipt), "trading_windows": receipt.get("trading_windows", []), "window_count": len(receipt.get("windows", [])), "error": error})
        active = next((item for item in sessions if item["is_trading_now"]), None)
        if active is None:
            return {**base, "status": "pending_market_open", "message": "COMEX futures session is not trading at validation time; rerun during the next trading window.", "sessions": sessions, "next_trading_window": self._next_trading_window(sessions, now)}
        first = self._datafeed_latest(client, asset_class, ticker, source, limit)
        if poll_seconds > 0:
            self.sleeper(float(poll_seconds))
        second = self._datafeed_latest(client, asset_class, ticker, source, limit)
        return self._market_open_payload(base=base, preflight={"ready": True, "status": "pass", "message": "datafeed adapter ready"}, sessions=sessions, active_session=active, first=first, second=second, now=now, max_lag_seconds=max_lag_seconds)

    @staticmethod
    def _datafeed_latest(client: DatafeedMarketClient, asset_class: str, ticker: str, source: str, limit: int) -> dict:
        payload = client.candles(asset_class=asset_class, ticker=ticker, timeframe="1m", limit=limit, source=source, cache_policy="bypass", quality="strict", require_execution_venue=True)
        rows = payload.get("candles", [])
        return rows[-1] if rows else {}

    def _market_open_payload(
        self,
        *,
        base: dict,
        preflight: dict,
        sessions: list[dict],
        active_session: dict,
        first: dict,
        second: dict,
        now: datetime,
        max_lag_seconds: float,
    ) -> dict:
        if not second:
            return {
                **base,
                "status": "fail",
                "message": "Tiger OpenAPI returned no bars while COMEX session is trading.",
                "preflight": self._redacted_preflight(preflight),
                "sessions": sessions,
                "active_session": active_session,
                "polls": {"first": first, "second": second},
            }

        latest_ts = self._parse_as_of(second["timestamp"])
        first_ts = self._parse_as_of(first["timestamp"]) if first else None
        age_seconds = (now - latest_ts).total_seconds()
        advanced = bool(first_ts and latest_ts > first_ts)
        fresh = age_seconds <= max_lag_seconds
        if advanced and fresh:
            status = "pass"
            message = "Tiger futures bars advanced during the market-hours validation window."
        elif fresh:
            status = "warn"
            message = "Tiger futures latest bar is fresh, but did not advance during the validation window."
        else:
            status = "fail"
            message = "Tiger futures latest bar is stale while COMEX session is trading."
        return {
            **base,
            "status": status,
            "message": message,
            "preflight": self._redacted_preflight(preflight),
            "sessions": sessions,
            "active_session": active_session,
            "polls": {"first": first, "second": second},
            "latest_bar_age_seconds": round(age_seconds, 2),
            "bar_advanced": advanced,
            "fresh": fresh,
        }

    def _fetch_latest(self, client: Any, *, limit: int) -> dict:
        bars = client.fetch(limit=limit)
        return self._bar_payload(bars[-1]) if bars else {}

    def _bar_payload(self, bar: Any) -> dict:
        return {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "timestamp": bar.timestamp,
            "close": bar.close,
            "provider": bar.provider,
            "quality_flags": bar.quality_flags,
        }

    def _session_payloads(self, client: Any, *, now: datetime, trading_date: str | None) -> list[dict]:
        results = []
        for candidate in self._candidate_trading_dates(now, trading_date):
            try:
                sessions = client.fetch_trading_times(candidate)
                error = ""
            except Exception as exc:  # noqa: BLE001 - validation should degrade to evidence.
                sessions = {"trading_windows": [], "windows": [], "trading_date": candidate}
                error = f"{type(exc).__name__}: {exc}"
            results.append(
                {
                    "trading_date": candidate,
                    "timezone": sessions.get("timezone", ""),
                    "is_trading_now": _is_trading_at(now, sessions),
                    "trading_windows": sessions.get("trading_windows", []),
                    "window_count": len(sessions.get("windows", [])),
                    "error": error,
                }
            )
        return results

    def _candidate_trading_dates(self, now: datetime, trading_date: str | None) -> list[str]:
        if trading_date:
            return [trading_date]
        base = now.date()
        dates = [base - timedelta(days=1), base, base + timedelta(days=1)]
        return [item.isoformat() for item in dates]

    def _next_trading_window(self, sessions: list[dict], now: datetime) -> dict:
        candidates: dict[tuple[str, str], dict] = {}
        for session in sessions:
            for window in session.get("trading_windows", []):
                start = self._parse_as_of(window["start"])
                if start > now:
                    key = (str(window.get("start", "")), str(window.get("end", "")))
                    current = candidates.get(key)
                    candidate = {**window, "trading_date": session.get("trading_date", "")}
                    if current is None or str(candidate.get("trading_date", "")) > str(current.get("trading_date", "")):
                        candidates[key] = candidate
        if not candidates:
            return {}
        return min(candidates.values(), key=lambda item: self._parse_as_of(item["start"]))

    def _base_payload(
        self,
        client: Any,
        now: datetime,
        poll_seconds: float,
        max_lag_seconds: float,
    ) -> dict:
        return {
            "schema_version": "tiger-realtime-validation-v1",
            "provider": client.provider,
            "contract": client.contract,
            "output_symbol": client.output_symbol,
            "timeframe": client.timeframe,
            "checked_at": now.isoformat(),
            "poll_seconds": float(poll_seconds),
            "max_lag_seconds": float(max_lag_seconds),
            "safety": {
                "read_only": True,
                "writes_market_db": False,
                "opens_quote_client": self.quote_client is None,
                "opens_trade_client": False,
                "opens_order_clients": False,
                "submits_orders": False,
            },
        }

    def _redacted_preflight(self, preflight: dict) -> dict:
        checks = dict(preflight.get("checks", {}) or {})
        return {
            "ready": bool(preflight.get("ready")),
            "status": preflight.get("status", ""),
            "message": preflight.get("message", ""),
            "contract": preflight.get("contract", ""),
            "output_symbol": preflight.get("output_symbol", ""),
            "checks": {
                "props_path_env": checks.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH"),
                "props_path_present": bool(checks.get("props_path_present")),
                "props_path_exists": bool(checks.get("props_path_exists")),
                "props_path_owner_only": bool(checks.get("props_path_owner_only")),
                "props_path_mode": checks.get("props_path_mode", ""),
                "quote_permission": checks.get("quote_permission", {}),
                "contract": checks.get("contract", {}),
            },
        }

    def _parse_as_of(self, value: str | datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc).replace(microsecond=0)
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)


def run_tiger_realtime_validation(
    run_date: str,
    *,
    output_root: Path | None = None,
    config: dict | None = None,
    quote_client: Any | None = None,
    as_of: str | datetime | None = None,
    trading_date: str | None = None,
    poll_seconds: float = 75.0,
    max_lag_seconds: float = 180.0,
    limit: int = 5,
    require_market_hours_pass: bool = False,
    sleeper: Callable[[float], None] | None = None,
) -> dict:
    pipeline_config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
    feed_config = dict(pipeline_config.get("tiger_futures_feed", {}))
    if config:
        feed_config.update(config)
    result = TigerRealtimeValidation(feed_config, quote_client=quote_client, sleeper=sleeper).run(
        as_of=as_of,
        trading_date=trading_date,
        poll_seconds=poll_seconds,
        max_lag_seconds=max_lag_seconds,
        limit=limit,
    )
    result["run_date"] = run_date
    result["market_hours_gate"] = market_hours_validation_gate(
        result,
        require_market_hours_pass=require_market_hours_pass,
    )
    write_json(output_root / "tiger_realtime_validation" / "current.json", [result])
    write_json(output_root / "tiger_realtime_validation" / f"{run_date}.json", [result])
    return result


def market_hours_validation_gate(result: dict, *, require_market_hours_pass: bool = False) -> dict:
    status = str(result.get("status") or "missing")
    next_window = result.get("next_trading_window", {}) if isinstance(result.get("next_trading_window"), dict) else {}
    active_session = result.get("active_session", {}) if isinstance(result.get("active_session"), dict) else {}
    ready = status == "pass"
    market_hours_observed = bool(active_session)

    if not require_market_hours_pass:
        return {
            "required": False,
            "ready_for_price_feed_promotion": ready,
            "market_hours_observed": market_hours_observed,
            "exit_code": 0,
            "operator_action": "not_enforced",
        }

    if ready:
        exit_code = 0
        operator_action = "passed"
    elif status == "pending_market_open":
        exit_code = 75
        operator_action = "rerun_after_next_trading_window"
    elif status == "warn":
        exit_code = 2
        operator_action = "review_fresh_but_not_advancing"
    elif status == "fail":
        exit_code = 2
        operator_action = "fix_market_data_access_or_staleness"
    else:
        exit_code = 2
        operator_action = "review_unknown_validation_status"

    return {
        "required": True,
        "ready_for_price_feed_promotion": ready,
        "market_hours_observed": market_hours_observed,
        "exit_code": exit_code,
        "operator_action": operator_action,
        "next_trading_window": {
            "start": str(next_window.get("start") or ""),
            "end": str(next_window.get("end") or ""),
            "trading_date": str(next_window.get("trading_date") or ""),
        },
    }
