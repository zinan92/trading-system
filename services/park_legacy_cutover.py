"""Durable, exact-set legacy Paper order cutover authority.

The normal Park strategy lifecycle starts only from a clean slate.  This
module is the one explicit bridge for the operator's initial migration from
legacy accepted Paper entries.  It records the exact order set first, binds a
bounded confirmation to that set, and exposes a cutover approval only after a
successful exact cancellation receipt.  It never flattens positions or
creates a replacement strategy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PARK_LEGACY_CUTOVER_SCHEMA = "park-legacy-cutover-v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.IGNORECASE)


class ParkLegacyCutoverError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ParkLegacyCutoverError("journal_corrupt", "legacy cutover row is not an object")
        rows.append(value)
    return rows


def _order_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-secret identity/risk fields in the durable proposal."""

    order_id = str(row.get("order_id") or "").strip()
    cycle_id = str(row.get("cycle_id") or "").strip()
    if not order_id or not cycle_id:
        raise ParkLegacyCutoverError("legacy_order_identity_missing", "legacy order identity is incomplete")
    return {
        "order_id": order_id,
        "cycle_id": cycle_id,
        "strategy_plan_id": str(row.get("strategy_plan_id") or ""),
        "state": str(row.get("state") or "").lower(),
        "side": str(row.get("side") or "").lower(),
        "price": row.get("price"),
        "quantity": row.get("quantity"),
        "engine": str(row.get("engine") or ""),
    }


