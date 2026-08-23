"""Hyperliquid TP/SL grouping and approved local runtime protection mapping."""

from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock

from ...errors import BrokerCapabilityError, RuntimeBoundaryError
from ...models import BrokerEnvironment
from ...orders import OrderSide
from ...protection import (
    ProtectionCapabilityMatrix,
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionLifecycleState,
    ProtectionLifecycleStatus,
    ProtectionQuantityPolicy,
    ProtectionReceipt,
    ProtectionRetryPlan,
    ProtectionType,
    TriggerReference,
)
from .bridge import NautilusHyperliquidRuntime, NautilusRuntimeState
from .resilience import RateLimitError, RetryPolicy, plan_retry


def default_external_testnet_protection_capabilities() -> ProtectionCapabilityMatrix:
    """Return the explicit, intentionally unavailable external Testnet profile."""

    capabilities = {
        "submit": False,
        "cancel": False,
        "replace": False,
        "query": False,
        "retry": False,
        "position_coverage": False,
        "partial_fill_repair": False,
        "reduce_only_close": True,
        "reduce_only": False,
        "mark_price_trigger": False,
        "grouped_tp_sl": False,
        "sibling_cancellation": False,
        "bracket": False,
        "parent_child": False,
        "fixed_size": False,
        "position_following": False,
        "position_level_tpsl": False,
        "take_profit_market": False,
        "take_profit_limit": False,
        "stop_loss_market": False,
        "stop_loss_limit": False,
        "cancel_replace": False,
    }
    return ProtectionCapabilityMatrix(
        profile_id="hyperliquid-testnet-protection-v1",
        values=capabilities,
    )


def enabled_external_testnet_position_protection_capabilities() -> ProtectionCapabilityMatrix:
    """Return the opt-in positionTpsl capability matrix.

    This is intentionally separate from the default external profile.  The
    default canary remains ordinary-close-only until a downstream release
    binds this exact protection profile and supplies its own attended approval.
    """

    supported = {
        "submit": True,
        "cancel": True,
        "replace": True,
        "query": True,
        "retry": False,
        "position_coverage": True,
        "partial_fill_repair": True,
        "reduce_only_close": True,
        "reduce_only": True,
        "mark_price_trigger": True,
        "grouped_tp_sl": True,
        "sibling_cancellation": True,
        "bracket": False,
        "parent_child": False,
        "fixed_size": False,
        "position_following": True,
        "position_level_tpsl": True,
        "take_profit_market": True,
        "take_profit_limit": True,
        "stop_loss_market": True,
        "stop_loss_limit": True,
        "cancel_replace": True,
    }
    return ProtectionCapabilityMatrix(
        profile_id="hyperliquid-testnet-position-protection-v1",
        values=supported,
    )


@dataclass(frozen=True)
class HyperliquidProtectionLeg:
    side: OrderSide
    tpsl: str
    execution: str
    trigger_price: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    trigger_reference: TriggerReference
    sibling_id: str


@dataclass(frozen=True)
class HyperliquidProtectionRequest:
    grouping: str
    quantity: Decimal
    quantity_policy: ProtectionQuantityPolicy
    legs: tuple[HyperliquidProtectionLeg, ...]


