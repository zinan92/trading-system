from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.paper_supervisor_exception_provenance import (
    PaperSupervisorExceptionEvidenceError,
    PaperSupervisorExceptionStore,
    redact_exception_message,
)
from services.paper_supervisor_read_model import (
    build_paper_supervisor_polling_summary,
)


CYCLE = "2026-08-07_DAY"
ATTEMPT = "supervisor-attempt-exception-proof"
OCCURRED_AT = "2026-08-07T01:00:10+00:00"
SOURCE = {
    "source_sha": "a" * 40,
    "source_tree_sha": "b" * 40,
    "tracked_tree_clean": True,
}


class TypedProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _store(output: Path) -> PaperSupervisorExceptionStore:
    return PaperSupervisorExceptionStore(
        output,
        source_attestation=lambda: SOURCE,
    )


def _error() -> TypedProviderError:
    try:
        raise ValueError(
            "token=top-secret /Users/park/private/provider.json"
        )
    except ValueError as cause:
        error = TypedProviderError(
            "strategy_recommendation_provider_timeout",
            "payload={'prompt':'private'} api_key=sk-secret "
            "https://provider.example/call?token=secret",
        )
        error.__cause__ = cause
        return error


def test_receipt_is_source_bound_sanitized_and_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "outputs")
    error = _error()

    first = store.record_exception(
        cycle_id=CYCLE,
        attempt_id=ATTEMPT,
        phase="provider_fallback",
        occurred_at=OCCURRED_AT,
        exc=error,
    )
    second = store.record_exception(
        cycle_id=CYCLE,
        attempt_id=ATTEMPT,
        phase="provider_fallback",
        occurred_at=OCCURRED_AT,
        exc=error,
    )

    assert first == second
    assert first["original_machine_code"] == (
        "strategy_recommendation_provider_timeout"
    )
    assert first["phase"] == "provider_fallback"
    assert first["start_intent_persisted"] is False
    assert first["source"] == SOURCE
    assert first["redacted_message"] == "payload=<redacted-content>"
    assert first["cause_chain"][0]["redacted_message"] == (
        "<redacted-secret> <path>"
    )
    assert len(first["stack_fingerprint"]) == 64
    serialized = str(first)
    for forbidden in (
        "top-secret",
        "sk-secret",
        "/Users/park",
        "private/provider.json",
        "provider.example",
        "'prompt':'private'",
    ):
        assert forbidden not in serialized
    assert store.cycle_evidence(CYCLE)["receipts"] == [first]


def test_conflicting_exception_for_same_attempt_phase_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "outputs")
    store.record_exception(
        cycle_id=CYCLE,
        attempt_id=ATTEMPT,
        phase="provider_fallback",
        occurred_at=OCCURRED_AT,
        exc=_error(),
    )

    with pytest.raises(
        PaperSupervisorExceptionEvidenceError,
        match="paper_supervisor_exception_identity_conflict",
    ):
        store.record_exception(
            cycle_id=CYCLE,
            attempt_id=ATTEMPT,
            phase="provider_fallback",
            occurred_at=OCCURRED_AT,
            exc=RuntimeError("different failure"),
        )


def test_concurrent_duplicate_receipts_append_once(tmp_path: Path) -> None:
    store = _store(tmp_path / "outputs")
    error = _error()

    def record(_index: int) -> dict:
        return store.record_exception(
            cycle_id=CYCLE,
            attempt_id=ATTEMPT,
            phase="provider_fallback",
            occurred_at=OCCURRED_AT,
            exc=error,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(record, range(24)))

    assert len({row["receipt_digest"] for row in receipts}) == 1
    assert len(store.receipts(CYCLE)) == 1


def test_tampered_exception_journal_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = _store(output)
    store.record_exception(
        cycle_id=CYCLE,
        attempt_id=ATTEMPT,
        phase="provider_fallback",
        occurred_at=OCCURRED_AT,
        exc=_error(),
    )
    path = (
        output
        / "dualtrack"
        / "supervisor"
        / "exception_provenance"
        / f"{CYCLE}.json"
    )
    rows = load_json(path)
    rows[0]["redacted_message"] = "rewritten"
    write_json(path, rows)

    with pytest.raises(
        PaperSupervisorExceptionEvidenceError,
        match="paper_supervisor_exception_store_corrupt",
    ):
        store.receipts(CYCLE)

    read_model = build_paper_supervisor_polling_summary(
        output,
        cycle_id=CYCLE,
        as_of="2026-08-07T01:01:00+00:00",
    )
    assert read_model["status"] == "unavailable"
    assert read_model["exception_provenance"]["status"] == "unavailable"
    assert read_model["source_errors"] == [
        {
            "cycle_id": CYCLE,
            "machine_code": "attempt_store_corrupt",
        }
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "password=hunter2 /var/lib/gridmind/private.json",
        "prompt: private strategy and customer payload",
    ],
)
def test_redactor_never_returns_known_sensitive_material(raw: str) -> None:
    redacted = redact_exception_message(raw)

    assert "hunter2" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "/var/lib" not in redacted
    assert "private strategy" not in redacted
