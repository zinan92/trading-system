"""Immutable, engine-neutral input bundles for DualTrack shadow execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services.dualtrack_execution_contract import canonical_market_event


SHADOW_INPUT_SCHEMA = "dualtrack-shadow-input-v1"


def build_shadow_input(
    *,
    cycle_id: str,
    authoritative_snapshot: dict[str, Any],
    market_events: list[dict[str, Any]],
    commands: list[dict[str, Any]] | None = None,
    execution_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one replayable input bundle without altering an execution ledger.

    The legacy snapshot is evidence of the authoritative outcome. The candidate
    engine receives only the copied command/fill evidence and canonical market
    event stream; no browser payload or derived chart series may be substituted.
    """

    if str(authoritative_snapshot.get("cycle_id") or "") != cycle_id:
        raise ValueError("authoritative snapshot cycle_id mismatch")
    normalized_events = [canonical_market_event(event) for event in market_events]
    if not normalized_events:
        raise ValueError("shadow input requires at least one canonical market event")
    if any(event["cycle_id"] != cycle_id for event in normalized_events):
        raise ValueError("shadow input event cycle_id mismatch")
    if any(not event.get("provider") for event in normalized_events):
        raise ValueError("shadow input event provider is required")
    if any(not event.get("instrument_id") for event in normalized_events):
        raise ValueError("shadow input event instrument_id is required")
    event_ids = [event["event_id"] for event in normalized_events]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("shadow input contains duplicate market event IDs")

    payload = {
        "schema_version": SHADOW_INPUT_SCHEMA,
        "cycle_id": cycle_id,
        "authoritative_engine": str(authoritative_snapshot.get("engine") or ""),
        # Raw fills are copied as immutable command/economics evidence. The
        # Nautilus runner must never read these files directly during replay.
        "legacy_fills": list(authoritative_snapshot.get("fills") or []),
        "commands": _normalized_commands(commands or [], cycle_id=cycle_id),
        "market_events": normalized_events,
    }
    if execution_settings is not None:
        capital = float(execution_settings.get("starting_cash") or 0.0)
        leverage = float(execution_settings.get("max_leverage") or 0.0)
        if capital <= 0 or leverage <= 0:
            raise ValueError("shadow execution settings require positive starting_cash and max_leverage")
        payload["execution_settings"] = {
            "starting_cash": capital,
            "max_leverage": leverage,
        }
    payload["input_id"] = _stable_id(payload)
    return payload


def _stable_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"shadow-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _normalized_commands(commands: list[dict[str, Any]], *, cycle_id: str) -> list[dict[str, Any]]:
    ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in commands:
        if not isinstance(row, dict):
            raise ValueError("shadow input command must be an object")
        command_id = str(row.get("command_id") or "")
        if not command_id:
            raise ValueError("shadow input command_id is required")
        if str(row.get("cycle_id") or "") != cycle_id:
            raise ValueError("shadow input command cycle_id mismatch")
        if command_id in ids:
            raise ValueError("shadow input contains duplicate command IDs")
        ids.add(command_id)
        rows.append(dict(row))
    return rows