def normalize_order_identities(orders: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    identities = [_order_identity(row) for row in orders]
    identities.sort(key=lambda row: (str(row["cycle_id"]), str(row["order_id"])))
    ids = [str(row["order_id"]) for row in identities]
    if len(ids) != len(set(ids)):
        raise ParkLegacyCutoverError("legacy_order_identity_duplicate", "legacy order identity is duplicated")
    if any(row["state"] != "accepted" for row in identities):
        raise ParkLegacyCutoverError("legacy_order_not_accepted", "cutover may target accepted orders only")
    return identities


def parse_legacy_cutover_digest(text: str) -> str | None:
    """Extract a digest from an explicit legacy cutover confirmation."""

    source = str(text or "").strip()
    lowered = source.lower()
    if not (lowered.startswith("confirm") or source.startswith("确认")):
        return None
    match = _DIGEST.search(source)
    return match.group(0).lower() if match else None


class ParkLegacyCutoverLedger:
    def __init__(self, output_root: Path, *, park_user_id: str, chat_id: str) -> None:
        self.path = Path(output_root) / "park_strategy" / "legacy_cutover.jsonl"
        self.park_user_id = str(park_user_id or "").strip()
        self.chat_id = str(chat_id or "").strip()
        if not self.park_user_id or not self.chat_id:
            raise ParkLegacyCutoverError("identity_missing", "Park user and chat are required")

    def rows(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def _latest_for(self, proposal_id: str, events: set[str] | None = None) -> dict[str, Any] | None:
        allowed = events or {"proposal", "confirmed", "completed", "blocked"}
        return next(
            (
                dict(row)
                for row in reversed(self.rows())
                if str(row.get("proposal_id") or "") == proposal_id
                and str(row.get("event") or "") in allowed
            ),
            None,
        )

    def pending(self) -> list[dict[str, Any]]:
        proposals: dict[str, dict[str, Any]] = {}
        latest: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            proposal_id = str(row.get("proposal_id") or "")
            if proposal_id:
                if row.get("event") == "proposal":
                    proposals[proposal_id] = dict(row)
                latest[proposal_id] = dict(row)
        return [
            {**proposals.get(proposal_id, {}), **row}
            for proposal_id, row in reversed(list(latest.items()))
            if (
                row.get("event") in {"proposal", "confirmed"}
                or (row.get("event") == "blocked" and row.get("retryable") is True)
            )
            and float(proposals.get(proposal_id, row).get("expires_at") or 0) > time.time()
        ]

    def confirmed_pending(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self.pending()
            if row.get("event") == "confirmed"
            or (row.get("event") == "blocked" and row.get("retryable") is True)
        ]

    def proposal_for_digest(self, digest: str) -> dict[str, Any] | None:
        expected = str(digest or "").lower()
        return next(
            (
                dict(row)
                for row in self.rows()
                if row.get("event") == "proposal"
                and str(row.get("proposal_digest") or "").lower() == expected
            ),
            None,
        )

    def create_proposal(
        self,
        *,
        update_id: int | None,
        source_text_digest: str,
        expected_order_count: int,
        orders: list[Mapping[str, Any]],
        positions: list[Mapping[str, Any]],
        reconciliation: Mapping[str, Any],
    ) -> dict[str, Any]:
        identities = normalize_order_identities(orders)
        if expected_order_count != len(identities):
            raise ParkLegacyCutoverError(
                "legacy_order_count_mismatch",
                f"requested {expected_order_count} old orders but authoritative snapshot has {len(identities)}",
            )
        position_ids = sorted(
            str(row.get("position_id") or row.get("trade_id") or "")
            for row in positions
        )
        if any(not value for value in position_ids):
            raise ParkLegacyCutoverError("legacy_position_identity_missing", "open position identity is incomplete")
        if position_ids:
            raise ParkLegacyCutoverError("legacy_positions_present", "clean slate cutover cannot flatten positions")
        if str(reconciliation.get("status") or "") != "ok" or reconciliation.get("issues"):
            raise ParkLegacyCutoverError("legacy_reconciliation_unhealthy", "legacy Paper reconciliation is not healthy")
        body = {
            "expected_order_count": int(expected_order_count),
            "orders": identities,
            "positions": position_ids,
            "reconciliation_digest": _digest(dict(reconciliation)),
            "source_text_digest": str(source_text_digest),
            "requested_action": "cancel_exact_legacy_orders_enable_park_paper",
            "park_user_id": self.park_user_id,
            "chat_id": self.chat_id,
        }
        proposal_digest = _digest(body)
        proposal_id = "legacy-cutover-" + proposal_digest.removeprefix("sha256:")[:24]
        existing = self._latest_for(proposal_id)
        if existing and existing.get("event") in {"proposal", "confirmed"}:
            try:
                still_valid = float(existing.get("expires_at") or 0) > time.time()
            except (TypeError, ValueError):
                still_valid = False
            if still_valid:
                return existing
        if existing and existing.get("event") == "completed":
            original = next(
                (
                    dict(row)
                    for row in self.rows()
                    if row.get("event") == "proposal" and row.get("proposal_id") == proposal_id
                ),
                None,
            )
            return original or existing
        row = {
            "schema_version": PARK_LEGACY_CUTOVER_SCHEMA,
            "event": "proposal",
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "update_id": update_id,
            "created_at": time.time(),
            "expires_at": time.time() + 900,
            "execution_authorized": False,
            "clean_slate_verified": False,
            "enabled_park_paper": False,
            **body,
        }
        _append(self.path, row)
        return row

    def confirm(self, proposal: Mapping[str, Any], *, update_id: int | None, mode: str) -> dict[str, Any]:
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        proposal_digest = str(proposal.get("proposal_digest") or "").lower()
        if not proposal_id or not proposal_digest:
            raise ParkLegacyCutoverError("proposal_identity_missing", "legacy cutover proposal identity is incomplete")
        existing = self._latest_for(proposal_id)
        if existing and existing.get("event") in {"confirmed", "completed"}:
            return existing
        if float(proposal.get("expires_at") or 0) <= time.time():
            raise ParkLegacyCutoverError("legacy_cutover_expired", "legacy cutover proposal has expired")
        row = {
            "schema_version": PARK_LEGACY_CUTOVER_SCHEMA,
            "event": "confirmed",
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "update_id": update_id,
            "confirmed_at": time.time(),
            "confirmation_mode": str(mode),
            "execution_authorized": True,
            "clean_slate_verified": False,
            "enabled_park_paper": False,
            "next_action": "revalidate_exact_order_set_and_cancel",
        }
        _append(self.path, row)
        return row

    def record(self, *, proposal: Mapping[str, Any], event: str, **payload: Any) -> dict[str, Any]:
        if event not in {"completed", "blocked"}:
            raise ParkLegacyCutoverError("invalid_cutover_event", "legacy cutover event is unsupported")
        row = {
            "schema_version": PARK_LEGACY_CUTOVER_SCHEMA,
            "event": event,
            "proposal_id": str(proposal.get("proposal_id") or ""),
            "proposal_digest": str(proposal.get("proposal_digest") or ""),
            "recorded_at": time.time(),
            "execution_authorized": event == "completed",
            "paper_only": True,
            **payload,
        }
        _append(self.path, row)
        return row


def load_effective_park_config(output_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Enable the Park runtime only after a durable clean-slate receipt."""

    settings = dict(config)
    if settings.get("feature_enabled") is True:
        return settings
    path = Path(output_root) / "park_strategy" / "legacy_cutover.jsonl"
    latest: dict[str, Any] | None = None
    for row in _rows(path):
        if row.get("event") in {"completed", "blocked"}:
            latest = dict(row)
    if latest and latest.get("event") == "completed" and latest.get("clean_slate_verified") is True and latest.get("enabled_park_paper") is True:
        settings["feature_enabled"] = True
        settings["_park_cutover_authorized"] = True
    return settings
