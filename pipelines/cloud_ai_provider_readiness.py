"""Run the bounded, side-effect-free Cloud Paper provider readiness check."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.cloud_ai_provider import (
    READINESS_SCHEMA,
    CloudAIProviderReadiness,
    _digest,
    _sha256,
)
from services.config_loader import ROOT
from services.dualtrack_config import dualtrack_config
from services.journal_store import write_json
from services.paper_release_receipt import current_source_attestation


REQUIRED_RESPONSE_KEYS = ("direction", "style", "rationale", "ai_self_assessment")
RENEWAL_INTERVAL_SECONDS = 6 * 60 * 60
MIN_PROVIDER_TIMEOUT_SECONDS = 25
MAX_PROVIDER_TIMEOUT_SECONDS = 60
DEFAULT_PROVIDER_READINESS_TIMEOUT_SECONDS = 60
SMOKE_PROMPT = (
    "Return exactly one JSON object with keys direction, style, rationale, "
    "ai_self_assessment. Use direction neutral, style steady, rationale "
    "provider readiness smoke, and ai_self_assessment 5. No markdown."
)
_PROVIDER_OUTPUT_LIMIT = 4096
_PROVIDER_SECRET_PATTERNS = (
    re.compile(r"(?is)-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----"),
    re.compile(r"(?i)(\bbearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)\s*[:=]\s*)[^\s,;]+"
    ),
)


class ProviderReadinessFailure(RuntimeError):
    pass


def _provider_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bounded_redacted_provider_output(value: Any) -> str:
    text = _provider_output_text(value)
    for pattern in _PROVIDER_SECRET_PATTERNS:
        if pattern.pattern.startswith("(?is)-----"):
            text = pattern.sub("[REDACTED_PRIVATE_KEY]", text)
        elif "bearer" in pattern.pattern.lower():
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub(r"\1[REDACTED]", text)
    if len(text) > _PROVIDER_OUTPUT_LIMIT:
        return text[:_PROVIDER_OUTPUT_LIMIT] + "...[truncated]"
    return text


def _provider_call_trace(*, deadline_seconds: int | None) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    return {
        "deadline_seconds": deadline_seconds,
        "started_at": started_at.isoformat(),
        "deadline_at": (
            (started_at + timedelta(seconds=deadline_seconds)).isoformat()
            if deadline_seconds is not None
            else None
        ),
        "finished_at": None,
        "elapsed_ms": None,
        "phase_timings_ms": {},
        "phase_return_codes": {},
        "return_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "partial_output": False,
    }


def _finish_provider_call_trace(
    trace: dict[str, Any],
    started_monotonic: float,
    *,
    stdout: Any = None,
    stderr: Any = None,
    return_code: int | None = None,
    timed_out: bool | None = None,
    partial_output: bool | None = None,
) -> None:
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()
    trace["elapsed_ms"] = round((time.monotonic() - started_monotonic) * 1000)
    trace["return_code"] = return_code
    if stdout is not None:
        trace["stdout"] = _bounded_redacted_provider_output(stdout)
    if stderr is not None:
        trace["stderr"] = _bounded_redacted_provider_output(stderr)
    if timed_out is not None:
        trace["timed_out"] = bool(timed_out)
    if partial_output is not None:
        trace["partial_output"] = bool(partial_output)


def _record_phase(trace: dict[str, Any], name: str, started_monotonic: float) -> None:
    trace["phase_timings_ms"][name] = round(
        (time.monotonic() - started_monotonic) * 1000
    )


def resolve_provider_readiness_timeout(config: dict[str, Any]) -> int:
    """Resolve the read-only smoke budget without borrowing live-tick budget."""

    convergence = config.get("convergence")
    convergence = convergence if isinstance(convergence, dict) else {}
    raw_timeout = convergence.get(
        "provider_readiness_timeout_seconds",
        DEFAULT_PROVIDER_READINESS_TIMEOUT_SECONDS,
    )
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ProviderReadinessFailure(
            "strategy_recommendation_provider_timeout_configuration_invalid"
        ) from exc
    if not MIN_PROVIDER_TIMEOUT_SECONDS <= timeout_seconds <= MAX_PROVIDER_TIMEOUT_SECONDS:
        raise ProviderReadinessFailure(
            "strategy_recommendation_provider_timeout_configuration_invalid"
        )
    return timeout_seconds


def run(
    *,
    output_root: Path | None = None,
    repo_root: Path = ROOT,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    output = Path(output_root or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT") or ROOT / "outputs")
    observed = (now or (lambda: datetime.now(timezone.utc)))()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    checked_at = observed.astimezone(timezone.utc).replace(microsecond=0)
    attestation = current_source_attestation(repo_root)
    config = dualtrack_config()
    planner = config.get("machine_planner") if isinstance(config.get("machine_planner"), dict) else {}
    command = str(planner.get("command") or "codex")
    timeout_seconds = DEFAULT_PROVIDER_READINESS_TIMEOUT_SECONDS
    receipt: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA,
        "checked_at": checked_at.isoformat(),
        "expires_at": (checked_at + timedelta(hours=24)).isoformat(),
        "status": "blocked",
        "source_sha": attestation.get("source_sha"),
        "source_tree_sha": attestation.get("source_tree_sha"),
        "provider": {
            "name": "codex_cli_chatgpt",
            "configured_command": command,
            "executable": None,
            "executable_sha256": None,
            "version": None,
            "auth_status": "unknown",
        },
        "timeout_seconds": timeout_seconds,
        "response_contract": {
            "status": "unknown",
            "required_keys": list(REQUIRED_RESPONSE_KEYS),
        },
        "operations": {
            "orders_allowed": False,
            "production_mutation_allowed": False,
            "uses_exchange_credentials": False,
        },
        "control_actions_executed": 0,
        "failure_code": None,
    }
    provider_trace = _provider_call_trace(
        deadline_seconds=DEFAULT_PROVIDER_READINESS_TIMEOUT_SECONDS
    )
    provider_started_monotonic = time.monotonic()
    receipt["provider_call"] = provider_trace
    try:
        timeout_seconds = resolve_provider_readiness_timeout(config)
        receipt["timeout_seconds"] = timeout_seconds
        provider_trace["deadline_seconds"] = timeout_seconds
        started_at = datetime.fromisoformat(provider_trace["started_at"])
        provider_trace["deadline_at"] = (
            started_at + timedelta(seconds=timeout_seconds)
        ).isoformat()
        resolution_started = time.monotonic()
        command_args = shlex.split(command)
        if not command_args:
            raise ProviderReadinessFailure("strategy_recommendation_provider_command_invalid")
        resolved = command_args[0] if os.path.isabs(command_args[0]) else shutil.which(command_args[0])
        if not resolved:
            raise ProviderReadinessFailure("strategy_recommendation_provider_missing")
        executable = Path(resolved).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ProviderReadinessFailure("strategy_recommendation_provider_not_executable")
        _record_phase(provider_trace, "command_resolution", resolution_started)
        receipt["provider"].update(
            {
                "executable": str(executable),
                "executable_sha256": _sha256(executable),
            }
        )
        env = _provider_env()
        version_started = time.monotonic()
        try:
            version = subprocess.run(
                [*command_args, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            _record_phase(provider_trace, "version", version_started)
            provider_trace["phase_return_codes"]["version"] = None
            _finish_provider_call_trace(
                provider_trace,
                provider_started_monotonic,
                stdout=exc.stdout,
                stderr=exc.stderr,
                timed_out=True,
                partial_output=bool(exc.stdout or exc.stderr),
            )
            raise ProviderReadinessFailure(
                "strategy_recommendation_provider_timeout"
            ) from exc
        _record_phase(provider_trace, "version", version_started)
        provider_trace["phase_return_codes"]["version"] = int(version.returncode)
        if version.returncode != 0:
            _finish_provider_call_trace(
                provider_trace,
                provider_started_monotonic,
                stdout=version.stdout,
                stderr=version.stderr,
                return_code=int(version.returncode),
            )
            raise ProviderReadinessFailure("strategy_recommendation_provider_failed")
        receipt["provider"]["version"] = _safe_line(version.stdout or version.stderr)
        login_started = time.monotonic()
        try:
            login = subprocess.run(
                [*command_args, "login", "status"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            _record_phase(provider_trace, "login", login_started)
            provider_trace["phase_return_codes"]["login"] = None
            _finish_provider_call_trace(
                provider_trace,
                provider_started_monotonic,
                stdout=exc.stdout,
                stderr=exc.stderr,
                timed_out=True,
                partial_output=bool(exc.stdout or exc.stderr),
            )
            raise ProviderReadinessFailure(
                "strategy_recommendation_provider_timeout"
            ) from exc
        _record_phase(provider_trace, "login", login_started)
        provider_trace["phase_return_codes"]["login"] = int(login.returncode)
        if not _auth_status_ready(login):
            _finish_provider_call_trace(
                provider_trace,
                provider_started_monotonic,
                stdout=login.stdout,
                stderr=login.stderr,
                return_code=int(login.returncode),
            )
            raise ProviderReadinessFailure("strategy_recommendation_provider_auth_not_ready")
        receipt["provider"]["auth_status"] = "logged_in"

        with tempfile.NamedTemporaryFile(prefix="cloud-ai-provider-", suffix=".json", delete=False) as handle:
            result_path = Path(handle.name)
        try:
            smoke_started = time.monotonic()
            try:
                smoke = subprocess.run(
                    [
                        *command_args,
                        "--ask-for-approval",
                        "never",
                        "exec",
                        "--ignore-user-config",
                        "--ephemeral",
                        "--model",
                        str(planner.get("model") or "gpt-5.4"),
                        "--sandbox",
                        "read-only",
                        "--cd",
                        str(repo_root),
                        "--output-last-message",
                        str(result_path),
                        "-",
                    ],
                    input=SMOKE_PROMPT,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                _record_phase(provider_trace, "smoke", smoke_started)
                provider_trace["phase_return_codes"]["smoke"] = None
                _finish_provider_call_trace(
                    provider_trace,
                    provider_started_monotonic,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    timed_out=True,
                    partial_output=bool(exc.stdout or exc.stderr),
                )
                raise
            _record_phase(provider_trace, "smoke", smoke_started)
            provider_trace["phase_return_codes"]["smoke"] = int(smoke.returncode)
            if smoke.returncode != 0:
                _finish_provider_call_trace(
                    provider_trace,
                    provider_started_monotonic,
                    stdout=smoke.stdout,
                    stderr=smoke.stderr,
                    return_code=int(smoke.returncode),
                )
                raise ProviderReadinessFailure("strategy_recommendation_provider_failed")
            output_started = time.monotonic()
            try:
                response = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _record_phase(provider_trace, "output_read", output_started)
                _finish_provider_call_trace(
                    provider_trace,
                    provider_started_monotonic,
                    stdout=smoke.stdout,
                    stderr=smoke.stderr,
                    return_code=int(smoke.returncode),
                )
                raise ProviderReadinessFailure("strategy_recommendation_provider_invalid_output") from exc
            _record_phase(provider_trace, "output_read", output_started)
            parse_started = time.monotonic()
            if not isinstance(response, dict) or not _valid_response(response):
                _record_phase(provider_trace, "parse", parse_started)
                _finish_provider_call_trace(
                    provider_trace,
                    provider_started_monotonic,
                    stdout=smoke.stdout,
                    stderr=smoke.stderr,
                    return_code=int(smoke.returncode),
                )
                raise ProviderReadinessFailure("strategy_recommendation_provider_invalid_output")
            _record_phase(provider_trace, "parse", parse_started)
            _finish_provider_call_trace(
                provider_trace,
                provider_started_monotonic,
                stdout=smoke.stdout,
                stderr=smoke.stderr,
                return_code=int(smoke.returncode),
            )
            receipt["response_contract"].update(
                {
                    "status": "pass",
                    "keys": sorted(response),
                    "direction": response["direction"],
                    "style": response["style"],
                    "ai_self_assessment_type": type(response["ai_self_assessment"]).__name__,
                }
            )
        finally:
            result_path.unlink(missing_ok=True)
        receipt["status"] = "pass"
    except ProviderReadinessFailure as exc:
        receipt["failure_code"] = str(exc) or "strategy_recommendation_provider_failed"
    except subprocess.TimeoutExpired:
        receipt["failure_code"] = "strategy_recommendation_provider_timeout"
    except OSError:
        receipt["failure_code"] = "strategy_recommendation_provider_unavailable"
    finally:
        if provider_trace["finished_at"] is None:
            _finish_provider_call_trace(provider_trace, provider_started_monotonic)
    receipt["readiness_digest"] = _digest(receipt)
    provider_root = output / "cloud" / "provider"
    path = provider_root / "readiness_current.json"
    history_path = (
        provider_root
        / "receipts"
        / (
            f"{checked_at.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{receipt['readiness_digest'][:12]}.json"
        )
    )
    write_json(path, [receipt])
    write_json(history_path, [receipt])
    last_success_path = provider_root / "readiness_last_success.json"
    if receipt["status"] == "pass":
        write_json(last_success_path, [receipt])
    return {
        "ok": receipt["status"] == "pass",
        "path": str(path),
        "history_path": str(history_path),
        "last_success_path": str(last_success_path),
        **receipt,
    }


def renew_if_due(
    *,
    output_root: Path | None = None,
    repo_root: Path = ROOT,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Renew on a six-hour proof age, while polling failure recovery every 5m."""

    output = Path(
        output_root
        or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        or ROOT / "outputs"
    )
    observed = (now or (lambda: datetime.now(timezone.utc)))()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc).replace(microsecond=0)
    verification = CloudAIProviderReadiness(
        output,
        repo_root=repo_root,
        now=lambda: observed,
        source_attestation=lambda: current_source_attestation(repo_root),
    ).verify()
    checked_at = _parse_timestamp(verification.get("checked_at"))
    age_seconds = (
        (observed - checked_at).total_seconds()
        if verification.get("ok") is True and checked_at is not None
        else None
    )
    if (
        verification.get("ok") is True
        and age_seconds is not None
        and age_seconds < RENEWAL_INTERVAL_SECONDS
    ):
        return {
            "ok": True,
            "status": "not_due",
            "renewed": False,
            "checked_at": observed.isoformat(),
            "current_readiness_checked_at": verification.get("checked_at"),
            "current_readiness_expires_at": verification.get("expires_at"),
            "current_readiness_digest": verification.get("readiness_digest"),
            "source_sha": verification.get("source_sha"),
            "source_tree_sha": verification.get("source_tree_sha"),
            "next_renewal_due_at": (
                checked_at + timedelta(seconds=RENEWAL_INTERVAL_SECONDS)
            ).isoformat(),
            "control_actions_executed": 0,
            "operations": {
                "orders_allowed": False,
                "production_mutation_allowed": False,
                "uses_exchange_credentials": False,
            },
        }
    result = run(
        output_root=output,
        repo_root=repo_root,
        now=lambda: observed,
    )
    return {
        **result,
        "renewed": True,
        "previous_readiness_blocker": verification.get("blocker"),
    }


