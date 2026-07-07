from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.market_store import MarketStore


class TigerFuturesFeedClient:
    """Read-only Tiger OpenAPI futures bars importer.

    The first Tiger milestone is market-data only. This client intentionally
    uses QuoteClient endpoints and never imports TradeClient or order APIs.
    """

    def __init__(
        self,
        store: MarketStore,
        config: dict | None = None,
        quote_client: Any | None = None,
    ) -> None:
        self.store = store
        self.config = config or load_pipeline_config().get("tiger_futures_feed", {})
        self._quote_client = quote_client
        self._injected_quote_client = quote_client is not None

    def preflight(self) -> dict:
        props_path = self._props_path()
        checks = {
            "props_path_env": str(self.config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH")),
            "props_path_present": bool(props_path),
            "props_path_exists": bool(props_path and Path(props_path).exists()),
            "props_path_owner_only": False,
            "props_path_mode": "",
            "quote_permission": {},
            "contract": {},
        }
        if props_path and Path(props_path).exists():
            mode = stat.S_IMODE(Path(props_path).stat().st_mode)
            checks["props_path_mode"] = oct(mode)
            checks["props_path_owner_only"] = not bool(mode & 0o077)
        if self._quote_client is None and not checks["props_path_exists"]:
            return {
                "provider": self.provider,
                "ready": False,
                "status": "fail",
                "message": "Tiger OpenAPI config path is missing or does not exist.",
                "contract": self.contract,
                "output_symbol": self.output_symbol,
                "timeframe": self.timeframe,
                "checks": checks,
                "checked_at": self._now(),
            }
        if self._quote_client is None and not checks["props_path_owner_only"]:
            return {
                "provider": self.provider,
                "ready": False,
                "status": "fail",
                "message": "Tiger OpenAPI config file must be owner-only (chmod 600).",
                "contract": self.contract,
                "output_symbol": self.output_symbol,
                "timeframe": self.timeframe,
                "checks": checks,
                "checked_at": self._now(),
            }

        client = self._client()
        try:
            checks["quote_permission"] = self._quote_permission(client)
            checks["contract"] = self._contract_metadata(client)
        except Exception as exc:
            return {
                "provider": self.provider,
                "ready": False,
                "status": "fail",
                "message": f"Tiger OpenAPI preflight failed: {type(exc).__name__}: {exc}",
                "contract": self.contract,
                "output_symbol": self.output_symbol,
                "timeframe": self.timeframe,
                "checks": checks,
                "checked_at": self._now(),
            }

        return {
            "provider": self.provider,
            "ready": True,
            "status": "pass",
            "message": "Tiger OpenAPI futures quote client is reachable.",
            "contract": self.contract,
            "output_symbol": self.output_symbol,
            "timeframe": self.timeframe,
            "checks": checks,
            "checked_at": self._now(),
        }

    def fetch_and_store(self, limit: int | None = None) -> dict:
        preflight = self.preflight()
        if not preflight["ready"]:
            return {**preflight, "imported_rows": 0, "coverage": self._coverage()}
        try:
            bars = self.fetch(limit=limit)
        except Exception as exc:
            return {
                **preflight,
                "status": "fail",
                "message": f"Tiger futures bars fetch failed: {type(exc).__name__}: {exc}",
                "imported_rows": 0,
                "latest_timestamp": "",
                "latest_price": None,
                "coverage": self._coverage(),
            }
        self.store.upsert_bars(bars)
        if bars:
            self.store.upsert_quote(bars[-1])
        return {
            **preflight,
            "status": "pass" if bars else "warn",
            "message": "imported Tiger OpenAPI futures bars" if bars else "Tiger OpenAPI returned no futures bars",
            "imported_rows": len(bars),
            "latest_timestamp": bars[-1].timestamp if bars else "",
            "latest_price": bars[-1].close if bars else None,
            "coverage": self._coverage(),
        }

    def fetch(self, limit: int | None = None) -> list[Bar]:
        client = self._client()
        payload = client.get_future_bars(
            [self.contract],
            period=self._bar_period(),
            begin_time=-1,
            end_time=-1,
            limit=int(limit or self.config.get("limit", 500)),
        )
        rows = self._payload_rows(payload)
        bars = [self._row_to_bar(row) for row in rows]
        unique = {bar.timestamp: bar for bar in bars}
        return [unique[key] for key in sorted(unique)]

    def fetch_range(
        self,
        start: str,
        end: str,
        *,
        total: int | None = None,
        page_size: int | None = None,
        time_interval: int | float | None = None,
    ) -> list[Bar]:
        client = self._client()
        period = self._bar_period()
        if hasattr(client, "get_future_bars_by_page"):
            payload = client.get_future_bars_by_page(
                self.contract,
                period=period,
                begin_time=_iso_to_ms(start),
                end_time=_iso_to_ms(end),
                total=int(total or self.config.get("backfill_total", 20_000)),
                page_size=int(page_size or self.config.get("backfill_page_size", 500)),
                time_interval=float(time_interval if time_interval is not None else self.config.get("backfill_time_interval", 0)),
            )
        else:
            payload = client.get_future_bars(
                [self.contract],
                period=period,
                begin_time=_iso_to_ms(start),
                end_time=_iso_to_ms(end),
                limit=int(total or self.config.get("backfill_total", 20_000)),
            )
        rows = self._payload_rows(payload)
        bars = [self._row_to_bar(row) for row in rows]
        start_ts = self._parse_timestamp(start)
        end_ts = self._parse_timestamp(end)
        unique = {
            bar.timestamp: bar
            for bar in bars
            if start_ts <= self._parse_timestamp(bar.timestamp) <= end_ts
        }
        return [unique[key] for key in sorted(unique)]

    def backfill_and_store(
        self,
        start: str,
        end: str,
        *,
        total: int | None = None,
        page_size: int | None = None,
        time_interval: int | float | None = None,
    ) -> dict:
        preflight = self.preflight()
        if not preflight["ready"]:
            return {**preflight, "backfilled_rows": 0, "coverage": self._coverage(), "start": start, "end": end}
        try:
            bars = self.fetch_range(start, end, total=total, page_size=page_size, time_interval=time_interval)
        except Exception as exc:
            return {
                **preflight,
                "status": "fail",
                "message": f"Tiger futures backfill failed: {type(exc).__name__}: {exc}",
                "backfilled_rows": 0,
                "first_timestamp": "",
                "last_timestamp": "",
                "start": start,
                "end": end,
                "coverage": self._coverage(),
            }
        if bars:
            self.store.upsert_bars(bars)
            self.store.upsert_quote(bars[-1])
        return {
            **preflight,
            "status": "pass" if bars else "warn",
            "message": "backfilled Tiger OpenAPI futures bars" if bars else "Tiger OpenAPI returned no futures bars for range",
            "backfilled_rows": len(bars),
            "first_timestamp": bars[0].timestamp if bars else "",
            "last_timestamp": bars[-1].timestamp if bars else "",
            "start": start,
            "end": end,
            "coverage": self._coverage(),
        }

    def fetch_trading_times(self, trading_date: str | None = None) -> dict:
        client = self._client()
        payload = client.get_future_trading_times(self.contract, trading_date=trading_date)
        windows = []
        for row in self._payload_rows(payload):
            if row.get("start") is None or row.get("end") is None:
                continue
            kind = "trading" if bool(row.get("trading")) else "bidding" if bool(row.get("bidding")) else "closed"
            windows.append({
                "kind": kind,
                "trading": bool(row.get("trading")),
                "bidding": bool(row.get("bidding")),
                "start": self._normalize_timestamp(row["start"]),
                "end": self._normalize_timestamp(row["end"]),
                "zone": str(row.get("zone") or row.get("time_zone") or self.config.get("time_zone", "")),
            })
        windows = sorted(windows, key=lambda item: item["start"])
        return {
            "provider": self.provider,
            "contract": self.contract,
            "output_symbol": self.output_symbol,
            "trading_date": trading_date or "",
            "timezone": next((item["zone"] for item in windows if item.get("zone")), ""),
            "windows": windows,
            "trading_windows": [item for item in windows if item["trading"]],
            "checked_at": self._now(),
        }

    @property
    def provider(self) -> str:
        return str(self.config.get("provider", "tiger_openapi:COMEX"))

    @property
    def contract(self) -> str:
        return str(self.config.get("contract", "MGCmain"))

    @property
    def output_symbol(self) -> str:
        return str(self.config.get("output_symbol", self.contract))

    @property
    def timeframe(self) -> str:
        return str(self.config.get("timeframe", "1m"))

    def _client(self):
        if self._quote_client is not None:
            return self._quote_client
        try:
            from tigeropen.quote.quote_client import QuoteClient
            from tigeropen.tiger_open_config import TigerOpenClientConfig
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; install it locally to use Tiger OpenAPI feeds") from exc

        props_path = self._props_path()
        if not props_path:
            raise RuntimeError("Tiger OpenAPI config path is required")
        client_config = TigerOpenClientConfig(props_path=props_path)
        grab_permission = bool(self.config.get("grab_quote_permission", False))
        self._quote_client = QuoteClient(client_config, is_grab_permission=grab_permission)
        return self._quote_client

    def _props_path(self) -> str:
        if self.config.get("props_path"):
            return str(self.config["props_path"])
        from services.live_env import apply_live_env

        apply_live_env()
        env_name = str(self.config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH"))
        return os.getenv(env_name, "")

    def _bar_period(self):
        period = str(self.config.get("period", "1m")).lower()
        if self._injected_quote_client:
            return period
        try:
            from tigeropen.common.consts import BarPeriod
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; cannot resolve Tiger bar period") from exc
        mapping = {
            "1m": BarPeriod.ONE_MINUTE,
            "1min": BarPeriod.ONE_MINUTE,
            "one_minute": BarPeriod.ONE_MINUTE,
            "5m": BarPeriod.FIVE_MINUTES,
            "5min": BarPeriod.FIVE_MINUTES,
            "day": BarPeriod.DAY,
            "1d": BarPeriod.DAY,
        }
        return mapping.get(period, BarPeriod.ONE_MINUTE)

    def _payload_rows(self, payload) -> list[dict]:
        if payload is None:
            return []
        if hasattr(payload, "to_dict"):
            try:
                return list(payload.to_dict("records"))
            except TypeError:
                pass
        if isinstance(payload, list):
            return [self._object_to_dict(item) for item in payload]
        if isinstance(payload, tuple):
            return [self._object_to_dict(item) for item in payload]
        return [self._object_to_dict(payload)]

    def _object_to_dict(self, item) -> dict:
        if isinstance(item, dict):
            return item
        if hasattr(item, "_asdict"):
            return dict(item._asdict())
        if hasattr(item, "__dict__"):
            return dict(item.__dict__)
        raise TypeError(f"unsupported Tiger bar row type: {type(item).__name__}")

    def _row_to_bar(self, row: dict) -> Bar:
        timestamp = self._normalize_timestamp(row.get("time") or row.get("timestamp") or row.get("latest_time"))
        contract = str(row.get("identifier") or row.get("contract_code") or self.contract)
        exchange = str(row.get("exchange") or self.config.get("exchange", "COMEX"))
        return Bar(
            symbol=self.output_symbol,
            timeframe=self.timeframe,
            timestamp=timestamp,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0),
            provider=self.provider,
            quality_flags=[
                "official_broker_feed",
                "execution_venue_feed",
                "exchange_futures",
                "tiger_openapi",
                exchange.lower(),
                contract.lower(),
            ],
        )

    def _normalize_timestamp(self, value) -> str:
        if value is None:
            raise ValueError("Tiger bar row is missing time/timestamp")
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        else:
            text = str(value).strip()
            if text.isdigit():
                parsed = datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _parse_timestamp(self, value: str | datetime) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    def _quote_permission(self, client) -> dict:
        if not hasattr(client, "get_quote_permission"):
            return {}
        permissions = client.get_quote_permission()
        rows = self._payload_rows(permissions)
        names = [str(row.get("name", "")) for row in rows]
        return {
            "names": names,
            "has_futures_realtime": any("future" in name.lower() or "futures" in name.lower() for name in names),
        }

    def _contract_metadata(self, client) -> dict:
        if not hasattr(client, "get_future_contract"):
            return {}
        payload = client.get_future_contract(self.contract)
        rows = self._payload_rows(payload)
        return rows[0] if rows else {}

    def _coverage(self) -> list[dict]:
        return [
            item
            for item in self.store.coverage()
            if item["symbol"] == self.output_symbol and item["timeframe"] == self.timeframe
        ]

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_tiger_futures_feed_import(
    run_date: str,
    output_root: Path | None = None,
    market_db: Path | None = None,
    config: dict | None = None,
    quote_client: Any | None = None,
    limit: int | None = None,
) -> dict:
    pipeline_config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))))
    feed_config = dict(pipeline_config.get("tiger_futures_feed", {}))
    if config:
        feed_config.update(config)
    result = TigerFuturesFeedClient(MarketStore(market_db), feed_config, quote_client=quote_client).fetch_and_store(limit=limit)
    result["run_date"] = run_date
    result["market_db"] = str(market_db)
    write_json(output_root / "tiger_futures_feed" / "current.json", [result])
    write_json(output_root / "tiger_futures_feed" / f"{run_date}.json", [result])
    return result


