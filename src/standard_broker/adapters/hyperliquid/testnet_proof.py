"""Human-gated, one-lifecycle Hyperliquid Testnet proof runner."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
import time
from uuid import uuid4

from ...evidence import (
    EvidenceClass,
    EvidenceIdentity,
    ExternalTestnetLifecycleEvidence,
    ReconciliationEvidence,
    TESTNET_LIFECYCLE_STEPS,
)
from ...models import AccountScope, BrokerEnvironment, Provenance
from ...market_data import FreshnessPolicy, FreshnessState
from ...orders import OrderIntent, OrderReceipt, OrderSide, OrderState, OrderType, TimeInForce
from ...runtime import RuntimeBoundaryError
from ...runtime_facts import RuntimeFactLedger
from .fees import HyperliquidRuntimeFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .orders import HyperliquidRuntimeOrderAdapter


@dataclass(frozen=True)
class TestnetProofPlan:
    """Explicit prices and quantity for one small default-perp proof."""

    instrument_id: str
    quantity: Decimal
    resting_price: Decimal | None
    aggressive_price: Decimal | None
    fill_price: Decimal | None
    close_price: Decimal | None
    max_slippage_bps: Decimal = Decimal("100")
    max_quote_age_seconds: float = 5.0
    poll_attempts: int = 10
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.instrument_id.strip():
            raise ValueError("instrument_id is required")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("quantity must be positive and finite")
        for name in ("resting_price", "aggressive_price", "fill_price", "close_price"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be positive and finite")
        if not self.max_slippage_bps.is_finite() or self.max_slippage_bps <= 0:
            raise ValueError("max_slippage_bps must be positive and finite")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.poll_attempts < 1:
            raise ValueError("poll_attempts must be positive")
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds cannot be negative")


@dataclass(frozen=True)
class TestnetProofResult:
    evidence: ExternalTestnetLifecycleEvidence
    entry_receipt: OrderReceipt
    replacement_receipt: OrderReceipt
    close_receipt: OrderReceipt


def run_testnet_lifecycle(
    *,
    runtime: object,
    plan: TestnetProofPlan,
    sleep: Callable[[float], None] = time.sleep,
) -> TestnetProofResult:
    """Run one explicit Testnet round trip after runtime activation preflight."""

    session = getattr(runtime, "session", None)
    if session is None or session.environment is not BrokerEnvironment.TESTNET:
        raise RuntimeBoundaryError(
            "testnet_runtime_required",
            "external lifecycle proof requires a Testnet runtime session",
        )
    release_sha = getattr(getattr(runtime, "_config", None), "expected_release_sha", None)
    if not release_sha:
        raise RuntimeBoundaryError(
            "testnet_release_binding_required",
            "external lifecycle proof requires a release-bound runtime",
        )

    runtime.start()
    raw_instruments = runtime._invoke_native("instrument", "read", {})
    if not isinstance(raw_instruments, Mapping) or not isinstance(raw_instruments.get("meta"), Mapping):
        raise RuntimeBoundaryError(
            "instrument_metadata_missing",
            "Testnet proof requires canonical instrument metadata before order submission",
        )
    instruments = HyperliquidInstrumentAdapter.from_meta(
        raw_instruments["meta"],
        revision=session.capabilities.revision,
    )
    instrument = instruments.get(plan.instrument_id)
    if not instrument.supports_order_type(OrderType.LIMIT):
        raise RuntimeBoundaryError("capability_gap", "Testnet proof requires limit order support")
    ticker = runtime._invoke_native(
        "market_data",
        "ticker",
        {"instrument_id": plan.instrument_id},
    )
    bid, ask = _fresh_bbo(ticker, plan.max_quote_age_seconds)
    slippage = plan.max_slippage_bps / Decimal(10000)
    resting_price = plan.resting_price or _passive_price(bid, instrument, slippage)
    aggressive_price = plan.aggressive_price or _passive_price(bid, instrument, slippage / Decimal(2))
    fill_price = plan.fill_price or ask
    close_price = plan.close_price or bid
    for price in (resting_price, aggressive_price, fill_price, close_price):
        if not instrument.price_rule.is_valid(price):
            raise RuntimeBoundaryError("price_precision_invalid", "proof price violates instrument precision")
    if plan.quantity % instrument.quantity_step != 0:
        raise RuntimeBoundaryError("quantity_precision_invalid", "proof quantity violates instrument step")
    if plan.quantity < instrument.minimum_quantity_for_price(fill_price):
        raise RuntimeBoundaryError("minimum_notional_invalid", "proof quantity is below the venue minimum")
    if resting_price >= bid:
        raise RuntimeBoundaryError("resting_price_not_passive", "entry resting price must be below the fresh bid")
    if aggressive_price >= bid:
        raise RuntimeBoundaryError("replacement_price_not_passive", "cancel-replace price must remain passive")
    if not ask <= fill_price <= ask * (Decimal(1) + slippage):
        raise RuntimeBoundaryError("entry_slippage_invalid", "entry IOC price exceeds the approved fresh-BBO slippage")
    if not bid * (Decimal(1) - slippage) <= close_price <= bid:
        raise RuntimeBoundaryError("close_slippage_invalid", "close IOC price exceeds the approved fresh-BBO slippage")

    ledger = RuntimeFactLedger()
    orders = HyperliquidRuntimeOrderAdapter(
        runtime=runtime,
        instruments=instruments,
        ledger=ledger,
    )
    entry_id = f"testnet-entry-{uuid4().hex}"
    entry_intent = OrderIntent(
        order_id=entry_id,
        instrument_id=plan.instrument_id,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=plan.quantity,
        limit_price=resting_price,
        time_in_force=TimeInForce.GTC,
        idempotency_key=f"testnet-entry:{entry_id}",
    )
    entry = orders.submit(entry_intent)
    if entry.state is not OrderState.RESTING:
        raise RuntimeBoundaryError("entry_not_resting", "Testnet proof entry did not reach RESTING")

    replacement_intent = OrderIntent(
        order_id=entry_id,
        instrument_id=plan.instrument_id,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=plan.quantity,
        limit_price=aggressive_price,
        # Hyperliquid's modify action accepts a full replacement order but
        # rejects an IOC replacement on this Testnet path.  A crossing GTC
        # replacement still proves cancel/replace; the bounded lifecycle
        # cancels any remainder before opening the close leg.
        time_in_force=TimeInForce.GTC,
        idempotency_key=f"testnet-replace:{entry_id}",
    )
    try:
        replacement = orders.modify(entry_id, replacement_intent)
    except Exception as exc:
        try:
            _cancel_and_reconcile(orders, entry_id)
        except Exception as cleanup_error:
            raise RuntimeBoundaryError(
                "replace_cleanup_failed",
                "Testnet proof could not reconcile the original order after replace failure",
            ) from cleanup_error
        raise exc
    replacement = _wait_for_resting(orders, replacement, plan, sleep)
    replacement = _cancel_and_reconcile(orders, entry_id)

    fill_id = f"testnet-fill-entry-{uuid4().hex}"
    fill_intent = OrderIntent(
        order_id=fill_id,
        instrument_id=plan.instrument_id,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=plan.quantity,
        limit_price=fill_price,
        time_in_force=TimeInForce.IOC,
        idempotency_key=f"testnet-fill:{fill_id}",
    )
    entry = orders.submit(fill_intent)
    entry = _wait_for_fill(orders, entry, plan, sleep)
    entry_fills = tuple(fill for fill in orders.fills.values() if fill.order_id == fill_id)
    if not entry_fills:
        raise RuntimeBoundaryError("entry_fill_missing", "Testnet proof did not observe an entry fill")
    filled_quantity = sum((fill.quantity for fill in entry_fills), Decimal(0))

    positions = runtime._invoke_native(
        "account",
        "positions",
        {"instrument_id": plan.instrument_id},
    )
    position_ids = _position_ids(positions)
    if not position_ids:
        raise RuntimeBoundaryError("position_observation_missing", "Testnet proof did not observe the entry position")

    close_id = f"testnet-close-{uuid4().hex}"
    close_intent = OrderIntent(
        order_id=close_id,
        instrument_id=plan.instrument_id,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=filled_quantity,
        limit_price=close_price,
        time_in_force=TimeInForce.IOC,
        idempotency_key=f"testnet-close:{close_id}",
        reduce_only=True,
        close_position=True,
    )
    close = orders.submit(close_intent)
    close = _wait_for_fill(orders, close, plan, sleep)
    close_fills = tuple(fill for fill in orders.fills.values() if fill.order_id == close_id)
    if not close_fills:
        raise RuntimeBoundaryError("close_fill_missing", "Testnet proof did not observe the reduce-only close fill")
    if sum((fill.quantity for fill in close_fills), Decimal(0)) != filled_quantity:
        raise RuntimeBoundaryError(
            "close_partial",
            "Testnet proof close did not remove the full observed entry quantity",
        )

    fee_adapter = HyperliquidRuntimeFeeAdapter(
        runtime=runtime,
        instruments=instruments,
        ledger=ledger,
    )
    fee_facts = fee_adapter.fill_facts
    if not fee_facts:
        raise RuntimeBoundaryError("fee_observation_missing", "Testnet proof did not observe actual fill fees")
    final_positions = runtime._invoke_native(
        "account",
        "positions",
        {"instrument_id": plan.instrument_id},
    )
    final_account = runtime._invoke_native("account", "read", {})
    if not isinstance(final_account, Mapping) or not isinstance(final_account.get("data"), Mapping):
        raise RuntimeBoundaryError(
            "account_reconciliation_missing",
            "Testnet proof did not observe final account state",
        )
    if orders.open_orders(plan.instrument_id):
        raise RuntimeBoundaryError("open_order_unreconciled", "Testnet proof ended with an open order")
    if _has_open_position(final_positions):
        raise RuntimeBoundaryError("position_unreconciled", "Testnet proof ended with unreconciled exposure")
    final_position_ids = _position_ids(final_positions)
    position_ids = tuple(sorted(set(position_ids + final_position_ids)))
    provenance = _provenance(positions, session.capabilities.revision, session.execution_scope)
    watermark = max(
        _watermarks(final_account) + _watermarks(final_positions),
        default=0,
    )
    if watermark <= 0:
        raise RuntimeBoundaryError(
            "reconciliation_watermark_missing",
            "Testnet proof did not observe an authoritative reconciliation watermark",
        )
    reconciliation_id = f"reconciliation:{session.lifecycle_id}:{watermark}"
    reconciliation = ReconciliationEvidence(
        reconciliation_id=reconciliation_id,
        broker_id=session.broker_id,
        environment=session.environment,
        account_scope=AccountScope.MASTER,
        account_address=session.account.address,
        execution_scope=session.execution_scope,
        lifecycle_id=session.lifecycle_id,
        watermark=watermark,
        provenance=provenance,
    )
    identity = EvidenceIdentity(
        evidence_class=EvidenceClass.TESTNET,
        broker_id=session.broker_id,
        environment=session.environment,
        account_scope=AccountScope.MASTER,
        account_address=session.account.address,
        execution_scope=session.execution_scope,
        lifecycle_id=session.lifecycle_id,
        release_sha=release_sha,
        order_ids=tuple(sorted({entry_id, fill_id, close_id})),
        fill_ids=tuple(sorted(orders.fills)),
        fee_ids=tuple(sorted(fact.fee.fee_id for fact in fee_facts)),
        position_ids=position_ids,
        reconciliation_ids=(reconciliation_id,),
    )
    evidence = ExternalTestnetLifecycleEvidence(
        identity=identity,
        completed_steps=TESTNET_LIFECYCLE_STEPS,
        final_reconciliation_id=reconciliation_id,
        reconciliation=reconciliation,
        provenance=provenance,
    )
    return TestnetProofResult(
        evidence=evidence,
        entry_receipt=entry,
        replacement_receipt=replacement,
        close_receipt=close,
    )


def _wait_for_resting(
    orders: HyperliquidRuntimeOrderAdapter,
    receipt: OrderReceipt,
    plan: TestnetProofPlan,
    sleep: Callable[[float], None],
) -> OrderReceipt:
    current = receipt
    for attempt in range(plan.poll_attempts):
        if current.state is OrderState.RESTING:
            return current
        if current.state in {OrderState.UNKNOWN, OrderState.REJECTED, OrderState.CANCELED}:
            raise RuntimeBoundaryError("replace_lifecycle_failed", "Testnet cancel-replace did not reach RESTING")
        if attempt:
            sleep(plan.poll_interval_seconds)
        current = orders.query(current.order_id)
    raise RuntimeBoundaryError("replace_timeout", "Testnet cancel-replace was not reconciled within the bounded poll window")


def _wait_for_fill(
    orders: HyperliquidRuntimeOrderAdapter,
    receipt: OrderReceipt,
    plan: TestnetProofPlan,
    sleep: Callable[[float], None],
) -> OrderReceipt:
    current = receipt
    for attempt in range(plan.poll_attempts):
        if current.state in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
            return current
        if current.state in {OrderState.UNKNOWN, OrderState.REJECTED, OrderState.CANCELED}:
            raise RuntimeBoundaryError("order_lifecycle_failed", f"Testnet proof order entered {current.state.value}")
        if attempt:
            sleep(plan.poll_interval_seconds)
        current = orders.query(current.order_id)
    try:
        _cancel_and_reconcile(orders, current.order_id)
    except Exception as exc:
        raise RuntimeBoundaryError(
            "fill_timeout_cleanup_failed",
            "Testnet proof could not cancel and reconcile the timed-out order",
        ) from exc
    raise RuntimeBoundaryError("fill_timeout", "Testnet proof fill was not reconciled within the bounded poll window")


def _cancel_and_reconcile(orders: HyperliquidRuntimeOrderAdapter, order_id: str) -> OrderReceipt:
    cancellation = orders.cancel(order_id)
    if cancellation.state is OrderState.CANCEL_PENDING:
        cancellation = orders.query(order_id)
    if cancellation.state is not OrderState.CANCELED:
        raise RuntimeBoundaryError(
            "order_cancel_unreconciled",
            "Testnet proof cancellation did not reach a terminal canceled state",
        )
    return cancellation


def _position_ids(response: object) -> tuple[str, ...]:
    if not isinstance(response, Mapping):
        return ()
    rows = response.get("positions")
    if not isinstance(rows, list):
        return ()
    result: list[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            position = row.get("position") if isinstance(row.get("position"), Mapping) else row
            value = (
                row.get("report_id")
                or row.get("position_id")
                or row.get("observation_id")
                or position.get("positionId")
                or position.get("timestamp")
            )
            if value:
                result.append(str(value))
    return tuple(result)


def _has_open_position(response: object) -> bool:
    if not isinstance(response, Mapping):
        return True
    rows = response.get("positions")
    if not isinstance(rows, list):
        return True
    for row in rows:
        if not isinstance(row, Mapping):
            return True
        position = row.get("position") if isinstance(row.get("position"), Mapping) else row
        quantity = (
            position.get("quantity")
            or position.get("signed_decimal_qty")
            or position.get("signed_quantity")
            or position.get("szi")
            or "0"
        )
        try:
            if Decimal(str(quantity)) != 0:
                return True
        except Exception:
            return True
    return False


def _provenance(response: object, revision: str, execution_scope: str) -> Provenance:
    if isinstance(response, Mapping) and isinstance(response.get("provenance"), Provenance):
        return response["provenance"]
    return Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=execution_scope,
        transport_state="external_testnet",
        mapping_revision=revision,
    )


def _fresh_bbo(response: object, max_age_seconds: float) -> tuple[Decimal, Decimal]:
    if not isinstance(response, Mapping):
        raise RuntimeBoundaryError("bbo_missing", "Testnet proof requires a canonical ticker response")
    provenance = response.get("provenance")
    if not isinstance(provenance, Provenance):
        raise RuntimeBoundaryError("bbo_provenance_missing", "Testnet proof BBO requires provenance")
    freshness = FreshnessPolicy(timedelta(seconds=max_age_seconds)).classify(
        provenance.received_at,
        datetime.now(UTC),
        transport_state=provenance.transport_state,
    )
    if freshness is not FreshnessState.FRESH:
        raise RuntimeBoundaryError("bbo_stale", "Testnet proof requires a fresh external Testnet BBO")
    bbo = response.get("bbo")
    if not isinstance(bbo, Mapping) or not isinstance(bbo.get("bbo"), list) or len(bbo["bbo"]) != 2:
        raise RuntimeBoundaryError("bbo_incomplete", "Testnet proof requires both bid and ask")
    try:
        bid = Decimal(str(bbo["bbo"][0]["px"]))
        ask = Decimal(str(bbo["bbo"][1]["px"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeBoundaryError("bbo_invalid", "Testnet proof BBO price is invalid") from exc
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
        raise RuntimeBoundaryError("bbo_invalid", "Testnet proof BBO must be positive and ordered")
    return bid, ask


def _passive_price(bid: Decimal, instrument: object, slippage: Decimal) -> Decimal:
    """Choose a valid passive buy price just inside the approved oracle band."""

    raw = bid * (Decimal(1) - slippage)
    for decimal_places in range(instrument.price_rule.max_decimal_places, -1, -1):
        candidate = raw.quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUND_FLOOR)
        if instrument.price_rule.is_valid(candidate):
            return candidate
    raise RuntimeBoundaryError("price_precision_invalid", "could not derive a valid passive price")


def _watermarks(value: object) -> list[int]:
    result: list[int] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"ts_event", "ts_last", "ts_init", "timestamp", "time", "watermark"}:
                try:
                    candidate = int(item)
                except (TypeError, ValueError):
                    candidate = 0
                if candidate > 0:
                    result.append(candidate)
            result.extend(_watermarks(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_watermarks(item))
    return result
