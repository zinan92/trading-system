"""Translate the datafeed HTTP contract into the trading-domain envelope."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from schemas.market_data import (
    MARKET_DATA_ENVELOPE_SCHEMA,
    Bar,
    MarketDataEnvelope,
)


UPSTREAM_CANDLE_SCHEMAS = {"kline-candles-v1", "kline-candles-v2"}


class DatafeedContractError(ValueError):
    """Raised when a successful HTTP response violates the datafeed contract."""


def map_candle_response(
    payload: dict,
    *,
    expected_asset_class: str,
    expected_timeframe: str,
    expected_source: str,
    require_execution_venue: bool,
) -> MarketDataEnvelope:
    """Validate and map one supported versioned candle response atomically.

    The mapper is deliberately stricter than a chart consumer.  It either
    returns the complete typed envelope or raises ``DatafeedContractError``;
    it never returns partially accepted bars.
    """

    if not isinstance(payload, dict):
        raise DatafeedContractError("datafeed payload must be an object")
    upstream_schema = _required_text(payload, "schema_version")
    if upstream_schema not in UPSTREAM_CANDLE_SCHEMAS:
        raise DatafeedContractError(
            f"unsupported datafeed schema: {upstream_schema or '<missing>'}"
        )

    asset_class = _required_text(payload, "asset_class")
    if asset_class != str(expected_asset_class):
        raise DatafeedContractError(
            f"datafeed asset_class mismatch: expected {expected_asset_class}, got {asset_class}"
        )
    timeframe = _required_text(payload, "timeframe")
    if timeframe != str(expected_timeframe):
        raise DatafeedContractError(
            f"datafeed timeframe mismatch: expected {expected_timeframe}, got {timeframe}"
        )
    instrument_id = _required_text(payload, "instrument_id")
    provider = _required_text(payload, "provider")
    source_mode = _required_text(payload, "source_mode")
    selected_source = _required_text(payload, "selected_source")
    requested_source = _required_text(payload, "requested_source")
    if selected_source != str(expected_source):
        raise DatafeedContractError(
            f"datafeed selected_source mismatch: expected {expected_source}, got {selected_source}"
        )
    is_synthetic = _required_bool(payload, "is_synthetic")
    if is_synthetic:
        raise DatafeedContractError("synthetic data cannot enter the trusted envelope")
    reject_reason = _optional_text(payload.get("reject_reason"), "reject_reason")
    if reject_reason:
        raise DatafeedContractError(f"datafeed response was rejected: {reject_reason}")

    response_requires_venue = _required_bool(payload, "require_execution_venue")
    execution_venue = _required_bool(payload, "execution_venue")
    if require_execution_venue and not response_requires_venue:
        raise DatafeedContractError(
            "datafeed response did not preserve the execution venue request"
        )
    if require_execution_venue and not execution_venue:
        raise DatafeedContractError("datafeed source is not an execution venue")

    candles = payload.get("candles")
    if not isinstance(candles, list):
        raise DatafeedContractError("datafeed candles must be a list")
    count = _required_int(payload, "count")
    if count != len(candles):
        raise DatafeedContractError(
            f"datafeed count mismatch: declared {count}, received {len(candles)}"
        )

    response_flags = _string_tuple(payload.get("quality_flags"), "quality_flags")
    bars: list[Bar] = []
    previous_timestamp: datetime | None = None
    for index, row in enumerate(candles):
        if not isinstance(row, dict):
            raise DatafeedContractError(f"candle {index} must be an object")
        timestamp = _required_text(row, "timestamp", prefix=f"candle {index} ")
        parsed_timestamp = _parse_timestamp(timestamp, timeframe, index)
        if previous_timestamp is not None and parsed_timestamp <= previous_timestamp:
            raise DatafeedContractError(
                "candle timestamps must be strictly chronological and unique"
            )
        previous_timestamp = parsed_timestamp

        open_price = _finite_number(row.get("open"), f"candle {index} open")
        high = _finite_number(row.get("high"), f"candle {index} high")
        low = _finite_number(row.get("low"), f"candle {index} low")
        close = _finite_number(row.get("close"), f"candle {index} close")
        volume = _finite_number(row.get("volume", 0), f"candle {index} volume")
        for label, value in (
            ("open", open_price),
            ("high", high),
            ("low", low),
            ("close", close),
        ):
            if value <= 0:
                raise DatafeedContractError(
                    f"candle {index} {label} must be positive"
                )
        if high < max(open_price, close):
            raise DatafeedContractError(
                f"candle {index} high is below open or close"
            )
        if low > min(open_price, close):
            raise DatafeedContractError(
                f"candle {index} low is above open or close"
            )
        if high < low:
            raise DatafeedContractError(f"candle {index} high is below low")
        if volume < 0:
            raise DatafeedContractError(f"candle {index} volume must be non-negative")

        row_provider = str(row.get("provider") or provider).strip()
        if row_provider != provider:
            raise DatafeedContractError(
                f"candle {index} provider mismatch: {row_provider} != {provider}"
            )
        row_flags = _string_tuple(
            row.get("quality_flags", ()),
            f"candle {index} quality_flags",
        )
        bars.append(
            Bar(
                symbol=instrument_id,
                timeframe=timeframe,
                timestamp=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                provider=provider,
                quality_flags=list(_dedupe((*response_flags, *row_flags))),
            )
        )

    latest_timestamp = _optional_text(
        payload.get("latest_timestamp"),
        "latest_timestamp",
    )
    if latest_timestamp is not None:
        parsed_latest_timestamp = _parse_timestamp(
            latest_timestamp,
            timeframe,
            "latest",
        )
        if bars and parsed_latest_timestamp != previous_timestamp:
            raise DatafeedContractError(
                "datafeed latest_timestamp does not match the final candle"
            )

    fresh = payload.get("fresh")
    if fresh is not None and not isinstance(fresh, bool):
        raise DatafeedContractError("fresh must be true, false, or null")

    attempted_sources = _string_tuple(
        payload.get("attempted_sources"),
        "attempted_sources",
        require_non_empty=True,
    )
    if selected_source not in attempted_sources:
        raise DatafeedContractError(
            "datafeed selected_source is missing from attempted_sources"
        )
    cache_policy = _enum_text(
        payload,
        "cache_policy",
        {"allow", "bypass", "require"},
    )
    quality_policy = _enum_text(
        payload,
        "quality_policy",
        {"standard", "strict"},
    )
    fallback_policy = _enum_text(
        payload,
        "fallback_policy",
        {"none", "explicit"},
    )
    if fallback_policy != "none":
        raise DatafeedContractError(
            "datafeed fallback_policy must be none for an exact-source envelope"
        )
    served_from = _enum_text(
        payload,
        "served_from",
        {"cache", "upstream", "websocket"},
    )

    age_seconds = _optional_non_negative_number(
        payload.get("age_seconds"),
        "age_seconds",
    )
    max_age_seconds = _optional_non_negative_number(
        payload.get("max_age_seconds"),
        "max_age_seconds",
    )
    (
        continuous_market,
        market_open,
        session_status,
        session_checked_at,
        current_session_end,
    ) = _session_contract(
        payload,
        upstream_schema=upstream_schema,
        timeframe=timeframe,
        fresh=fresh,
        max_age_seconds=max_age_seconds,
    )

    return MarketDataEnvelope(
        schema_version=MARKET_DATA_ENVELOPE_SCHEMA,
        upstream_schema_version=upstream_schema,
        instrument_id=instrument_id,
        provider_symbol=_required_text(payload, "provider_symbol"),
        asset_class=asset_class,
        timeframe=timeframe,
        provider=provider,
        source_mode=source_mode,
        requested_source=requested_source,
        selected_source=selected_source,
        selection_reason=_required_text(payload, "selection_reason"),
        attempted_sources=attempted_sources,
        cache_policy=cache_policy,
        quality_policy=quality_policy,
        fallback_policy=fallback_policy,
        require_execution_venue=response_requires_venue,
        quality_flags=response_flags,
        is_synthetic=is_synthetic,
        served_from=served_from,
        fresh=fresh,
        latest_timestamp=latest_timestamp,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        execution_venue=execution_venue,
        continuous_market=continuous_market,
        market_open=market_open,
        session_status=session_status,
        session_checked_at=session_checked_at,
        current_session_end=current_session_end,
        reject_reason=reject_reason,
        access_issues=_string_tuple(payload.get("access_issues"), "access_issues"),
        bars=tuple(bars),
    )


def _required_text(payload: dict[str, Any], field: str, *, prefix: str = "") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DatafeedContractError(f"{prefix}{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DatafeedContractError(f"{field} must be a string or null")
    return value.strip() or None


def _required_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise DatafeedContractError(f"{field} must be a boolean")
    return value


def _optional_bool(payload: dict[str, Any], field: str) -> bool | None:
    if field not in payload or payload[field] is None:
        return None
    value = payload[field]
    if not isinstance(value, bool):
        raise DatafeedContractError(f"{field} must be true, false, or null")
    return value


def _session_contract(
    payload: dict[str, Any],
    *,
    upstream_schema: str,
    timeframe: str,
    fresh: bool | None,
    max_age_seconds: float | None,
) -> tuple[bool, bool | None, str, str | None, str | None]:
    if upstream_schema == "kline-candles-v1":
        continuous_market = fresh is not None and max_age_seconds is not None
        return (
            continuous_market,
            True if continuous_market else None,
            "continuous" if continuous_market else "unknown",
            None,
            None,
        )

    continuous_market = _required_bool(payload, "continuous_market")
    if "market_open" not in payload:
        raise DatafeedContractError("market_open is required for kline-candles-v2")
    market_open = _optional_bool(payload, "market_open")
    session_status = _enum_text(
        payload,
        "session_status",
        {"continuous", "open", "closed", "unknown"},
    )
    session_checked_at = _required_text(payload, "session_checked_at")
    parsed_session_checked_at = _parse_timestamp(
        session_checked_at,
        timeframe,
        "session_checked_at",
    )
    current_session_end = _optional_text(
        payload.get("current_session_end"),
        "current_session_end",
    )
    parsed_session_end = None
    if current_session_end is not None:
        parsed_session_end = _parse_timestamp(
            current_session_end,
            timeframe,
            "current_session_end",
        )

    if continuous_market:
        if market_open is not True or session_status != "continuous":
            raise DatafeedContractError(
                "continuous market requires market_open=true and session_status=continuous"
            )
        if current_session_end is not None:
            raise DatafeedContractError(
                "continuous market must not declare current_session_end"
            )
    elif session_status == "open":
        if market_open is not True or current_session_end is None:
            raise DatafeedContractError(
                "open session requires market_open=true and current_session_end"
            )
        if fresh is None or max_age_seconds is None:
            raise DatafeedContractError(
                "open session requires an explicit freshness window"
            )
        if parsed_session_end is None or parsed_session_end <= parsed_session_checked_at:
            raise DatafeedContractError(
                "open session current_session_end must follow session_checked_at"
            )
    elif session_status == "closed":
        if (
            market_open is not False
            or fresh is not None
            or max_age_seconds is not None
            or current_session_end is not None
        ):
            raise DatafeedContractError(
                "closed session requires market_open=false, fresh=null, "
                "max_age_seconds=null, and current_session_end=null"
            )
    elif session_status == "unknown":
        if (
            market_open is not None
            or fresh is not None
            or max_age_seconds is not None
            or current_session_end is not None
        ):
            raise DatafeedContractError(
                "unknown session requires market_open=null, fresh=null, "
                "max_age_seconds=null, and current_session_end=null"
            )
    else:
        raise DatafeedContractError(
            "a sessioned market cannot use session_status=continuous"
        )
    return (
        continuous_market,
        market_open,
        session_status,
        session_checked_at,
        current_session_end,
    )


def _enum_text(
    payload: dict[str, Any],
    field: str,
    allowed: set[str],
) -> str:
    value = _required_text(payload, field)
    if value not in allowed:
        raise DatafeedContractError(
            f"{field} must be one of {', '.join(sorted(allowed))}"
        )
    return value


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatafeedContractError(f"{field} must be a non-negative integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise DatafeedContractError(f"{label} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DatafeedContractError(f"{label} must be finite") from exc
    if not math.isfinite(parsed):
        raise DatafeedContractError(f"{label} must be finite")
    return parsed


def _optional_non_negative_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    parsed = _finite_number(value, label)
    if parsed < 0:
        raise DatafeedContractError(f"{label} must be non-negative")
    return parsed


def _string_tuple(
    value: Any,
    label: str,
    *,
    require_non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise DatafeedContractError(f"{label} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DatafeedContractError(f"{label} must contain non-empty strings")
        result.append(item.strip())
    if require_non_empty and not result:
        raise DatafeedContractError(f"{label} must not be empty")
    return _dedupe(tuple(result))


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _parse_timestamp(value: str, timeframe: str, index: int | str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatafeedContractError(
            f"candle {index} timestamp must be ISO 8601"
        ) from exc
    intraday = str(timeframe).lower().endswith(("m", "h"))
    if parsed.tzinfo is None:
        if intraday:
            raise DatafeedContractError(
                f"candle {index} timestamp must include a timezone"
            )
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
