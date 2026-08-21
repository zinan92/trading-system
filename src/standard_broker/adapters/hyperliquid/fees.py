"""Hyperliquid fee, fill, funding, and schedule mapping."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC
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
        liquidity = "taker" if bool(raw.get("crossed")) else "maker"
        amount = Decimal(str(raw.get("fee", "0")))
        kind = FeeKind.REBATE if amount < 0 else FeeKind.TAKER if liquidity == "taker" else FeeKind.MAKER
        occurred_at = _timestamp(raw["time"])
        fee = FeeEvent(
            fee_id=f"fill:{fill_id}:fee",
            broker_id="hyperliquid",
            kind=kind,
            amount=amount,
            currency=str(raw.get("feeToken") or ""),
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
                currency=str(raw.get("feeToken") or ""),
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
