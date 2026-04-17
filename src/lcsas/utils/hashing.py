"""Cryptographic hashing utilities."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)

# Buffer size for streaming file hashes (64 KB)
_HASH_BUFFER_SIZE = 65_536

# Log progress every 1 GB
_PROGRESS_INTERVAL = 1_073_741_824


def sha256_file(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a file using streaming reads.

    Suitable for large files — reads in 64 KB chunks.  For files larger
    than 1 GB, progress is logged at 1 GB intervals.
    """
    h = hashlib.sha256()
    total = 0
    next_log = _PROGRESS_INTERVAL
    try:
        with open(path, "rb") as f:
            while True:
                try:
                    chunk = f.read(_HASH_BUFFER_SIZE)
                except OSError as exc:
                    raise OSError(
                        f"I/O error reading {path} at byte offset {total}: {exc}"
                    ) from exc
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
                if total >= next_log:
                    _logger.info(
                        "Hashing %s: %.1f GB processed...",
                        Path(path).name, total / 1e9,
                    )
                    next_log += _PROGRESS_INTERVAL
        return h.hexdigest()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}") from None


def sha256_bytes(data: bytes) -> str:
    """Compute the SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()
