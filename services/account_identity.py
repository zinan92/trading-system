"""Canonical, credential-free account identity for public read models."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ACCOUNT_FINGERPRINT_SCHEME = "account-address-raw-v1"
LEGACY_ACCOUNT_FINGERPRINT_SCHEME = "account-address-json-v0"
_ACCOUNT_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


def canonical_account_address(address: Any) -> str:
    """Return the lowercase canonical form used by the account identity."""

    value = str(address or "").strip().lower()
    if _ACCOUNT_ADDRESS.fullmatch(value) is None:
        raise ValueError("testnet_account_address_invalid")
    return value


def account_fingerprint(address: Any) -> str:
    """Hash the account address exactly as supplied by the Broker contract."""

    value = str(address or "")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def legacy_account_fingerprint(address: Any) -> str:
    """Return the pre-1173 fingerprint for read-only migration diagnostics."""

    normalized = canonical_account_address(address)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ACCOUNT_FINGERPRINT_SCHEME",
    "LEGACY_ACCOUNT_FINGERPRINT_SCHEME",
    "account_fingerprint",
    "canonical_account_address",
    "legacy_account_fingerprint",
]
