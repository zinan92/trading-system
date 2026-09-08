"""Credential-free Hyperliquid Testnet account facts for admission/read models."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

from services.account_identity import ACCOUNT_FINGERPRINT_SCHEME, account_fingerprint


HYPERLIQUID_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
_ACCOUNT_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
ACCOUNT_FACT_MAX_AGE_SECONDS = 120.0


class HyperliquidTestnetAccountError(ValueError):
    """Stable, redacted public-account read blocker."""


class HyperliquidTestnetAccountReader:
    """Read public account/position/order/fill facts for one Testnet address."""

    def __init__(
        self,
        account_address: str,
        *,
        opener: Callable[..., Any] = urlopen,
        endpoint: str = HYPERLIQUID_TESTNET_INFO_URL,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        address = str(account_address or "").strip()
        if _ACCOUNT_ADDRESS.fullmatch(address) is None:
            raise HyperliquidTestnetAccountError("testnet_account_address_invalid")
        self.account_address = address
        self.opener = opener
        self.endpoint = str(endpoint)
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.clock = clock

    def read(self, instrument_id: str | None = None) -> dict[str, Any]:
        read_started = float(self.clock())
        state = self._post("clearinghouseState")
        open_orders = self._post("openOrders")
        frontend_orders = self._post("frontendOpenOrders")
        fills = self._post("userFills")
        if not isinstance(state, Mapping):
            raise HyperliquidTestnetAccountError("testnet_account_payload_invalid")
        positions = self._positions(state.get("assetPositions"))
        normalized_open_orders = self._orders(open_orders)
        normalized_frontend_orders = self._orders(frontend_orders)
        normalized_fills = self._fills(fills)
        selected = str(instrument_id or "").strip()
        selected_positions = (
            [row for row in positions if row["instrument_id"] == selected]
            if selected
            else list(positions)
        )
        selected_open_orders = (
            [row for row in normalized_open_orders if row["instrument_id"] == selected]
            if selected
            else list(normalized_open_orders)
        )
        read_finished = float(self.clock())
        observed_at = datetime.fromtimestamp(read_finished, tz=timezone.utc).isoformat()
        fact_timestamp = self._fact_timestamp(state, fallback=read_finished)
        fact_age_seconds = max(0.0, read_finished - fact_timestamp)
        freshness = fact_age_seconds <= ACCOUNT_FACT_MAX_AGE_SECONDS
        coherence_issues = self._coherence_issues(
            state,
            normalized_open_orders,
            normalized_frontend_orders,
            positions,
        )
        raw = {
            "clearinghouseState": state,
            "openOrders": open_orders,
            "frontendOpenOrders": frontend_orders,
            "userFills": fills,
        }
        return {
            "schema_version": "hyperliquid-testnet-account-facts-v1",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "account_fingerprint": account_fingerprint(self.account_address),
            "fingerprint_scheme": ACCOUNT_FINGERPRINT_SCHEME,
            "equity": self._equity(state),
            "positions": positions,
            "open_orders": normalized_open_orders,
            "frontend_open_orders": normalized_frontend_orders,
            "fills": normalized_fills,
            "selected_instrument_id": selected or None,
            "selected_positions": selected_positions,
            "selected_open_orders": selected_open_orders,
            "fees": [
                {"instrument_id": row["instrument_id"], "fee": row["fee"], "oid": row.get("oid")}
                for row in normalized_fills
                if row.get("fee") not in (None, "")
            ],
            "fresh": freshness,
            "coherent": not coherence_issues,
            "fact_age_seconds": round(fact_age_seconds, 3),
            "fact_max_age_seconds": ACCOUNT_FACT_MAX_AGE_SECONDS,
            "coherence_issues": coherence_issues,
            "read_started_at": datetime.fromtimestamp(
                read_started, tz=timezone.utc
            ).isoformat(),
            "observed_at": observed_at,
            "source": "hyperliquid.external_testnet",
            "source_cursor": self._fingerprint(raw),
            "capabilities": {
                "account_read": True,
                "positions_read": True,
                "open_orders_read": True,
                "fills_read": True,
                "fees_read": True,
                "protection": False,
            },
            "account_address_exposed": False,
        }

    @staticmethod
    def _fact_timestamp(state: Mapping[str, Any], *, fallback: float) -> float:
        value: Any = None
        for key in ("timestamp", "time", "updatedAt", "lastUpdate"):
            if state.get(key) not in (None, ""):
                value = state.get(key)
                break
        if value is None:
            return fallback
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return fallback
            if parsed.tzinfo is None:
                return fallback
            return parsed.astimezone(timezone.utc).timestamp()
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return numeric if numeric == numeric and numeric > 0 else fallback

    @staticmethod
    def _coherence_issues(
        state: Mapping[str, Any],
        open_orders: list[dict[str, Any]],
        frontend_orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> list[str]:
        issues: list[str] = []
        open_keys = {
            str(row.get("oid") or row.get("cloid") or "")
            for row in open_orders
        }
        frontend_keys = {
            str(row.get("oid") or row.get("cloid") or "")
            for row in frontend_orders
        }
        if open_keys != frontend_keys:
            issues.append("open_order_views_mismatch")
        position_keys = [str(row.get("instrument_id") or "") for row in positions]
        if len(position_keys) != len(set(position_keys)):
            issues.append("duplicate_position_identity")
        summary = state.get("marginSummary") or state.get("crossMarginSummary")
        if not isinstance(summary, Mapping):
            issues.append("margin_summary_missing")
        else:
            try:
                account_value = float(summary.get("accountValue"))
                margin_used = float(summary.get("totalMarginUsed", 0.0))
                if account_value < 0 or margin_used < 0:
                    issues.append("margin_summary_negative")
            except (TypeError, ValueError):
                issues.append("margin_summary_invalid")
        return sorted(set(issues))

    def _post(self, request_type: str) -> Mapping[str, Any] | list[Any]:
        request = Request(
            self.endpoint,
            data=json.dumps(
                {"type": request_type, "user": self.account_address},
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - redact public transport details.
            raise HyperliquidTestnetAccountError("testnet_account_unavailable") from exc
        if not isinstance(payload, (Mapping, list)):
            raise HyperliquidTestnetAccountError("testnet_account_payload_invalid")
        return payload

    @classmethod
    def _equity(cls, state: Mapping[str, Any]) -> float | None:
        summaries = [state.get("marginSummary"), state.get("crossMarginSummary")]
        for summary in summaries:
            if isinstance(summary, Mapping):
                for key in ("accountValue", "totalRawUsd", "totalMarginUsed"):
                    value = summary.get(key)
                    try:
                        rendered = float(value)
                    except (TypeError, ValueError):
                        continue
                    if rendered >= 0:
                        return rendered
        return None

    @staticmethod
    def _coin_instrument(coin: Any) -> str:
        symbol = str(coin or "").strip()
        return f"{symbol}-USD-PERP" if symbol else ""

    @classmethod
    def _positions(cls, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise HyperliquidTestnetAccountError("testnet_account_positions_invalid")
        result: list[dict[str, Any]] = []
        for row in rows:
            position = row.get("position") if isinstance(row, Mapping) else None
            if not isinstance(position, Mapping):
                raise HyperliquidTestnetAccountError("testnet_account_position_invalid")
            instrument_id = cls._coin_instrument(position.get("coin"))
            if not instrument_id:
                raise HyperliquidTestnetAccountError("testnet_account_position_identity_missing")
            result.append(
                {
                    "instrument_id": instrument_id,
                    "signed_quantity": str(position.get("szi") or "0"),
                    "entry_price": position.get("entryPx"),
                    "raw_position": {
                        key: value
                        for key, value in position.items()
                        if str(key).lower() not in {"secret", "signature", "private_key"}
                    },
                }
            )
        return result

    @classmethod
    def _orders(cls, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise HyperliquidTestnetAccountError("testnet_account_orders_invalid")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise HyperliquidTestnetAccountError("testnet_account_order_invalid")
            instrument_id = cls._coin_instrument(row.get("coin"))
            if not instrument_id:
                raise HyperliquidTestnetAccountError("testnet_account_order_identity_missing")
            result.append(
                {
                    "instrument_id": instrument_id,
                    "oid": row.get("oid"),
                    "cloid": row.get("cloid"),
                    "side": row.get("side"),
                    "size": row.get("sz"),
                    "price": row.get("limitPx"),
                    "status": row.get("status"),
                }
            )
        return result

    @classmethod
    def _fills(cls, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise HyperliquidTestnetAccountError("testnet_account_fills_invalid")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise HyperliquidTestnetAccountError("testnet_account_fill_invalid")
            instrument_id = cls._coin_instrument(row.get("coin"))
            if not instrument_id:
                raise HyperliquidTestnetAccountError("testnet_account_fill_identity_missing")
            result.append(
                {
                    "instrument_id": instrument_id,
                    "oid": row.get("oid"),
                    "cloid": row.get("cloid"),
                    "side": row.get("side"),
                    "price": row.get("px"),
                    "size": row.get("sz"),
                    "fee": row.get("fee"),
                    "time": row.get("time"),
                }
            )
        return result

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "HYPERLIQUID_TESTNET_INFO_URL",
    "HyperliquidTestnetAccountError",
    "HyperliquidTestnetAccountReader",
]
