"""Venue and Instrument selection seam for Dashboard V5.

The control plane is intentionally small: it owns the operator's selection
identity and read-only catalog projection, while the Trading System
composition root remains responsible for strategy, risk, authorization, and
execution.  Later phases extend this same seam with preview and activation;
this module never calls a Broker or exposes credentials.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from services.dca_plan import build_dca_preview
from services.grid_sizing import build_grid_preview
from services.journal_store import load_json, write_json


DASHBOARD_CONTROL_SCHEMA = "dashboard-control-plane-v1"
SELECTION_SCHEMA = "dashboard-selection-v1"
SELECTION_PATH = "dashboard_control_plane/selection.json"


_VENUE_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "binance.paper",
        "label": "Binance Paper",
        "broker_id": "binance",
        "environment": "paper",
        "instrument_scope": "paper",
        "catalog_source": "configured_paper_instruments",
    },
    {
        "id": "hyperliquid.testnet",
        "label": "Hyperliquid Testnet",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "instrument_scope": "default_perpetuals",
        "catalog_source": "hyperliquid.external_testnet",
    },
)


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _public(value: Any) -> Any:
    sensitive = {
        "api_key",
        "api_secret",
        "private_key",
        "secret",
        "signer",
        "signature",
        "signed_payload",
        "authorization",
        "credential",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).strip().lower() not in sensitive
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _normalise_instrument(row: Mapping[str, Any], *, profile_id: str) -> dict[str, Any]:
    instrument_id = str(row.get("instrument_id") or row.get("symbol") or "").strip()
    if not instrument_id:
        raise ValueError("instrument_id_required")
    asset = str(row.get("asset") or row.get("base_asset") or instrument_id.split("-", 1)[0]).strip()
    eligibility = str(row.get("eligibility") or row.get("status") or "unknown").strip().lower()
    if eligibility not in {"eligible", "blocked", "unknown"}:
        eligibility = "unknown"
    blockers = [
        str(item)
        for item in (row.get("blockers") or [])
        if str(item).strip()
    ]
    if eligibility != "eligible" and not blockers:
        blockers = ["market_facts_pending"]
    return {
        "instrument_id": instrument_id,
        "asset": asset,
        "asset_index": row.get("asset_index", row.get("index")),
        "size_decimals": row.get("size_decimals", row.get("szDecimals")),
        "price_decimals": row.get("price_decimals", row.get("pxDecimals")),
        "max_leverage": row.get("max_leverage", row.get("maxLeverage")),
        "eligibility": eligibility,
        "blockers": sorted(set(blockers)),
        "market_fresh": row.get("market_fresh"),
        "market_quality": _public(row.get("market_quality") or {}),
        "venue_profile_id": profile_id,
    }


def _default_catalog_loader(profile_id: str) -> Iterable[Mapping[str, Any]]:
    if profile_id == "binance.paper":
        return (
            {
                "instrument_id": "XAUUSDT.BINANCE",
                "asset": "XAU",
                "eligibility": "eligible",
                "market_fresh": None,
            },
        )
    if profile_id == "hyperliquid.testnet":
        # The public reader can replace this seed with the complete meta
        # universe.  Keeping BTC visible while the catalog is unavailable is
        # deliberate: a missing catalog is an explicit blocker, never an
        # empty list that looks like a successful filter.
        return (
            {
                "instrument_id": "BTC-USD-PERP",
                "asset": "BTC",
                "asset_index": 0,
                "size_decimals": 5,
                "max_leverage": 50,
                "eligibility": "unknown",
                "blockers": ["instrument_catalog_unavailable"],
            },
        )
    raise ValueError("venue_profile_not_found")


def public_catalog_loader(profile_id: str) -> Iterable[Mapping[str, Any]]:
    """Load a public catalog for a selected Venue Profile.

    The Dashboard calls this only after the operator asks for catalog data;
    ordinary unit tests can keep using the deterministic seed loader or inject
    their own facts.  A public-reader failure is propagated to the control
    plane, which renders an explicit catalog blocker.
    """

    if profile_id != "hyperliquid.testnet":
        return _default_catalog_loader(profile_id)
    from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader

    return HyperliquidTestnetMarketReader().read_catalog().get("instruments") or ()


class DashboardControlPlane:
    """Durable, read-only venue/instrument selection boundary."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        *,
        catalog_loader: Callable[[str], Iterable[Mapping[str, Any]]] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.catalog_loader = catalog_loader or _default_catalog_loader
        self.clock = clock or _now
        self.selection_path = self.output_root / SELECTION_PATH

    @staticmethod
    def venue_profiles() -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in _VENUE_PROFILES)

    def catalog(self) -> dict[str, Any]:
        profiles: list[dict[str, Any]] = []
        for profile in _VENUE_PROFILES:
            profile_id = str(profile["id"])
            blockers: list[str] = []
            try:
                rows = [
                    _normalise_instrument(row, profile_id=profile_id)
                    for row in self.catalog_loader(profile_id)
                    if isinstance(row, Mapping)
                ]
            except Exception as exc:  # noqa: BLE001 - catalog is a read blocker.
                rows = []
                blockers.append(
                    "instrument_catalog_unavailable"
                    if isinstance(exc, (OSError, RuntimeError, ValueError))
                    else "instrument_catalog_invalid"
                )
            rows.sort(key=lambda item: (str(item.get("asset") or ""), str(item["instrument_id"])))
            profile_payload = {
                **dict(profile),
                "instruments": rows,
                "catalog_revision": _digest(rows),
                "catalog_status": "blocked" if blockers else "ready",
                "blockers": sorted(set(blockers)),
            }
            profiles.append(profile_payload)
        return {
            "schema_version": DASHBOARD_CONTROL_SCHEMA,
            "catalog_revision": _digest(profiles),
            "venue_profiles": profiles,
            "selection": self.status(),
            "safety": {
                "read_only": True,
                "credentials_exposed": False,
                "broker_calls": False,
                "orders_submitted": False,
            },
        }

    def status(self) -> dict[str, Any]:
        rows = load_json(self.selection_path)
        if rows and isinstance(rows[-1], Mapping):
            return dict(rows[-1])
        return {
            "schema_version": SELECTION_SCHEMA,
            "venue_profile_id": None,
            "instrument_id": None,
            "strategy_family": None,
            "selection_digest": _digest({"venue_profile_id": None, "instrument_id": None}),
            "updated_at": None,
        }

    def select(
        self,
        *,
        venue_profile_id: str,
        instrument_id: str | None = None,
        strategy_family: str | None = None,
    ) -> dict[str, Any]:
        profile_id = str(venue_profile_id or "").strip().lower()
        profile = next((row for row in _VENUE_PROFILES if row["id"] == profile_id), None)
        if profile is None:
            raise ValueError("venue_profile_not_found")
        instrument = str(instrument_id or "").strip() or None
        if instrument is not None and any(character in instrument for character in "\r\n"):
            raise ValueError("instrument_id_invalid")
        family = str(strategy_family or "").strip().lower() or None
        if family is not None and family not in {"dca", "grid"}:
            raise ValueError("strategy_family_invalid")
        previous = self.status()
        changed_upstream = (
            previous.get("venue_profile_id") != profile_id
            or previous.get("instrument_id") != instrument
        )
        if changed_upstream:
            family = None
        identity = {
            "venue_profile_id": profile_id,
            "instrument_id": instrument,
            "strategy_family": family,
        }
        result = {
            "schema_version": SELECTION_SCHEMA,
            **identity,
            "selection_digest": _digest(identity),
            "updated_at": self.clock(),
        }
        write_json(self.selection_path, [result])
        return result

    def preview(
        self,
        *,
        venue_profile_id: str,
        instrument_id: str,
        strategy_family: str,
        strategy: Mapping[str, Any],
        market: Mapping[str, Any] | None = None,
        account: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a deterministic, non-authorizing DCA/Grid preview.

        The canonical strategy builders remain the economic source of truth.
        This facade only maps the Dashboard form into those builders and adds
        venue/Instrument identity and explicit blockers around the result.
        """

        profile_id = str(venue_profile_id or "").strip().lower()
        instrument = str(instrument_id or "").strip()
        family = str(strategy_family or "").strip().lower()
        if not any(row["id"] == profile_id for row in _VENUE_PROFILES):
            raise ValueError("venue_profile_not_found")
        if not instrument:
            raise ValueError("instrument_id_required")
        if family not in {"dca", "grid"}:
            raise ValueError("strategy_family_invalid")
        if not isinstance(strategy, Mapping):
            raise ValueError("strategy_configuration_invalid")

        blockers: list[str] = []
        source_market = dict(market or {})
        if not source_market:
            blockers.append("market_facts_required")
        source_account = dict(account or {})
        if profile_id == "hyperliquid.testnet" and account is None:
            blockers.append("testnet_account_unavailable")

        normalized_market = self._normalise_market(source_market, instrument)
        normalized_strategy = dict(strategy)
        preview: dict[str, Any] | None = None
        try:
            if not source_market:
                raise ValueError("market_facts_required")
            if family == "dca":
                preview = build_dca_preview(
                    "dashboard-preview",
                    {
                        "direction": normalized_strategy.get("direction"),
                        "dca": self._normalise_dca(normalized_strategy),
                        "risk_budget": dict(normalized_strategy.get("risk_budget") or {}),
                    },
                    market=normalized_market,
                    account=source_account,
                    config=dict(config or self._default_strategy_config()),
                )
            else:
                preview = build_grid_preview(
                    "dashboard-preview",
                    self._normalise_grid(normalized_strategy),
                    market=normalized_market,
                    account=source_account,
                    config=dict(config or self._default_strategy_config()),
                    allow_unsafe_manual_preview=False,
                )
        except (TypeError, ValueError, KeyError) as exc:
            code = str(exc).strip() or "strategy_preview_invalid"
            blockers.append(code)

        hard_blockers = sorted(set(blockers))
        payload = {
            "schema_version": "dashboard-strategy-preview-v1",
            "venue_profile_id": profile_id,
            "instrument_id": instrument,
            "strategy_family": family,
            "requested": _public(normalized_strategy),
            "preview": _public(preview) if preview is not None else None,
            "market": _public(normalized_market),
            "account": self._account_summary(source_account),
            "blockers": hard_blockers,
            "execution_ready": not hard_blockers,
            "authorizing": False,
        }
        payload["preview_digest"] = _digest(payload)
        return payload

    def account_admission(
        self,
        *,
        venue_profile_id: str,
        instrument_id: str,
        account_reader: object | None,
    ) -> dict[str, Any]:
        """Project a fail-closed, non-secret Testnet account admission result."""

        profile_id = str(venue_profile_id or "").strip().lower()
        instrument = str(instrument_id or "").strip()
        blockers: list[str] = []
        account: dict[str, Any] = {}
        if profile_id != "hyperliquid.testnet":
            return {
                "schema_version": "dashboard-account-admission-v1",
                "venue_profile_id": profile_id,
                "instrument_id": instrument,
                "ready": True,
                "clean_state": True,
                "account": account,
                "blockers": [],
                "safety": self._account_safety(),
            }
        reader_method = getattr(account_reader, "read", None)
        if not callable(reader_method):
            blockers.append("testnet_account_unavailable")
        else:
            try:
                raw = reader_method(instrument_id=instrument)
            except Exception as exc:  # noqa: BLE001 - redact reader details.
                blockers.append(
                    str(getattr(exc, "code", "")).strip()
                    or "testnet_account_unavailable"
                )
                raw = {}
            if isinstance(raw, Mapping):
                account = self._account_summary(raw)
            else:
                blockers.append("testnet_account_payload_invalid")
                raw = {}
            if raw:
                if str(raw.get("broker_id") or "").lower() != "hyperliquid":
                    blockers.append("testnet_account_broker_mismatch")
                if str(raw.get("environment") or "").lower() != "testnet":
                    blockers.append("testnet_account_environment_mismatch")
                if raw.get("fresh") is not True:
                    blockers.append("testnet_account_stale")
                if raw.get("coherent") is not True:
                    blockers.append("testnet_account_incoherent")
                if raw.get("equity") in (None, ""):
                    blockers.append("testnet_account_equity_unavailable")
                capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), Mapping) else {}
                if capabilities.get("protection") is not True:
                    blockers.append("testnet_protection_capability_unavailable")
                positions = [
                    row
                    for row in (raw.get("positions") or [])
                    if isinstance(row, Mapping)
                    and self._non_zero(row.get("signed_quantity"))
                ]
                open_orders = [row for row in (raw.get("open_orders") or []) if isinstance(row, Mapping)]
                if positions or open_orders or raw.get("unknown_exposure") is True:
                    blockers.append("account_not_clean")
                account["selected_instrument_id"] = instrument
                account["open_position_count"] = len(positions)
                account["open_order_count"] = len(open_orders)
                account["unknown_exposure"] = raw.get("unknown_exposure") is True
        clean_state = "account_not_clean" not in blockers
        return {
            "schema_version": "dashboard-account-admission-v1",
            "venue_profile_id": profile_id,
            "instrument_id": instrument,
            "ready": not blockers,
            "clean_state": clean_state and not blockers,
            "account": account,
            "blockers": sorted(set(blockers)),
            "safety": self._account_safety(),
        }

    @staticmethod
    def _non_zero(value: Any) -> bool:
        try:
            return float(value) != 0.0
        except (TypeError, ValueError):
            return bool(value)

    @staticmethod
    def _account_safety() -> dict[str, bool]:
        return {
            "credentials_exposed": False,
            "broker_mutation": False,
            "orders_submitted": False,
        }

    @staticmethod
    def _normalise_market(market: Mapping[str, Any], instrument_id: str) -> dict[str, Any]:
        result = dict(market)
        if result:
            result.setdefault("latest_close", result.get("price") or result.get("mid"))
            result.setdefault("latest_timestamp", result.get("observed_at"))
            result.setdefault("status", "ready" if result.get("fresh") is True else result.get("raw_status"))
            result.setdefault("is_synthetic", False)
            result.setdefault("provider", result.get("source") or "dashboard")
            result.setdefault("symbol", instrument_id)
            result.setdefault("timeframe", "1m")
        return result

    @staticmethod
    def _normalise_dca(strategy: Mapping[str, Any]) -> dict[str, Any]:
        settings = dict(strategy.get("dca") or strategy)
        levels = settings.get("entry_levels", settings.get("entry_prices"))
        if levels is None and settings.get("range") is not None:
            range_value = settings.get("range")
            if isinstance(range_value, Mapping):
                low = range_value.get("low")
                high = range_value.get("high")
                count = int(settings.get("max_additions") or settings.get("count") or 0)
                if count > 0 and low is not None and high is not None:
                    step = (float(high) - float(low)) / max(count - 1, 1)
                    direction = str(strategy.get("direction") or "long").lower()
                    levels = [
                        float(high) - step * index
                        if direction == "long"
                        else float(low) + step * index
                        for index in range(count)
                    ]
        return {
            **settings,
            "entry_levels": list(levels or []),
            "target_price": settings.get("target_price", settings.get("take_profit")),
            "stop_price": settings.get("stop_price", settings.get("stop_loss")),
            "notional_per_addition": settings.get(
                "notional_per_addition",
                settings.get("notional_per_entry"),
            ),
            "max_additions": settings.get("max_additions", settings.get("count")),
            "loop_enabled": False,
        }

    @staticmethod
    def _normalise_grid(strategy: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(strategy)
        grid = dict(result.get("grid") or {})
        range_value = dict(result.get("range") or {})
        if "low" not in range_value:
            range_value["low"] = result.get("lower_boundary")
        if "high" not in range_value:
            range_value["high"] = result.get("upper_boundary")
        grid.setdefault("count", result.get("count"))
        grid.setdefault("notional_per_grid", result.get("notional_per_grid"))
        grid.setdefault("notional_mode", "manual")
        result["range"] = range_value
        result["grid"] = grid
        result.setdefault("solver", {"mode": "manual_adaptive", "locked": ["range", "grid_count", "notional_per_grid"]})
        return result

    @staticmethod
    def _account_summary(account: Mapping[str, Any]) -> dict[str, Any]:
        sensitive = {
            "private_key",
            "secret",
            "api_key",
            "api_secret",
            "signer",
            "account_address",
            "address",
        }
        return {
            str(key): _public(value)
            for key, value in account.items()
            if str(key).lower() not in sensitive
        }

    @staticmethod
    def _default_strategy_config() -> dict[str, Any]:
        return {
            "max_leverage": 10.0,
            "cost_per_side_bp": 0.5,
            "execution_contract": {
                "schema_version": "dashboard-preview-execution-v1",
                "execution_instrument_id": "dashboard-preview",
                "price_increment": "0.01",
                "quantity_increment": "0.00001",
            },
            "strategy_grid": {
                "range_timeframe": "1d",
                "spacing_timeframe": "4h",
                "execution_timeframe": "1m",
                "range_atr_period": 14,
                "spacing_atr_period": 14,
                "styles": {
                    "steady": {"range_atr_multiple": 2.0, "spacing_atr_multiple": 1.0},
                    "aggressive": {"range_atr_multiple": 1.5, "spacing_atr_multiple": 0.75},
                },
                "capital_utilization_cap": 1.0,
                "min_net_profit_per_grid_usd": 10.0,
                "required_leverage": 10.0,
                "min_grid_count": 2,
                "max_grid_count": 70,
                "default_mode": "arithmetic",
            },
        }


__all__ = [
    "DASHBOARD_CONTROL_SCHEMA",
    "DashboardControlPlane",
    "public_catalog_loader",
    "SELECTION_SCHEMA",
]
