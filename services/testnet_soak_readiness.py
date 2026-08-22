"""Accelerated seven-day Testnet soak and Live-readiness evidence contract."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from services.journal_store import load_json, write_json
from services.park_recording_track import ParkRecordingError, ParkRecordingTrack, REQUIRED_CATEGORIES
from services.paper_release_receipt import current_source_attestation


SOAK_SCHEMA = "testnet-soak-readiness-v1"
WINDOW_COUNT = 14
WINDOW_HOURS = 12
REQUIRED_GATE_EVIDENCE = (
    "orders_fills_positions_reconciliation",
    "protection_coverage",
    "capability_status",
    "market_freshness_trust",
    "runtime_health",
    "retry_outcomes",
    "release_account_environment_identity",
    "recording_package",
)


class TestnetSoakError(RuntimeError):
    """A durable soak blocker; elapsed time never upgrades it to ready."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TestnetSoakError("timestamp_invalid") from exc
    if result.tzinfo is None:
        raise TestnetSoakError("timestamp_timezone_missing")
    return result.astimezone(timezone.utc)


class TestnetSoakReadiness:
    """Persist one continuous strategy's 14 recording-window readiness chain."""

    __test__ = False

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "testnet_soak"
        self.windows_path = self.root / "windows.json"
        self.receipts_path = self.root / "readiness_receipts.json"
        self.recording = ParkRecordingTrack(self.output_root)

    def windows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in load_json(self.windows_path) if isinstance(row, dict)]

    def receipts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in load_json(self.receipts_path) if isinstance(row, dict)]

    def record_window(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        identity = self._identity(observation)
        index = int(observation.get("window_index") or 0)
        if not 0 <= index < WINDOW_COUNT:
            raise TestnetSoakError("window_index_out_of_range")
        starts_at = str(observation.get("starts_at") or "")
        ends_at = str(observation.get("ends_at") or "")
        start = _parse_timestamp(starts_at)
        end = _parse_timestamp(ends_at)
        if end - start != timedelta(hours=WINDOW_HOURS):
            raise TestnetSoakError("recording_window_not_12_hours")
        all_windows = self.windows()
        existing = next((row for row in all_windows if row.get("window_index") == index), None)
        if existing is not None:
            if self._window_identity(existing) != identity:
                raise TestnetSoakError("soak_window_identity_conflict")
            if self._provenance_identity(existing) != self._provenance_identity(observation):
                raise TestnetSoakError("broker_release_account_changed_across_soak")
            if str(existing.get("observation_digest") or "") == self._observation_digest(observation):
                return dict(existing)
            raise TestnetSoakError("soak_window_replay_conflict")
            return dict(existing)
        prior = sorted([row for row in all_windows if row.get("window_index") != index], key=lambda row: int(row.get("window_index") or 0))
        if not (start.astimezone(ZoneInfo("Asia/Shanghai")).hour == 9 and start.astimezone(ZoneInfo("Asia/Shanghai")).minute == 0) and not (start.astimezone(ZoneInfo("Asia/Shanghai")).hour == 21 and start.astimezone(ZoneInfo("Asia/Shanghai")).minute == 0):
            raise TestnetSoakError("recording_window_boundary_not_09_or_21_bjt")
        if prior:
            previous = prior[-1]
            if index != int(previous["window_index"]) + 1:
                raise TestnetSoakError("soak_window_sequence_gap")
            if _parse_timestamp(starts_at) != _parse_timestamp(str(previous["ends_at"])):
                raise TestnetSoakError("soak_window_boundary_gap")
            if self._window_identity(previous) != identity:
                raise TestnetSoakError("strategy_identity_changed_across_soak")
            if self._provenance_identity(previous) != self._provenance_identity(observation):
                raise TestnetSoakError("broker_release_account_changed_across_soak")
        evidence = observation.get("evidence") if isinstance(observation.get("evidence"), Mapping) else {}
        blockers = self._gate_blockers(observation, evidence)
        if observation.get("_collection_blocker"):
            blockers.append({"code": "artifact_collection_blocked", "detail": str(observation["_collection_blocker"])})
        for category, payload in evidence.items():
            if isinstance(payload, Mapping) and payload.get("observed_at"):
                observed_at = _parse_timestamp(str(payload["observed_at"]))
                if not start <= observed_at <= end:
                    blockers.append({"code": "evidence_timestamp_outside_window", "category": category})
        attestation = observation.get("source_attestation") if isinstance(observation.get("source_attestation"), Mapping) else {}
        if not attestation:
            try:
                attestation = current_source_attestation()
            except Exception:
                attestation = {}
        attestation_verified = attestation.get("status") == "verified" or (attestation.get("tracked_tree_clean") is True and str(attestation.get("source_sha") or "").strip() and str(attestation.get("source_tree_sha") or attestation.get("tree_sha") or "").strip())
        if not attestation_verified:
            blockers.append({"code": "source_attestation_missing_or_unverified", "evidence": attestation})
        mutations = observation.get("execution_mutations") or []
        if mutations:
            blockers.append({"code": "boundary_execution_mutation", "detail": "Recording Window evidence contains execution mutation"})
        window_id = str(observation.get("record_window_id") or f"testnet-soak-{index:02d}")
        try:
            self.recording.start_window(record_window_id=window_id, strategy_session_id=identity["strategy_session_id"], strategy_revision_id=identity["strategy_revision_id"], starts_at=starts_at, ends_at=ends_at)
            for category in REQUIRED_CATEGORIES:
                payload = dict(evidence.get(category) or {})
                self.recording.record_event(record_window_id=window_id, strategy_session_id=identity["strategy_session_id"], strategy_revision_id=identity["strategy_revision_id"], category=category, event_type="soak_observation", source=str(payload.get("source") or ""), occurred_at=str(payload.get("observed_at") or ""), payload=payload)
            package = self.recording.close_package(record_window_id=window_id, strategy_session_id=identity["strategy_session_id"], strategy_revision_id=identity["strategy_revision_id"], strategy_open=True, positions_open=int(observation.get("positions_open") or 0))
            review = self.recording.review(record_window_id=window_id)
            reviews = load_json(self.root / "reviews.json")
            if not any(isinstance(item, Mapping) and item.get("record_window_id") == window_id and item.get("review_digest") == _digest(review) for item in reviews):
                reviews.append({"schema_version": SOAK_SCHEMA, "event": "window_review", "record_window_id": window_id, "review": review, "review_digest": _digest(review)})
                write_json(self.root / "reviews.json", reviews)
            if package.get("status") == "complete":
                package = self.recording.mark_review_complete(record_window_id=window_id)
        except ParkRecordingError as exc:
            blockers.append({"code": exc.code, "detail": str(exc)})
            package = {"status": "blocked", "error": exc.code}
        row = {
            "schema_version": SOAK_SCHEMA,
            "event": "window_recorded",
            "window_index": index,
            "record_window_id": window_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            **identity,
            "environment": "testnet",
            "broker_id": str(observation.get("broker_id") or "hyperliquid"),
            "release_sha": str(observation.get("release_sha") or ""),
            "account_fingerprint": str(observation.get("account_fingerprint") or ""),
            "package_status": package.get("status"),
            "package_revision": package.get("revision"),
            "review_digest": _digest(review) if "review" in locals() else None,
            "gate_evidence": dict(evidence),
            "source_attestation": dict(attestation),
            "observation_digest": self._observation_digest(observation),
            "blockers": blockers,
            "execution_mutations": [],
            "status": "blocked" if blockers or package.get("status") != "complete" else "pass",
            "next_action": "notify_park_and_wait" if blockers else "continue_soak",
            "evidence_digest": _digest({"identity": identity, "evidence": evidence, "package": package, "blockers": blockers, "source_attestation": attestation}),
        }
        row["row_digest"] = _digest({key: value for key, value in row.items() if key != "row_digest"})
        rows = [row for row in self.windows() if row.get("window_index") != index]
        rows.append(row)
        write_json(self.windows_path, rows)
        return dict(row)

    def record_window_from_artifacts(
        self,
        observation: Mapping[str, Any],
        *,
        artifact_paths: Mapping[str, str | Path],
    ) -> dict[str, Any]:
        """Build one observation from existing runtime/broker evidence files.

        This is the production handoff seam: DCA/Grid runners and the broker
        recorder publish their receipts first, then the soak only reads and
        hashes those artifacts. It never invents a pass from elapsed time.
        """

        evidence = dict(observation.get("evidence") or {})
        try:
            for category in sorted(set(REQUIRED_CATEGORIES) | set(REQUIRED_GATE_EVIDENCE)):
                reference = artifact_paths.get(category)
                if reference is None:
                    raise TestnetSoakError(f"artifact_reference_missing:{category}")
                path = Path(reference)
                if not path.is_absolute():
                    path = self.output_root / path
                if not path.exists() or not path.is_file():
                    raise TestnetSoakError(f"artifact_missing:{category}")
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    raise TestnetSoakError(f"artifact_unreadable:{category}") from exc
                if not isinstance(payload, Mapping):
                    raise TestnetSoakError(f"artifact_shape_invalid:{category}")
                evidence[category] = {**dict(payload), "artifact_ref": str(path), "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "artifact_kind": category}
        except TestnetSoakError as exc:
            return self.record_window({**dict(observation), "evidence": evidence, "_collection_blocker": str(exc)})
        return self.record_window({**dict(observation), "evidence": evidence})

    def finalize(self, *, now: str | None = None) -> dict[str, Any]:
        rows = sorted(self.windows(), key=lambda row: int(row.get("window_index") or 0))
        existing = self.receipts()
        if existing and existing[-1].get("status") == "ready" and self._receipt_integrity_ok(existing[-1], rows) and self._receipt_is_fresh(existing[-1], rows, now=now):
            return dict(existing[-1])
        blockers: list[dict[str, Any]] = []
        if len(rows) != WINDOW_COUNT or [int(row.get("window_index")) if row.get("window_index") is not None else -1 for row in rows] != list(range(WINDOW_COUNT)):
            blockers.append({"code": "soak_window_count_incomplete", "expected": WINDOW_COUNT, "actual": len(rows)})
        if rows:
            identity = self._window_identity(rows[0])
            if any(self._window_identity(row) != identity for row in rows):
                blockers.append({"code": "strategy_identity_changed_across_soak"})
            if any(self._provenance_identity(row) != self._provenance_identity(rows[0]) for row in rows):
                blockers.append({"code": "broker_release_account_changed_across_soak"})
        else:
            identity = {"strategy_session_id": "", "strategy_revision_id": "", "plan_digest": ""}
        provenance = self._provenance_identity(rows[0]) if rows else {"environment": "testnet", "broker_id": "", "release_sha": "", "account_fingerprint": ""}
        source_attestation = dict(rows[0].get("source_attestation") or {}) if rows else {}
        if rows and not self._receipt_is_fresh({"created_at": now or datetime.now(timezone.utc).isoformat()}, rows, now=now):
            blockers.append({"code": "soak_evidence_stale"})
        if rows and not self._artifacts_intact(rows):
            blockers.append({"code": "soak_artifact_integrity_failed"})
        try:
            current_source = current_source_attestation(Path(__file__).resolve().parents[1])
            if rows and (current_source.get("tracked_tree_clean") is not True or current_source.get("source_sha") != provenance.get("release_sha")):
                blockers.append({"code": "soak_source_changed_before_finalize"})
        except Exception:
            blockers.append({"code": "soak_source_attestation_unavailable"})
        for row in rows:
            blockers.extend({"window_index": row.get("window_index"), **dict(blocker)} for blocker in row.get("blockers") or [])
            if row.get("status") != "pass":
                blockers.append({"window_index": row.get("window_index"), "code": "window_not_pass"})
            if row.get("package_status") != "complete":
                blockers.append({"window_index": row.get("window_index"), "code": "recording_package_incomplete"})
            if not row.get("review_digest"):
                blockers.append({"window_index": row.get("window_index"), "code": "recording_review_missing"})
        receipt = {
            "schema_version": SOAK_SCHEMA,
            "event": "readiness_receipt",
            "environment": "testnet",
            **identity,
            **provenance,
            "source_attestation": source_attestation,
            "window_count": len(rows),
            "day_count": len(rows) // 2,
            "required_window_count": WINDOW_COUNT,
            "required_day_count": 7,
            "status": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "live_enabled": False,
            "live_writes_enabled": False,
            "automatic_promotion": False,
            "created_at": str(now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            "receipt_revision": len(existing),
            "window_digests": [str(row.get("row_digest") or "") for row in rows],
            "next_action": "await_manual_live_activation" if not blockers else "notify_park_and_wait",
            "receipt_digest": "",
        }
        receipt["receipt_digest"] = _digest({key: value for key, value in receipt.items() if key != "receipt_digest"})
        write_json(self.receipts_path, [receipt])
        return dict(receipt)

    def public_status(self, *, now: str | None = None) -> dict[str, Any]:
        receipt = self.receipts()[-1] if self.receipts() else None
        now = now or datetime.now(timezone.utc).isoformat()
        rows = self.windows()
        if receipt is not None:
            status = str(receipt.get("status") or "incomplete")
            if not self._receipt_integrity_ok(receipt, rows):
                status = "blocked"
                receipt = {**receipt, "blockers": [*list(receipt.get("blockers") or []), {"code": "readiness_receipt_integrity_invalid"}]}
            elif status == "ready" and not self._receipt_is_fresh(receipt, rows, now=now):
                status = "stale"
            if status == "ready" and now is not None:
                age = _parse_timestamp(now) - _parse_timestamp(str(receipt.get("created_at") or now))
                if age > timedelta(hours=24):
                    status = "stale"
            return {"status": status, "environment": "testnet", "broker_id": receipt.get("broker_id"), "release_sha": receipt.get("release_sha"), "account_fingerprint": receipt.get("account_fingerprint"), "source_attestation": dict(receipt.get("source_attestation") or {}), "window_count": len(rows), "required_window_count": WINDOW_COUNT, "day_count": len(rows) // 2, "blockers": list(receipt.get("blockers") or []), "live_enabled": False, "live_writes_enabled": False, "next_action": "notify_park_and_wait" if status == "blocked" else "refresh_soak_evidence" if status == "stale" else receipt.get("next_action")}
        return {"status": "incomplete" if rows else "missing", "environment": "testnet", "broker_id": None, "release_sha": None, "account_fingerprint": None, "source_attestation": {}, "window_count": len(rows), "required_window_count": WINDOW_COUNT, "day_count": len(rows) // 2, "blockers": [], "live_enabled": False, "live_writes_enabled": False, "next_action": "continue_soak" if rows else "start_attended_testnet_soak"}

    @staticmethod
    def _identity(value: Mapping[str, Any]) -> dict[str, str]:
        result = {key: str(value.get(key) or "").strip() for key in ("strategy_session_id", "strategy_revision_id", "plan_digest")}
        if any(not item for item in result.values()):
            raise TestnetSoakError("strategy_identity_incomplete")
        return result

    @staticmethod
    def _window_identity(value: Mapping[str, Any]) -> dict[str, str]:
        return {key: str(value.get(key) or "") for key in ("strategy_session_id", "strategy_revision_id", "plan_digest")}

    @staticmethod
    def _provenance_identity(value: Mapping[str, Any]) -> dict[str, str]:
        return {key: str(value.get(key) or "") for key in ("environment", "broker_id", "release_sha", "account_fingerprint")}

    @staticmethod
    def _observation_digest(value: Mapping[str, Any]) -> str:
        material = {key: value.get(key) for key in ("window_index", "record_window_id", "starts_at", "ends_at", "strategy_session_id", "strategy_revision_id", "plan_digest", "environment", "broker_id", "release_sha", "account_fingerprint", "evidence", "execution_mutations", "source_attestation", "fresh", "trusted", "network_io", "real_money_eligible", "positions_open")}
        return _digest(material)

    @staticmethod
    def _receipt_is_fresh(receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, now: str | None) -> bool:
        reference = _parse_timestamp(now or datetime.now(timezone.utc).isoformat())
        created = receipt.get("created_at")
        if created and reference - _parse_timestamp(str(created)) > timedelta(hours=24):
            return False
        latest_end = max((_parse_timestamp(str(row.get("ends_at"))) for row in rows if row.get("ends_at")), default=reference)
        if latest_end > reference:
            return False
        return reference - latest_end <= timedelta(hours=24)

    def _receipt_integrity_ok(self, receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]] | None = None) -> bool:
        supplied = str(receipt.get("receipt_digest") or "")
        if not supplied:
            return False
        payload = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        if supplied != _digest(payload):
            return False
        try:
            current = current_source_attestation(Path(__file__).resolve().parents[1])
        except Exception:
            return False
        attestation = receipt.get("source_attestation") if isinstance(receipt.get("source_attestation"), Mapping) else {}
        if current.get("source_sha") != attestation.get("source_sha") or current.get("source_tree_sha") != (attestation.get("source_tree_sha") or attestation.get("tree_sha")) or current.get("tracked_tree_clean") is not True:
            return False
        if rows is None:
            return True
        ordered = sorted(rows, key=lambda item: int(item.get("window_index") or 0))
        expected = [str(row.get("row_digest") or "") for row in ordered]
        recomputed = [_digest({key: value for key, value in row.items() if key != "row_digest"}) for row in ordered]
        return int(receipt.get("window_count") or 0) == len(rows) and expected == recomputed and expected == list(receipt.get("window_digests") or []) and all(expected) and self._artifacts_intact(ordered)

    def _artifacts_intact(self, rows: Sequence[Mapping[str, Any]]) -> bool:
        for row in rows:
            evidence = row.get("gate_evidence") if isinstance(row.get("gate_evidence"), Mapping) else {}
            for category, payload in evidence.items():
                if not isinstance(payload, Mapping):
                    return False
                reference = str(payload.get("artifact_ref") or "")
                expected = str(payload.get("artifact_sha256") or "")
                path = Path(reference) if Path(reference).is_absolute() else self.output_root / reference
                if not reference or not path.exists() or not path.is_file() or not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
                    return False
                try:
                    artifact = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    return False
                if not isinstance(artifact, Mapping) or (artifact.get("artifact_kind") not in (None, "") and str(artifact.get("artifact_kind")) != str(payload.get("artifact_kind") or category)):
                    return False
                for identity_key in ("strategy_session_id", "strategy_revision_id", "plan_digest", "environment", "release_sha", "account_fingerprint"):
                    if str(artifact.get(identity_key) or "") != str(row.get(identity_key) or ""):
                        return False
                for window_key in ("window_index", "record_window_id", "starts_at", "ends_at"):
                    if str(artifact.get(window_key) or "") != str(row.get(window_key) or ""):
                        return False
        return True

    def _gate_blockers(self, observation: Mapping[str, Any], evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if str(observation.get("environment") or "") != "testnet":
            blockers.append({"code": "observation_environment_not_testnet"})
        for key in REQUIRED_GATE_EVIDENCE:
            value = evidence.get(key)
            if not isinstance(value, Mapping) or value.get("status") not in {"pass", "ready", "ok"} or not str(value.get("source") or "").strip() or not str(value.get("observed_at") or "").strip() or not str(value.get("artifact_ref") or "").strip() or not str(value.get("artifact_sha256") or "").strip() or str(value.get("artifact_kind") or "") != key:
                blockers.append({"code": f"gate_{key}_not_ready", "evidence": value})
            if isinstance(value, Mapping):
                reference = str(value.get("artifact_ref") or "")
                artifact_path = Path(reference) if Path(reference).is_absolute() else self.output_root / reference
                if not reference or not artifact_path.exists() or not artifact_path.is_file():
                    blockers.append({"code": f"artifact_{key}_missing", "artifact_ref": reference})
                elif value.get("artifact_sha256"):
                    actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    if actual != str(value.get("artifact_sha256") or "").lower():
                        blockers.append({"code": f"artifact_{key}_digest_mismatch", "artifact_ref": reference})
                if artifact_path.exists() and not self._artifact_content_matches(artifact_path, value, observation):
                    blockers.append({"code": f"artifact_{key}_identity_mismatch", "artifact_ref": reference})
        identity_evidence = evidence.get("release_account_environment_identity")
        if isinstance(identity_evidence, Mapping):
            for field in ("release_sha", "account_fingerprint", "environment", "broker_id"):
                if not str(identity_evidence.get(field) or "").strip():
                    blockers.append({"code": f"identity_{field}_missing"})
            if str(identity_evidence.get("environment") or "") != "testnet":
                blockers.append({"code": "identity_environment_not_testnet"})
            for field in ("release_sha", "account_fingerprint", "broker_id"):
                if str(identity_evidence.get(field) or "") != str(observation.get(field) or ""):
                    blockers.append({"code": f"identity_{field}_mismatch"})
        market_evidence = evidence.get("market_freshness_trust")
        if isinstance(market_evidence, Mapping) and (market_evidence.get("fresh") is not True or market_evidence.get("trusted") is not True):
            blockers.append({"code": "market_freshness_or_trust_missing"})
        for key in ("fresh", "trusted", "network_io", "real_money_eligible"):
            expected = {"fresh": True, "trusted": True, "network_io": False, "real_money_eligible": False}[key]
            if key not in observation or observation.get(key) is not expected:
                blockers.append({"code": f"safety_{key}_invalid", "actual": observation.get(key)})
        for category in REQUIRED_CATEGORIES:
            payload = evidence.get(category)
            if not isinstance(payload, Mapping) or not payload or payload.get("status") not in {"pass", "ready", "ok"} or not str(payload.get("source") or "").strip() or not str(payload.get("observed_at") or "").strip() or not str(payload.get("artifact_ref") or "").strip() or not str(payload.get("artifact_sha256") or "").strip() or str(payload.get("artifact_kind") or "") != category:
                blockers.append({"code": f"recording_{category}_payload_missing"})
                continue
            reference = str(payload["artifact_ref"])
            artifact_path = Path(reference) if Path(reference).is_absolute() else self.output_root / reference
            if not artifact_path.exists() or not artifact_path.is_file():
                blockers.append({"code": f"recording_{category}_artifact_missing", "artifact_ref": reference})
            elif payload.get("artifact_sha256") and hashlib.sha256(artifact_path.read_bytes()).hexdigest() != str(payload["artifact_sha256"]).lower():
                blockers.append({"code": f"recording_{category}_artifact_digest_mismatch", "artifact_ref": reference})
            if artifact_path.exists() and not self._artifact_content_matches(artifact_path, payload, observation):
                blockers.append({"code": f"recording_{category}_artifact_identity_mismatch", "artifact_ref": reference})
        attestation = observation.get("source_attestation") if isinstance(observation.get("source_attestation"), Mapping) else {}
        attestation_tree = str(attestation.get("tree_sha") or attestation.get("source_tree_sha") or "")
        attestation_release = str(attestation.get("release_sha") or attestation.get("source_sha") or "")
        valid_hex = lambda value: len(value) in {40, 64} and all(char in "0123456789abcdefABCDEF" for char in value)
        try:
            current = current_source_attestation(Path(__file__).resolve().parents[1])
        except Exception:
            current = {"attestation_error": True}
        if current.get("attestation_error") or attestation.get("tracked_tree_clean") is not True or not valid_hex(attestation_tree) or not valid_hex(attestation_release) or attestation_release != str(observation.get("release_sha") or "") or (current and (str(current.get("source_sha") or "") != str(attestation.get("source_sha") or attestation.get("release_sha") or "") or str(current.get("source_tree_sha") or "") != attestation_tree or current.get("tracked_tree_clean") is not True)):
            blockers.append({"code": "source_attestation_tree_or_release_invalid"})
        return blockers

    @staticmethod
    def _artifact_content_matches(path: Path, payload: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(artifact, Mapping):
            return False
        if artifact.get("artifact_kind") not in (None, "") and str(artifact.get("artifact_kind")) != str(payload.get("artifact_kind") or ""):
            return False
        for key in ("strategy_session_id", "strategy_revision_id", "plan_digest", "environment", "broker_id", "release_sha", "account_fingerprint", "window_index", "record_window_id", "starts_at", "ends_at"):
            if str(artifact.get(key) or "") != str(observation.get(key) or ""):
                return False
        return True
