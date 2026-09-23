"""Small, non-secret-returning source checks for Paper artifacts."""

import re

_PATTERNS = (
    ("hex_private_key", re.compile(r"0x[a-fA-F0-9]{64}")),
    ("private_key_assignment", re.compile(r"(?i)private[_-]?key\s*[:=]\s*['\"]0x")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
)


def find_secret_like_literals(text: str) -> tuple[str, ...]:
    """Return redacted pattern names, never matched secret contents."""

    return tuple(name for name, pattern in _PATTERNS if pattern.search(text))
