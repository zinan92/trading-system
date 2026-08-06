"""Run the bounded, side-effect-free Cloud Paper provider readiness check."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.cloud_ai_provider import READINESS_SCHEMA, _digest, _sha256
from services.config_loader import ROOT
from services.dualtrack_config import dualtrack_config
from services.journal_store import write_json
from services.paper_release_receipt import current_source_attestation


REQUIRED_RESPONSE_KEYS = ("direction", "style", "rationale", "ai_self_assessment")
SMOKE_PROMPT = (
    "Return exactly one JSON object with keys direction, style, rationale, "
    "ai_self_assessment. Use direction neutral, style steady, rationale "
    "provider readiness smoke, and ai_self_assessment 5. No markdown."
)


def run(*, output_root: Path | None = None, repo_root: Path = ROOT) -> dict[str, Any]:
    output = Path(output_root or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT") or ROOT / "outputs")
    checked_at = datetime.now(timezone.utc).replace(microsecond=0)
    attestation = current_source_attestation(repo_root)
    config = dualtrack_config()
    planner = config.get("machine_planner") if isinstance(config.get("machine_planner"), dict) else {}
    command = str(planner.get("command") or "codex")
    timeout_seconds = min(
        60,
        max(5, int((config.get("convergence") or {}).get("provider_timeout_seconds") or 25)),
    )
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
        "failure_code": None,
    }
    try:
        command_args = shlex.split(command)
        if not command_args:
            raise ProviderReadinessFailure("strategy_recommendation_provider_command_invalid")
        resolved = command_args[0] if os.path.isabs(command_args[0]) else shutil.which(command_args[0])
        if not resolved:
            raise ProviderReadinessFailure("strategy_recommendation_provider_missing")
        executable = Path(resolved).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ProviderReadinessFailure("strategy_recommendation_provider_not_executable")
        receipt["provider"].update(
            {
                "executable": str(executable),
                "executable_sha256": _sha256(executable),
            }
        )
        env = _provider_env()
        version = subprocess.run(
            [*command_args, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        if version.returncode != 0:
            raise ProviderReadinessFailure("strategy_recommendation_provider_failed")
        receipt["provider"]["version"] = _safe_line(version.stdout or version.stderr)
        login = subprocess.run(
            [*command_args, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        if not _auth_status_ready(login):
            raise ProviderReadinessFailure("strategy_recommendation_provider_auth_not_ready")
        receipt["provider"]["auth_status"] = "logged_in"

        with tempfile.NamedTemporaryFile(prefix="cloud-ai-provider-", suffix=".json", delete=False) as handle:
            result_path = Path(handle.name)
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
            if smoke.returncode != 0:
                raise ProviderReadinessFailure("strategy_recommendation_provider_failed")
            try:
                response = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProviderReadinessFailure("strategy_recommendation_provider_invalid_output") from exc
            if not isinstance(response, dict) or not _valid_response(response):
                raise ProviderReadinessFailure("strategy_recommendation_provider_invalid_output")
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
    receipt["readiness_digest"] = _digest(receipt)
    path = output / "cloud" / "provider" / "readiness_current.json"
    write_json(path, [receipt])
    return {"ok": receipt["status"] == "pass", "path": str(path), **receipt}


class ProviderReadinessFailure(RuntimeError):
    pass


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Cloud Paper AI provider readiness check.")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run(output_root=args.output_root, repo_root=args.repo_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"cloud_ai_provider_readiness: {'pass' if result['ok'] else 'blocked'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
