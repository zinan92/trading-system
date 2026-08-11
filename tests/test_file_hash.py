from __future__ import annotations

import hashlib
import tracemalloc
from pathlib import Path

import pytest

from services.file_hash import DEFAULT_HASH_CHUNK_BYTES, sha256_file


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"gridmind",
        (b"bounded-hash" * (DEFAULT_HASH_CHUNK_BYTES // 12 + 1)) + b"tail",
    ],
)
def test_streaming_hash_matches_sha256_for_all_file_shapes(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(payload)

    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_streaming_hash_peak_allocation_is_bounded_for_large_sparse_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large-artifact.bin"
    with path.open("wb") as handle:
        handle.seek(64 * 1024 * 1024 - 1)
        handle.write(b"\0")

    tracemalloc.start()
    try:
        sha256_file(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * DEFAULT_HASH_CHUNK_BYTES


def test_streaming_hash_rejects_nonpositive_chunk_size(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="hash_chunk_bytes_must_be_positive"):
        sha256_file(path, chunk_bytes=0)
