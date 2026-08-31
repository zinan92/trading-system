"""Composition seams for the attended Hyperliquid Testnet host.

The Dashboard and Jessie control paths intentionally know nothing about
provider-native signers or order payloads.  This module is the small host
adapter that joins their public facts to the reviewed standard-broker
position-protection profile.  Account reads remain credential-free; the
opaque signer path is only handed to the protected binding after an explicit
Park Testnet confirmation reaches the start handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.hyperliquid_testnet_account_reader import HyperliquidTestnetAccountReader
from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader
from services.journal_store import load_json
from services.strategy_control_plane import StrategyControlPlane


TESTNET_PROFILE = "hyperliquid-testnet-position-protection"
TESTNET_CAPABILITY_REVISION = "hyperliquid-testnet-position-protection-runtime-v1"
DEFAULT_INSTRUMENT_ID = "BTC-USD-PERP"
DEFAULT_MAX_SLIPPAGE = 50.0
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class HyperliquidTestnetRuntimeError(RuntimeError):
    """Redacted blocker at the Dashboard/Jessie composition boundary."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


@dataclass(frozen=True)
class HyperliquidTestnetRuntimeConfig:
    """Non-secret host configuration for one Hyperliquid Testnet account."""

    account_address: str
    instrument_id: str = DEFAULT_INSTRUMENT_ID
    protected_profile_enabled: bool = False
    secret_file: Path | None = None
    runtime_id: str = ""
    release_sha: str = ""
    standard_broker_release_sha: str = "2d3a5cc26538b24fb31ab81201facd5ee46476ba"
    capability_revision: str = TESTNET_CAPABILITY_REVISION
    approved_by: str = "park"
    max_slippage: float = DEFAULT_MAX_SLIPPAGE

    def __post_init__(self) -> None:
        address = str(self.account_address or "").strip()
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
            raise ValueError("testnet_account_address_invalid")
        instrument = str(self.instrument_id or "").strip()
        if not instrument:
            raise ValueError("testnet_instrument_required")
        object.__setattr__(self, "account_address", address)
        object.__setattr__(self, "instrument_id", instrument)
        if self.secret_file is not None:
            object.__setattr__(self, "secret_file", Path(self.secret_file))
        try:
            slippage = float(self.max_slippage)
        except (TypeError, ValueError) as exc:
            raise ValueError("testnet_max_slippage_invalid") from exc
        if slippage <= 0:
            raise ValueError("testnet_max_slippage_invalid")
        object.__setattr__(self, "max_slippage", slippage)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "HyperliquidTestnetRuntimeConfig | None":
        env = environ if environ is not None else os.environ
        address = str(env.get("HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS") or "").strip()
        if not address:
            return None
        profile = str(env.get("HYPERLIQUID_TESTNET_PROTECTION_PROFILE") or "").strip()
        secret_path = str(env.get("HYPERLIQUID_TESTNET_SECRET_FILE") or "").strip()
        release_sha = str(
            env.get("TRADING_ORCHESTRATOR_RELEASE_SHA")
            or env.get("TRADING_SYSTEM_RELEASE_SHA")
            or ""
        ).strip().lower()
        try:
            max_slippage = float(
                env.get("HYPERLIQUID_TESTNET_MAX_SLIPPAGE")
                or DEFAULT_MAX_SLIPPAGE
            )
        except (TypeError, ValueError):
            max_slippage = DEFAULT_MAX_SLIPPAGE
        return cls(
            account_address=address,
            instrument_id=str(
                env.get("HYPERLIQUID_TESTNET_INSTRUMENT_ID") or DEFAULT_INSTRUMENT_ID
            ).strip(),
            protected_profile_enabled=(profile == TESTNET_PROFILE and bool(secret_path)),
            secret_file=Path(secret_path) if secret_path else None,
            runtime_id=str(env.get("HYPERLIQUID_TESTNET_RUNTIME_ID") or "").strip(),
            release_sha=release_sha,
            standard_broker_release_sha=str(
                env.get("STANDARD_BROKER_RELEASE_SHA")
                or "2d3a5cc26538b24fb31ab81201facd5ee46476ba"
            ).strip().lower(),
            capability_revision=str(
                env.get("HYPERLIQUID_TESTNET_CAPABILITY_REVISION")
                or TESTNET_CAPABILITY_REVISION
            ).strip(),
            approved_by=str(env.get("HYPERLIQUID_TESTNET_APPROVED_BY") or "park").strip(),
            max_slippage=max_slippage,
        )

    @property
    def account_fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(self.account_address.encode("utf-8")).hexdigest()

    @property
    def start_ready(self) -> bool:
        return bool(
            self.protected_profile_enabled
            and self.secret_file is not None
            and self.runtime_id
            and _SHA1.fullmatch(self.release_sha) is not None
            and _SHA1.fullmatch(self.standard_broker_release_sha) is not None
            and self.capability_revision == TESTNET_CAPABILITY_REVISION
        )


