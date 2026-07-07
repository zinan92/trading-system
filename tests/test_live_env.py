from pathlib import Path

from services.journal_store import load_json
from services.live_env import LiveEnvStatus, apply_live_env, initialize_live_env, live_env_value_present, parse_env_file


def test_parse_env_file_supports_quotes_and_comments(tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text(
        "\n".join(
            [
                "# secret values stay local",
                'OANDA_API_TOKEN="token-value"',
                "OANDA_ACCOUNT_ID=account-1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert parse_env_file(env) == {"OANDA_API_TOKEN": "token-value", "OANDA_ACCOUNT_ID": "account-1"}


def test_apply_live_env_sets_missing_keys_without_override(monkeypatch, tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=file-token\nOANDA_ACCOUNT_ID=file-account\n", encoding="utf-8")
    monkeypatch.setenv("OANDA_API_TOKEN", "existing-token")
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = apply_live_env(env)

    assert result["exists"] is True
    assert result["loaded_keys"] == ["OANDA_ACCOUNT_ID"]
    assert result["available_keys"] == ["OANDA_ACCOUNT_ID", "OANDA_API_TOKEN"]
    assert "existing-token" == __import__("os").getenv("OANDA_API_TOKEN")
    assert "file-account" == __import__("os").getenv("OANDA_ACCOUNT_ID")


def test_apply_live_env_does_not_load_placeholder_credentials(monkeypatch, tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=CHANGE_ME\nOANDA_ACCOUNT_ID=your_account_id\n", encoding="utf-8")
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = apply_live_env(env)

    assert result["loaded_keys"] == []
    assert __import__("os").getenv("OANDA_API_TOKEN") is None
    assert live_env_value_present("OANDA_API_TOKEN") is False


def test_initialize_live_env_creates_file_with_private_permissions(tmp_path: Path):
    env = tmp_path / "configs" / "live.env"
    template = tmp_path / "configs" / "live.env.template"
    template.parent.mkdir()
    template.write_text("OANDA_API_TOKEN=\nOANDA_ACCOUNT_ID=\n", encoding="utf-8")

    result = initialize_live_env(env, template)

    assert result["created"] is True
    assert result["overwritten"] is False
    assert result["permissions"]["mode"] == "0o600"
    assert env.exists()
    assert "OANDA_API_TOKEN=" in env.read_text(encoding="utf-8")


def test_initialize_live_env_does_not_overwrite_existing_file_without_force(tmp_path: Path):
    env = tmp_path / "live.env"
    template = tmp_path / "live.env.template"
    env.write_text("OANDA_API_TOKEN=real-token\n", encoding="utf-8")
    env.chmod(0o644)
    template.write_text("OANDA_API_TOKEN=\n", encoding="utf-8")

    result = initialize_live_env(env, template)

    assert result["created"] is False
    assert result["overwritten"] is False
    assert result["permissions"]["mode"] == "0o600"
    assert env.read_text(encoding="utf-8") == "OANDA_API_TOKEN=real-token\n"


def test_initialize_live_env_force_overwrites_from_template(tmp_path: Path):
    env = tmp_path / "live.env"
    template = tmp_path / "live.env.template"
    env.write_text("OANDA_API_TOKEN=real-token\n", encoding="utf-8")
    template.write_text("OANDA_API_TOKEN=\nOANDA_ACCOUNT_ID=\n", encoding="utf-8")

    result = initialize_live_env(env, template, force=True)

    assert result["created"] is False
    assert result["overwritten"] is True
    assert result["permissions"]["mode"] == "0o600"
    assert env.read_text(encoding="utf-8") == "OANDA_API_TOKEN=\nOANDA_ACCOUNT_ID=\n"


def test_live_env_status_masks_values_and_writes_artifact(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=secret-key\nBINANCE_API_SECRET=secret-secret\n", encoding="utf-8")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    result = LiveEnvStatus(root, env).run("2026-05-26")

    assert result["status"] in {"pass", "warn"}
    assert result["present_keys"] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    assert result["file_keys"] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    assert "secret_hygiene" in result
    serialized = str(load_json(root / "live_env" / "current.json"))
    assert "secret-key" not in serialized
    assert "secret-secret" not in serialized


def test_live_env_status_reports_secret_hygiene(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=CHANGE_ME\nOANDA_ACCOUNT_ID=acct\n", encoding="utf-8")
    env.chmod(0o644)
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    result = LiveEnvStatus(root, env).run("2026-05-26")

    hygiene = result["secret_hygiene"]
    assert result["status"] == "warn"
    assert result["present_keys"] == []
    assert result["missing_keys"] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    assert hygiene["status"] == "warn"
    assert "OANDA_API_TOKEN" in hygiene["placeholder_keys"]
    assert hygiene["permissions"]["group_or_other_access"] is True


def test_live_env_status_requires_tiger_props_path(monkeypatch, tmp_path: Path):
    from services import live_env as live_env_module

    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("TIGER_OPENAPI_CONFIG_PATH=/secure/tiger_openapi_config.properties\n", encoding="utf-8")
    env.chmod(0o600)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("TIGER_OPENAPI_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        live_env_module,
        "load_pipeline_config",
        lambda: {
            "output_root": str(root),
            "broker": {"provider": "tiger_openapi", "props_path_env": "TIGER_OPENAPI_CONFIG_PATH"},
        },
    )

    result = live_env_module.LiveEnvStatus(output_root=root, env_path=env).run("2026-07-05")

    assert result["status"] == "pass"
    assert result["required_keys"] == ["TIGER_OPENAPI_CONFIG_PATH"]
    assert "TIGER_OPENAPI_CONFIG_PATH" in result["present_keys"]


def test_live_env_status_resolves_tiger_broker_profile(monkeypatch, tmp_path: Path):
    from services import live_env as live_env_module

    root = tmp_path / "outputs"
    env = tmp_path / "live.env"
    env.write_text("TIGER_OPENAPI_CONFIG_PATH=/secure/tiger_openapi_config.properties\n", encoding="utf-8")
    env.chmod(0o600)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("TIGER_OPENAPI_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        live_env_module,
        "load_pipeline_config",
        lambda: {
            "output_root": str(root),
            "broker": {"profile": "tiger_openapi_paper"},
            "broker_profiles": {
                "tiger_openapi_paper": {
                    "provider": "tiger_openapi",
                    "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
                }
            },
        },
    )

    result = live_env_module.LiveEnvStatus(output_root=root, env_path=env).run("2026-07-05")

    assert result["status"] == "pass"
    assert result["required_keys"] == ["TIGER_OPENAPI_CONFIG_PATH"]
