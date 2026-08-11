"""Bounded-memory hashes for files that may grow with runtime history."""

from __future__ import annotations

import hashlib
from pathlib import Path


DEFAULT_HASH_CHUNK_BYTES = 1024 * 1024


def sha256_file(
    path: Path,
    *,
    chunk_bytes: int = DEFAULT_HASH_CHUNK_BYTES,
) -> str:
    """Return a SHA-256 digest while retaining only one fixed-size buffer."""
    if chunk_bytes <= 0:
        raise ValueError("hash_chunk_bytes_must_be_positive")
    digest = hashlib.sha256()
    buffer = bytearray(chunk_bytes)
    view = memoryview(buffer)
    with Path(path).open("rb", buffering=0) as handle:
        while read := handle.readinto(buffer):
            digest.update(view[:read])
    return digest.hexdigest()
