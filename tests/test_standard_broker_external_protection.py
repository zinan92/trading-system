from __future__ import annotations

from pathlib import Path

import pytest

from services.standard_broker_external_protection import (
    ExternalProtectionBuildConfig,
    PROTECTION_CAPABILITY_REVISION,
    StandardBrokerExternalProtectionError,
    build_external_position_protection_binding,
)


ACCOUNT = "0x" + "11" * 20
RELEASE = "a" * 40


def test_opt_in_protection_builder_starts_without_backend_invocation(tmp_path: Path) -> None:
    with pytest.raises(
        StandardBrokerExternalProtectionError,
        match="standard_broker_protection_dependency_unavailable",
    ):
        build_external_position_protection_binding(
            ExternalProtectionBuildConfig(
                account_address=ACCOUNT,
                runtime_id="dca-protection-runtime-1",
                release_sha=RELEASE,
                approval_id="approval-protection-1",
                approved_by="park",
                secret_file=tmp_path / "not-read-secret",
            )
        )


def test_protection_builder_rejects_unreviewed_revision_before_runtime() -> None:
    with pytest.raises(ValueError, match="capability revision"):
        ExternalProtectionBuildConfig(
            account_address=ACCOUNT,
            runtime_id="dca-protection-runtime-1",
            release_sha=RELEASE,
            approval_id="approval-protection-1",
            approved_by="park",
            secret_file=Path("/tmp/never-read-secret"),
            capability_revision="unreviewed",
        )


def test_protection_builder_exports_exact_revision() -> None:
    assert PROTECTION_CAPABILITY_REVISION == "hyperliquid-testnet-position-protection-runtime-v1"
