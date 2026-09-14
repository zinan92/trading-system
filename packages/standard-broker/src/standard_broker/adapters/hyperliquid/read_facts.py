"""Canonical external Hyperliquid read/fact mapping."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ...account import AccountSnapshot, PositionFact, PositionSide
from ...errors import BrokerCapabilityError, RuntimeBoundaryError
from ...external_host import ExternalBrokerBuildContext, ExternalFactEnvelope
from ...fees import FeeScheduleSnapshot, FillFact
from ...instruments import InstrumentSpec, MarginMode
from ...market_data import FreshnessPolicy, MarketDataEnvelope, Ticker
from ...models import BrokerEnvironment, Provenance
from .account import HyperliquidAccountAdapter
from .fees import HyperliquidFeeAdapter
from .instruments import HyperliquidInstrumentAdapter
from .market_data import HyperliquidMarketDataAdapter


def _provenance(raw: Mapping[str, object], context: ExternalBrokerBuildContext) -> Provenance:
    value = raw.get("provenance")
    if not isinstance(value, Provenance):
        raise RuntimeBoundaryError("provenance_required", "external fact mapping requires Broker provenance")
    if (
        value.transport_state != context.runtime_identity.transport_state
        or value.execution_scope != context.identity.execution_scope
        or value.mapping_revision != context.capabilities.revision
        or value.mapping_revision != context.runtime_identity.mapping_revision
        or context.runtime_identity.adapter_id not in value.source
    ):
        raise RuntimeBoundaryError(
            "fact_provenance_mismatch",
            "external fact provenance does not match the bound runtime context",
        )
    return value


def _data(raw: Mapping[str, object]) -> Mapping[str, object]:
    value = raw.get("data", raw)
    if not isinstance(value, Mapping):
        raise RuntimeBoundaryError("fact_payload_invalid", "external fact response data must be a mapping")
    return value


def _decimal(value: object | None) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    return Decimal(str(value))


class HyperliquidExternalFactAdapter:
    """Map supported external observations into canonical fact envelopes."""

    name = "hyperliquid_external_facts"

    def __init__(
        self,
        *,
        context: ExternalBrokerBuildContext,
        instruments: HyperliquidInstrumentAdapter,
        freshness_policy: FreshnessPolicy,
    ) -> None:
        self._context = context
        self._instruments = instruments
        self._freshness_policy = freshness_policy
        self._market = HyperliquidMarketDataAdapter(instruments)
        self._account = HyperliquidAccountAdapter(instruments)
        self._fees = HyperliquidFeeAdapter(instruments)

    def map_ticker(
        self,
        *,
        request_id: str,
        broker_symbol: str,
        raw: Mapping[str, object],
        now: datetime,
    ) -> ExternalFactEnvelope[MarketDataEnvelope[Ticker]]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        bbo = payload.get("bbo")
        if not isinstance(bbo, Mapping) or not isinstance(bbo.get("bbo"), list) or len(bbo["bbo"]) != 2:
            raise BrokerCapabilityError("market_data", "ticker", "bbo_gap")
        ticker = self._market.map_ticker(
            broker_symbol,
            mid=payload.get("mid"),
            bbo=bbo,
            provenance=provenance,
        )
        envelope = self._market.envelope(ticker, self._freshness_policy, now=now)
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="market_data.ticker",
            data=envelope,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_instruments(
        self,
        *,
        request_id: str,
        raw: Mapping[str, object],
    ) -> ExternalFactEnvelope[tuple[InstrumentSpec, ...]]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        meta = payload.get("meta")
        if not isinstance(meta, Mapping):
            raise BrokerCapabilityError("instrument", "read", "instrument_metadata_gap")
        mapped = HyperliquidInstrumentAdapter.from_meta(
            meta,
            revision=self._context.capabilities.revision,
        )
        data = tuple(mapped.instruments[key] for key in sorted(mapped.instruments))
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="instrument.catalog",
            data=data,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_positions(
        self,
        *,
        request_id: str,
        broker_symbol: str,
        raw: Mapping[str, object],
    ) -> ExternalFactEnvelope[tuple[PositionFact, ...]]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        rows = payload.get("positions")
        if not isinstance(rows, list):
            raise BrokerCapabilityError("account", "positions", "position_report_gap")
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        positions: list[PositionFact] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise BrokerCapabilityError("account", "positions", "position_report_gap")
            _validate_account_identity(row, self._context)
            instrument_value = str(row.get("instrument_id") or "")
            if instrument_value not in {instrument.canonical_symbol, f"{instrument.canonical_symbol}.HYPERLIQUID"}:
                raise ValueError("position instrument identity mismatch")
            quantity_value = row.get("signed_decimal_qty")
            if quantity_value is None:
                quantity_value = row.get("signed_quantity")
            if quantity_value is None:
                quantity_value = row.get("quantity")
            if quantity_value is None:
                raise BrokerCapabilityError("account", "positions", "position_quantity_gap")
            signed_quantity = Decimal(str(quantity_value))
            side = (
                PositionSide.LONG
                if signed_quantity > 0
                else PositionSide.SHORT
                if signed_quantity < 0
                else None
            )
            raw_side = str(row.get("position_side") or "").upper()
            if raw_side == "LONG":
                if signed_quantity <= 0:
                    raise ValueError("position side conflicts with signed quantity")
                side = PositionSide.LONG
            elif raw_side == "SHORT":
                if signed_quantity >= 0:
                    raise ValueError("position side conflicts with signed quantity")
                side = PositionSide.SHORT
            elif raw_side:
                raise ValueError("unknown position side")
            observation_id = str(row.get("report_id") or row.get("position_id") or row.get("observation_id") or "")
            if not observation_id:
                raise BrokerCapabilityError("account", "positions", "position_identity_gap")
            positions.append(
                PositionFact(
                    instrument_id=instrument.canonical_symbol,
                    signed_quantity=signed_quantity,
                    side=side,
                    entry_price=_decimal(row.get("entry_price") or row.get("entryPx")),
                    leverage=_decimal(row.get("leverage")),
                    margin_mode={
                        "CROSS": MarginMode.CROSS,
                        "ISOLATED": MarginMode.ISOLATED,
                    }.get(str(row.get("margin_mode") or row.get("leverage_type") or "").upper()),
                    liquidation_price=_decimal(row.get("liquidation_price") or row.get("liquidationPx")),
                    margin_used=_decimal(row.get("margin_used") or row.get("marginUsed")),
                    position_value=_decimal(row.get("position_value") or row.get("positionValue")),
                    unrealized_pnl=_decimal(row.get("unrealized_pnl") or row.get("unrealizedPnl")),
                    broker_id=self._context.identity.broker_id,
                    environment=self._context.identity.environment,
                    observation_id=observation_id,
                    provenance=provenance,
                )
            )
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="account.positions",
            data=tuple(positions),
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_account(
        self,
        *,
        request_id: str,
        raw: Mapping[str, object],
    ) -> ExternalFactEnvelope[AccountSnapshot]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        if _is_nautilus_account_state(payload):
            return self._map_nautilus_account_state(
                request_id=request_id,
                payload=payload,
                provenance=provenance,
            )
        if not isinstance(payload.get("assetPositions"), list) or not isinstance(
            payload.get("marginSummary") or payload.get("crossMarginSummary"),
            Mapping,
        ):
            raise BrokerCapabilityError("account", "read", "canonical_account_snapshot_gap")
        summary = payload.get("marginSummary") or payload.get("crossMarginSummary")
        if not all(key in summary for key in ("accountValue", "totalRawUsd")):
            raise BrokerCapabilityError("account", "read", "canonical_account_snapshot_gap")
        _validate_account_identity(payload, self._context)
        observation_id = str(payload.get("snapshotId") or payload.get("timestamp") or "")
        if not observation_id:
            raise BrokerCapabilityError("account", "read", "account_snapshot_identity_gap")
        snapshot = self._account.map_clearinghouse(
            account_address=self._context.identity.account_address or "",
            raw=payload,
            provenance=provenance,
        )
        positions = tuple(replace(position, environment=self._context.identity.environment) for position in snapshot.positions)
        data = replace(
            snapshot,
            environment=self._context.identity.environment,
            positions=positions,
            observation_id=observation_id,
        )
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="account.snapshot",
            data=data,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def _map_nautilus_account_state(
        self,
        *,
        request_id: str,
        payload: Mapping[str, object],
        provenance: Provenance,
    ) -> ExternalFactEnvelope[AccountSnapshot]:
        """Map Nautilus' canonical AccountState without guessing venue fields."""

        _validate_account_identity(payload, self._context)
        if payload.get("reported") is not True:
            raise BrokerCapabilityError("account", "read", "account_state_unreported")
        balances = payload.get("balances")
        margins = payload.get("margins")
        if not isinstance(balances, list) or not isinstance(margins, list):
            raise BrokerCapabilityError("account", "read", "account_state_balance_margin_gap")
        base_currency = str(payload.get("base_currency") or "").strip().upper()
        if base_currency in {"NONE", "NULL"}:
            base_currency = ""
        if not base_currency:
            currencies = {
                str(row.get("currency") or "").strip().upper()
                for row in balances
                if isinstance(row, Mapping) and str(row.get("currency") or "").strip()
            }
            if currencies != {"USDC"}:
                raise BrokerCapabilityError("account", "read", "account_base_currency_gap")
            # Hyperliquid default perps expose one USDC collateral balance in
            # Nautilus AccountState but omit AccountState.base_currency. The
            # sole explicit collateral row is sufficient; multiple/ambiguous
            # currencies remain a capability gap above.
            base_currency = "USDC"
        balance_rows = [
            row
            for row in balances
            if isinstance(row, Mapping)
            and str(row.get("currency") or "").strip().upper() == base_currency
        ]
        if len(balance_rows) != 1:
            raise BrokerCapabilityError("account", "read", "account_state_balance_identity_gap")
        balance = balance_rows[0]
        total = _account_state_decimal(balance, "total", nonnegative=True)
        free = _account_state_decimal(balance, "free", nonnegative=True)
        locked = _account_state_decimal(balance, "locked", nonnegative=True)
        if free + locked != total:
            raise BrokerCapabilityError("account", "read", "account_state_balance_reconciliation_gap")
        margin_used = Decimal("0")
        for row in margins:
            if not isinstance(row, Mapping):
                raise BrokerCapabilityError("account", "read", "account_state_margin_identity_gap")
            currency = str(row.get("currency") or "").strip().upper()
            if currency != base_currency:
                continue
            margin_used += _account_state_decimal(row, "initial", nonnegative=True)
        observation_id = str(payload.get("event_id") or "").strip()
        if not observation_id:
            raise BrokerCapabilityError("account", "read", "account_state_identity_gap")
        snapshot = AccountSnapshot(
            broker_id="hyperliquid",
            account_address=self._context.identity.account_address or "",
            equity=total,
            balance=total,
            withdrawable=free,
            margin_used=margin_used,
            exposure=None,
            realized_pnl=None,
            unrealized_pnl=None,
            positions=(),
            provenance=provenance,
            environment=BrokerEnvironment.TESTNET,
            observation_id=observation_id,
        )
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="account.snapshot",
            data=snapshot,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_fee_schedule(
        self,
        *,
        request_id: str,
        raw: Mapping[str, object],
    ) -> ExternalFactEnvelope[FeeScheduleSnapshot]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        data = replace(
            self._fees.map_fee_schedule(payload, provenance),
            environment=self._context.identity.environment,
        )
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="fee.schedule",
            data=data,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_fill(
        self,
        *,
        request_id: str,
        broker_symbol: str,
        raw: Mapping[str, object],
    ) -> ExternalFactEnvelope[FillFact]:
        provenance = _provenance(raw, self._context)
        payload = _data(raw)
        if str(payload.get("coin") or "") != broker_symbol:
            raise ValueError("fill instrument identity mismatch")
        data = self._fees.map_fill(payload, provenance)
        data = replace(
            data,
            environment=self._context.identity.environment,
            fee=replace(data.fee, environment=self._context.identity.environment),
            builder_fee=replace(data.builder_fee, environment=self._context.identity.environment)
            if data.builder_fee
            else None,
        )
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type="fee.fill",
            data=data,
            request_id=request_id,
            provenance=provenance,
            raw_payload=payload,
        )

    def map_funding(self, *, request_id: str, raw: Mapping[str, object]) -> None:
        del request_id, raw
        raise BrokerCapabilityError("fee", "funding", "funding_transport_gap")

    def map_liquidation(self, *, request_id: str, raw: Mapping[str, object]) -> None:
        del request_id, raw
        raise BrokerCapabilityError("account", "liquidation", "liquidation_transport_gap")


def _validate_account_identity(
    payload: Mapping[str, object],
    context: ExternalBrokerBuildContext,
) -> None:
    expected = context.identity.account_address
    if not expected:
        raise RuntimeBoundaryError(
            "account_identity_required",
            "external account facts require a bound account address",
        )
    for key in ("account_address", "accountAddress", "account_id"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        if text != expected and not text.startswith(f"{expected}-"):
            raise ValueError("account_identity_mismatch")


def _is_nautilus_account_state(payload: Mapping[str, object]) -> bool:
    return (
        str(payload.get("type") or "") == "AccountState"
        and "balances" in payload
        and "margins" in payload
    )


def _account_state_decimal(
    row: Mapping[str, object],
    field: str,
    *,
    nonnegative: bool,
) -> Decimal:
    value = row.get(field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BrokerCapabilityError("account", "read", f"account_state_{field}_gap") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise BrokerCapabilityError("account", "read", f"account_state_{field}_gap")
    return result