class _ParkTestnetAccountReader:
    """Adapt public Hyperliquid facts to the Park clean-slate contract."""

    def __init__(
        self,
        config: HyperliquidTestnetRuntimeConfig,
        *,
        public_reader: object | None = None,
    ) -> None:
        self.config = config
        self.public_reader = public_reader or HyperliquidTestnetAccountReader(
            config.account_address
        )

    def read(self, instrument_id: str | None = None) -> dict[str, Any]:
        method = getattr(self.public_reader, "read", None)
        if not callable(method):
            raise HyperliquidTestnetRuntimeError("testnet_account_reader_invalid")
        selected = str(instrument_id or self.config.instrument_id).strip()
        try:
            raw = dict(method(selected))
        except TypeError:
            raw = dict(method(instrument_id=selected))
        capabilities = dict(raw.get("capabilities") or {})
        # The protected capability is an explicit host opt-in.  It never
        # becomes true merely because a public account read succeeded.
        capabilities["protection"] = bool(self.config.protected_profile_enabled)
        if self.config.protected_profile_enabled:
            capabilities["protection_profile"] = TESTNET_PROFILE
            capabilities["capability_revision"] = TESTNET_CAPABILITY_REVISION
        raw["capabilities"] = capabilities
        raw["selected_instrument_id"] = selected
        return raw

    def for_park(self, _output_root: Path, _cycle_id: str) -> dict[str, Any]:
        raw = self.read(self.config.instrument_id)
        positions = [
            dict(row)
            for row in (raw.get("positions") or [])
            if isinstance(row, Mapping) and _non_zero(row.get("signed_quantity"))
        ]
        orders = [
            dict(row)
            for row in (raw.get("open_orders") or [])
            if isinstance(row, Mapping)
        ]
        fills = [dict(row) for row in (raw.get("fills") or []) if isinstance(row, Mapping)]
        healthy = raw.get("fresh") is True and raw.get("coherent") is True
        reconciliation = {
            "status": "ok" if healthy else "blocked",
            "issues": list(raw.get("coherence_issues") or []),
            "source": raw.get("source"),
            "source_cursor": raw.get("source_cursor"),
            "observed_at": raw.get("observed_at"),
            "environment": "testnet",
            "broker_id": "hyperliquid",
        }
        account = {
            "equity": raw.get("equity"),
            "account_fingerprint": raw.get("account_fingerprint"),
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "fresh": raw.get("fresh") is True,
            "coherent": raw.get("coherent") is True,
            "source_cursor": raw.get("source_cursor"),
            "capabilities": capabilities_public(raw.get("capabilities")),
            "observed_at": raw.get("observed_at"),
        }
        snapshot = {
            "account": account,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "account_wide_legacy_exposure": {"positions": positions, "orders": orders},
        }
        return {
            **raw,
            "account": account,
            "snapshot": snapshot,
            "reconciliation": reconciliation,
            "reconciliation_healthy": healthy,
            "open_positions": len(positions),
            "open_or_accepted_orders": len(orders),
            "unresolved_runtime": raw.get("unknown_exposure") is True,
            "pending_terminal_actions": False,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "capabilities": capabilities_public(raw.get("capabilities")),
        }


def capabilities_public(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): value[key]
        for key in value
        if str(key).lower() not in {"private_key", "secret", "signer", "signature"}
    }


def _non_zero(value: object) -> bool:
    try:
        return abs(float(value or 0)) > 1e-12
    except (TypeError, ValueError):
        return True


def build_park_account_reader(
    config: HyperliquidTestnetRuntimeConfig,
    *,
    public_reader: object | None = None,
) -> Callable[[Path, str], Mapping[str, Any]]:
    """Return the source-bound Park account seam without touching secrets."""

    reader = _ParkTestnetAccountReader(config, public_reader=public_reader)
    return reader.for_park


