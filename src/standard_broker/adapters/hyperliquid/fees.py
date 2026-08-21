"""Hyperliquid fee, fill, funding, and schedule mapping."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC
from dataclasses import replace
from decimal import Decimal

from ...fees import (
    FeeEvent,
    FeeKind,
    FeeScheduleSnapshot,
    FeeSource,
    FeeState,
    FillFact,
    FundingPayment,
)
from ...models import Provenance
from ...runtime_facts import RuntimeFactLedger
from .bridge import NautilusHyperliquidRuntime
from .errors import UnknownInstrumentError
from .instruments import HyperliquidInstrumentAdapter


def _timestamp(milliseconds: object):
    from datetime import datetime

    return datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC)


def _side(value: object) -> str:
    if value == "B":
        return "buy"
    if value == "A":
        return "sell"
    raise ValueError(f"unsupported Hyperliquid side: {value}")


class HyperliquidFeeAdapter:
    """Maps Hyperliquid fee-bearing facts without estimating actual accounting."""

    name = "fee"

    def __init__(self, instruments: HyperliquidInstrumentAdapter) -> None:
        self._instruments = instruments

    def _instrument(self, broker_symbol: object):
        try:
            return self._instruments.get_by_broker_symbol(str(broker_symbol))
        except KeyError as exc:
            raise UnknownInstrumentError(str(broker_symbol)) from exc

    def map_fill(self, raw: Mapping[str, object], provenance: Provenance) -> FillFact:
        instrument = self._instrument(raw.get("coin"))
        fill_id = str(raw.get("tid") or raw.get("hash") or "")
        if not fill_id:
            raise ValueError("Hyperliquid fill requires tid or hash")
        if (
            raw.get("fee") is None
            or raw.get("feeToken") is None
            or not str(raw["feeToken"]).strip()
            or raw.get("crossed") is None
        ):
            raise ValueError("Hyperliquid fill fee facts require fee, feeToken, and crossed")
        liquidity = "taker" if bool(raw["crossed"]) else "maker"
        amount = Decimal(str(raw["fee"]))
        kind = FeeKind.REBATE if amount < 0 else FeeKind.TAKER if liquidity == "taker" else FeeKind.MAKER
        occurred_at = _timestamp(raw["time"])
        fee = FeeEvent(
            fee_id=f"fill:{fill_id}:fee",
            broker_id="hyperliquid",
            kind=kind,
            amount=amount,
            currency=str(raw["feeToken"]),
            occurred_at=occurred_at,
            source=FeeSource.ACTUAL_FILL,
            state=FeeState.ACTUAL,
            provenance=provenance,
            instrument_id=instrument.canonical_symbol,
            fill_id=fill_id,
            order_id=str(raw["oid"]) if raw.get("oid") is not None else None,
            liquidity=liquidity,
        )
        builder_fee = None
        if raw.get("builderFee") is not None:
            builder_fee = FeeEvent(
                fee_id=f"fill:{fill_id}:builder",
                broker_id="hyperliquid",
                kind=FeeKind.BUILDER,
                amount=Decimal(str(raw["builderFee"])),
                currency=str(raw["feeToken"]),
                occurred_at=occurred_at,
                source=FeeSource.ACTUAL_FILL,
                state=FeeState.ACTUAL,
                provenance=provenance,
                instrument_id=instrument.canonical_symbol,
                fill_id=fill_id,
                order_id=str(raw["oid"]) if raw.get("oid") is not None else None,
                liquidity=liquidity,
            )
        return FillFact(
            fill_id=fill_id,
            broker_id="hyperliquid",
            instrument_id=instrument.canonical_symbol,
            side=_side(raw.get("side")),
            price=Decimal(str(raw["px"])),
            quantity=Decimal(str(raw["sz"])),
            occurred_at=occurred_at,
            closed_pnl=Decimal(str(raw.get("closedPnl", "0"))),
            fee=fee,
            provenance=provenance,
            builder_fee=builder_fee,
        )

    def map_funding(self, raw: Mapping[str, object], provenance: Provenance) -> FundingPayment:
        instrument = self._instrument(raw.get("coin"))
        occurred_at = _timestamp(raw["time"])
        funding_id = f"funding:{instrument.canonical_symbol}:{raw['time']}"
        amount = Decimal(str(raw["usdc"]))
        fee = FeeEvent(
            fee_id=funding_id,
            broker_id="hyperliquid",
            kind=FeeKind.FUNDING,
            amount=amount,
            currency="USDC",
            occurred_at=occurred_at,
            source=FeeSource.FUNDING_EVENT,
            state=FeeState.ACTUAL,
            provenance=provenance,
            instrument_id=instrument.canonical_symbol,
        )
        return FundingPayment(
            funding_id=funding_id,
            broker_id="hyperliquid",
            instrument_id=instrument.canonical_symbol,
            amount=amount,
            currency="USDC",
            funding_rate=Decimal(str(raw["fundingRate"])),
            signed_quantity=Decimal(str(raw["szi"])),
            occurred_at=occurred_at,
            fee=fee,
            provenance=provenance,
        )

    def map_fee_schedule(
        self,
        raw: Mapping[str, object],
        provenance: Provenance,
    ) -> FeeScheduleSnapshot:
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return FeeScheduleSnapshot(
            broker_id="hyperliquid",
            maker_rate=Decimal(str(raw["userAddRate"])),
            taker_rate=Decimal(str(raw["userCrossRate"])),
            active_referral_discount=(
                Decimal(str(raw["activeReferralDiscount"]))
                if raw.get("activeReferralDiscount") is not None
                else None
            ),
            schedule_identity=f"{provenance.mapping_revision}:{digest}",
            retrieved_at=provenance.received_at,
            source=FeeSource.SCHEDULE,
            state=FeeState.ESTIMATED,
            provenance=provenance,
        )


class HyperliquidRuntimeFeeAdapter:
    """Connect canonical fee, fill, funding, and schedule facts to runtime reads."""

    name = "hyperliquid_runtime_fee"

    def __init__(
        self,
        *,
        runtime: NautilusHyperliquidRuntime,
        instruments: HyperliquidInstrumentAdapter,
        ledger: RuntimeFactLedger,
    ) -> None:
        self._runtime = runtime
        self._mapper = HyperliquidFeeAdapter(instruments)
        self._ledger = ledger
        self._ledger.bind_session(
            broker_id=runtime.session.broker_id,
            environment=runtime.session.environment.value,
            account_address=runtime.session.account.address,
        )
        self._ledger.register_fill_enricher(self._enrich_order_fill)

    def _enrich_order_fill(
        self,
        order_fill,
        raw: Mapping[str, object],
    ) -> FillFact | None:
        provenance = raw.get("provenance")
        if not isinstance(provenance, Provenance):
            return None
        mapped = self._mapper.map_fill(raw, provenance)
        if (
            mapped.fill_id != order_fill.fill_id
            or mapped.instrument_id != order_fill.instrument_id
            or mapped.side != order_fill.side.value
            or mapped.price != order_fill.price
            or mapped.quantity != order_fill.quantity
        ):
            raise ValueError("fill_identity_mismatch")
        key = self._key(mapped.fill_id)
        existing = self._ledger.fills.get(key)
        if existing is not None:
            return existing
        fact = replace(
            mapped,
            fee=replace(mapped.fee, environment=self._runtime.session.environment),
            builder_fee=replace(mapped.builder_fee, environment=self._runtime.session.environment)
            if mapped.builder_fee
            else None,
            environment=self._runtime.session.environment,
        )
        self._ledger.fills[key] = fact
        return fact

    def _invoke(self, operation: str, request: Mapping[str, object]) -> tuple[Mapping[str, object], Provenance]:
        response = self._runtime._invoke_native("fee", operation, request)
        if not isinstance(response, Mapping):
            raise ValueError(f"Hyperliquid fee {operation} response must be a mapping")
        provenance = response.get("provenance")
        if not isinstance(provenance, Provenance):
            raise ValueError("Broker fee facts require source provenance")
        data = response.get("data", response)
        if not isinstance(data, Mapping):
            raise ValueError(f"Hyperliquid fee {operation} data must be a mapping")
        return data, provenance

    def read_fill(self, request: Mapping[str, object]) -> FillFact:
        raw, provenance = self._invoke("fill", request)
        mapped = self._mapper.map_fill(raw, provenance)
        key = self._key(mapped.fill_id)
        lifecycle_fill = self._ledger.order_fills.get(key)
        if lifecycle_fill is not None:
            if (
                lifecycle_fill.instrument_id != mapped.instrument_id
                or lifecycle_fill.side.value != mapped.side
                or lifecycle_fill.price != mapped.price
                or lifecycle_fill.quantity != mapped.quantity
            ):
                raise ValueError("fill_identity_mismatch")
        existing = self._ledger.fills.get(key)
        if existing is not None:
            return existing
        fee = replace(mapped.fee, environment=self._runtime.session.environment)
        builder_fee = replace(mapped.builder_fee, environment=self._runtime.session.environment) if mapped.builder_fee else None
        fact = replace(
            mapped,
            fee=fee,
            builder_fee=builder_fee,
            environment=self._runtime.session.environment,
        )
        self._ledger.fills[key] = fact
        return fact

    def read_funding(self, request: Mapping[str, object]) -> FundingPayment:
        raw, provenance = self._invoke("funding", request)
        mapped = self._mapper.map_funding(raw, provenance)
        existing = self._ledger.funding.get(self._key(mapped.funding_id))
        if existing is not None:
            return existing
        fact = replace(
            mapped,
            fee=replace(mapped.fee, environment=self._runtime.session.environment),
            environment=self._runtime.session.environment,
        )
        self._ledger.funding[self._key(fact.funding_id)] = fact
        return fact

    def read_schedule(self, request: Mapping[str, object]) -> FeeScheduleSnapshot:
        raw, provenance = self._invoke("schedule", request)
        return replace(
            self._mapper.map_fee_schedule(raw, provenance),
            environment=self._runtime.session.environment,
        )

    @property
    def fill_facts(self) -> tuple[FillFact, ...]:
        return tuple(
            value for key, value in self._ledger.fills.items() if key.startswith(self._namespace())
        )

    @property
    def funding_payments(self) -> tuple[FundingPayment, ...]:
        return tuple(
            value for key, value in self._ledger.funding.items() if key.startswith(self._namespace())
        )

    def _namespace(self) -> str:
        return ":".join(
            (
                self._runtime.session.broker_id,
                self._runtime.session.environment.value,
                self._runtime.session.account.address,
            )
        ) + ":"

    def _key(self, identity: str) -> str:
        return self._namespace() + identity
