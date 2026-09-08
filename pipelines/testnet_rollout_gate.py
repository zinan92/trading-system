"""Read-only, source-bound Testnet rollout gate for M5-S4.

The gate consumes already-published JSON evidence.  It does not call a Broker,
write to the supplied output root, start a scheduler, or infer execution from
HTTP/UI/fixture state.  ``--write-report`` only writes the repository evidence
report requested by Issue #1147.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "testnet-rollout-gate-v1"
FAMILIES = ("dca", "grid")
WINDOW_COUNT = 2
MAX_AGE_HOURS = 24
LOCAL_OWNER_ID = "local-mac"
IDENTITY_FIELDS = (
    "trading_system_release_sha",
    "standard_broker_release_sha",
    "transport_profile",
    "account_fingerprint",
    "instrument_id",
    "capability_revision",
    "owner_epoch",
)
ALIASES = {
    "trading_system_release_sha": ("trading_system_release_sha", "release_sha"),
    "standard_broker_release_sha": ("standard_broker_release_sha", "broker_release_sha"),
    "transport_profile": ("transport_profile", "testnet_profile", "profile"),
    "account_fingerprint": ("account_fingerprint",),
    "instrument_id": ("instrument_id",),
    "capability_revision": ("capability_revision",),
    "owner_epoch": ("owner_epoch", "scheduler_owner_epoch"),
}


class RolloutGateError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _timestamp(value) if value else None
    return parsed or datetime.now(timezone.utc)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_files(root: Path) -> Iterable[Path]:
    if not root.exists() or not root.is_dir():
        return ()
    return (path for path in sorted(root.rglob("*.json")) if path.is_file() and not path.name.endswith(".lock"))


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _field(row: Mapping[str, Any], name: str) -> Any:
    for key in ALIASES[name]:
        if key in row and row[key] not in (None, ""):
            return row[key]
    identity = row.get("identity")
    if isinstance(identity, Mapping):
        for key in ALIASES[name]:
            if key in identity and identity[key] not in (None, ""):
                return identity[key]
    return None


def _family(row: Mapping[str, Any], path: Path) -> str | None:
    value = _text(row.get("strategy_family") or row.get("strategy_scope") or row.get("family")).lower()
    if value in FAMILIES:
        return value
    lowered = str(path).lower()
    return next((family for family in FAMILIES if family in lowered), None)


def _stage(row: Mapping[str, Any], path: Path) -> str | None:
    value = _text(row.get("milestone") or row.get("stage") or row.get("evidence_stage")).lower().replace("_", "-")
    if "m5-s2" in value or "attended" in value or "proof" in value:
        return "m5-s2"
    if "m5-s3" in value or "soak" in value or "window" in value or "readiness" in value:
        return "m5-s3"
    lowered = str(path).lower()
    if any(token in lowered for token in ("m5-s2", "attended", "proof")):
        return "m5-s2"
    if any(token in lowered for token in ("m5-s3", "soak", "readiness", "window")):
        return "m5-s3"
    return None


def _load(root: Path) -> tuple[list[tuple[Path, Mapping[str, Any]]], list[dict[str, str]]]:
    records: list[tuple[Path, Mapping[str, Any]]] = []
    blockers: list[dict[str, str]] = []
    for path in _json_files(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            blockers.append({"code": "evidence_unreadable", "path": str(path)})
            continue
        for row in _rows(payload):
            if _family(row, path) and _stage(row, path):
                records.append((path, row))
    return records, blockers


def _check_identity(row: Mapping[str, Any], expected: Mapping[str, Any]) -> list[dict[str, str]]:
    blockers = []
    for name in IDENTITY_FIELDS:
        actual = _field(row, name)
        wanted = expected.get(name)
        if actual in (None, ""):
            blockers.append({"code": f"{name}_missing"})
        elif wanted not in (None, "") and _text(actual) != _text(wanted):
            blockers.append({"code": f"{name}_mismatch", "expected": _text(wanted), "actual": _text(actual)})
    return blockers


def _check_record(path: Path, row: Mapping[str, Any], expected: Mapping[str, Any], now: datetime) -> list[dict[str, str]]:
    blockers = _check_identity(row, expected)
    if _text(row.get("environment")).lower() != "testnet":
        blockers.append({"code": "environment_not_testnet"})
    if _text(row.get("broker_id")).lower() not in {"hyperliquid", "hyperliquid-testnet"}:
        blockers.append({"code": "broker_not_hyperliquid"})
    if _text(row.get("status")).lower() not in {"pass", "ready", "complete", "verified"}:
        blockers.append({"code": "evidence_not_passing", "status": _text(row.get("status"))})
    observed = _timestamp(row.get("observed_at") or row.get("completed_at") or row.get("ended_at"))
    if observed is None:
        blockers.append({"code": "observed_at_missing_or_invalid"})
    elif (now - observed).total_seconds() > MAX_AGE_HOURS * 3600:
        blockers.append({"code": "evidence_stale", "observed_at": observed.isoformat()})
    elif observed > now:
        blockers.append({"code": "evidence_future_dated"})
    if row.get("execution_mutation") is True or row.get("network_operation_invoked") is True:
        blockers.append({"code": "execution_mutation_claimed"})
    if path.exists() and row.get("artifact_sha256") and _text(row["artifact_sha256"]) != _digest(path):
        blockers.append({"code": "artifact_digest_mismatch"})
    return blockers


def _owner_check(root: Path, expected: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    path = root / "testnet_automation" / "scheduler_ownership" / "current.json"
    if not path.exists():
        return {"status": "missing", "path": str(path)}, [{"code": "testnet_owner_epoch_missing"}]
    try:
        rows = _rows(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"status": "blocked", "path": str(path)}, [{"code": "testnet_owner_epoch_unreadable"}]
    current = rows[-1] if rows else {}
    blockers = []
    if current.get("status") != "active" or current.get("active_owner_id") != LOCAL_OWNER_ID:
        blockers.append({"code": "testnet_owner_not_local_active"})
    epoch = current.get("epoch")
    if type(epoch) is not int or epoch < 1:
        blockers.append({"code": "testnet_owner_epoch_invalid"})
    if expected.get("owner_epoch") not in (None, "") and _text(epoch) != _text(expected["owner_epoch"]):
        blockers.append({"code": "owner_epoch_mismatch", "expected": _text(expected["owner_epoch"]), "actual": _text(epoch)})
    return {"status": "pass" if not blockers else "blocked", "path": str(path), "owner_id": current.get("active_owner_id"), "epoch": epoch}, blockers


def evaluate_rollout(output_root: str | Path, *, now: str | datetime | None = None, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate both strategy families without mutating ``output_root``."""
    root = Path(output_root)
    current = _now(now)
    expected = {key: value for key, value in (expected or {}).items() if value not in (None, "")}
    records, load_blockers = _load(root)
    owner, owner_blockers = _owner_check(root, expected)
    # The first published record becomes the immutable comparison anchor when
    # callers do not supply command-line identity expectations.  This catches
    # drift between otherwise individually well-formed artifacts.
    anchor = dict(expected)
    if records:
        first = records[0][1]
        for field in IDENTITY_FIELDS:
            if field not in anchor and _field(first, field) not in (None, ""):
                anchor[field] = _field(first, field)
    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "status": "incomplete",
        "evaluated_at": current.isoformat(),
        "output_root": str(root),
        "owner": owner,
        "families": {},
        "blockers": list(load_blockers),
        "live_enabled": False,
        "execution_authorized": False,
        "next_action": "collect_m5_s2_and_m5_s3_evidence",
    }
    # An untouched output root is an honest incomplete rollout, not a failed
    # rollout.  Once evidence exists, owner absence/drift is a hard blocker.
    if records:
        result["blockers"].extend(owner_blockers)
    for family in FAMILIES:
        family_records = [(path, row) for path, row in records if _family(row, path) == family]
        s2 = [(path, row) for path, row in family_records if _stage(row, path) == "m5-s2"]
        s3 = [(path, row) for path, row in family_records if _stage(row, path) == "m5-s3"]
        blockers: list[dict[str, str]] = []
        if family_records and not s2:
            blockers.append({"code": "m5_s2_evidence_missing"})
        if family_records and len(s3) < WINDOW_COUNT:
            blockers.append({"code": "m5_s3_windows_incomplete", "expected": str(WINDOW_COUNT), "actual": str(len(s3))})
        for path, row in family_records:
            blockers.extend(_check_record(path, row, anchor, current))
        window_ids = [_text(row.get("window_index") or row.get("record_window_id")) for _, row in s3]
        if len(window_ids) != len(set(window_ids)):
            blockers.append({"code": "m5_s3_window_duplicate"})
        status = "incomplete" if not family_records else "ready" if not blockers and len(s2) >= 1 and len(s3) >= WINDOW_COUNT else "blocked"
        result["families"][family] = {"status": status, "m5_s2_count": len(s2), "m5_s3_window_count": len(s3), "blockers": blockers, "evidence": [{"path": str(path), "sha256": _digest(path)} for path, _ in family_records]}
        result["blockers"].extend({"family": family, **blocker} for blocker in blockers)
    statuses = [result["families"][family]["status"] for family in FAMILIES]
    result["status"] = "blocked" if result["blockers"] or "blocked" in statuses else "ready" if statuses == ["ready", "ready"] else "incomplete"
    result["next_action"] = "notify_park_and_wait" if result["status"] == "blocked" else "await_both_family_evidence" if result["status"] != "ready" else "remain_read_only_pending_owner_decision"
    return result