def build_dashboard_account_reader(
    config: HyperliquidTestnetRuntimeConfig | None = None,
) -> object | None:
    """Build the public Dashboard account reader when an address is configured."""

    runtime_config = config or HyperliquidTestnetRuntimeConfig.from_environment()
    if runtime_config is None:
        return None
    try:
        return _ParkTestnetAccountReader(runtime_config)
    except Exception:
        return None


def build_testnet_start_handler(
    output_root: Path,
    *,
    config: HyperliquidTestnetRuntimeConfig | None,
    park_user_id: str,
    chat_id: str,
    market_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Build the guarded Testnet activation callback used by Jessie.

    The callback does not accept arbitrary strategy input.  It resolves the
    exact immutable plan digest from the Park journal and only then composes a
    protected external adapter.  The first order can therefore be submitted
    only after the durable confirmation ledger has already recorded Park's
    Testnet decision.
    """

    root = Path(output_root)
    read_market = market_reader or HyperliquidTestnetMarketReader().read

    def blocked(code: str) -> dict[str, Any]:
        return {
            "status": "blocked",
            "code": code,
            "next_action": "notify_park_and_wait",
            "execution_authorized": False,
            "network_operation_invoked": False,
        }

    def handler(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if config is None or not config.start_ready:
            return blocked("testnet_protected_runtime_unavailable")
        if not isinstance(payload, Mapping) or str(payload.get("environment") or "").lower() != "testnet":
            return blocked("testnet_environment_required")
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        proposal = payload.get("proposal") if isinstance(payload.get("proposal"), Mapping) else {}
        if decision.get("execution_authorized") is not True:
            return blocked("testnet_confirmation_required")
        digest = str(payload.get("plan_digest") or proposal.get("plan_digest") or "").strip()
        if not digest:
            return blocked("testnet_plan_digest_required")
        rows = load_json(root / "park_strategy" / "plans.jsonl")
        stored = next(
            (
                dict(row)
                for row in reversed(rows)
                if isinstance(row, Mapping) and str(row.get("plan_digest") or "") == digest
            ),
            None,
        )
        if stored is None:
            return blocked("testnet_plan_not_found")
        try:
            instrument = _plan_instrument(stored, config)
            market = _execution_market(dict(read_market(instrument)), instrument)
            lifecycle_plan = _park_plan_to_lifecycle_plan(
                stored,
                config=config,
                market=market,
            )
            confirmation = {
                **dict(decision),
                "proposal_id": str(decision.get("proposal_id") or proposal.get("proposal_id") or ""),
                "receipt_digest": str(decision.get("receipt_digest") or ""),
                "plan_digest": digest,
                "strategy_session_id": str(stored.get("strategy_session_id") or ""),
                "strategy_revision_id": str(stored.get("strategy_revision_id") or ""),
                "start_or_order_submitted": False,
            }
            if not confirmation["proposal_id"] or not confirmation["receipt_digest"]:
                return blocked("testnet_confirmation_receipt_required")
            context = BrokerBuildContext(
                output_root=root,
                execution_mode="live",
                live_trading_enabled=False,
                broker_config={
                    "provider": "standard_broker",
                    "broker_id": "hyperliquid",
                    "environment": "testnet",
                    "transport_profile": TESTNET_PROFILE,
                    "account_id": config.account_address,
                    "runtime_id": config.runtime_id,
                    "release_sha": config.release_sha,
                    "standard_broker_release_sha": config.standard_broker_release_sha,
                    "capability_revision": config.capability_revision,
                    # The proposal decision is the human approval for this
                    # one lifecycle; it is never emitted as secret material.
                    "approval_id": confirmation["proposal_id"],
                    "approved_by": config.approved_by or park_user_id or "park",
                    "secret_file": str(config.secret_file),
                    "instrument_id": instrument,
                    "instrument_binding": {"instrument_id": instrument},
                    "market_source": {
                        "source_id": str(market.get("source") or "hyperliquid.external_testnet"),
                        "broker_id": "hyperliquid",
                        "environment": "testnet",
                        "instrument_id": instrument,
                    },
                    "execution_scope": "hypercore:default",
                },
            )
            broker = build_broker_execution_port(context)
            try:
                preflight = broker.preflight(strategy_family=str(lifecycle_plan.get("strategy_type") or ""))
            except TypeError:
                preflight = broker.preflight()
            if not isinstance(preflight, Mapping) or preflight.get("ready") is not True:
                return {
                    **blocked("testnet_preflight_blocked"),
                    "preflight": dict(preflight) if isinstance(preflight, Mapping) else {},
                }
            control = StrategyControlPlane(root)
            family = str(lifecycle_plan.get("strategy_type") or "").lower()
            if family == "dca":
                result = control.start_testnet_dca(
                    lifecycle_plan,
                    confirmation=confirmation,
                    market=market,
                    adapter=broker,
                    actor={"actor": park_user_id or "park", "channel": "telegram", "chat_id": chat_id},
                )
            elif family == "grid":
                result = control.start_testnet_grid(
                    lifecycle_plan,
                    confirmation=confirmation,
                    market=market,
                    adapter=broker,
                    actor={"actor": park_user_id or "park", "channel": "telegram", "chat_id": chat_id},
                )
            else:
                return blocked("testnet_strategy_type_invalid")
            runtime = dict(result.get("runtime") or {})
            return {
                "status": "testnet_started",
                "runtime": runtime,
                "lifecycle": dict(result.get("lifecycle") or {}),
                "preflight": dict(preflight),
                "execution_authorized": True,
                "network_operation_invoked": True,
            }
        except HyperliquidTestnetRuntimeError as exc:
            return blocked(exc.code)
        except Exception as exc:  # noqa: BLE001 - redact provider/signer details.
            code = str(getattr(exc, "code", "") or "").strip()
            return blocked(code or f"testnet_start_blocked:{type(exc).__name__}")
        finally:
            close = locals().get("broker")
            close_method = getattr(close, "close", None)
            if callable(close_method):
                close_method()

    return handler


def _plan_instrument(plan: Mapping[str, Any], config: HyperliquidTestnetRuntimeConfig) -> str:
    normalized = plan.get("normalized_input") if isinstance(plan.get("normalized_input"), Mapping) else {}
    market = plan.get("market") if isinstance(plan.get("market"), Mapping) else {}
    return str(
        normalized.get("instrument_id")
        or plan.get("instrument_id")
        or market.get("instrument_id")
        or config.instrument_id
    ).strip()


def _execution_market(market: dict[str, Any], instrument: str) -> dict[str, Any]:
    price = market.get("price", market.get("mid"))
    if price in (None, ""):
        raise HyperliquidTestnetRuntimeError("testnet_market_price_missing")
    source = str(market.get("source") or "hyperliquid.external_testnet")
    cursor = str(market.get("source_cursor") or market.get("cursor") or "")
    return {
        **market,
        "price": float(price),
        "latest_close": float(price),
        "latest_timestamp": str(market.get("observed_at") or ""),
        "source": source,
        "cursor": cursor or _digest(market),
        "instrument_id": instrument,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "fresh": market.get("fresh") is True,
        "trusted": market.get("trusted") is True,
        "is_synthetic": False,
        "fallback_policy": "none",
        "execution_ready": market.get("trusted") is True and market.get("fresh") is True,
    }


def _park_plan_to_lifecycle_plan(
    plan: Mapping[str, Any],
    *,
    config: HyperliquidTestnetRuntimeConfig,
    market: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(plan.get("normalized_input") or {})
    risk = dict(plan.get("risk") or {})
    family = str(normalized.get("strategy_type") or "").lower()
    direction = str(normalized.get("direction") or "").lower()
    if family not in {"dca", "grid"}:
        raise HyperliquidTestnetRuntimeError("testnet_strategy_type_invalid")
    if direction not in {"long", "short", "neutral"}:
        raise HyperliquidTestnetRuntimeError("testnet_direction_invalid")
    session = str(plan.get("strategy_session_id") or normalized.get("strategy_session_id") or "").strip()
    revision = str(plan.get("strategy_revision_id") or normalized.get("strategy_revision_id") or "").strip()
    digest = str(plan.get("plan_digest") or "").strip()
    if not session or not revision or not digest:
        raise HyperliquidTestnetRuntimeError("testnet_plan_identity_incomplete")
    instrument = _plan_instrument(plan, config)
    current = float(market.get("price") or (plan.get("market") or {}).get("price") or 0)
    if current <= 0:
        raise HyperliquidTestnetRuntimeError("testnet_market_price_missing")
    plan_id = "jessie-testnet-" + digest.removeprefix("sha256:")[:32]
    cycle_id = "testnet-" + session
    timestamp = str(market.get("observed_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    base: dict[str, Any] = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": family,
        "strategy_plan_id": plan_id,
        "cycle_id": cycle_id,
        "version": 1,
        "status": "active",
        "locked_at": timestamp,
        "strategy_session_id": session,
        "strategy_revision_id": revision,
        "plan_digest": digest,
        "direction": direction,
        "instrument_id": instrument,
        "execution_context": {"market": dict(market), "instrument_id": instrument},
        "risk_budget": _risk_budget(risk, normalized, config, family),
        "field_sources": {"direction": "park_telegram", "risk_budget": "park_telegram"},
    }
    if family == "dca":
        count = max(1, int(risk.get("order_count") or normalized.get("order_count") or 1))
        upper = float(normalized.get("upper_price_boundary") or 0)
        lower = float(normalized.get("lower_price_boundary") or 0)
        prices = normalized.get("entry_prices")
        if not isinstance(prices, (list, tuple)):
            step = (upper - current) / count if direction == "short" else (current - lower) / count
            prices = [current + step * (index + 1) if direction == "short" else current - step * (index + 1) for index in range(count)]
        quantities = risk.get("per_order_quantities") if isinstance(risk.get("per_order_quantities"), list) else None
        quantity = float(risk.get("per_order_quantity") or 0)
        if quantity <= 0:
            raise HyperliquidTestnetRuntimeError("testnet_dca_quantity_missing")
        base["dca"] = {
            "entry_levels": [float(value) for value in prices],
            "notional_per_addition": float(risk.get("per_order_notional") or 0),
            "target_price": float(normalized.get("take_profit_price") or 0),
            "stop_price": float(normalized.get("stop_price") or 0),
            "max_additions": count,
            "loop_enabled": False,
            "entry_quantities": [float(value) for value in quantities] if quantities else [quantity] * count,
        }
    else:
        raw_rungs = risk.get("grid_rungs")
        if not isinstance(raw_rungs, list) or not raw_rungs:
            raise HyperliquidTestnetRuntimeError("testnet_grid_geometry_missing")
        base["upper_price_boundary"] = float(normalized.get("upper_price_boundary") or 0)
        base["lower_price_boundary"] = float(normalized.get("lower_price_boundary") or 0)
        base["midpoint"] = current
        base["grid"] = {
            "rungs": [
                {
                    "rung": int(row.get("rung") or index),
                    "price": float(row.get("price") or 0),
                    "side": str(row.get("side") or "").lower(),
                    "take_profit": float(row.get("take_profit") or 0),
                    "hard_stop": float(row.get("hard_stop") or 0),
                    "quantity": float(risk.get("per_order_quantity") or 0),
                }
                for index, row in enumerate(raw_rungs, start=1)
                if isinstance(row, Mapping)
            ],
            "spacing": risk.get("grid_spacing"),
            "mode": "arithmetic",
        }
    return base


def _risk_budget(
    risk: Mapping[str, Any],
    normalized: Mapping[str, Any],
    config: HyperliquidTestnetRuntimeConfig,
    family: str,
) -> dict[str, Any]:
    equity = float(risk.get("account_equity") or 0)
    max_notional = float(risk.get("maximum_notional") or 0)
    leverage = float(risk.get("effective_leverage") or normalized.get("maximum_leverage") or 0)
    max_loss = float(risk.get("theoretical_max_loss") or normalized.get("maximum_acceptable_loss") or 0)
    count = max(1, int(risk.get("order_count") or normalized.get("order_count") or 1))
    if min(equity, max_notional, leverage, max_loss) <= 0:
        raise HyperliquidTestnetRuntimeError("testnet_risk_budget_incomplete")
    return {
        "maximum_loss_at_full_depth": max_loss,
        "equity": equity,
        "leverage_limit": leverage,
        "max_notional": max_notional,
        "max_open_orders": count,
        "max_open_positions": 1 if family == "dca" else count,
        "max_slippage": config.max_slippage,
        "max_submit_retries": 1,
    }


def _digest(value: Mapping[str, Any]) -> str:
    import json

    return "sha256:" + hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DEFAULT_INSTRUMENT_ID",
    "HyperliquidTestnetRuntimeConfig",
    "HyperliquidTestnetRuntimeError",
    "TESTNET_CAPABILITY_REVISION",
    "TESTNET_PROFILE",
    "build_dashboard_account_reader",
    "build_park_account_reader",
    "build_testnet_start_handler",
]
