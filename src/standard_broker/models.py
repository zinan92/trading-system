"""Provider-neutral identity and provenance vocabulary."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class BrokerEnvironment(str, Enum):
    """Execution environment owned by a Broker boundary."""

    PAPER = "paper"
    TESTNET = "testnet"
    MAINNET = "mainnet"


class AccountScope(str, Enum):
    """Account identity scope used for Broker observations."""

    MASTER = "master"
    SUBACCOUNT = "subaccount"
    VAULT = "vault"


class SignerKind(str, Enum):
    """Kind of identity that can authorize Broker actions."""

    NONE = "none"
    WALLET = "wallet"
    API_AGENT = "api_agent"


@dataclass(frozen=True)
class BrokerIdentity:
    """Public identity and environment of one Broker composition."""

    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope = AccountScope.MASTER
    account_address: str | None = None
    signer_kind: SignerKind = SignerKind.NONE
    execution_scope: str = "default"

    def __post_init__(self) -> None:
        if not self.broker_id or self.broker_id != self.broker_id.strip():
            raise ValueError("broker_id must be a non-empty trimmed string")
        if self.broker_id != self.broker_id.lower():
            raise ValueError("broker_id must be lowercase")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("environment must be a BrokerEnvironment")
        if not isinstance(self.account_scope, AccountScope):
            raise TypeError("account_scope must be an AccountScope")
        if not isinstance(self.signer_kind, SignerKind):
            raise TypeError("signer_kind must be a SignerKind")
        if not self.execution_scope or self.execution_scope != self.execution_scope.strip():
            raise ValueError("execution_scope must be a non-empty trimmed string")


@dataclass(frozen=True)
class Provenance:
    """Source facts attached to a canonical Broker observation or receipt."""

    source: str
    execution_scope: str
    transport_state: str
    mapping_revision: str
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    venue_timestamp: datetime | None = None
    raw_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("source", "execution_scope", "transport_state", "mapping_revision"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
