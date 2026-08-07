"""Canonical, immutable inputs for one Paper start candidate.

The record deliberately separates the full planning-market digest from the
smaller execution authority used immediately before ``prepare_start``.  AI
and sizing may consume the full trusted market bundle, while the control plane
can prove that the executable mark, Paper account, execution contract, Park
policy, and deployed source are still the exact authority that built the
candidate.  A changed authority creates a new record; an old digest is never
relabelled as current.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import load_json
from services.paper_supervisor_recovery import authoritative_paper_equity
from services.risk_port import canonical_market_risk_state


START_FACTS_SCHEMA_VERSION = "paper-start-facts-v1"
_CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = frozenset(
    {
        "schema_version",
        "cycle_id",
        "observed_at",
        "market",
        "execution",
        "execution_contract",
        "outer_policy",
        "source",
        "execution_authority_digest",
        "start_facts_digest",
    }
)


class PaperStartFactsError(ValueError):
    """Stable fail-closed error for malformed or conflicting start facts."""


class PaperStartFactsStore:
    """Append and verify content-addressed StartFacts per 12-hour cycle."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "start_facts"
        )

    def record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        candidate = validate_start_facts(record)
        path = self._path(candidate["cycle_id"])
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rows = self.records(candidate["cycle_id"])
            for existing in rows:
                if (
                    existing["start_facts_digest"]
                    != candidate["start_facts_digest"]
                ):
                    continue
                if existing != candidate:
                    raise PaperStartFactsError(
                        "paper_start_facts_identity_conflict"
                    )
                return existing
            _atomic_write_json_fsync(path, [*rows, candidate])
            return candidate

    def records(self, cycle_id: str) -> list[dict[str, Any]]:
        cycle = _cycle_id(cycle_id)
        path = self._path(cycle)
        if self.root.exists() and (
            not self.root.is_dir() or self.root.is_symlink()
        ):
            raise PaperStartFactsError("paper_start_facts_store_corrupt")
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise PaperStartFactsError("paper_start_facts_store_corrupt")
        try:
            rows = load_json(path)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PaperStartFactsError(
                "paper_start_facts_store_corrupt"
            ) from exc
        if not isinstance(rows, list):
            raise PaperStartFactsError("paper_start_facts_store_corrupt")
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in rows:
            if not isinstance(value, Mapping):
                raise PaperStartFactsError(
                    "paper_start_facts_store_corrupt"
                )
            row = validate_start_facts(value)
            identity = row["start_facts_digest"]
            if identity in seen:
                raise PaperStartFactsError(
                    "paper_start_facts_store_corrupt"
                )
            seen.add(identity)
            validated.append(row)
        return validated

    def require(
        self,
        cycle_id: str,
        start_facts_digest: str,
    ) -> dict[str, Any]:
        digest = _required_digest(start_facts_digest)
        matches = [
            row
            for row in self.records(cycle_id)
            if row["start_facts_digest"] == digest
        ]
        if len(matches) != 1:
            raise PaperStartFactsError("paper_start_facts_missing")
        return matches[0]

    def _path(self, cycle_id: str) -> Path:
        return self.root / f"{_cycle_id(cycle_id)}.json"


