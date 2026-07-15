"""Scheduled market-data refresh commands routed only through datafeed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.datafeed_market_client import DatafeedMarketClient
from services.journal_store import write_json


def refresh_market_data(
    *,
    run_date: str,
    symbol: str,
    timeframe: str,
    output_kind: str,
    source: str | None = None,
    start: str | None = None,
    end: str | None = None,
    output_root: Path | None = None,
) -> dict:
    config = load_pipeline_config()
    datafeed = config.get("datafeed", {}) or {}
    route = (datafeed.get("instrument_routes", {}) or {}).get(symbol)
    if not isinstance(route, dict):
        raise KeyError(f"No datafeed instrument route configured for {symbol}")
    client = DatafeedMarketClient(
        base_url=str(datafeed.get("base_url") or "http://127.0.0.1:8100"),
        timeout_seconds=float(datafeed.get("timeout_seconds", 10)),
    )
    payload = client.candles(
        asset_class=str(route["asset_class"]),
        ticker=str(route.get("ticker") or symbol),
        timeframe=timeframe,
        limit=60_000 if start else 1_500,
        source=str(source or route["source"]),
        cache_policy="bypass",
        quality="strict",
        require_execution_venue=bool(route.get("require_execution_venue", False)),
        start=start,
        end=end,
    )
    result = {
        "status": "pass" if payload.get("count") else "fail",
        "run_date": run_date,
        "provider": payload.get("provider"),
        "source": payload.get("selected_source"),
        "symbol": symbol,
        "provider_symbol": payload.get("provider_symbol"),
        "timeframe": timeframe,
        "imported_rows": int(payload.get("count") or 0),
        "backfilled_rows": int(payload.get("count") or 0) if start else 0,
        "latest_timestamp": payload.get("latest_timestamp"),
        "fresh": payload.get("fresh"),
        "is_synthetic": bool(payload.get("is_synthetic", False)),
        "market_data_backend": "datafeed",
        "start": start,
        "end": end,
    }
    root = output_root or Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / str(config.get("output_root", "outputs"))),
        )
    )
    write_json(root / output_kind / "current.json", [result])
    write_json(root / output_kind / f"{run_date}.json", [result])
    return result


def refresh_market_data_range(
    *,
    run_date: str,
    symbol: str,
    timeframe: str,
    output_kind: str,
    start: str,
    end: str,
    chunk_days: int,
    output_root: Path | None = None,
) -> dict:
    cursor = _parse(start)
    end_dt = _parse(end)
    total = 0
    latest: dict = {}
    failed_windows: list[dict] = []
    while cursor <= end_dt:
        window_end = min(cursor + timedelta(days=max(1, chunk_days)), end_dt)
        try:
            latest = refresh_market_data(
                run_date=run_date,
                symbol=symbol,
                timeframe=timeframe,
                output_kind=output_kind,
                start=cursor.isoformat(),
                end=window_end.isoformat(),
                output_root=output_root,
            )
            total += int(latest.get("imported_rows") or 0)
        except Exception as error:
            failed_windows.append(
                {"start": cursor.isoformat(), "end": window_end.isoformat(), "error": f"{type(error).__name__}: {error}"}
            )
        cursor = window_end + timedelta(microseconds=1)
    result = {
        **latest,
        "status": "pass" if total and not failed_windows else ("warn" if total else "fail"),
        "run_date": run_date,
        "start": start,
        "end": end,
        "imported_rows": total,
        "backfilled_rows": total,
        "failed_windows": failed_windows,
        "chunk_days": chunk_days,
        "market_data_backend": "datafeed",
    }
    config = load_pipeline_config()
    root = output_root or Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / str(config.get("output_root", "outputs"))),
        )
    )
    write_json(root / output_kind / "current.json", [result])
    write_json(root / output_kind / f"{run_date}.json", [result])
    return result


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
