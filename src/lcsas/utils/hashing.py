"""Cryptographic hashing utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path

# Buffer size for streaming file hashes (64 KB)
_HASH_BUFFER_SIZE = 65_536


def sha256_file(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a file using streaming reads.

    Suitable for large files — reads in 64 KB chunks.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_BUFFER_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Compute the SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()
