"""Secrets posture audit — reports whether secrets are configured WITHOUT ever
emitting their values, and checks the secret file's permissions and gitignore
coverage.

Deliberately redacted: the payload contains only key names + booleans, never a
secret value, so the artifact is safe to write to disk / serve on the dashboard.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import is_placeholder_value

# Secret-bearing env keys the system may use. Account ID / chat ID are not
# strictly secret but are credentials worth tracking; values are never emitted.
KNOWN_SECRET_KEYS = [
    "OANDA_API_TOKEN",
    "OANDA_ACCOUNT_ID",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TRADING_ORCHESTRATOR_FEISHU_WEBHOOK_URL",
    "TRADING_ORCHESTRATOR_FEISHU_SECRET",
    "TRADING_ORCHESTRATOR_REPORT_FEISHU_WEBHOOK_URL",
    "TRADING_ORCHESTRATOR_REPORT_FEISHU_SECRET",
    "TRADING_ORCHESTRATOR_FEISHU_REPORT_WEBHOOK_URL",
    "TRADING_ORCHESTRATOR_FEISHU_REPORT_SECRET",
]


class SecretsAudit:
    def __init__(self, output_root: Path | None = None, env_path: Path | None = None, repo_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.repo_root = Path(repo_root) if repo_root else ROOT
        self.env_path = Path(env_path) if env_path else Path(os.getenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(self.repo_root / "configs" / "live.env")))

    def run(self, run_date: str) -> dict:
        secrets = [
            {
                "key": key,
                "configured": bool(os.getenv(key)) and not is_placeholder_value(os.getenv(key)),
                "placeholder": is_placeholder_value(os.getenv(key)) and os.getenv(key) is not None and os.getenv(key) != "",
            }
            for key in KNOWN_SECRET_KEYS
        ]
        checks = [self._file_permissions_check(), self._gitignore_check()]
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "env_path": str(self.env_path),
            "secrets": secrets,  # booleans only — never values
            "checks": checks,
        }
        write_json(self.output_root / "secrets_audit" / "current.json", [payload])
        write_json(self.output_root / "secrets_audit" / f"{run_date}.json", [payload])
        return payload

    def _file_permissions_check(self) -> dict:
        if not self.env_path.exists():
            return self._check("env_file_permissions", "warn", "no live env file present (paper-only is fine).", {"exists": False})
        mode = stat.S_IMODE(self.env_path.stat().st_mode)
        group_other_readable = bool(mode & 0o077)
        if group_other_readable:
            return self._check(
                "env_file_permissions",
                "fail",
                f"secret file is group/other-accessible (mode {oct(mode)}); run `chmod 600 {self.env_path}`.",
                {"mode": oct(mode)},
            )
        return self._check("env_file_permissions", "pass", "secret file is owner-only (0600).", {"mode": oct(mode)})

    def _gitignore_check(self) -> dict:
        gitignore = self.repo_root / ".gitignore"
        if not gitignore.exists():
            return self._check("env_gitignored", "warn", "no .gitignore found — cannot confirm secrets are excluded.", {})
        patterns = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
        name = self.env_path.name
        try:
            rel = str(self.env_path.relative_to(self.repo_root))
        except ValueError:
            rel = name
        covered = any(p in {name, rel, "*.env", "configs/live.env"} or (p.endswith("*.env") and name.endswith(".env")) for p in patterns)
        if covered:
            return self._check("env_gitignored", "pass", "live env file is covered by .gitignore.", {})
        return self._check("env_gitignored", "warn", f"live env file ({rel}) is not in .gitignore — risk of committing secrets.", {})

    def _rollup(self, checks: list[dict]) -> str:
        states = {c["status"] for c in checks}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}


def run_secrets_audit(run_date: str, output_root: Path | None = None) -> dict:
    return SecretsAudit(output_root=output_root).run(run_date)
