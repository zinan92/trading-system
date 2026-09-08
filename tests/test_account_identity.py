from __future__ import annotations

import hashlib

from services.account_identity import ACCOUNT_FINGERPRINT_SCHEME, account_fingerprint


ACCOUNT_ADDRESS = "0x7b9d494f79217246b0C80576c26ceDdf2c315f04"


def test_account_fingerprint_matches_standard_broker_raw_address_contract() -> None:
    from standard_broker.external_canary import account_fingerprint as broker_account_fingerprint

    expected = "sha256:" + hashlib.sha256(ACCOUNT_ADDRESS.encode("utf-8")).hexdigest()

    assert account_fingerprint(ACCOUNT_ADDRESS) == expected
    assert account_fingerprint(ACCOUNT_ADDRESS) == broker_account_fingerprint(ACCOUNT_ADDRESS)
    assert expected == "sha256:75b327ae65b27db8eb8955fd922a291945da160ba52e9f64995ed9e430df0c56"
    assert ACCOUNT_FINGERPRINT_SCHEME == "account-address-raw-v1"


def test_account_fingerprint_preserves_address_case() -> None:
    address = "0x7b9d494f79217246b0C80576c26ceDdf2c315f04"

    assert account_fingerprint(address) != account_fingerprint(address.lower())
