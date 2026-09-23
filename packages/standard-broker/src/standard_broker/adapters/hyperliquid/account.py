"""Hyperliquid account-state mapping for default perpetuals."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from ...account import AccountSnapshot, LiquidationFact, PositionFact, PositionSide
from ...fees import FillFact
from ...instruments import MarginMode
from ...models import BrokerEnvironment, Provenance
from .bridge import NautilusHyperliquidRuntime
from ...runtime_facts import RuntimeFactLedger
from .errors import UnknownInstrumentError
from .instruments import HyperliquidInstrumentAdapter


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "None" or value == "":
        return None
    return Decimal(str(value))


class HyperliquidAccountAdapter:
    """Maps official Hyperliquid clearinghouse facts to canonical account facts."""

    name = "account"

    def __init__(self, instruments: HyperliquidInstrumentAdapter) -> None:
        self._instruments = instruments

    def map_clearinghouse(
        self,
        *,
        account_address: str,
        raw: Mapping[str, object],
        provenance: Provenance,
        realized_pnl: Decimal | None = None,
    ) -> AccountSnapshot:
        summary = raw.get("marginSummary") or raw.get("crossMarginSummary") or {}
        if not isinstance(summary, Mapping):
            raise TypeError("Hyperliquid margin summary must be a mapping")
        raw_positions = raw.get("assetPositions") or []
        if not isinstance(raw_positions, list):
            raise TypeError("Hyperliquid assetPositions must be a list")

        positions = tuple(
            self._map_position(item, provenance) for item in raw_positions
        )
        unrealized_values = [position.unrealized_pnl for position in positions]
        if not positions:
            unrealized_pnl = Decimal(0)
        elif all(value is not None for value in unrealized_values):
            unrealized_pnl = sum(value for value in unrealized_values if value is not None)
        else:
            unrealized_pnl = None

        return AccountSnapshot(
            broker_id="hyperliquid",
            account_address=account_address,
            equity=_optional_decimal(summary.get("accountValue")),
            balance=_optional_decimal(summary.get("totalRawUsd")),
            withdrawable=_optional_decimal(raw.get("withdrawable")),
            margin_used=_optional_decimal(summary.get("totalMarginUsed")),
            exposure=_optional_decimal(summary.get("totalNtlPos")),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            positions=positions,
            provenance=provenance,
        )

    def _map_position(self, raw: object, provenance: Provenance) -> PositionFact:
        if not isinstance(raw, Mapping):
            raise TypeError("Hyperliquid asset position must be a mapping")
        position = raw.get("position") or raw
        if not isinstance(position, Mapping):
            raise TypeError("Hyperliquid position must be a mapping")
        broker_symbol = str(position.get("coin") or "")
        try:
            instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        except KeyError as exc:
            raise UnknownInstrumentError(broker_symbol) from exc
        signed_quantity = Decimal(str(position.get("szi", "0")))
        leverage = position.get("leverage") or {}
        if not isinstance(leverage, Mapping):
            raise TypeError("Hyperliquid leverage must be a mapping")
        leverage_type = str(leverage.get("type") or "")
        margin_mode = {
            "cross": MarginMode.CROSS,
            "isolated": MarginMode.ISOLATED,
        }.get(leverage_type)
        side = (
            PositionSide.LONG
            if signed_quantity > 0
            else PositionSide.SHORT
            if signed_quantity < 0
            else None
        )
        observation_id = str(position.get("positionId") or position.get("timestamp") or "")
        if not observation_id:
            raise ValueError("position_identity_required")
        return PositionFact(
            instrument_id=instrument.canonical_symbol,
            signed_quantity=signed_quantity,
            side=side,
            entry_price=_optional_decimal(position.get("entryPx")),
            leverage=_optional_decimal(leverage.get("value")),
            margin_mode=margin_mode,
            liquidation_price=_optional_decimal(position.get("liquidationPx")),
            margin_used=_optional_decimal(position.get("marginUsed")),
            position_value=_optional_decimal(position.get("positionValue")),
            unrealized_pnl=_optional_decimal(position.get("unrealizedPnl")),
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            observation_id=observation_id,
            provenance=provenance,
        )

    def map_liquidation(
        self,
        raw: Mapping[str, object],
        provenance: Provenance,
    ) -> LiquidationFact:
        liquidation_id = str(raw.get("lid") or raw.get("id") or "")
        if not liquidation_id:
            raise ValueError("liquidation_identity_required")
        return LiquidationFact(
            liquidation_id=liquidation_id,
            broker_id="hyperliquid",
            account_address=str(raw.get("liquidated_user") or ""),
            liquidator=str(raw["liquidator"]) if raw.get("liquidator") else None,
            notional=_optional_decimal(raw.get("liquidated_ntl_pos")),
            account_value=_optional_decimal(raw.get("liquidated_account_value")),
            method=str(raw["method"]) if raw.get("method") else None,
            liquidation_fee=None,
            occurred_at=provenance.received_at,
            provenance=provenance,
        )

    @staticmethod
    def realized_pnl_from_fills(fills: Sequence[FillFact]) -> Decimal:
        """Aggregate unique fill closed-PnL facts without double counting."""

        seen: set[str] = set()
        total = Decimal(0)
        for fill in fills:
            if fill.fill_id in seen:
                continue
            seen.add(fill.fill_id)
            total += fill.closed_pnl
        return total


class HyperliquidRuntimeAccountAdapter:
    """Connect canonical account facts to the Paper-safe runtime seam."""

    name = "hyperliquid_runtime_account"

    def __init__(
        self,
        *,
        runtime: NautilusHyperliquidRuntime,
        instruments: HyperliquidInstrumentAdapter,
        ledger: RuntimeFactLedger,
    ) -> None:
        self._runtime = runtime
        self._mapper = HyperliquidAccountAdapter(instruments)
        self._ledger = ledger
        self._ledger.bind_session(
            broker_id=runtime.session.broker_id,
            environment=runtime.session.environment.value,
            account_address=runtime.session.account.address,
        )

    def _invoke(self, operation: str, request: Mapping[str, object]) -> tuple[Mapping[str, object], Provenance]:
        response = self._runtime._invoke_native("account", operation, request)
        if not isinstance(response, Mapping):
            raise ValueError(f"Hyperliquid account {operation} response must be a mapping")
        provenance = response.get("provenance")
        if not isinstance(provenance, Provenance):
            raise ValueError("Broker account facts require source provenance")
        data = response.get("data", response)
        if not isinstance(data, Mapping):
            raise ValueError(f"Hyperliquid account {operation} data must be a mapping")
        return data, provenance

    def read_account(self, account_address: str) -> AccountSnapshot:
        if account_address != self._runtime.session.account.address:
            raise ValueError("account_identity_mismatch")
        raw, provenance = self._invoke("read", {"account_address": account_address})
        returned_account = raw.get("accountAddress") or raw.get("account_address")
        if returned_account is not None and str(returned_account) != self._runtime.session.account.address:
            raise ValueError("account_identity_mismatch")
        realized = raw.get("realizedPnl")
        realized_pnl = (
            Decimal(str(realized))
            if realized is not None
            else HyperliquidAccountAdapter.realized_pnl_from_fills(
                tuple(value for key, value in self._ledger.fills.items() if key.startswith(self._namespace()))
            )
        )
        observation_id = str(raw.get("snapshotId") or raw.get("timestamp") or "")
        if not observation_id:
            raise ValueError("account_snapshot_identity_required")
        existing = self._ledger.accounts.get(self._key(observation_id))
        snapshot = self._mapper.map_clearinghouse(
            account_address=account_address,
            raw=raw,
            provenance=provenance,
            realized_pnl=realized_pnl,
        )
        positions = tuple(
            replace(position, environment=self._runtime.session.environment)
            for position in snapshot.positions
        )
        result = replace(
            snapshot,
            environment=self._runtime.session.environment,
            positions=positions,
            observation_id=observation_id,
        )
        if existing == result:
            return existing
        if existing is not None and replace(result, realized_pnl=existing.realized_pnl) != existing:
            raise ValueError("account_observation_conflict")
        self._ledger.accounts[self._key(observation_id)] = result
        return result

    def read_liquidation(self, request: Mapping[str, object]) -> LiquidationFact:
        requested_account = str(request.get("account_address") or self._runtime.session.account.address)
        if requested_account != self._runtime.session.account.address:
            raise ValueError("account_identity_mismatch")
        raw, provenance = self._invoke("liquidation", request)
        fact = self._mapper.map_liquidation(raw, provenance)
        if fact.account_address != self._runtime.session.account.address:
            raise ValueError("account_identity_mismatch")
        existing = self._ledger.liquidations.get(self._key(fact.liquidation_id))
        if existing is not None:
            if existing != fact:
                raise ValueError("liquidation_observation_conflict")
            return existing
        result = replace(fact, environment=self._runtime.session.environment)
        self._ledger.liquidations[self._key(result.liquidation_id)] = result
        return result

    @property
    def fill_facts(self) -> tuple[FillFact, ...]:
        return tuple(
            value for key, value in self._ledger.fills.items() if key.startswith(self._namespace())
        )

    def record_fill_fact(self, fact: FillFact) -> FillFact:
        """Ingest one canonical fill identity for realized-PnL aggregation."""

        if fact.broker_id != self._runtime.session.broker_id or fact.environment is not self._runtime.session.environment:
            raise ValueError("fill_runtime_identity_mismatch")
        existing = self._ledger.fills.get(self._key(fact.fill_id))
        if existing is not None:
            return existing
        self._ledger.fills[self._key(fact.fill_id)] = fact
        return fact

    def _key(self, identity: str) -> str:
        return self._namespace() + identity

    def _namespace(self) -> str:
        return ":".join(
            (
                self._runtime.session.broker_id,
                self._runtime.session.environment.value,
                self._runtime.session.account.address,
            )
        ) + ":"
