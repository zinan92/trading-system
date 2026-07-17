"""Pure DualTrack projection and parity receipt for trusted market envelopes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from schemas.market_data import MarketDataEnvelope


SHADOW_RECEIPT_SCHEMA = "market-data-contract-shadow-v1"
PROJECTION_RECEIPT_SCHEMA = "market-data-envelope-projection-v1"
_MAX_REPORTED_DIFFERENCES = 50
_TOP_LEVEL_COMPARISON_FIELDS = (
    "status",
    "source_mode",
    "symbol",
    "provider_symbol",
    "timeframe",
    "bar_count",
    "latest_timestamp",
    "latest_close",
    "provider",
    "quality_flags",
    "is_synthetic",
    "fresh",
    "age_minutes",
    "max_age_minutes",
    "access_issues",
    "selection_reason",
    "attempted_sources",
)
_BAR_COMPARISON_FIELDS = (
    "symbol",
    "provider_symbol",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "provider",
    "quality_flags",
)


def project_dualtrack_market_payload(
    envelope: MarketDataEnvelope,
    *,
    requested: dict,
    datafeed_url: str,
    checked_at: datetime,
) -> dict:
    """Project one validated envelope into the existing DualTrack read shape.

    The datafeed remains authoritative for session and source truth.  The
    consumer independently rechecks the final timestamp so a delayed response
    cannot remain ready merely because its original freshness receipt was true.
    """

    reference = _as_utc(checked_at)
    bars = [
        {
            "symbol": envelope.instrument_id,
            "provider_symbol": envelope.provider_symbol,
            "timeframe": envelope.timeframe,
            "timestamp": bar.timestamp,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "provider": bar.provider,
            "quality_flags": list(bar.quality_flags),
        }
        for bar in envelope.bars
    ]
    age_minutes = _age_minutes(bars[-1]["timestamp"], reference) if bars else None
    max_age_minutes = (
        round(envelope.max_age_seconds / 60.0, 2)
        if envelope.max_age_seconds is not None
        else None
    )
    consumer_session_ready = _consumer_session_ready(envelope, reference)
    consumer_bar_fresh = bool(
        bars
        and age_minutes is not None
        and max_age_minutes is not None
        and age_minutes <= max_age_minutes
    )
    consumer_fresh = bool(
        envelope.execution_ready
        and consumer_session_ready
        and consumer_bar_fresh
    )
    if consumer_fresh:
        status = "ready"
    elif not bars or not consumer_session_ready:
        status = "blocked"
    elif envelope.fresh is False or (
        envelope.execution_ready and not consumer_bar_fresh
    ):
        status = "stale"
    else:
        status = "blocked"

    return {
        "schema_version": "dualtrack-market-bars-v1",
        "status": status,
        "source_mode": envelope.selected_source or envelope.source_mode,
        "symbol": envelope.instrument_id,
        "provider_symbol": envelope.provider_symbol,
        "timeframe": envelope.timeframe,
        "provider": envelope.provider,
        "quality_flags": list(envelope.quality_flags),
        "is_synthetic": envelope.is_synthetic,
        "requested": dict(requested),
        "bar_count": len(bars),
        "latest_timestamp": bars[-1]["timestamp"] if bars else "",
        "latest_close": bars[-1]["close"] if bars else None,
        "fresh": consumer_fresh,
        "age_minutes": age_minutes,
        "max_age_minutes": max_age_minutes,
        "bars": bars,
        "access_issues": list(envelope.access_issues),
        "datafeed": datafeed_url,
        "selection_reason": envelope.selection_reason,
        "attempted_sources": list(envelope.attempted_sources),
        "safety": _datafeed_safety(),
        "market_data_contract": {
            "schema_version": PROJECTION_RECEIPT_SCHEMA,
            "envelope_schema_version": envelope.schema_version,
            "upstream_schema_version": envelope.upstream_schema_version,
            "execution_ready": envelope.execution_ready,
            "consumer_fresh": consumer_fresh,
            "consumer_bar_fresh": consumer_bar_fresh,
            "consumer_session_ready": consumer_session_ready,
            "continuous_market": envelope.continuous_market,
            "market_open": envelope.market_open,
            "session_status": envelope.session_status,
            "session_checked_at": envelope.session_checked_at,
            "current_session_end": envelope.current_session_end,
            "reported_age_seconds": envelope.age_seconds,
            "reported_max_age_seconds": envelope.max_age_seconds,
            "checked_at": reference.replace(microsecond=0).isoformat(),
        },
    }


def compare_market_payloads(legacy: dict, candidate: dict) -> dict:
    """Compare every safety-relevant DualTrack field with exact equality."""

    differences: list[dict[str, Any]] = []
    difference_count = 0
    comparison_count = 0

    def compare(field: str, legacy_value: Any, candidate_value: Any) -> None:
        nonlocal comparison_count, difference_count
        comparison_count += 1
        if legacy_value == candidate_value:
            return
        difference_count += 1
        if len(differences) < _MAX_REPORTED_DIFFERENCES:
            differences.append(
                {
                    "field": field,
                    "legacy": legacy_value,
                    "candidate": candidate_value,
                }
            )

    for field in _TOP_LEVEL_COMPARISON_FIELDS:
        compare(field, legacy.get(field), candidate.get(field))

    legacy_bars = legacy.get("bars") if isinstance(legacy.get("bars"), list) else []
    candidate_bars = (
        candidate.get("bars") if isinstance(candidate.get("bars"), list) else []
    )
    compare("bars.length", len(legacy_bars), len(candidate_bars))
    for index in range(max(len(legacy_bars), len(candidate_bars))):
        if index >= len(legacy_bars) or index >= len(candidate_bars):
            compare(
                f"bars[{index}]",
                legacy_bars[index] if index < len(legacy_bars) else None,
                candidate_bars[index] if index < len(candidate_bars) else None,
            )
            continue
        legacy_bar = legacy_bars[index]
        candidate_bar = candidate_bars[index]
        if not isinstance(legacy_bar, dict) or not isinstance(candidate_bar, dict):
            compare(f"bars[{index}]", legacy_bar, candidate_bar)
            continue
        for field in _BAR_COMPARISON_FIELDS:
            compare(
                f"bars[{index}].{field}",
                legacy_bar.get(field),
                candidate_bar.get(field),
            )

    legacy_contract = _comparison_contract(legacy)
    candidate_contract = _comparison_contract(candidate)
    return {
        "schema_version": SHADOW_RECEIPT_SCHEMA,
        "status": "pass" if difference_count == 0 else "drift",
        "comparison_count": comparison_count,
        "difference_count": difference_count,
        "differences": differences,
        "differences_truncated": difference_count > len(differences),
        "legacy_digest": _digest(legacy_contract),
        "candidate_digest": _digest(candidate_contract),
        "compared_top_level_fields": list(_TOP_LEVEL_COMPARISON_FIELDS),
        "compared_bar_fields": list(_BAR_COMPARISON_FIELDS),
    }


def _comparison_contract(payload: dict) -> dict:
    return {
        field: _canonical_value(field, payload.get(field))
        for field in _TOP_LEVEL_COMPARISON_FIELDS
    } | {
        "bars": [
            {
                field: _canonical_value(field, row.get(field))
                for field in _BAR_COMPARISON_FIELDS
            }
            for row in payload.get("bars", [])
            if isinstance(row, dict)
        ]
    }


def _canonical_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in {
        "latest_close",
        "age_minutes",
        "max_age_minutes",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }:
        return float(value)
    return value


def _digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _age_minutes(timestamp: str, checked_at: datetime) -> float:
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = max(0.0, (checked_at - parsed.astimezone(timezone.utc)).total_seconds())
    return round(age / 60.0, 2)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _consumer_session_ready(
    envelope: MarketDataEnvelope,
    checked_at: datetime,
) -> bool:
    if envelope.continuous_market:
        return bool(
            envelope.market_open is True
            and envelope.session_status == "continuous"
        )
    if (
        envelope.market_open is not True
        or envelope.session_status != "open"
        or not envelope.session_checked_at
        or not envelope.current_session_end
    ):
        return False
    session_checked_at = _parse_utc(envelope.session_checked_at)
    session_end = _parse_utc(envelope.current_session_end)
    receipt_age = (checked_at - session_checked_at).total_seconds()
    max_receipt_age = max(float(envelope.max_age_seconds or 0.0), 60.0)
    return bool(
        -60.0 <= receipt_age <= max_receipt_age
        and session_checked_at < session_end
        and checked_at < session_end
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datafeed_safety() -> dict:
    return {
        "read_only": True,
        "writes_market_db": False,
        "opens_broker_clients": False,
        "opens_order_clients": False,
        "uses_browser_exchange_socket": False,
        "reads_private_market_db": False,
    }
