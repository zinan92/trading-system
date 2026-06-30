from __future__ import annotations


OFFICIAL_TRUTH_LEVELS = {"official", "official_broker"}
EXECUTION_VENUE_TRUTH_LEVELS = {"execution_venue"}


def official_rows(payload: dict) -> int:
    return _safe_int(payload.get("official_rows"))


def execution_venue_rows(payload: dict) -> int:
    return _safe_int(payload.get("execution_venue_rows"))


def is_execution_grade_ohlc_ready(payload: dict) -> bool:
    rows = official_rows(payload)
    venue_rows = execution_venue_rows(payload)
    provider = str(payload.get("latest_provider") or payload.get("provider") or "")
    official_providers = set(payload.get("official_broker_providers") or payload.get("official_providers") or [])
    execution_venue_providers = set(payload.get("execution_venue_providers") or [])
    live_data_mode = str(payload.get("live_data_mode") or "")
    truth_level = str(payload.get("truth_level") or payload.get("latest_truth_level") or "")
    provider_is_official = bool(provider and provider in official_providers)
    provider_is_execution_venue = bool(provider and provider in execution_venue_providers)
    source_is_official = live_data_mode == "official_broker" or truth_level in OFFICIAL_TRUTH_LEVELS or provider_is_official
    source_is_execution_venue = live_data_mode == "execution_venue" or truth_level in EXECUTION_VENUE_TRUTH_LEVELS or provider_is_execution_venue
    return bool(payload.get("ready_for_live")) and ((rows > 0 and source_is_official) or (venue_rows > 0 and source_is_execution_venue))


def is_official_broker_ohlc_ready(payload: dict) -> bool:
    return is_execution_grade_ohlc_ready(payload)


def official_broker_ohlc_status(payload: dict) -> tuple[bool, str]:
    if is_execution_grade_ohlc_ready(payload):
        if execution_venue_rows(payload) > 0 or payload.get("live_data_mode") == "execution_venue":
            return True, "GOLD/XAUUSD execution venue 5m OHLC is ready for trading checks."
        return True, "GOLD/XAUUSD broker 5m OHLC is ready for trading checks."
    if execution_venue_rows(payload) > 0 or payload.get("live_data_mode") == "execution_venue":
        return False, "Execution venue data exists, but the latest GOLD/XAUUSD 5m OHLC is not live-ready."
    if payload.get("ready_for_paper"):
        return False, "Only paper-ready public/local data is active; trading checks require execution-grade GOLD/XAUUSD 5m OHLC."
    return False, "GOLD/XAUUSD execution-grade 5m OHLC is not ready."


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