def _provider_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = os.getenv("GRIDMIND_CODEX_HOME_ROOT", "/opt/gridmind")
    env["CODEX_HOME"] = os.getenv("GRIDMIND_CODEX_HOME", "/opt/gridmind/.codex")
    return env


def _valid_response(value: dict[str, Any]) -> bool:
    if any(key not in value for key in REQUIRED_RESPONSE_KEYS):
        return False
    if str(value.get("direction") or "").lower() not in {"neutral", "long", "short"}:
        return False
    if str(value.get("style") or "").lower() not in {"steady", "aggressive"}:
        return False
    if not str(value.get("rationale") or "").strip():
        return False
    try:
        score = float(value.get("ai_self_assessment"))
    except (TypeError, ValueError):
        return False
    return 1 <= score <= 10


def _safe_line(value: str) -> str:
    return str(value).strip().splitlines()[0][:200]


def _auth_status_ready(result: subprocess.CompletedProcess[str]) -> bool:
    """Accept the CLI's status stream without weakening its exit-code gate.

    Codex CLI currently writes the successful human-readable login status to
    stderr.  Both streams are intentionally inspected, while a non-zero exit
    remains a hard failure and missing text remains fail-closed.
    """

    if result.returncode != 0:
        return False
    combined = "\n".join((str(result.stdout or ""), str(result.stderr or ""))).lower()
    return "logged in" in combined


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Cloud Paper AI provider readiness check.")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--renew-if-due", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = (
        renew_if_due(output_root=args.output_root, repo_root=args.repo_root)
        if args.renew_if_due
        else run(output_root=args.output_root, repo_root=args.repo_root)
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_ai_provider_readiness: {'pass' if result['ok'] else 'blocked'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
