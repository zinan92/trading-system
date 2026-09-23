"""Public typed external ProtectionOrder binding.

This module owns no venue serialization.  It composes the public external host
authorization/fact seam and turns the provider-neutral ``ProtectionGroup``
request into a redacted, cursor-bound protection observation.  A profile with
missing capability values fails before the runtime is invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from .errors import BrokerCapabilityError, RuntimeBoundaryError
from .external_host import (
    ExternalBrokerHost,
    ExternalFactEnvelope,
    ExternalHostRequest,
    digest_canonical,
)
from .host import CanonicalHostRequest
from .models import BrokerEnvironment, Provenance
from .protection import (
    ProtectionCapabilityMatrix,
    ProtectionGroup,
    ProtectionLifecycleState,
    ProtectionLifecycleStatus,
    ProtectionReceipt,
)
from .runtime import BrokerRuntimeSession


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,240}$")
_STATES = {
    "submitted": ProtectionLifecycleState.SUBMITTED,
    "active": ProtectionLifecycleState.ACTIVE,
    "canceled": ProtectionLifecycleState.CANCELED,
    "cancelled": ProtectionLifecycleState.CANCELED,
    "frozen": ProtectionLifecycleState.FROZEN,
    "unknown": ProtectionLifecycleState.UNKNOWN,
    "rejected": ProtectionLifecycleState.UNKNOWN,
}


@dataclass(frozen=True)
class ExternalProtectionObservation:
    """Canonical remote observation for one ProtectionOrder group."""

    protection_id: str
    operation: str
    state: ProtectionLifecycleState
    accepted: bool
    covered_quantity: Decimal
    order_ids: tuple[str, ...]
    observed_at: datetime
    provenance: Provenance
    observation_digest: str

    def __post_init__(self) -> None:
        for name in ("protection_id", "operation", "observation_digest"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.observation_digest.lower()):
            raise ValueError("observation_digest must be a sha256 digest")
        if not isinstance(self.state, ProtectionLifecycleState):
            raise TypeError("protection observation state must be canonical")
        if not isinstance(self.accepted, bool):
            raise TypeError("protection observation accepted must be bool")
        if not self.covered_quantity.is_finite() or self.covered_quantity < 0:
            raise ValueError("protection covered quantity must be finite and non-negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("protection observation timestamp must be timezone-aware")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("protection observation provenance must be canonical")
        for order_id in self.order_ids:
            if not _SAFE_ID.fullmatch(str(order_id)):
                raise ValueError("protection observation contains an invalid order identity")


@dataclass(frozen=True)
class ExternalProtectionReceipt:
    """Canonical receipt enriched with the remote observation evidence."""

    canonical: ProtectionReceipt
    observation: ExternalProtectionObservation

    def __getattr__(self, name: str) -> object:
        canonical = object.__getattribute__(self, "canonical")
        return getattr(canonical, name)


@runtime_checkable
class ExternalProtectionPort(Protocol):
    """Public canonical protection port consumed by strategy hosts."""

    @property
    def runtime_session(self) -> BrokerRuntimeSession:
        ...

    @property
    def local_only(self) -> bool:
        ...

    @property
    def transport_state(self) -> str:
        ...

    def submit(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        ...

    def cancel(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        ...

    def replace(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        ...

    def reconcile(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        ...


class ExternalProtectionBinding:
    """Typed protection facade over one exact external host profile."""

    name = "hyperliquid_external_protection_order"

    def __init__(self, *, host: ExternalBrokerHost) -> None:
        if not isinstance(host, ExternalBrokerHost):
            raise TypeError("external protection binding requires the public ExternalBrokerHost")
        if host.identity.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "external_protection_environment_invalid",
                "external protection binding is Testnet-only",
            )
        if host.runtime_identity.transport_state != "external_testnet":
            raise RuntimeBoundaryError(
                "external_protection_transport_invalid",
                "external protection binding requires external_testnet transport",
            )
        if not callable(getattr(host, "read_fact", None)):
            raise TypeError("external protection binding requires the public typed fact seam")
        self._host = host
        self._statuses: dict[str, ProtectionLifecycleStatus] = {}

    @property
    def runtime_session(self) -> BrokerRuntimeSession:
        return self._host.context.session

    @property
    def local_only(self) -> bool:
        return False

    @property
    def transport_state(self) -> str:
        return self._host.runtime_identity.transport_state

    @property
    def protection_capabilities(self) -> ProtectionCapabilityMatrix | None:
        return self._host.protection_capabilities

    def submit(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        observation = self._read(group, operation="submit")
        if observation.state not in {
            ProtectionLifecycleState.SUBMITTED,
            ProtectionLifecycleState.ACTIVE,
        }:
            return self._freeze(group, observation, "protection_submit_not_accepted")
        receipt = self._receipt(group, observation)
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=observation.state,
            reason=None,
            attempts=0,
        )
        return receipt

    def cancel(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        observation = self._read(group, operation="cancel")
        if observation.state is not ProtectionLifecycleState.CANCELED:
            return self._freeze(group, observation, "protection_cancel_not_confirmed")
        receipt = self._receipt(group, observation)
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=ProtectionLifecycleState.CANCELED,
            reason=None,
            attempts=0,
        )
        return receipt

    def replace(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        observation = self._read(group, operation="replace")
        if observation.state not in {
            ProtectionLifecycleState.SUBMITTED,
            ProtectionLifecycleState.ACTIVE,
        }:
            return self._freeze(group, observation, "protection_replace_not_accepted")
        receipt = self._receipt(group, observation)
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=observation.state,
            reason=None,
            attempts=0,
        )
        return receipt

    def reconcile(self, group: ProtectionGroup) -> ExternalProtectionReceipt:
        observation = self._read(group, operation="query")
        if observation.state is not ProtectionLifecycleState.ACTIVE:
            return self._freeze(group, observation, "protection_query_not_active")
        if observation.covered_quantity < group.quantity:
            return self._freeze(group, observation, "protection_coverage_below_group_quantity")
        receipt = self._receipt(group, observation)
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=ProtectionLifecycleState.ACTIVE,
            reason=None,
            attempts=0,
        )
        return receipt

    def reconcile_position_coverage(
        self,
        group: ProtectionGroup,
        *,
        owned_quantity: Decimal,
    ) -> ExternalProtectionReceipt | ProtectionLifecycleStatus:
        if not owned_quantity.is_finite() or owned_quantity < 0:
            raise ValueError("owned_quantity must be finite and non-negative")
        if owned_quantity == 0:
            return self.cancel(group)
        if owned_quantity != group.quantity:
            if group.quantity_policy.value != "position_following":
                raise BrokerCapabilityError(
                    "protection_order",
                    "position_coverage",
                    "fixed-size protection cannot cover the residual position",
                )
            return self.replace(
                ProtectionGroup(
                    protection_id=group.protection_id,
                    parent_order_id=group.parent_order_id,
                    instrument_id=group.instrument_id,
                    entry_side=group.entry_side,
                    entry_price=group.entry_price,
                    quantity=owned_quantity,
                    quantity_policy=group.quantity_policy,
                    take_profit=group.take_profit,
                    stop_loss=group.stop_loss,
                )
            )
        return self.reconcile(group)

    def status(self, protection_id: str) -> ProtectionLifecycleStatus:
        return self._statuses.get(
            protection_id,
            ProtectionLifecycleStatus(
                protection_id=protection_id,
                state=ProtectionLifecycleState.UNKNOWN,
                reason=None,
                attempts=0,
            ),
        )

    def _read(self, group: ProtectionGroup, *, operation: str) -> ExternalProtectionObservation:
        request_id = f"protection:{group.protection_id}:{operation}"
        request = ExternalHostRequest(
            request_id=request_id,
            request=CanonicalHostRequest(
                port="protection_order",
                operation=operation,
                payload=group,
            ),
        )
        def mapper(raw: Mapping[str, object]) -> ExternalFactEnvelope[ExternalProtectionObservation]:
            observation = self._parse_observation(raw)
            return ExternalFactEnvelope.create(
                context=self._host.context,
                fact_type="protection.observation",
                data=observation,
                request_id=request_id,
                provenance=observation.provenance,
                raw_payload=raw,
            )

        envelope = self._host.read_fact(request=request, mapper=mapper)
        if not isinstance(envelope.data, ExternalProtectionObservation):
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "external protection fact mapper returned an invalid observation",
            )
        if envelope.data.protection_id != group.protection_id:
            raise RuntimeBoundaryError(
                "external_protection_identity_mismatch",
                "external protection observation does not match the requested group",
            )
        return envelope.data

    def _receipt(
        self,
        group: ProtectionGroup,
        observation: ExternalProtectionObservation,
    ) -> ExternalProtectionReceipt:
        canonical = ProtectionReceipt(
            protection_id=group.protection_id,
            parent_order_id=group.parent_order_id,
            operation=observation.operation,
            accepted=observation.accepted,
            broker_id=self._host.identity.broker_id,
            environment=self._host.identity.environment,
            provenance=observation.provenance,
            account_address=self._host.identity.account_address,
            lifecycle_id=self._host.context.session.lifecycle_id,
            release_sha=self._host.context.release_sha,
        )
        return ExternalProtectionReceipt(
            canonical=canonical,
            observation=observation,
        )

    def _freeze(
        self,
        group: ProtectionGroup,
        observation: ExternalProtectionObservation,
        reason: str,
    ) -> ExternalProtectionReceipt:
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=ProtectionLifecycleState.FROZEN,
            reason=reason,
            attempts=1,
        )
        raise BrokerCapabilityError("protection_order", observation.operation, reason)

    @staticmethod
    def _parse_observation(raw: Mapping[str, object]) -> ExternalProtectionObservation:
        if not isinstance(raw, Mapping):
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "protection runtime response must be a mapping",
            )
        protection_id = str(raw.get("protection_id") or "").strip()
        operation = str(raw.get("operation") or "").strip()
        state_name = str(raw.get("state") or "unknown").strip().lower()
        state = _STATES.get(state_name, ProtectionLifecycleState.UNKNOWN)
        try:
            covered_quantity = Decimal(str(raw.get("covered_quantity") or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "protection covered quantity is not numeric",
            ) from exc
        order_ids_value = raw.get("order_ids") or ()
        if not isinstance(order_ids_value, (list, tuple)):
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "protection order identities must be a list",
            )
        order_ids = tuple(str(value) for value in order_ids_value)
        provenance = raw.get("provenance")
        if not isinstance(provenance, Provenance):
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "protection observation provenance is missing",
            )
        observed_at = provenance.received_at
        digest = str(raw.get("observation_digest") or "").strip()
        if not protection_id or not operation or not digest:
            raise RuntimeBoundaryError(
                "external_protection_observation_invalid",
                "protection observation identity/digest is missing",
            )
        expected_digest = digest_canonical(
            {
                str(key): value
                for key, value in raw.items()
                if str(key) != "observation_digest"
            }
        )
        if digest.lower() != expected_digest:
            raise RuntimeBoundaryError(
                "external_protection_observation_digest_mismatch",
                "protection observation digest does not match its canonical fields",
            )
        observation = ExternalProtectionObservation(
            protection_id=protection_id,
            operation=operation,
            state=state,
            accepted=raw.get("accepted") is True,
            covered_quantity=covered_quantity,
            order_ids=order_ids,
            observed_at=observed_at,
            provenance=provenance,
            observation_digest=digest,
        )
        return observation