def build_start_facts(
    *,
    cycle_id: str,
    observed_at: str,
    market: Mapping[str, Any],
    execution_snapshot: Mapping[str, Any],
    execution_adapter_name: str,
    execution_contract: Mapping[str, Any],
    outer_policy_preflight: Mapping[str, Any],
    source_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one exact authority record from already-trusted server inputs."""

    cycle = _cycle_id(cycle_id)
    timestamp = _timestamp(observed_at)
    market_state = canonical_market_risk_state(market)
    if (
        market_state.get("status") not in {"ready", "derived"}
        or market_state.get("fresh") is not True
        or market_state.get("is_synthetic") is not False
        or not market_state.get("provider")
        or not market_state.get("source_mode")
        or not market_state.get("symbol")
        or not market_state.get("timeframe")
        or not market_state.get("price")
        or not market_state.get("latest_timestamp")
    ):
        raise PaperStartFactsError("trusted_market_provenance_invalid")

    equity = authoritative_paper_equity(execution_snapshot)
    account = (
        dict(execution_snapshot.get("account") or {})
        if isinstance(execution_snapshot.get("account"), Mapping)
        else {}
    )
    account_authority = {
        "equity": equity,
        "ending_cash": _optional_number(account.get("ending_cash")),
        "starting_cash": _optional_number(account.get("starting_cash")),
    }
    accepted_order_ids = sorted(
        str(row.get("order_id") or "")
        for row in execution_snapshot.get("orders") or []
        if isinstance(row, Mapping)
        and str(row.get("state") or "").lower() == "accepted"
        and str(row.get("order_id") or "")
    )
    open_position_ids = sorted(
        str(row.get("position_id") or row.get("trade_id") or "")
        for row in execution_snapshot.get("positions") or []
        if isinstance(row, Mapping)
        and str(row.get("status") or "").lower() == "open"
        and str(row.get("position_id") or row.get("trade_id") or "")
    )
    execution_authority = {
        "adapter_name": _required_text(execution_adapter_name),
        "account": account_authority,
        "accepted_order_ids": accepted_order_ids,
        "open_position_ids": open_position_ids,
    }
    contract = _json_mapping(execution_contract)
    policy = _outer_policy_reference(outer_policy_preflight)
    source = _source_reference(source_attestation)
    planning_market = {
        "execution": market_state,
        "planning_input_digest": _digest(_json_mapping(market)),
    }
    contract_record = {
        "value": contract,
        "digest": _digest(contract),
    }
    execution_record = {
        **execution_authority,
        "snapshot_digest": _digest(_json_mapping(execution_snapshot)),
        "authority_digest": _digest(execution_authority),
    }
    authority_digest = _digest(
        {
            "cycle_id": cycle,
            "market": market_state,
            "execution": execution_authority,
            "execution_contract_digest": contract_record["digest"],
            "outer_policy": policy,
            "source": source,
        }
    )
    record: dict[str, Any] = {
        "schema_version": START_FACTS_SCHEMA_VERSION,
        "cycle_id": cycle,
        "observed_at": timestamp,
        "market": planning_market,
        "execution": execution_record,
        "execution_contract": contract_record,
        "outer_policy": policy,
        "source": source,
        "execution_authority_digest": authority_digest,
    }
    record["start_facts_digest"] = _digest(record)
    return validate_start_facts(record)


def validate_start_facts(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a detached JSON copy only when every identity is exact."""

    try:
        row = json.loads(
            json.dumps(dict(value), sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise PaperStartFactsError("paper_start_facts_invalid") from exc
    if (
        set(row) != _FIELDS
        or row.get("schema_version") != START_FACTS_SCHEMA_VERSION
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    _cycle_id(row.get("cycle_id"))
    _timestamp(row.get("observed_at"))
    supplied = _required_digest(row.get("start_facts_digest"))
    expected = _digest(
        {
            key: item
            for key, item in row.items()
            if key != "start_facts_digest"
        }
    )
    if not hmac.compare_digest(supplied, expected):
        raise PaperStartFactsError("paper_start_facts_invalid")

    market = _required_mapping(row.get("market"))
    if set(market) != {"execution", "planning_input_digest"}:
        raise PaperStartFactsError("paper_start_facts_invalid")
    execution_market = _required_mapping(market.get("execution"))
    if set(execution_market) != {
        "status",
        "fresh",
        "is_synthetic",
        "provider",
        "source_mode",
        "symbol",
        "timeframe",
        "price",
        "latest_timestamp",
        "batch_id",
        "trust_status",
    }:
        raise PaperStartFactsError("paper_start_facts_invalid")
    if (
        execution_market.get("status") not in {"ready", "derived"}
        or execution_market.get("fresh") is not True
        or execution_market.get("is_synthetic") is not False
        or not execution_market.get("price")
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    _required_digest(market.get("planning_input_digest"))

    execution = _required_mapping(row.get("execution"))
    if set(execution) != {
        "adapter_name",
        "account",
        "accepted_order_ids",
        "open_position_ids",
        "snapshot_digest",
        "authority_digest",
    }:
        raise PaperStartFactsError("paper_start_facts_invalid")
    _required_text(execution.get("adapter_name"))
    account = _required_mapping(execution.get("account"))
    if set(account) != {"equity", "ending_cash", "starting_cash"}:
        raise PaperStartFactsError("paper_start_facts_invalid")
    if (
        isinstance(account.get("equity"), bool)
        or not isinstance(account.get("equity"), (int, float))
        or float(account["equity"]) <= 0
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    for field in ("accepted_order_ids", "open_position_ids"):
        rows = execution.get(field)
        if (
            not isinstance(rows, list)
            or any(not isinstance(item, str) or not item for item in rows)
            or rows != sorted(set(rows))
        ):
            raise PaperStartFactsError("paper_start_facts_invalid")
    _required_digest(execution.get("snapshot_digest"))
    authority = {
        key: execution[key]
        for key in (
            "adapter_name",
            "account",
            "accepted_order_ids",
            "open_position_ids",
        )
    }
    if execution.get("authority_digest") != _digest(authority):
        raise PaperStartFactsError("paper_start_facts_invalid")

    contract = _required_mapping(row.get("execution_contract"))
    if set(contract) != {"value", "digest"}:
        raise PaperStartFactsError("paper_start_facts_invalid")
    if contract.get("digest") != _digest(_required_mapping(contract["value"])):
        raise PaperStartFactsError("paper_start_facts_invalid")
    policy = _validate_outer_policy_reference(row.get("outer_policy"))
    source = _validate_source_reference(row.get("source"))
    expected_authority = _digest(
        {
            "cycle_id": row["cycle_id"],
            "market": execution_market,
            "execution": authority,
            "execution_contract_digest": contract["digest"],
            "outer_policy": policy,
            "source": source,
        }
    )
    if row.get("execution_authority_digest") != expected_authority:
        raise PaperStartFactsError("paper_start_facts_invalid")
    return row


def same_execution_authority(
    bound: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    left = validate_start_facts(bound)
    right = validate_start_facts(current)
    return hmac.compare_digest(
        left["execution_authority_digest"],
        right["execution_authority_digest"],
    )


def authoritative_account_from_start_facts(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = validate_start_facts(value)
    account = dict(row["execution"]["account"])
    return {
        **account,
        "execution_account_source": "canonical_paper_start_facts",
        "start_facts_digest": row["start_facts_digest"],
    }


def _outer_policy_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    expected = {
        "binding_id",
        "binding_version",
        "binding_digest",
        "policy_id",
        "policy_version",
        "policy_digest",
        "policy_expires_at",
        "passed",
    }
    if not expected <= set(row) or row.get("passed") is not True:
        raise PaperStartFactsError("outer_strategy_policy_invalid")
    result = {key: row[key] for key in expected if key != "passed"}
    return _validate_outer_policy_reference(result)


def _validate_outer_policy_reference(value: Any) -> dict[str, Any]:
    row = _required_mapping(value)
    expected = {
        "binding_id",
        "binding_version",
        "binding_digest",
        "policy_id",
        "policy_version",
        "policy_digest",
        "policy_expires_at",
    }
    if set(row) != expected:
        raise PaperStartFactsError("paper_start_facts_invalid")
    _required_text(row.get("binding_id"))
    _required_text(row.get("policy_id"))
    if (
        isinstance(row.get("binding_version"), bool)
        or not isinstance(row.get("binding_version"), int)
        or row["binding_version"] <= 0
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    if (
        isinstance(row.get("policy_version"), bool)
        or not isinstance(row.get("policy_version"), int)
        or row["policy_version"] <= 0
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    _required_digest(row.get("binding_digest"))
    _required_digest(row.get("policy_digest"))
    _timestamp(row.get("policy_expires_at"))
    return row


def _source_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    return _validate_source_reference(
        {
            "source_sha": str(row.get("source_sha") or "").lower(),
            "source_tree_sha": str(
                row.get("source_tree_sha") or ""
            ).lower(),
            "tracked_tree_clean": row.get("tracked_tree_clean") is True,
        }
    )


def _validate_source_reference(value: Any) -> dict[str, Any]:
    row = _required_mapping(value)
    if set(row) != {
        "source_sha",
        "source_tree_sha",
        "tracked_tree_clean",
    }:
        raise PaperStartFactsError("paper_start_facts_invalid")
    if (
        not _SOURCE_SHA.fullmatch(str(row.get("source_sha") or ""))
        or not _SOURCE_SHA.fullmatch(
            str(row.get("source_tree_sha") or "")
        )
        or not isinstance(row.get("tracked_tree_clean"), bool)
    ):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return row


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = json.loads(
            json.dumps(dict(value), sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise PaperStartFactsError("paper_start_facts_invalid") from exc
    if not isinstance(result, dict):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return result


def _required_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return dict(value)


def _cycle_id(value: Any) -> str:
    text = str(value or "")
    if not _CYCLE_ID.fullmatch(text):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return text


def _timestamp(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperStartFactsError("paper_start_facts_invalid") from exc
    if parsed.tzinfo is None:
        raise PaperStartFactsError("paper_start_facts_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _required_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise PaperStartFactsError("paper_start_facts_invalid")
    return text


def _required_digest(value: Any) -> str:
    text = str(value or "")
    if not _DIGEST.fullmatch(text):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return text


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PaperStartFactsError("paper_start_facts_invalid") from exc
    if not (float("-inf") < parsed < float("inf")):
        raise PaperStartFactsError("paper_start_facts_invalid")
    return parsed


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_write_json_fsync(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        rows,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
