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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from services.strategy_package_adapter import build_dca_preview, build_grid_preview
from services.journal_store import load_json, write_json


DASHBOARD_CONTROL_SCHEMA = "dashboard-control-plane-v1"
SELECTION_SCHEMA = "dashboard-selection-v1"
SELECTION_PATH = "dashboard_control_plane/selection.json"
CONFIRMATION_PATH = "dashboard_control_plane/confirmations.json"
NOTIFICATION_PATH = "dashboard_control_plane/notifications.json"
PREVIEW_PATH = "dashboard_control_plane/previews.json"


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


def _canonical_preview_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value[key]
        for key in sorted(value)
        if str(key) not in {"preview_digest", "authorizing"}
    }


def canonical_preview_digest(value: Mapping[str, Any]) -> str:
    """Recompute a Preview digest without trusting a client-supplied token."""

    return _digest(_canonical_preview_payload(value))


def canonical_risk_gate_digest(value: Mapping[str, Any]) -> str:
    """Return the stable digest for requested/effective gate evidence."""

    return _digest(_public(dict(value)))


def _catalog_revision_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project only stable instrument facts into the catalog revision."""

    return {
        str(key): row[key]
        for key in sorted(row)
        if str(key) not in {"market_fresh", "market_quality"}
    }


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

    reader = HyperliquidTestnetMarketReader()
    rows = [dict(row) for row in (reader.read_catalog().get("instruments") or ())]
    # The public metadata call establishes structural inventory.  The first
    # proof candidate (BTC) is execution-eligible only when its current public
    # market fact also passes the reader's BBO/mid checks; other pairs remain
    # visible as unknown until an explicit market fact is read for them.
    for row in rows:
        if str(row.get("instrument_id") or "") != "BTC-USD-PERP":
            continue
        try:
            market = reader.read("BTC-USD-PERP")
        except Exception:
            row["eligibility"] = "blocked"
            row["blockers"] = ["market_facts_unavailable"]
        else:
            row["eligibility"] = "eligible"
            row["blockers"] = []
            row["market_fresh"] = market.get("fresh") is True
            row["market_quality"] = {
                "bid": market.get("bid"),
                "ask": market.get("ask"),
                "mid": market.get("mid"),
            }
    return rows


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
                rows = [
                    _normalise_instrument(row, profile_id=profile_id)
                    for row in _default_catalog_loader(profile_id)
                ]
                blockers.append(
                    "instrument_catalog_unavailable"
                    if isinstance(exc, (OSError, RuntimeError, ValueError))
                    else "instrument_catalog_invalid"
                )
            rows.sort(key=lambda item: (str(item.get("asset") or ""), str(item["instrument_id"])))
            profile_payload = {
                **dict(profile),
                "instruments": rows,
                "catalog_revision": _digest([_catalog_revision_row(row) for row in rows]),
                "catalog_status": "blocked" if blockers else "ready",
                "blockers": sorted(set(blockers)),
            }
            profiles.append(profile_payload)
        return {
            "schema_version": DASHBOARD_CONTROL_SCHEMA,
            "catalog_revision": _digest(
                [
                    {
                        "id": profile["id"],
                        "catalog_revision": profile["catalog_revision"],
                    }
                    for profile in profiles
                ]
            ),
            "venue_profiles": profiles,
            "selection": self.status(),
            "safety": {
                "read_only": True,
                "credentials_exposed": False,
                "broker_calls": False,
                "orders_submitted": False,
            },
        }

    def _catalog_entry(self, profile_id: str, instrument_id: str) -> tuple[dict[str, Any] | None, str]:
        snapshot = self.catalog()
        profile = next(
            (row for row in snapshot["venue_profiles"] if row.get("id") == profile_id),
            None,
        )
        if not isinstance(profile, Mapping):
            return None, str(snapshot.get("catalog_revision") or "")
        entry = next(
            (
                row
                for row in profile.get("instruments", [])
                if isinstance(row, Mapping)
                and str(row.get("instrument_id") or "") == instrument_id
            ),
            None,
        )
        return (dict(entry) if isinstance(entry, Mapping) else None), str(
            profile.get("catalog_revision") or snapshot.get("catalog_revision") or ""
        )

    def resolve_runtime_facts(
        self,
        *,
        venue_profile_id: str,
        instrument_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve trusted runtime facts at the composition-root seam.

        This method deliberately performs only public Testnet reads.  The
        Dashboard server calls this facade; it never imports a venue reader.
        """

        if str(venue_profile_id or "").strip().lower() != "hyperliquid.testnet":
            return None, None
        from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader

        market: dict[str, Any] | None = None
        account: dict[str, Any] | None = None
        try:
            reader = HyperliquidTestnetMarketReader()
            market = reader.read(instrument_id)
            # The canonical DCA/Grid builders require the same trusted
            # source for execution bars and their 1d/4h planning contexts.
            # Resolve these facts at the composition seam so Preview never
            # silently falls back to Paper or synthetic history.
            contexts: dict[str, Any] = {}
            for timeframe, limit in (("1m", 240), ("1d", 32), ("4h", 32)):
                bars = reader.read_bars(
                    instrument_id,
                    timeframe=timeframe,
                    limit=limit,
                    end=None,
                )
                contexts[timeframe] = {
                    "provider": bars.get("provider") or "hyperliquid",
                    "is_synthetic": bars.get("is_synthetic") is True,
                    "bars": list(bars.get("bars") or []),
                    "fresh": bars.get("fresh") is True,
                    "source": bars.get("source") or "hyperliquid.external_testnet",
                }
            market.update(
                {
                    "bars": contexts["1m"]["bars"],
                    "strategy_timeframes": contexts,
                    "latest_close": market.get("price") or market.get("mid"),
                    "latest_timestamp": market.get("observed_at"),
                    "timeframe": "1m",
                }
            )
        except Exception:  # noqa: BLE001 - preserve a typed blocker in preview.
            market = None
        reader = self.runtime_account_reader()
        if reader is not None:
            try:
                account = reader.read(instrument_id)
            except Exception:  # noqa: BLE001 - preserve a typed blocker in admission.
                account = None
        return market, account

    def resolve_market_bars(
        self,
        *,
        venue_profile_id: str,
        instrument_id: str,
        timeframe: str,
        limit: int,
        end: str | None,
    ) -> dict[str, Any]:
        """Resolve selected Testnet candles without a cross-venue fallback."""

        profile_id = str(venue_profile_id or "").strip().lower()
        instrument = str(instrument_id or "").strip()
        if profile_id != "hyperliquid.testnet":
            raise ValueError("dashboard_market_venue_not_supported")
        from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader

        return HyperliquidTestnetMarketReader().read_bars(
            instrument,
            timeframe=str(timeframe or ""),
            limit=int(limit),
            end=end,
        )

    @staticmethod
    def runtime_account_reader() -> object | None:
        """Build the configured public account reader without exposing secrets."""

        try:
            from services.hyperliquid_testnet_runtime import build_dashboard_account_reader

            return build_dashboard_account_reader()
        except Exception:  # noqa: BLE001 - invalid runtime identity stays unavailable.
            return None

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
        if instrument is not None:
            entry, _catalog_revision = self._catalog_entry(profile_id, instrument)
            if entry is None:
                raise ValueError("instrument_not_in_catalog")
            if str(entry.get("eligibility") or "unknown") != "eligible":
                raise ValueError("instrument_not_eligible")
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
        catalog_entry, catalog_revision = self._catalog_entry(profile_id, instrument)
        if catalog_entry is None:
            blockers.append("instrument_not_in_catalog")
        elif str(catalog_entry.get("eligibility") or "unknown") != "eligible":
            blockers.extend(
                str(item)
                for item in (catalog_entry.get("blockers") or [])
                if str(item).strip()
            )
            blockers.append("instrument_not_eligible")
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
                    output_root=self.output_root,
                )
            else:
                preview = build_grid_preview(
                    "dashboard-preview",
                    self._normalise_grid(normalized_strategy),
                    market=normalized_market,
                    account=source_account,
                    config=dict(config or self._default_strategy_config()),
                    allow_unsafe_manual_preview=False,
                    output_root=self.output_root,
                )
        except (TypeError, ValueError, KeyError) as exc:
            code = str(exc).strip() or "strategy_preview_invalid"
            blockers.append(code)

        if family == "grid" and preview is not None:
            grid_preview = preview.get("grid") if isinstance(preview.get("grid"), Mapping) else {}
            grid_preview = dict(grid_preview)
            range_value = normalized_strategy.get("range") if isinstance(normalized_strategy.get("range"), Mapping) else {}
            low = range_value.get("low")
            high = range_value.get("high")
            requested_hard_stop = normalized_strategy.get("hard_stop")
            if requested_hard_stop in (None, ""):
                direction = str(normalized_strategy.get("direction") or "neutral").lower()
                requested_hard_stop = (
                    low
                    if direction == "long"
                    else high
                    if direction == "short"
                    else {"long": low, "short": high}
                )
                hard_stop_source = "derived_from_grid_boundary"
            else:
                hard_stop_source = "operator_configured"
            grid_preview["hard_stop"] = requested_hard_stop
            grid_preview["hard_stop_source"] = hard_stop_source
            preview["grid"] = grid_preview

        hard_blockers = sorted(set(blockers))
        payload = {
            "schema_version": "dashboard-strategy-preview-v1",
            "venue_profile_id": profile_id,
            "instrument_id": instrument,
            "strategy_family": family,
            "catalog_revision": catalog_revision,
            "instrument_eligibility": (catalog_entry or {}).get("eligibility"),
            "requested": _public(normalized_strategy),
            "preview": _public(preview) if preview is not None else None,
            "market": _public(normalized_market),
            "account": self._account_summary(source_account),
            "blockers": hard_blockers,
            "execution_ready": not hard_blockers,
            "authorizing": False,
        }
        if profile_id == "hyperliquid.testnet":
            payload = self.execution_admission(
                payload,
                market=normalized_market if normalized_market else None,
                account=source_account if account is not None else None,
            )
            # Carry only the non-secret runtime identity needed by the later
            # confirmation seam.  The protected profile remains opt-in; no
            # identity value here authorizes an order or resolves a signer.
            try:
                from services.hyperliquid_testnet_runtime import (
                    TESTNET_PROFILE,
                    HyperliquidTestnetRuntimeConfig,
                )

                runtime_config = HyperliquidTestnetRuntimeConfig.from_environment()
            except Exception:  # noqa: BLE001 - leave activation identity blocked.
                runtime_config = None
                TESTNET_PROFILE = "hyperliquid-testnet-position-protection"
            if runtime_config is not None:
                payload.update(
                    {
                        "account_fingerprint": str(
                            source_account.get("account_fingerprint")
                            or runtime_config.account_fingerprint
                        ),
                        "runtime_id": runtime_config.runtime_id,
                        "release_sha": runtime_config.release_sha,
                        "capability_revision": runtime_config.capability_revision,
                        "transport_profile": (
                            TESTNET_PROFILE
                            if runtime_config.protected_profile_enabled
                            else "hyperliquid-testnet-default"
                        ),
                    }
                )
        payload["preview_digest"] = canonical_preview_digest(payload)
        self.persist_preview(payload)
        return payload

    def persist_preview(self, preview: Mapping[str, Any]) -> dict[str, Any]:
        rows = load_json(self.output_root / PREVIEW_PATH)
        digest = str(preview.get("preview_digest") or "")
        if rows and isinstance(rows[-1], Mapping) and str(rows[-1].get("preview_digest") or "") == digest:
            return dict(rows[-1])
        write_json(self.output_root / PREVIEW_PATH, [*rows, _public(preview)])
        return dict(preview)

    _persist_preview = persist_preview

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

    def execution_admission(
        self,
        preview: Mapping[str, Any],
        *,
        market: Mapping[str, Any] | None,
        account: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply execution-grade market checks and the subtractive risk gate."""

        if not isinstance(preview, Mapping):
            raise ValueError("dashboard_preview_invalid")
        result = dict(preview)
        blockers = [str(item) for item in (preview.get("blockers") or []) if str(item).strip()]
        try:
            requested_notional = self._requested_notional(preview)
        except ValueError:
            requested_notional = 0.0
            blockers.append("preview_notional_missing")
        market_blockers, normalized_market = self._market_quality_blockers(
            market,
            instrument_id=str(preview.get("instrument_id") or ""),
            requested_notional=requested_notional,
        )
        blockers.extend(market_blockers)
        body_account = dict(account or {})
        equity = self._float_or_none(body_account.get("equity"))
        if str(preview.get("venue_profile_id") or "").strip().lower() == "hyperliquid.testnet":
            if not body_account:
                blockers.append("testnet_account_unavailable")
            else:
                for field, blocker in (
                    ("account_fingerprint", "testnet_account_identity_missing"),
                    ("source_cursor", "testnet_account_cursor_missing"),
                ):
                    if body_account.get(field) in (None, ""):
                        blockers.append(blocker)
                if str(body_account.get("broker_id") or "").lower() != "hyperliquid":
                    blockers.append("testnet_account_broker_mismatch")
                if str(body_account.get("environment") or "").lower() != "testnet":
                    blockers.append("testnet_account_environment_mismatch")
                if body_account.get("fresh") is not True:
                    blockers.append("testnet_account_stale")
                if body_account.get("coherent") is not True:
                    blockers.append("testnet_account_incoherent")
        if body_account:
            capabilities = body_account.get("capabilities")
            if isinstance(capabilities, Mapping) and capabilities.get("protection") is not True:
                blockers.append("testnet_protection_capability_unavailable")
            positions = [
                row
                for row in (body_account.get("positions") or [])
                if isinstance(row, Mapping) and self._non_zero(row.get("signed_quantity"))
            ]
            open_orders = [row for row in (body_account.get("open_orders") or []) if isinstance(row, Mapping)]
            if positions or open_orders or body_account.get("unknown_exposure") is True:
                blockers.append("account_not_clean")
        requested_loss = self._requested_loss(preview)
        notional_cap = None
        loss_cap = None
        effective_notional = requested_notional
        effective_loss = requested_loss
        if equity is None or equity <= 0:
            blockers.append("account_equity_unavailable")
        else:
            notional_cap = min(equity * 0.10, 100.0)
            loss_cap = min(equity * 0.05, 50.0)
            effective_notional = min(requested_notional, notional_cap)
            if requested_loss > 0 and requested_loss > loss_cap:
                effective_notional = min(
                    effective_notional,
                    requested_notional * loss_cap / requested_loss,
                )
            effective_loss = (
                requested_loss * effective_notional / requested_notional
                if requested_notional > 0 and requested_loss > 0
                else requested_loss
            )
        risk_outcome = "allow"
        if blockers:
            risk_outcome = "reject"
        elif effective_notional < requested_notional - 1e-9:
            risk_outcome = "scale"
        gate = {
            "outcome": risk_outcome,
            "requested_notional": round(requested_notional, 8),
            "effective_notional": round(effective_notional, 8),
            "requested_max_loss": round(requested_loss, 8),
            "effective_max_loss": round(effective_loss, 8),
            "notional_cap": None if notional_cap is None else round(notional_cap, 8),
            "loss_cap": None if loss_cap is None else round(loss_cap, 8),
            "subtractive_only": True,
        }
        result.update(
            {
                "market": _public(normalized_market),
                "blockers": sorted(set(blockers)),
                "risk_gate": gate,
                "risk_gate_digest": canonical_risk_gate_digest(gate),
                "execution_ready": bool(preview.get("execution_ready"))
                and not blockers,
                "authorizing": False,
            }
        )
        result["preview_digest"] = canonical_preview_digest(result)
        return result

    def confirm_and_run(
        self,
        preview: Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        coordinator: object | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Create one digest-bound activation intent after explicit approval."""

        blockers: list[str] = [
            str(item)
            for item in (preview.get("blockers") if isinstance(preview, Mapping) else [])
            if str(item).strip()
        ]
        if not isinstance(preview, Mapping):
            blockers.append("dashboard_preview_invalid")
            preview = {}
        if not isinstance(confirmation, Mapping):
            blockers.append("confirmation_invalid")
            confirmation = {}
        secret_keys = self._forbidden_fields(preview) | self._forbidden_fields(confirmation)
        if secret_keys:
            blockers.append("secret_field_forbidden")
        preview_digest = str(preview.get("preview_digest") or "").strip().lower()
        if not preview_digest:
            blockers.append("preview_digest_missing")
        elif canonical_preview_digest(preview) != preview_digest:
            blockers.append("preview_digest_invalid")
        if str(confirmation.get("preview_digest") or "").strip().lower() != preview_digest:
            blockers.append("confirmation_digest_mismatch")
        if preview_digest:
            persisted = load_json(self.output_root / PREVIEW_PATH)
            latest = persisted[-1] if persisted and isinstance(persisted[-1], Mapping) else None
            if not isinstance(latest, Mapping) or str(latest.get("preview_digest") or "").strip().lower() != preview_digest:
                blockers.append("preview_not_durable")
        if confirmation.get("acknowledged") is not True:
            blockers.append("operator_confirmation_required")
        if str(confirmation.get("operator_id") or "").strip().lower() != "park":
            blockers.append("operator_identity_invalid")
        if preview.get("execution_ready") is not True:
            blockers.append("preview_not_execution_ready")
        profile_id = str(preview.get("venue_profile_id") or "").strip().lower()
        if profile_id != "hyperliquid.testnet":
            blockers.append("testnet_venue_required")
        if str(preview.get("environment") or "testnet").strip().lower() != "testnet":
            blockers.append("testnet_only")
        account = preview.get("account") if isinstance(preview.get("account"), Mapping) else {}
        for field, blocker in (
            ("account_fingerprint", "testnet_account_identity_missing"),
            ("source_cursor", "testnet_account_cursor_missing"),
        ):
            if account.get(field) in (None, ""):
                blockers.append(blocker)
        if str(account.get("broker_id") or "").lower() != "hyperliquid":
            blockers.append("testnet_account_broker_mismatch")
        if str(account.get("environment") or "").lower() != "testnet":
            blockers.append("testnet_account_environment_mismatch")
        if account.get("fresh") is not True:
            blockers.append("testnet_account_stale")
        if account.get("coherent") is not True:
            blockers.append("testnet_account_incoherent")
        capabilities = account.get("capabilities") if isinstance(account.get("capabilities"), Mapping) else {}
        if capabilities.get("protection") is not True:
            blockers.append("testnet_protection_capability_unavailable")
        account_fingerprint = str(
            preview.get("account_fingerprint") or account.get("account_fingerprint") or ""
        ).strip().lower()
        instrument_id = str(preview.get("instrument_id") or "").strip()
        catalog_entry, catalog_revision = self._catalog_entry(profile_id, instrument_id)
        if catalog_entry is None:
            blockers.append("instrument_not_in_catalog")
        else:
            if str(catalog_entry.get("eligibility") or "unknown") != "eligible":
                blockers.append("instrument_not_eligible")
            if str(preview.get("catalog_revision") or "") != catalog_revision:
                blockers.append("catalog_revision_mismatch")
            if catalog_entry.get("market_fresh") is not True:
                blockers.append("market_freshness_unavailable")
        runtime_id = str(preview.get("runtime_id") or "").strip()
        release_sha = str(preview.get("release_sha") or "").strip().lower()
        capability_revision = str(preview.get("capability_revision") or "").strip()
        transport_profile = str(
            preview.get("transport_profile") or "hyperliquid-testnet-position-protection"
        ).strip()
        if not account_fingerprint or not runtime_id or not release_sha or not capability_revision:
            blockers.append("activation_identity_incomplete")
        if len(release_sha) != 40:
            blockers.append("release_sha_invalid")
        risk_gate = preview.get("risk_gate") if isinstance(preview.get("risk_gate"), Mapping) else {}
        requested_notional = risk_gate.get("requested_notional")
        effective_notional = risk_gate.get("effective_notional")
        requested_max_loss = risk_gate.get("requested_max_loss")
        effective_max_loss = risk_gate.get("effective_max_loss")
        if requested_notional in (None, "") or effective_notional in (None, ""):
            blockers.append("effective_plan_missing")
        else:
            requested_number = self._float_or_none(requested_notional)
            effective_number = self._float_or_none(effective_notional)
            if requested_number is None or effective_number is None:
                blockers.append("effective_plan_invalid")
            elif effective_number > requested_number:
                blockers.append("effective_plan_increased")
        risk_gate_digest = canonical_risk_gate_digest(risk_gate) if risk_gate else ""
        supplied_risk_gate_digest = str(preview.get("risk_gate_digest") or "").strip().lower()
        if risk_gate and supplied_risk_gate_digest != risk_gate_digest:
            blockers.append("risk_gate_digest_mismatch")
        if risk_gate and not supplied_risk_gate_digest:
            blockers.append("risk_gate_digest_missing")
        if blockers:
            return self._blocked_confirmation(preview_digest, sorted(set(blockers)))

        rows = load_json(self.output_root / CONFIRMATION_PATH)
        previous = rows[-1] if rows and isinstance(rows[-1], Mapping) else None
        if previous and previous.get("status") == "confirmed":
            if str(previous.get("preview_digest") or "") == preview_digest:
                return dict(previous)
            return self._blocked_confirmation(preview_digest, ["active_plan_conflict"])
        strategy_family = str(preview.get("strategy_family") or "").strip().lower()
        suffix = preview_digest.replace("sha256:", "")[:16]
        activation = {
            "strategy_family": strategy_family,
            "strategy_session_id": f"dashboard-session:{suffix}",
            "strategy_revision_id": f"dashboard-revision:{suffix}",
            "plan_digest": preview_digest,
            "account_fingerprint": account_fingerprint,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": transport_profile,
            "instrument_id": instrument_id,
            "runtime_id": runtime_id,
            "release_sha": release_sha,
            "capability_revision": capability_revision,
            "requested_notional": None if requested_notional in (None, "") else str(requested_notional),
            "effective_notional": None if effective_notional in (None, "") else str(effective_notional),
            "requested_max_loss": None if requested_max_loss in (None, "") else str(requested_max_loss),
            "effective_max_loss": None if effective_max_loss in (None, "") else str(effective_max_loss),
            "risk_gate_digest": risk_gate_digest or None,
        }
        activation_id = _digest(activation)
        occurred_at = str(now or self.clock())
        result = {
            "schema_version": "dashboard-confirmation-v1",
            "status": "confirmed",
            "event": "operator_confirmed",
            "confirmation_id": f"dashboard-confirmation:{suffix}",
            "confirmation_digest": _digest({"preview_digest": preview_digest, "operator_id": "park"}),
            "preview_digest": preview_digest,
            "acknowledged": True,
            "operator_id": "park",
            "confirmed_at": occurred_at,
            "activation_id": activation_id,
            **activation,
            "execution_mutation": False,
            "network_operation_invoked": False,
            "secret_material_present": False,
            "coordinator_invoked": False,
            "coordinator_result": None,
            "blockers": [],
            "next_action": "await_coordinator_execution",
        }
        if coordinator is not None:
            activate = getattr(coordinator, "activate", None)
            if not callable(activate):
                return self._blocked_confirmation(preview_digest, ["coordinator_activation_unavailable"])
            try:
                coordinator_result = activate(
                    activation,
                    command_id=result["confirmation_id"],
                    now=occurred_at,
                )
            except Exception as exc:  # noqa: BLE001 - activation is fail-closed.
                code = str(getattr(exc, "code", "")).strip() or "coordinator_activation_blocked"
                return self._blocked_confirmation(preview_digest, [code])
            result["coordinator_invoked"] = True
            result["coordinator_result"] = _public(coordinator_result)
            result["next_action"] = str(
                coordinator_result.get("next_action")
                if isinstance(coordinator_result, Mapping)
                else "await_coordinator_execution"
            )
        write_json(self.output_root / CONFIRMATION_PATH, [*rows, result])
        return result

    @staticmethod
    def _forbidden_fields(value: Any) -> set[str]:
        forbidden = {
            "private_key",
            "secret",
            "api_key",
            "api_secret",
            "signer",
            "signature",
            "signed_payload",
            "authorization",
        }
        found: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized in forbidden:
                    found.add(normalized)
                found.update(DashboardControlPlane._forbidden_fields(item))
        elif isinstance(value, (list, tuple)):
            for item in value:
                found.update(DashboardControlPlane._forbidden_fields(item))
        return found

    @staticmethod
    def _blocked_confirmation(preview_digest: str, blockers: list[str]) -> dict[str, Any]:
        return {
            "schema_version": "dashboard-confirmation-v1",
            "status": "blocked",
            "event": "operator_confirmation_blocked",
            "confirmation_id": None,
            "confirmation_digest": None,
            "preview_digest": preview_digest or None,
            "execution_mutation": False,
            "network_operation_invoked": False,
            "secret_material_present": False,
            "coordinator_invoked": False,
            "coordinator_result": None,
            "blockers": sorted(set(blockers)),
            "next_action": "notify_park_and_wait",
        }

    def control(
        self,
        action: str,
        *,
        coordinator: object | None,
        reason: str = "",
        now: str | None = None,
    ) -> dict[str, Any]:
        """Issue one explicit operator intent through the Coordinator."""

        normalized = str(action or "").strip().lower()
        if normalized not in {"pause", "stop", "flatten", "interrupt", "resume"}:
            return {
                "schema_version": "dashboard-runtime-control-v1",
                "status": "blocked",
                "action": normalized,
                "blockers": ["control_action_invalid"],
                "execution_mutation": False,
                "next_action": "notify_park_and_wait",
            }
        command = getattr(coordinator, "command", None)
        if not callable(command):
            return {
                "schema_version": "dashboard-runtime-control-v1",
                "status": "blocked",
                "action": normalized,
                "blockers": ["coordinator_control_unavailable"],
                "execution_mutation": False,
                "next_action": "notify_park_and_wait",
            }
        command_id = f"dashboard-control:{normalized}:{_digest({'reason': reason, 'now': now or self.clock()})[7:19]}"
        try:
            response = command(
                normalized,
                {"reason": str(reason or "")},
                command_id=command_id,
                now=now or self.clock(),
            )
        except Exception as exc:  # noqa: BLE001 - control outcomes fail closed.
            return {
                "schema_version": "dashboard-runtime-control-v1",
                "status": "blocked",
                "action": normalized,
                "blockers": [str(getattr(exc, "code", "")).strip() or "coordinator_control_blocked"],
                "execution_mutation": False,
                "next_action": "reconcile_identity_bound",
            }
        result = {
            "schema_version": "dashboard-runtime-control-v1",
            "status": str(response.get("status") or f"{normalized}_requested") if isinstance(response, Mapping) else f"{normalized}_requested",
            "action": normalized,
            "reason": str(reason or "") or None,
            "coordinator_result": _public(response),
            "execution_mutation": False,
            "next_action": (
                str(response.get("next_action") or "await_reconcile")
                if isinstance(response, Mapping)
                else "await_reconcile"
            ),
            "blockers": [],
        }
        if normalized in {"stop", "flatten"}:
            result["next_action"] = "await_cancel_and_flat_reconcile"
        return result

    def record_notification(
        self,
        *,
        event: str,
        strategy_family: str,
        instrument_id: str,
        plan_digest: str,
        message: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist one terminal/operator notification without creating a plan."""

        normalized_event = str(event or "").strip().lower()
        if normalized_event not in {"take_profit", "stop_loss", "interrupt", "unknown", "broker_outage"}:
            raise ValueError("notification_event_invalid")
        digest = str(plan_digest or "").strip().lower()
        if not digest:
            raise ValueError("notification_plan_digest_required")
        suffix = _digest({"event": normalized_event, "plan_digest": digest})[7:19]
        status = "AWAITING_OPERATOR" if normalized_event in {"take_profit", "stop_loss"} else "PAUSED"
        if normalized_event == "unknown":
            status = "BLOCKED"
        row = {
            "schema_version": "dashboard-notification-v1",
            "notification_id": f"dashboard-notification:{suffix}",
            "event": normalized_event,
            "strategy_family": str(strategy_family or "").strip().lower(),
            "instrument_id": str(instrument_id or "").strip(),
            "plan_digest": digest,
            "message": str(message or "").strip(),
            "status": status,
            "automatic_next_plan": False,
            "retry_authorized": False,
            "channels": {"dashboard": "persisted", "telegram": "queued"},
            "created_at": str(now or self.clock()),
            "next_action": "await_operator_next_plan" if status == "AWAITING_OPERATOR" else "notify_park_and_wait",
        }
        rows = load_json(self.output_root / NOTIFICATION_PATH)
        if rows and isinstance(rows[-1], Mapping) and rows[-1].get("notification_id") == row["notification_id"]:
            return dict(rows[-1])
        write_json(self.output_root / NOTIFICATION_PATH, [*rows, row])
        telegram_path = self.output_root / "dashboard_control_plane" / "telegram_outbox.json"
        telegram_rows = load_json(telegram_path)
        write_json(
            telegram_path,
            [
                *telegram_rows,
                {
                    "schema_version": "dashboard-telegram-outbox-v1",
                    "notification_id": row["notification_id"],
                    "status": "queued",
                    "message": row["message"],
                    "plan_digest": row["plan_digest"],
                    "instrument_id": row["instrument_id"],
                    "created_at": row["created_at"],
                    "network_sent": False,
                    "credentials_exposed": False,
                },
            ],
        )
        return row

    def handle_unknown(
        self,
        *,
        operation: str,
        plan_digest: str,
        instrument_id: str,
        coordinator: object | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Record unknown side effects and require identity-bound reconcile."""

        del coordinator
        notification = self.record_notification(
            event="unknown",
            strategy_family="",
            instrument_id=instrument_id,
            plan_digest=plan_digest,
            message=f"Unknown outcome for {str(operation or 'operation').strip() or 'operation'}; reconcile before any retry.",
            now=now,
        )
        return {
            "schema_version": "dashboard-unknown-outcome-v1",
            "status": "blocked",
            "operation": str(operation or "").strip(),
            "plan_digest": str(plan_digest or "").strip().lower(),
            "instrument_id": str(instrument_id or "").strip(),
            "reconcile_required": True,
            "retry_authorized": False,
            "automatic_retry": False,
            "notification_id": notification["notification_id"],
            "next_action": "reconcile_identity_bound",
            "execution_mutation": False,
            "blockers": ["unknown_side_effect"],
        }

    def runtime_status(self, *, coordinator: object | None) -> dict[str, Any]:
        """Project Coordinator state and the latest durable notification."""

        status_method = getattr(coordinator, "status", None)
        runtime = status_method() if callable(status_method) else {
            "status": "blocked",
            "next_action": "notify_park_and_wait",
        }
        runtime_payload = dict(runtime) if isinstance(runtime, Mapping) else {"status": "blocked"}
        runtime = {**runtime_payload, "execution_mutation": False}
        execution = runtime.get("execution") or runtime.get("execution_slice")
        if isinstance(execution, Mapping):
            for field in (
                "orders",
                "open_orders",
                "fills",
                "fees",
                "positions",
                "protection",
                "reconciliation",
            ):
                if field in execution:
                    runtime.setdefault(field, _public(execution[field]))
        rows = load_json(self.output_root / NOTIFICATION_PATH)
        latest = dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else None
        return {
            "schema_version": "dashboard-runtime-status-v1",
            "runtime": _public(runtime),
            "notification": latest,
            "execution_mutation": False,
            "safety": {
                "credentials_exposed": False,
                "orders_submitted": False,
                "browser_is_scheduler": False,
            },
        }

    def _market_quality_blockers(
        self,
        market: Mapping[str, Any] | None,
        *,
        instrument_id: str,
        requested_notional: float,
    ) -> tuple[list[str], dict[str, Any]]:
        if not isinstance(market, Mapping) or not market:
            return ["market_facts_required"], {}
        source = dict(market)
        blockers: list[str] = []
        required = (
            "source",
            "cursor",
            "broker_id",
            "environment",
            "instrument_id",
            "asset_index",
            "mapping_revision",
            "universe_revision",
            "connection_epoch",
            "observed_at",
        )
        blockers.extend(f"market_{field}_missing" for field in required if source.get(field) in (None, ""))
        if source.get("fresh") is not True:
            blockers.append("market_stale")
        if source.get("execution_ready") is not True:
            blockers.append("market_not_execution_ready")
        if str(source.get("broker_id") or "").lower() != "hyperliquid":
            blockers.append("market_broker_mismatch")
        if str(source.get("environment") or "").lower() != "testnet":
            blockers.append("market_environment_mismatch")
        if instrument_id and str(source.get("instrument_id") or "") != instrument_id:
            blockers.append("market_instrument_mismatch")
        numbers: dict[str, float] = {}
        for field in (
            "bid",
            "ask",
            "mid",
            "mark",
            "oracle",
            "impact",
            "depth_notional",
            "max_slippage",
            "max_oracle_deviation_bps",
        ):
            value = self._float_or_none(source.get(field))
            if value is None:
                blockers.append(f"market_{field}_invalid")
            else:
                numbers[field] = value
        if len(numbers) == 9:
            if not numbers["bid"] < numbers["ask"]:
                blockers.append("bbo_not_two_sided")
            if not numbers["bid"] <= numbers["mid"] <= numbers["ask"]:
                blockers.append("mid_outside_bbo")
            if numbers["depth_notional"] < requested_notional:
                blockers.append("insufficient_depth")
            if numbers["max_slippage"] <= 0 or abs(numbers["impact"] - numbers["mid"]) > numbers["max_slippage"]:
                blockers.append("impact_slippage_exceeded")
            if numbers["oracle"] <= 0 or numbers["mark"] <= 0 or numbers["max_oracle_deviation_bps"] < 0:
                blockers.append("oracle_facts_invalid")
            elif (
                abs(numbers["mark"] - numbers["oracle"])
                / numbers["oracle"]
                * 10_000
                > numbers["max_oracle_deviation_bps"]
            ):
                blockers.append("oracle_dislocation")
        return sorted(set(blockers)), source

    @staticmethod
    def _requested_notional(preview: Mapping[str, Any]) -> float:
        body = preview.get("preview") if isinstance(preview.get("preview"), Mapping) else {}
        dca = body.get("dca") if isinstance(body.get("dca"), Mapping) else {}
        grid = body.get("grid") if isinstance(body.get("grid"), Mapping) else {}
        risk = body.get("risk") if isinstance(body.get("risk"), Mapping) else {}
        candidates = (
            dca.get("total_possible_notional"),
            risk.get("max_side_notional"),
            grid.get("notional_per_grid"),
            preview.get("requested_notional"),
        )
        for value in candidates:
            rendered = DashboardControlPlane._float_or_none(value)
            if rendered is not None and rendered >= 0:
                if value == grid.get("notional_per_grid") and grid.get("count"):
                    return rendered * max(1, int(grid.get("count")))
                return rendered
        raise ValueError("preview_notional_missing")

    @staticmethod
    def _requested_loss(preview: Mapping[str, Any]) -> float:
        body = preview.get("preview") if isinstance(preview.get("preview"), Mapping) else {}
        risk = body.get("risk") if isinstance(body.get("risk"), Mapping) else {}
        for key in ("maximum_loss_at_full_depth", "max_loss", "maximum_loss"):
            rendered = DashboardControlPlane._float_or_none(risk.get(key))
            if rendered is not None and rendered >= 0:
                return rendered
        return 0.0

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            rendered = float(value)
        except (TypeError, ValueError):
            return None
        return rendered if rendered == rendered and rendered not in {float("inf"), float("-inf")} else None

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
