from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


DEFAULT_ENV_PATH = ROOT / "configs" / "live.env"
PLACEHOLDER_VALUES = {"", "changeme", "change_me", "todo", "placeholder", "your-token", "your_account_id"}


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def apply_live_env(env_path: Path | None = None, override: bool = False) -> dict:
    path = env_path or Path(os.getenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(DEFAULT_ENV_PATH)))
    values = parse_env_file(path)
    loaded = []
    for key, value in values.items():
        if is_placeholder_value(value):
            continue
        if override or not os.getenv(key):
            os.environ[key] = value
            loaded.append(key)
    return {"path": str(path), "exists": path.exists(), "loaded_keys": sorted(loaded), "available_keys": sorted(values)}


def is_placeholder_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDER_VALUES


def live_env_value_present(key: str) -> bool:
    value = os.getenv(key)
    return bool(value) and not is_placeholder_value(value)


def initialize_live_env(env_path: Path | None = None, template_path: Path | None = None, force: bool = False) -> dict:
    target = env_path or Path(os.getenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(DEFAULT_ENV_PATH)))
    template = template_path or ROOT / "configs" / "live.env.template"
    created = False
    overwritten = False
    if target.exists() and not force:
        target.chmod(0o600)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if template.exists():
            content = template.read_text(encoding="utf-8")
        else:
            content = "OANDA_API_TOKEN=\nOANDA_ACCOUNT_ID=\n"
        overwritten = target.exists()
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
        created = not overwritten
    permissions = _permissions_for(target)
    return {
        "path": str(target),
        "template_path": str(template),
        "template_exists": template.exists(),
        "created": created,
        "overwritten": overwritten,
        "exists": target.exists(),
        "permissions": permissions,
        "message": "live env file is initialized; fill values locally and keep permissions at 600",
    }


class LiveEnvStatus:
    def __init__(self, output_root: Path | None = None, env_path: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.env_path = env_path or Path(os.getenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(DEFAULT_ENV_PATH)))
        self.oanda_config = config.get("oanda_feed", {})
        self.broker_config = self._resolve_broker_config(config)

    def run(self, run_date: str) -> dict:
        file_values = parse_env_file(self.env_path)
        required = self._required_keys()
        present = [
            key
            for key in required
            if live_env_value_present(key) or (bool(file_values.get(key)) and not is_placeholder_value(file_values.get(key)))
        ]
        missing = [key for key in required if key not in present]
        hygiene = self._secret_hygiene(file_values)
        status = "pass" if not missing and hygiene["status"] == "pass" else "warn"
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "env_path": str(self.env_path),
            "env_file_exists": self.env_path.exists(),
            "template_path": str(ROOT / "configs" / "live.env.template"),
            "template_exists": (ROOT / "configs" / "live.env.template").exists(),
            "required_keys": required,
            "present_keys": present,
            "missing_keys": missing,
            "file_keys": sorted(file_values),
            "secret_hygiene": hygiene,
            "note": "Values are intentionally not written to artifacts.",
        }
        write_json(self.output_root / "live_env" / "current.json", [payload])
        write_json(self.output_root / "live_env" / f"{run_date}.json", [payload])
        return payload

    def _required_keys(self) -> list[str]:
        keys: set[str] = set()
        provider = str(self.broker_config.get("provider", "manual_gateway"))
        if provider == "oanda_rest":
            keys.add(str(self.broker_config.get("api_key_env") or self.broker_config.get("token_env") or "OANDA_API_TOKEN"))
            keys.add(str(self.broker_config.get("account_id_env", "OANDA_ACCOUNT_ID")))
        elif provider == "binance_usdm":
            keys.add(str(self.broker_config.get("api_key_env", "BINANCE_API_KEY")))
            keys.add(str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET")))
        elif provider == "tiger_openapi":
            keys.add(str(self.broker_config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH")))
        elif provider not in {"manual_gateway", "mt5_file_bridge"}:
            keys.add(str(self.broker_config.get("api_key_env", "BROKER_API_KEY")))
            keys.add(str(self.broker_config.get("account_id_env", "BROKER_ACCOUNT_ID")))
        return sorted(keys)

    def _resolve_broker_config(self, config: dict) -> dict:
        broker_config = dict(config.get("broker", {}) or {})
        profile_name = str(config.get("broker_profile") or broker_config.get("broker_profile") or broker_config.get("profile") or "").strip()
        if not profile_name:
            return broker_config
        profiles = config.get("broker_profiles", {}) or {}
        profile = dict(profiles.get(profile_name, {}) or {})
        overrides = {
            key: value
            for key, value in broker_config.items()
            if key not in {"profile", "broker_profile"}
        }
        return {**profile, **overrides, "profile": profile_name}

    def _secret_hygiene(self, file_values: dict[str, str]) -> dict:
        template_path = ROOT / "configs" / "live.env.template"
        ignored = self._is_live_env_ignored()
        permissions = self._permissions()
        placeholder_keys = [
            key
            for key, value in file_values.items()
            if is_placeholder_value(value)
        ]
        issues = []
        if not template_path.exists():
            issues.append("configs/live.env.template is missing")
        if not ignored:
            issues.append("configs/live.env is not protected by .gitignore")
        if permissions.get("exists") and permissions.get("group_or_other_access"):
            issues.append("configs/live.env is readable or writable by group/other")
        if placeholder_keys:
            issues.append("configs/live.env contains placeholder values")
        return {
            "status": "pass" if not issues else "warn",
            "issues": issues,
            "gitignore_protects_live_env": ignored,
            "permissions": permissions,
            "placeholder_keys": sorted(placeholder_keys),
        }

    def _is_live_env_ignored(self) -> bool:
        gitignore = ROOT / ".gitignore"
        if not gitignore.exists():
            return False
        entries = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")}
        return any(item in entries for item in {"configs/live.env", "/configs/live.env", "configs/*.env", "*.env"})

    def _permissions(self) -> dict:
        return _permissions_for(self.env_path)


def _permissions_for(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "mode": "", "group_or_other_access": False}
    mode = path.stat().st_mode & 0o777
    return {
        "exists": True,
        "mode": oct(mode),
        "group_or_other_access": bool(mode & 0o077),
    }
