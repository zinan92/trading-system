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


__all__ = [
    "DASHBOARD_CONTROL_SCHEMA",
    "DashboardControlPlane",
    "public_catalog_loader",
    "SELECTION_SCHEMA",
]