class HyperliquidRuntimeProtectionAdapter:
    """Paper-only ProtectionOrderPort lifecycle with explicit capability gaps."""

    name = "hyperliquid_runtime_protection_order"

    def __init__(
        self,
        *,
        runtime: NautilusHyperliquidRuntime,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not isinstance(runtime, NautilusHyperliquidRuntime):
            raise RuntimeBoundaryError(
                "runtime_type_invalid",
                "protection adapter requires the canonical Hyperliquid runtime",
            )
        if runtime.state is not NautilusRuntimeState.READY:
            raise RuntimeBoundaryError(
                "runtime_not_ready",
                "protection adapter requires an approved ready runtime",
            )
        if runtime.session.broker_id != "hyperliquid":
            raise RuntimeBoundaryError(
                "broker_mismatch",
                "Hyperliquid protection adapter requires the Hyperliquid Broker",
            )
        if runtime.session.environment not in {
            BrokerEnvironment.PAPER,
            BrokerEnvironment.TESTNET,
        }:
            raise RuntimeBoundaryError(
                "protection_runtime_environment_unsupported",
                "protection lifecycle supports Paper and approved Testnet fixtures only",
            )
        if runtime.session.environment is BrokerEnvironment.TESTNET:
            runtime.preflight(
                required_operations={
                    "order_execution": {"submit", "cancel", "replace", "query", "open_orders"},
                    "protection_order": {"submit"},
                }
            )
        self._runtime = runtime
        self._mapper = HyperliquidProtectionAdapter()
        self._retry_policy = retry_policy or RetryPolicy()
        self._lock = RLock()
        self._statuses: dict[str, ProtectionLifecycleStatus] = {}
        self._last_errors: dict[str, BaseException] = {}
        self._last_operations: dict[str, str] = {}
        self._last_groups: dict[str, ProtectionGroup] = {}

    def submit(self, group: ProtectionGroup) -> ProtectionReceipt:
        with self._lock:
            return self._run(group, operation="submit", success_state=ProtectionLifecycleState.SUBMITTED)

    def cancel(self, group: ProtectionGroup) -> ProtectionReceipt:
        with self._lock:
            return self._run(
                group,
                operation="cancel",
                success_state=ProtectionLifecycleState.CANCELED,
                request={"protectionId": group.protection_id},
                require_group=False,
            )

    def reconcile(self, group: ProtectionGroup) -> ProtectionReceipt:
        """Confirm protection through an explicit Broker/fixture observation."""

        with self._lock:
            self._last_operations[group.protection_id] = "query"
            self._last_groups[group.protection_id] = group
            try:
                self._require("query")
                runtime_receipt = self._runtime.invoke(
                    "protection_order",
                    "query",
                    {"protectionId": group.protection_id},
                )
                if runtime_receipt.accepted is not True:
                    raise BrokerCapabilityError(
                        "protection_order",
                        "query",
                        "protection_query_not_accepted",
                    )
            except Exception as error:
                self._freeze(group.protection_id, error)
                raise
            self._statuses[group.protection_id] = ProtectionLifecycleStatus(
                protection_id=group.protection_id,
                state=ProtectionLifecycleState.ACTIVE,
                reason=None,
                attempts=0,
            )
            return ProtectionReceipt(
                protection_id=group.protection_id,
                parent_order_id=group.parent_order_id,
                operation="query",
                accepted=True,
                broker_id=runtime_receipt.broker_id,
                environment=runtime_receipt.environment,
                provenance=runtime_receipt.provenance,
                account_address=getattr(runtime_receipt, "account_address", None),
                lifecycle_id=getattr(runtime_receipt, "lifecycle_id", None),
                release_sha=getattr(runtime_receipt, "release_sha", None),
            )

    def replace(self, group: ProtectionGroup) -> ProtectionReceipt:
        with self._lock:
            return self._run(group, operation="replace", success_state=ProtectionLifecycleState.SUBMITTED)

    def repair_after_partial_fill(
        self,
        group: ProtectionGroup,
        *,
        filled_quantity: Decimal,
    ) -> ProtectionReceipt:
        with self._lock:
            self._last_operations[group.protection_id] = "replace"
            self._last_groups[group.protection_id] = group
            try:
                self._mapper.repair_after_partial_fill(
                    group,
                    filled_quantity=filled_quantity,
                )
                self._require("partial_fill_repair_position_following")
            except Exception as error:
                self._freeze(group.protection_id, error)
                raise
            repaired_group = replace(group, quantity=filled_quantity)
            return self._run(
                repaired_group,
                operation="replace",
                success_state=ProtectionLifecycleState.SUBMITTED,
            )

    def reconcile_position_coverage(
        self,
        group: ProtectionGroup,
        *,
        owned_quantity: Decimal,
    ) -> ProtectionReceipt | ProtectionLifecycleStatus:
        """Keep protection coverage aligned with the currently owned quantity."""

        with self._lock:
            if not owned_quantity.is_finite() or owned_quantity < 0:
                raise ValueError("owned_quantity must be finite and non-negative")
            self._last_operations[group.protection_id] = "cancel" if owned_quantity == 0 else "replace"
            self._last_groups[group.protection_id] = group
            if owned_quantity == 0:
                return self._run(
                    group,
                    operation="cancel",
                    success_state=ProtectionLifecycleState.CANCELED,
                    request={"protectionId": group.protection_id},
                    require_group=False,
                )
            if group.quantity_policy is ProtectionQuantityPolicy.POSITION_FOLLOWING:
                if owned_quantity != group.quantity:
                    try:
                        self._require("partial_fill_repair_position_following")
                    except Exception as error:
                        self._freeze(group.protection_id, error)
                        raise
                    return self._run(
                        replace(group, quantity=owned_quantity),
                        operation="replace",
                        success_state=ProtectionLifecycleState.SUBMITTED,
                    )
                return self._coverage_status_or_freeze(group.protection_id)
            if owned_quantity != group.quantity:
                error = BrokerCapabilityError(
                    "protection_order",
                    "position_coverage",
                    "partial_fill_protection_gap: fixed-size protection cannot cover the residual position",
                )
                self._freeze(group.protection_id, error)
                raise error
            return self._coverage_status_or_freeze(group.protection_id)

    def retry(
        self,
        group: ProtectionGroup,
        *,
        attempt: int,
        delay_elapsed: bool = False,
    ) -> ProtectionReceipt:
        with self._lock:
            plan = self.retry_plan(group, attempt=attempt)
            if not plan.retry_allowed:
                raise BrokerCapabilityError(
                    "protection_order",
                    "retry",
                    f"retry_blocked:{plan.disposition}",
                )
            if plan.delay_seconds > 0 and not delay_elapsed:
                raise BrokerCapabilityError(
                    "protection_order",
                    "retry",
                    "retry_wait_required",
                )
            retry_group = self._last_groups[group.protection_id]
            operation = plan.operation
            success_state = (
                ProtectionLifecycleState.CANCELED
                if operation == "cancel"
                else ProtectionLifecycleState.SUBMITTED
            )
            return self._run(
                retry_group,
                operation=operation,
                success_state=success_state,
                request={"protectionId": retry_group.protection_id}
                if operation == "cancel"
                else None,
                require_group=operation != "cancel",
                allow_frozen=True,
            )

    def retry_plan(self, group: ProtectionGroup, *, attempt: int) -> ProtectionRetryPlan:
        with self._lock:
            if type(attempt) is not int or attempt < 1:
                raise ValueError("protection retry attempt must be a positive integer")
            error = self._last_errors.get(group.protection_id)
            operation = self._last_operations.get(group.protection_id)
            status = self.status(group.protection_id)
            if error is None or operation is None:
                raise RuntimeBoundaryError(
                    "protection_retry_not_pending",
                    "protection retry requires a prior frozen failure",
                )
            effective_attempt = max(attempt, status.attempts + 1)
            plan = plan_retry(error, attempt=effective_attempt, policy=self._retry_policy)
            return ProtectionRetryPlan(
                protection_id=group.protection_id,
                operation=operation,
                attempt=effective_attempt,
                retry_allowed=plan.retry_allowed,
                delay_seconds=plan.delay_seconds,
                disposition=plan.disposition.value,
            )

    def status(self, protection_id: str) -> ProtectionLifecycleStatus:
        with self._lock:
            return self._statuses.get(
                protection_id,
                ProtectionLifecycleStatus(
                    protection_id=protection_id,
                    state=ProtectionLifecycleState.UNKNOWN,
                    reason=None,
                    attempts=0,
                ),
            )

    def _run(
        self,
        group: ProtectionGroup,
        *,
        operation: str,
        success_state: ProtectionLifecycleState,
        request: dict[str, object] | None = None,
        require_group: bool = True,
        allow_frozen: bool = False,
    ) -> ProtectionReceipt:
        current_status = self._statuses.get(group.protection_id)
        if (
            current_status is not None
            and current_status.state is ProtectionLifecycleState.FROZEN
            and not allow_frozen
        ):
            raise BrokerCapabilityError(
                "protection_order",
                "retry",
                "frozen_protection_requires_explicit_retry",
            )
        self._last_operations[group.protection_id] = operation
        self._last_groups[group.protection_id] = group
        try:
            if require_group:
                self._require_group_capabilities(group, operation)
            else:
                self._require(operation)
            if operation == "replace":
                self._require("cancel_replace")
            native_request = request or self._serialize(
                group,
                self._mapper.build_group(group),
            )
            if operation != "cancel" and any(
                not leg["reduceOnly"] for leg in native_request["legs"]
            ):
                raise BrokerCapabilityError(
                    "protection_order",
                    "reduce_only",
                    "every close leg must remain reduce-only",
                )
            runtime_receipt = self._runtime.invoke(
                "protection_order",
                operation,
                native_request,
            )
            if runtime_receipt.accepted is not True:
                raise BrokerCapabilityError(
                    "protection_order",
                    operation,
                    "protection_receipt_not_accepted",
                )
        except Exception as error:
            self._freeze(group.protection_id, error)
            raise
        self._statuses[group.protection_id] = ProtectionLifecycleStatus(
            protection_id=group.protection_id,
            state=success_state,
            reason=None,
            attempts=0,
        )
        self._last_errors.pop(group.protection_id, None)
        return ProtectionReceipt(
            protection_id=group.protection_id,
            parent_order_id=group.parent_order_id,
            operation=operation,
            accepted=runtime_receipt.accepted,
            broker_id=runtime_receipt.broker_id,
            environment=runtime_receipt.environment,
            provenance=runtime_receipt.provenance,
            account_address=runtime_receipt.account_address,
            lifecycle_id=runtime_receipt.lifecycle_id,
            release_sha=runtime_receipt.release_sha,
        )

    def _require_group_capabilities(self, group: ProtectionGroup, operation: str) -> None:
        self._require(operation)
        self._require("reduce_only")
        self._require("mark_price_trigger")
        if group.take_profit is not None and group.stop_loss is not None:
            self._require("grouped_tp_sl")
            self._require("sibling_cancellation")
        if group.quantity_policy is ProtectionQuantityPolicy.FIXED_SIZE:
            self._require("fixed_size")
        else:
            self._require("position_following")
            self._require("position_level_tpsl")
        for leg in (group.take_profit, group.stop_loss):
            if leg is not None:
                self._require(f"{leg.protection_type.value}_{leg.execution.value}")

    def _require(self, operation: str) -> None:
        self._runtime.session.capabilities.require("protection_order", operation)

    def _coverage_status_or_freeze(self, protection_id: str) -> ProtectionLifecycleStatus:
        status = self.status(protection_id)
        if status.state in {ProtectionLifecycleState.SUBMITTED, ProtectionLifecycleState.ACTIVE}:
            return status
        if status.state is ProtectionLifecycleState.FROZEN:
            raise BrokerCapabilityError(
                "protection_order",
                "position_coverage",
                "frozen_protection_requires_explicit_retry",
            )
        error = BrokerCapabilityError(
            "protection_order",
            "position_coverage",
            "protection coverage is not active",
        )
        self._freeze(protection_id, error)
        raise error

    @staticmethod
    def _serialize(
        group: ProtectionGroup,
        request: HyperliquidProtectionRequest,
    ) -> dict[str, object]:
        return {
            "protectionId": group.protection_id,
            "parentOrderId": group.parent_order_id,
            "instrumentId": group.instrument_id,
            "grouping": request.grouping,
            "quantity": str(request.quantity),
            "quantityPolicy": request.quantity_policy.value,
            "legs": [
                {
                    "side": "B" if leg.side is OrderSide.BUY else "A",
                    "tpsl": leg.tpsl,
                    "execution": leg.execution,
                    "triggerPx": str(leg.trigger_price),
                    "limitPx": str(leg.limit_price) if leg.limit_price is not None else None,
                    "reduceOnly": leg.reduce_only,
                    "triggerReference": leg.trigger_reference.value,
                    "siblingId": leg.sibling_id,
                }
                for leg in request.legs
            ],
        }

    def _freeze(self, protection_id: str, error: BaseException) -> None:
        previous = self._statuses.get(protection_id)
        attempts = previous.attempts + 1 if previous is not None else 1
        self._statuses[protection_id] = ProtectionLifecycleStatus(
            protection_id=protection_id,
            state=ProtectionLifecycleState.FROZEN,
            reason=f"protection_update_failed:{self._safe_reason_code(error)}",
            attempts=attempts,
        )
        self._last_errors[protection_id] = self._safe_retry_error(error)

    @staticmethod
    def _safe_reason_code(error: BaseException) -> str:
        if isinstance(error, BrokerCapabilityError):
            return "capability_gap"
        if isinstance(error, RateLimitError):
            return "rate_limit"
        if isinstance(error, (OSError, TimeoutError)):
            return "transport_error"
        if isinstance(error, RuntimeBoundaryError):
            return "runtime_boundary"
        return "invalid_protection_update"

    @staticmethod
    def _safe_retry_error(error: BaseException) -> BaseException:
        if isinstance(error, RateLimitError):
            return RateLimitError(
                retry_after_seconds=error.retry_after_seconds,
                side_effect_free=error.side_effect_free,
            )
        if isinstance(error, TimeoutError):
            return TimeoutError()
        if isinstance(error, OSError):
            return OSError()
        return ValueError()


class HyperliquidProtectionAdapter:
    """Builds explicit Hyperliquid protection semantics without network I/O."""

    name = "protection_order"

    def serialize_group(self, group: ProtectionGroup) -> dict[str, object]:
        """Return the internal canonical mapping consumed by the runtime port."""

        request = self.build_group(group)
        return {
            "protectionId": group.protection_id,
            "parentOrderId": group.parent_order_id,
            "instrumentId": group.instrument_id,
            "grouping": request.grouping,
            "quantity": str(request.quantity),
            "quantityPolicy": request.quantity_policy.value,
            "legs": [
                {
                    "side": "B" if leg.side is OrderSide.BUY else "A",
                    "tpsl": leg.tpsl,
                    "execution": leg.execution,
                    "triggerPx": str(leg.trigger_price),
                    "limitPx": str(leg.limit_price) if leg.limit_price is not None else None,
                    "reduceOnly": leg.reduce_only,
                    "triggerReference": leg.trigger_reference.value,
                    "siblingId": leg.sibling_id,
                }
                for leg in request.legs
            ],
        }

    def build_group(self, group: ProtectionGroup) -> HyperliquidProtectionRequest:
        legs = tuple(
            self._build_leg(group, leg)
            for leg in (group.take_profit, group.stop_loss)
            if leg is not None
        )
        return HyperliquidProtectionRequest(
            grouping=(
                "positionTpsl"
                if group.quantity_policy is ProtectionQuantityPolicy.POSITION_FOLLOWING
                else "normalTpsl"
            ),
            quantity=group.quantity,
            quantity_policy=group.quantity_policy,
            legs=legs,
        )

    def repair_after_partial_fill(
        self,
        group: ProtectionGroup,
        *,
        filled_quantity: Decimal,
    ) -> HyperliquidProtectionRequest:
        if filled_quantity <= 0 or filled_quantity > group.quantity:
            raise ValueError("filled_quantity must be within the protection group quantity")
        if group.quantity_policy is ProtectionQuantityPolicy.FIXED_SIZE:
            raise BrokerCapabilityError(
                "protection_order",
                "partial_fill_repair",
                "partial_fill_protection_gap: fixed parent protection is not automatically repaired",
            )
        return self.build_group(replace(group, quantity=filled_quantity))

    @staticmethod
    def _build_leg(group: ProtectionGroup, leg: ProtectionLeg) -> HyperliquidProtectionLeg:
        if leg.trigger_reference is not TriggerReference.MARK:
            raise ValueError("Hyperliquid protection triggers must use mark price")
        is_long = group.entry_side is OrderSide.BUY
        if leg.protection_type is ProtectionType.TAKE_PROFIT:
            valid_direction = leg.trigger_price > group.entry_price if is_long else leg.trigger_price < group.entry_price
            tpsl = "tp"
        else:
            valid_direction = leg.trigger_price < group.entry_price if is_long else leg.trigger_price > group.entry_price
            tpsl = "sl"
        if not valid_direction:
            raise ValueError("protection trigger direction would activate immediately")
        if leg.execution is ProtectionExecution.LIMIT and leg.limit_price is not None:
            if is_long and leg.limit_price > leg.trigger_price:
                raise ValueError("long exit limit protection cannot be more aggressive than its trigger")
            if not is_long and leg.limit_price < leg.trigger_price:
                raise ValueError("short exit limit protection cannot be more aggressive than its trigger")
        return HyperliquidProtectionLeg(
            side=OrderSide.SELL if is_long else OrderSide.BUY,
            tpsl=tpsl,
            execution=leg.execution.value,
            trigger_price=leg.trigger_price,
            limit_price=leg.limit_price,
            reduce_only=True,
            trigger_reference=leg.trigger_reference,
            sibling_id=group.protection_id,
        )
