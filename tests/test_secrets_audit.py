import json
import stat
from pathlib import Path

from services.secrets_audit import SecretsAudit


def test_audit_never_leaks_secret_values(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OANDA_API_TOKEN", "super-secret-abc123")
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=super-secret-abc123\n", encoding="utf-8")
    env.chmod(0o600)
    result = SecretsAudit(output_root=tmp_path / "out", env_path=env, repo_root=tmp_path).run("2026-05-31")

    blob = json.dumps(result)
    assert "super-secret-abc123" not in blob  # redaction is the whole point
    oanda = next(s for s in result["secrets"] if s["key"] == "OANDA_API_TOKEN")
    assert oanda["configured"] is True
    assert oanda["placeholder"] is False


def test_audit_flags_placeholder_and_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OANDA_API_TOKEN", "changeme")  # placeholder
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)  # missing
    result = SecretsAudit(output_root=tmp_path / "out", env_path=tmp_path / "live.env", repo_root=tmp_path).run("2026-05-31")
    by_key = {s["key"]: s for s in result["secrets"]}
    assert by_key["OANDA_API_TOKEN"]["placeholder"] is True
    assert by_key["OANDA_API_TOKEN"]["configured"] is False
    assert by_key["BINANCE_API_KEY"]["configured"] is False


def test_audit_fails_on_world_readable_env_file(tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=x\n", encoding="utf-8")
    env.chmod(0o644)  # group/other readable — a secret file must be 0600
    result = SecretsAudit(output_root=tmp_path / "out", env_path=env, repo_root=tmp_path).run("2026-05-31")
    perm = next(c for c in result["checks"] if c["name"] == "env_file_permissions")
    assert perm["status"] == "fail"
    assert result["status"] == "fail"


def test_audit_warns_when_env_not_gitignored(tmp_path: Path):
    env = tmp_path / "configs" / "live.env"
    env.parent.mkdir(parents=True)
    env.write_text("OANDA_API_TOKEN=x\n", encoding="utf-8")
    env.chmod(0o600)
    # repo with a .gitignore that does NOT cover live.env
    (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    result = SecretsAudit(output_root=tmp_path / "out", env_path=env, repo_root=tmp_path).run("2026-05-31")
    gi = next(c for c in result["checks"] if c["name"] == "env_gitignored")
    assert gi["status"] in {"warn", "fail"}


def test_audit_passes_clean_setup(tmp_path: Path):
    env = tmp_path / "configs" / "live.env"
    env.parent.mkdir(parents=True)
    env.write_text("OANDA_API_TOKEN=x\n", encoding="utf-8")
    env.chmod(0o600)
    (tmp_path / ".gitignore").write_text("configs/live.env\n", encoding="utf-8")
    result = SecretsAudit(output_root=tmp_path / "out", env_path=env, repo_root=tmp_path).run("2026-05-31")
    assert next(c for c in result["checks"] if c["name"] == "env_file_permissions")["status"] == "pass"
    assert next(c for c in result["checks"] if c["name"] == "env_gitignored")["status"] == "pass"
