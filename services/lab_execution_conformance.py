"""Fail-closed bridge from Lab research evidence to executable candidate truth."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.dualtrack_nautilus_parity_contract import FIXTURE_CLASSES, PLATFORM_PARITY_SCHEMA
from services.execution_conformance import CANDIDATE_RECEIPT_SCHEMA, candidate_receipt_blockers
from services.journal_store import load_json


LAB_EXECUTION_CONFORMANCE_TOKEN_SCHEMA = "lab-execution-conformance-token-v1"


def build_lab_execution_conformance_token(
    *,
    entry: dict[str, Any],
    candidate_receipt: dict[str, Any],
    platform_parity: dict[str, Any],
) -> dict[str, Any]:
    """Verify both evidence layers and return a content-addressed gate token."""

    blockers: list[str] = []
    candidate = entry.get("execution_candidate") if isinstance(entry.get("execution_candidate"), dict) else {}
    expected_scenario_id = str(candidate.get("scenario_id") or "")
    expected_candidate_id = str(candidate.get("candidate_id") or "")
    if not expected_scenario_id or not expected_candidate_id:
        blockers.append("execution_candidate_identity_missing")

    if not candidate_receipt:
        blockers.append("candidate_execution_receipt_missing")
    else:
        blockers.extend(candidate_receipt_blockers(
            candidate_receipt,
            expected_scenario_id=expected_scenario_id or None,
        ))
        if (
            candidate_receipt.get("schema_version") == CANDIDATE_RECEIPT_SCHEMA
            and expected_candidate_id
            and str(candidate_receipt.get("candidate_id") or "") != expected_candidate_id
        ):
            blockers.append("candidate_receipt_candidate_mismatch")

    blockers.extend(_platform_parity_blockers(platform_parity))
    payload = {
        "schema_version": LAB_EXECUTION_CONFORMANCE_TOKEN_SCHEMA,
        "status": "pass" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "source_exp_id": str(entry.get("exp_id") or ""),
        "strategy_id": str((entry.get("strategy_ref") or {}).get("strategy_id") or ""),
        "execution_candidate": {
            "scenario_id": expected_scenario_id,
            "candidate_id": expected_candidate_id,
        },
        "candidate_receipt_id": str(candidate_receipt.get("receipt_id") or ""),
        "candidate_receipt_hash": _hash(candidate_receipt),
        "platform_parity_hash": _hash(platform_parity),
        "contracts": dict(candidate_receipt.get("contracts") or {}),
        "input_hashes": dict(candidate_receipt.get("input_hashes") or {}),
        "safety": {
            "paper_only": True,
            "auto_apply": False,
            "real_money_eligible": False,
        },
    }
    payload["token_id"] = f"lab-conformance-{_raw_digest(payload)}"
    return payload


def load_lab_execution_conformance(output_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Read the canonical candidate receipt and current platform parity artifact."""

    candidate = entry.get("execution_candidate") if isinstance(entry.get("execution_candidate"), dict) else {}
    scenario_id = str(candidate.get("scenario_id") or "")
    candidate_rows = (
        load_json(
            Path(output_root)
            / "dualtrack"
            / "strategy_shadows"
            / "receipts"
            / f"{scenario_id}.json"
        )
        if scenario_id
        else []
    )
    parity_rows = load_json(
        Path(output_root) / "dualtrack" / "nautilus" / "parity" / "current.json"
    )
    candidate_receipt = candidate_rows[-1] if candidate_rows and isinstance(candidate_rows[-1], dict) else {}
    platform_parity = parity_rows[-1] if parity_rows and isinstance(parity_rows[-1], dict) else {}
    return build_lab_execution_conformance_token(
        entry=entry,
        candidate_receipt=candidate_receipt,
        platform_parity=platform_parity,
    )


def lab_execution_conformance_token_blockers(
    token: dict[str, Any],
    *,
    entry: dict[str, Any],
) -> list[str]:
    """Validate a token at the promotion call boundary; absence never passes."""

    if not isinstance(token, dict) or token.get("schema_version") != LAB_EXECUTION_CONFORMANCE_TOKEN_SCHEMA:
        return ["nautilus_execution_conformance_missing"]
    blockers = [str(item) for item in token.get("blockers") or [] if str(item)]
    if token.get("status") != "pass":
        blockers.append("nautilus_execution_conformance_not_pass")
    if str(token.get("source_exp_id") or "") != str(entry.get("exp_id") or ""):
        blockers.append("lab_execution_conformance_experiment_mismatch")
    expected = entry.get("execution_candidate") if isinstance(entry.get("execution_candidate"), dict) else {}
    if dict(token.get("execution_candidate") or {}) != {
        "scenario_id": str(expected.get("scenario_id") or ""),
        "candidate_id": str(expected.get("candidate_id") or ""),
    }:
        blockers.append("lab_execution_conformance_candidate_mismatch")
    if token.get("safety") != {
        "paper_only": True,
        "auto_apply": False,
        "real_money_eligible": False,
    }:
        blockers.append("lab_execution_conformance_safety_mismatch")
    expected_id = f"lab-conformance-{_raw_digest({key: value for key, value in token.items() if key != 'token_id'})}"
    if str(token.get("token_id") or "") != expected_id:
        blockers.append("lab_execution_conformance_integrity_mismatch")
    return sorted(set(blockers))


def _platform_parity_blockers(parity: dict[str, Any]) -> list[str]:
    if not parity:
        return ["platform_parity_missing"]
    if parity.get("schema_version") != PLATFORM_PARITY_SCHEMA:
        return ["unsupported_platform_parity_schema"]
    blockers: list[str] = []
    if parity.get("status") != "pass" or parity.get("blockers"):
        blockers.append("platform_parity_not_pass")
    if parity.get("scope") != "paper_shadow_only":
        blockers.append("platform_parity_scope_mismatch")
    if parity.get("real_money_eligible") is not False:
        blockers.append("platform_parity_real_money_flag_invalid")
    rows = parity.get("classes") if isinstance(parity.get("classes"), list) else []
    by_name = {
        str(row.get("class") or ""): row
        for row in rows
        if isinstance(row, dict) and str(row.get("class") or "")
    }
    if set(by_name) != set(FIXTURE_CLASSES):
        blockers.append("platform_parity_fixture_set_mismatch")
    if any(str(row.get("status") or "") != "pass" for row in by_name.values()):
        blockers.append("platform_parity_fixture_not_pass")
    return blockers


def _hash(value: Any) -> str:
    return f"sha256:{_raw_digest(value)}"


def _raw_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