def run_tiger_futures_backfill(
    start: str,
    end: str,
    output_root: Path | None = None,
    market_db: Path | None = None,
    config: dict | None = None,
    quote_client: Any | None = None,
    total: int | None = None,
    page_size: int | None = None,
    time_interval: int | float | None = None,
) -> dict:
    pipeline_config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))))
    feed_config = dict(pipeline_config.get("tiger_futures_feed", {}))
    if config:
        feed_config.update(config)
    result = TigerFuturesFeedClient(MarketStore(market_db), feed_config, quote_client=quote_client).backfill_and_store(
        start,
        end,
        total=total,
        page_size=page_size,
        time_interval=time_interval,
    )
    result["market_db"] = str(market_db)
    write_json(output_root / "tiger_futures_backfill" / "current.json", [result])
    write_json(output_root / "tiger_futures_backfill" / f"{start[:10]}.json", [result])
    return result


def run_tiger_futures_sessions(
    run_date: str,
    trading_date: str | None = None,
    output_root: Path | None = None,
    config: dict | None = None,
    quote_client: Any | None = None,
) -> dict:
    pipeline_config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
    feed_config = dict(pipeline_config.get("tiger_futures_feed", {}))
    if config:
        feed_config.update(config)
    result = TigerFuturesFeedClient(MarketStore(Path(":memory:")), feed_config, quote_client=quote_client).fetch_trading_times(
        trading_date=trading_date or run_date
    )
    result["run_date"] = run_date
    write_json(output_root / "tiger_futures_sessions" / "current.json", [result])
    write_json(output_root / "tiger_futures_sessions" / f"{run_date}.json", [result])
    return result


def is_trading_at(timestamp: str | datetime, sessions: dict) -> bool:
    if isinstance(timestamp, datetime):
        ts = timestamp
    else:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    for window in sessions.get("trading_windows", []):
        start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        if start <= ts < end:
            return True
    return False


def _iso_to_ms(value: str | datetime) -> int:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)
