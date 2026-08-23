"""Opt-in external position-protection composition for Testnet strategy hosts.

This module only assembles the reviewed standard-broker public profile.  It is
not a DCA/Grid lifecycle and does not authorize an order by itself.  Strategy
admission remains a separate trading-system gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re


PROTECTION_CAPABILITY_REVISION = "hyperliquid-testnet-position-protection-runtime-v1"
PROTECTION_PROFILE_ID = "hyperliquid-testnet-position-protection"
DEFAULT_CREDENTIAL_REFERENCE = "file-secret://hyperliquid-testnet"
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class StandardBrokerExternalProtectionError(RuntimeError):
    """Redacted blocker for opt-in external protection composition."""


@dataclass(frozen=True)
class ExternalProtectionBuildConfig:
    account_address: str
    runtime_id: str
    release_sha: str
    approval_id: str
    approved_by: str
    secret_file: Path
    credential_reference: str = DEFAULT_CREDENTIAL_REFERENCE
    capability_revision: str = PROTECTION_CAPABILITY_REVISION

    def __post_init__(self) -> None:
        for name in (
            "account_address",
            "runtime_id",
            "release_sha",
            "approval_id",
            "approved_by",
            "credential_reference",
        ):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
        if not _SHA1.fullmatch(self.release_sha):
            raise ValueError("release_sha must be a lowercase 40-character SHA")
        if self.capability_revision != PROTECTION_CAPABILITY_REVISION:
            raise ValueError("only the reviewed position-protection capability revision is supported")


def build_external_position_protection_binding(
    config: ExternalProtectionBuildConfig,
) -> tuple[object, object]:
    """Build ``(runtime, binding)`` for the exact opt-in Testnet profile.

    Runtime start performs only release/account/capability preflight.  The
    signer provider retains an opaque path and resolves the secret only when a
    later protection operation reaches the external backend.
    """

    try:
        from standard_broker import (
            AccountReference,
            AccountScope,
            BrokerEnvironment,
            BrokerRuntimeSession,
            ExternalBrokerBuildContext,
            ExternalEnvironmentApproval,
            ExternalRuntimeIdentity,
            RuntimeActivationPolicy,
            SignerKind,
            SignerReference,
            build_hyperliquid_testnet_position_protection_binding_from_runtime,
            enabled_testnet_position_protection_capabilities,
        )
        from standard_broker.adapters.hyperliquid import (
            HyperliquidTestnetBackendConfig,
            LocalFileSecretProvider,
            NautilusHyperliquidRuntime,
            NautilusHyperliquidTestnetBackend,
            NautilusRuntimeConfig,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise StandardBrokerExternalProtectionError(
            "standard_broker_protection_dependency_unavailable"
        ) from exc

    capabilities = enabled_testnet_position_protection_capabilities(
        config.capability_revision
    )
    signer = SignerReference(
        SignerKind.API_AGENT,
        "local-file",
        config.credential_reference,
    )
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, config.account_address),
        signer=signer,
        signer_provider=LocalFileSecretProvider(
            {config.credential_reference: Path(config.secret_file)}
        ),
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id=config.runtime_id,
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id=config.approval_id,
        release_sha=config.release_sha,
        approved_by=config.approved_by,
        approved_at=datetime.now(timezone.utc),
        account_address=config.account_address,
        lifecycle_id=config.runtime_id,
    )
    backend_config = HyperliquidTestnetBackendConfig(
        account_address=config.account_address,
        capability_revision=config.capability_revision,
        capabilities=capabilities,
    )
    backend = NautilusHyperliquidTestnetBackend(
        session=session,
        config=backend_config,
        secrets=session.signer_provider,  # type: ignore[arg-type]
    )
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=backend_config.expected_version,
            expected_commit=backend_config.expected_commit,
            policy=RuntimeActivationPolicy(testnet_approval=approval),
            expected_release_sha=config.release_sha,
        ),
    )
    try:
        runtime.start()
        context = ExternalBrokerBuildContext(
            session=session,
            runtime_identity=ExternalRuntimeIdentity(
                adapter_id=backend.metadata.package,
                version=backend.metadata.version,
                commit=backend.metadata.commit,
                mapping_revision=config.capability_revision,
                transport_state="external_testnet",
            ),
            release_sha=config.release_sha,
            approval=approval,
        )
        binding = build_hyperliquid_testnet_position_protection_binding_from_runtime(
            context=context,
            runtime=runtime,
        )
        matrix = getattr(binding, "protection_capabilities", None)
        if matrix is None:
            raise StandardBrokerExternalProtectionError(
                "external_protection_profile_missing"
            )
        if matrix.profile_id != "hyperliquid-testnet-position-protection-v1":
            raise StandardBrokerExternalProtectionError(
                "external_protection_profile_mismatch"
            )
        preflight = runtime.preflight(
            required_operations={"protection_order": {"submit", "query"}}
        )
        if (
            preflight.accepted is not True
            or preflight.external_network is not True
            or preflight.real_money_eligible is not False
            or preflight.account_address != config.account_address
            or preflight.lifecycle_id != config.runtime_id
            or preflight.release_sha != config.release_sha
            or preflight.capability_revision != config.capability_revision
        ):
            raise StandardBrokerExternalProtectionError(
                "external_protection_preflight_identity_mismatch"
            )
        if (
            binding.runtime_session.account.address != config.account_address
            or binding.runtime_session.lifecycle_id != config.runtime_id
            or binding.runtime_session.capability_revision != config.capability_revision
        ):
            raise StandardBrokerExternalProtectionError(
                "external_protection_binding_identity_mismatch"
            )
        return runtime, binding
    except StandardBrokerExternalProtectionError:
        runtime.close()
        raise
    except Exception as exc:  # noqa: BLE001 - redact provider details at host boundary.
        runtime.close()
        raise StandardBrokerExternalProtectionError(
            f"external_protection_build_blocked:{type(exc).__name__}"
        ) from exc
