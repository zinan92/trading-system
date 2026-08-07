"""Immutable, sanitized provenance for Paper Supervisor pre-intent errors.

The public blocker classifier intentionally reduces unknown failures to the
fail-closed ``unknown_blocker`` code.  This journal preserves enough bounded,
source-bound evidence to diagnose the original failure without persisting
provider payloads, prompts, credentials, local paths, or raw tracebacks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import load_json
from services.paper_release_receipt import current_source_attestation
from services.strategy_control_plane import production_mutation_lock


EXCEPTION_RECEIPT_SCHEMA_VERSION = (
    "paper-supervisor-pre-intent-exception-v1"
)
EXCEPTION_CYCLE_EVIDENCE_SCHEMA_VERSION = (
    "paper-supervisor-pre-intent-exception-cycle-evidence-v1"
)
_CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MACHINE_CODE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_RECEIPTS_PER_CYCLE = 128
MAX_RECEIPT_STORE_BYTES = 2 * 1024 * 1024
MAX_CAUSE_DEPTH = 4
MAX_REDACTED_MESSAGE_CHARS = 320
PRE_INTENT_PHASES = frozenset(
    {
        "authority_snapshot",
        "runtime_adoption",
        "outer_policy",
        "provider_readiness",
        "primary_ai",
        "provider_fallback",
        "candidate_build",
        "envelope_authorization",
        "plan_lock",
        "prepare_start",
        "prepared_receipt_validation",
        "pre_start_provider_readiness",
        "attempt_deadline",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "receipt_id",
        "cycle_id",
        "attempt_id",
        "phase",
        "occurred_at",
        "exception_class",
        "original_machine_code",
        "redacted_message",
        "cause_chain",
        "stack_fingerprint",
        "source",
        "start_intent_persisted",
        "previous_receipt_digest",
        "receipt_digest",
    }
)
_SOURCE_FIELDS = frozenset(
    {"source_sha", "source_tree_sha", "tracked_tree_clean"}
)
_CAUSE_FIELDS = frozenset(
    {"exception_class", "original_machine_code", "redacted_message"}
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|secret|token|authorization|"
    r"password|credential|cookie|session)\b\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9])")
_URL = re.compile(r"(?i)\bhttps?://[^\s]+")
_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-]+){2,}")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)+[^\\\s]+")
_PROVIDER_CONTENT = re.compile(
    r"(?is)\b(prompt|payload|messages|request_body|response_body)\b\s*[:=]\s*.*"
)


class PaperSupervisorExceptionEvidenceError(ValueError):
    """Stable fail-closed signal for malformed exception evidence."""


class PaperSupervisorExceptionStore:
    """Append and validate one hash-linked exception journal per cycle."""

    def __init__(
        self,
        output_root: Path,
        *,
        source_attestation: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "exception_provenance"
        )
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation()
        )

    def record_exception(
        self,
        *,
        cycle_id: str,
        attempt_id: str,
        phase: str,
        occurred_at: str,
        exc: BaseException,
    ) -> dict[str, Any]:
        """Persist a sanitized receipt before public blocker classification."""

        cycle = _cycle(cycle_id)
        attempt = _identity(attempt_id)
        stage = _phase(phase)
        source = _source(self.source_attestation())
        identity = {
            "cycle_id": cycle,
            "attempt_id": attempt,
            "phase": stage,
        }
        base = {
            "schema_version": EXCEPTION_RECEIPT_SCHEMA_VERSION,
            "receipt_id": (
                "paper-supervisor-exception-" + _digest(identity)[:24]
            ),
            **identity,
            "occurred_at": _timestamp(occurred_at),
            "exception_class": _exception_class(exc),
            "original_machine_code": _typed_machine_code(exc),
            "redacted_message": redact_exception_message(str(exc)),
            "cause_chain": _cause_chain(exc),
            "stack_fingerprint": _stack_fingerprint(exc),
            "source": source,
            "start_intent_persisted": False,
        }
        with production_mutation_lock(self.output_root):
            rows = self.receipts(cycle)
            for existing in rows:
                if (
                    existing["attempt_id"] != attempt
                    or existing["phase"] != stage
                ):
                    continue
                observed = {key: existing[key] for key in base}
                if observed != base:
                    raise PaperSupervisorExceptionEvidenceError(
                        "paper_supervisor_exception_identity_conflict"
                    )
                return existing
            if len(rows) >= MAX_RECEIPTS_PER_CYCLE:
                raise PaperSupervisorExceptionEvidenceError(
                    "paper_supervisor_exception_store_capacity_exceeded"
                )
            receipt = {
                **base,
                "sequence": len(rows) + 1,
                "previous_receipt_digest": (
                    rows[-1]["receipt_digest"] if rows else None
                ),
            }
            receipt["receipt_digest"] = _digest(receipt)
            validate_exception_receipt_chain(
                [*rows, receipt],
                cycle_id=cycle,
            )
            _atomic_write_json_fsync(self._path(cycle), [*rows, receipt])
            return receipt

    def receipts(self, cycle_id: str) -> list[dict[str, Any]]:
        cycle = _cycle(cycle_id)
        path = self._path(cycle)
        if self.root.exists() and (
            not self.root.is_dir() or self.root.is_symlink()
        ):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        if path.exists() and path.stat().st_size > MAX_RECEIPT_STORE_BYTES:
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        try:
            rows = load_json(path)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            ) from exc
        return validate_exception_receipt_chain(rows, cycle_id=cycle)

    def cycle_evidence(self, cycle_id: str) -> dict[str, Any]:
        cycle = _cycle(cycle_id)
        receipts = self.receipts(cycle)
        return {
            "schema_version": EXCEPTION_CYCLE_EVIDENCE_SCHEMA_VERSION,
            "cycle_id": cycle,
            "receipts": receipts,
            "receipt_count": len(receipts),
            "receipts_digest": _digest(receipts),
            "tail_receipt_digest": (
                receipts[-1]["receipt_digest"] if receipts else None
            ),
        }

    def projection(self, cycle_id: str) -> dict[str, Any]:
        evidence = self.cycle_evidence(cycle_id)
        receipts = evidence["receipts"]
        return {
            "schema_version": EXCEPTION_CYCLE_EVIDENCE_SCHEMA_VERSION,
            "cycle_id": evidence["cycle_id"],
            "status": "available",
            "receipt_count": evidence["receipt_count"],
            "receipts_digest": evidence["receipts_digest"],
            "tail_receipt_digest": evidence["tail_receipt_digest"],
            "latest_receipt": receipts[-1] if receipts else None,
        }

    def _path(self, cycle_id: str) -> Path:
        return self.root / f"{_cycle(cycle_id)}.json"


def validate_exception_receipt_chain(
    rows: Any,
    *,
    cycle_id: str,
) -> list[dict[str, Any]]:
    cycle = _cycle(cycle_id)
    if not isinstance(rows, list) or len(rows) > MAX_RECEIPTS_PER_CYCLE:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )
    result: list[dict[str, Any]] = []
    previous: str | None = None
    identities: set[tuple[str, str]] = set()
    for sequence, value in enumerate(rows, start=1):
        if not isinstance(value, Mapping):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        row = json.loads(json.dumps(dict(value)))
        supplied = str(row.get("receipt_digest") or "")
        identity = (str(row.get("attempt_id") or ""), str(row.get("phase") or ""))
        unsigned = {
            key: item
            for key, item in row.items()
            if key != "receipt_digest"
        }
        if (
            set(row) != _RECEIPT_FIELDS
            or row.get("schema_version") != EXCEPTION_RECEIPT_SCHEMA_VERSION
            or row.get("sequence") != sequence
            or row.get("cycle_id") != cycle
            or row.get("previous_receipt_digest") != previous
            or row.get("start_intent_persisted") is not False
            or not _DIGEST.fullmatch(supplied)
            or not hmac.compare_digest(supplied, _digest(unsigned))
            or identity in identities
        ):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        _identity(row.get("receipt_id"))
        _identity(row.get("attempt_id"))
        _phase(row.get("phase"))
        _timestamp(row.get("occurred_at"))
        _safe_class(row.get("exception_class"))
        machine_code = row.get("original_machine_code")
        if machine_code is not None and not _MACHINE_CODE.fullmatch(
            str(machine_code)
        ):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        message = str(row.get("redacted_message") or "")
        if (
            not message
            or len(message) > MAX_REDACTED_MESSAGE_CHARS
            or redact_exception_message(message) != message
        ):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        causes = row.get("cause_chain")
        if not isinstance(causes, list) or len(causes) > MAX_CAUSE_DEPTH:
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        for cause in causes:
            _validate_cause(cause)
        if not _DIGEST.fullmatch(str(row.get("stack_fingerprint") or "")):
            raise PaperSupervisorExceptionEvidenceError(
                "paper_supervisor_exception_store_corrupt"
            )
        _source(row.get("source"))
        identities.add(identity)
        result.append(row)
        previous = supplied
    return result


def validate_packaged_exception_evidence(package: Mapping[str, Any]) -> None:
    """Require a terminal package to embed the complete exception chain."""

    cycle = _cycle(package.get("cycle_id"))
    receipts = validate_exception_receipt_chain(
        package.get("pre_intent_exception_receipts"),
        cycle_id=cycle,
    )
    if (
        package.get("pre_intent_exception_receipt_count") != len(receipts)
        or package.get("pre_intent_exception_receipts_digest")
        != _digest(receipts)
        or package.get("pre_intent_exception_receipt_tail_digest")
        != (receipts[-1]["receipt_digest"] if receipts else None)
    ):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_package_invalid"
        )


def exception_receipt_ref(receipt: Mapping[str, Any]) -> dict[str, str]:
    ref = {
        "receipt_id": str(receipt.get("receipt_id") or ""),
        "receipt_digest": str(receipt.get("receipt_digest") or ""),
        "phase": str(receipt.get("phase") or ""),
    }
    _identity(ref["receipt_id"])
    if not _DIGEST.fullmatch(ref["receipt_digest"]):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_receipt_invalid"
        )
    _phase(ref["phase"])
    return ref


def redact_exception_message(value: Any) -> str:
    """Return a deterministic bounded diagnostic string, never raw content."""

    text = " ".join(str(value or "").split())
    text = _PROVIDER_CONTENT.sub(r"\1=<redacted-content>", text)
    text = _SECRET_ASSIGNMENT.sub("<redacted-secret>", text)
    text = _BEARER.sub("Bearer <redacted-secret>", text)
    text = _URL.sub("<url>", text)
    text = _WINDOWS_PATH.sub("<path>", text)
    text = _UNIX_PATH.sub("<path>", text)
    text = _LONG_TOKEN.sub("<redacted-token>", text)
    text = "".join(
        character if character.isprintable() else "?" for character in text
    ).strip()
    if not text:
        text = "<empty>"
    if len(text) > MAX_REDACTED_MESSAGE_CHARS:
        text = text[: MAX_REDACTED_MESSAGE_CHARS - 1] + "…"
    return text


def _cause_chain(exc: BaseException) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    seen = {id(exc)}
    current = exc.__cause__ or exc.__context__
    while current is not None and len(causes) < MAX_CAUSE_DEPTH:
        if id(current) in seen:
            break
        seen.add(id(current))
        causes.append(
            {
                "exception_class": _exception_class(current),
                "original_machine_code": _typed_machine_code(current),
                "redacted_message": redact_exception_message(str(current)),
            }
        )
        current = current.__cause__ or current.__context__
    return causes


def _validate_cause(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _CAUSE_FIELDS:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )
    _safe_class(value.get("exception_class"))
    machine_code = value.get("original_machine_code")
    if machine_code is not None and not _MACHINE_CODE.fullmatch(
        str(machine_code)
    ):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )
    message = str(value.get("redacted_message") or "")
    if (
        not message
        or len(message) > MAX_REDACTED_MESSAGE_CHARS
        or redact_exception_message(message) != message
    ):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )


def _stack_fingerprint(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    bounded = [
        {"function": frame.name, "line": int(frame.lineno)}
        for frame in frames[-24:]
    ]
    return _digest(
        {"exception_class": _exception_class(exc), "frames": bounded}
    )


def _typed_machine_code(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    if code is None:
        return None
    text = str(code).strip().lower()
    return text if _MACHINE_CODE.fullmatch(text) else None


def _exception_class(exc: BaseException) -> str:
    return _safe_class(type(exc).__name__)


def _safe_class(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", text):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )
    return text


def _source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_FIELDS:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_source_invalid"
        )
    source = dict(value)
    if (
        not _SOURCE_SHA.fullmatch(str(source.get("source_sha") or "").lower())
        or not _SOURCE_SHA.fullmatch(
            str(source.get("source_tree_sha") or "").lower()
        )
        or not isinstance(source.get("tracked_tree_clean"), bool)
    ):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_source_invalid"
        )
    return {
        "source_sha": str(source["source_sha"]).lower(),
        "source_tree_sha": str(source["source_tree_sha"]).lower(),
        "tracked_tree_clean": source["tracked_tree_clean"],
    }


def _cycle(value: Any) -> str:
    text = str(value or "").strip()
    if not _CYCLE_ID.fullmatch(text):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_cycle_id_invalid"
        )
    return text


def _identity(value: Any) -> str:
    text = str(value or "").strip()
    if not _IDENTITY.fullmatch(text):
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_identity_invalid"
        )
    return text


def _phase(value: Any) -> str:
    text = str(value or "").strip()
    if text not in PRE_INTENT_PHASES:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_phase_invalid"
        )
    return text


def _timestamp(value: Any) -> str:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_write_json_fsync(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_corrupt"
        )
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if len(payload.encode("utf-8")) > MAX_RECEIPT_STORE_BYTES:
        raise PaperSupervisorExceptionEvidenceError(
            "paper_supervisor_exception_store_capacity_exceeded"
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