def render_report(result: Mapping[str, Any], *, date: str) -> str:
    lines = [f"# Testnet rollout gate — {date}", "", f"- Status: `{result.get('status')}`", f"- Evaluated at: `{result.get('evaluated_at')}`", f"- Output root: `{result.get('output_root')}`", "- Live enabled: `false`", "- Execution authorized: `false`", "", "## Identity and owner", "", f"- Owner: `{json.dumps(result.get('owner', {}), ensure_ascii=False, sort_keys=True)}`", "", "## Family results", ""]
    for family, value in (result.get("families") or {}).items():
        lines += [f"### {family.upper()}", "", f"- Status: `{value.get('status')}`", f"- M5-S2 attended evidence: `{value.get('m5_s2_count')}`", f"- M5-S3 12h windows: `{value.get('m5_s3_window_count')}/{WINDOW_COUNT}`", f"- Blockers: `{json.dumps(value.get('blockers') or [], ensure_ascii=False, sort_keys=True)}`", ""]
    lines += ["## Overall blockers", "", f"`{json.dumps(result.get('blockers') or [], ensure_ascii=False, sort_keys=True)}`", "", f"Next action: `{result.get('next_action')}`", ""]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only M5-S4 Testnet rollout gate")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--now", default=None, help="UTC ISO timestamp for deterministic expiry checks")
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--write-report", action="store_true", help="write docs/evidence/testnet-rollout-gate-<date>.md")
    parser.add_argument("--evidence-dir", type=Path, default=Path("docs/evidence"))
    for field in IDENTITY_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", dest=field, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    expected = {field: getattr(args, field) for field in IDENTITY_FIELDS}
    result = evaluate_rollout(args.output_root, now=args.now, expected=expected)
    if args.write_report:
        path = args.evidence_dir / f"testnet-rollout-gate-{args.date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_report(result, date=args.date), encoding="utf-8")
        result = {**result, "report_path": str(path)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ready" else 2 if result["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "RolloutGateError", "evaluate_rollout", "render_report", "build_parser", "main"]
