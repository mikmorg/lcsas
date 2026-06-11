"""Device read-back hashing for post-burn verification (BURN-04).

VERIFIED is the catalog's strongest durability claim — deprecation
safety trusts it when deciding whether the last replica of a pack may
be retired.  ``xorriso -check_media`` alone is a readability smoke
test: any readable disc sitting in the drive passes it.  This module
provides the evidence step: read back exactly the mastered image's
byte length from the device and compare its SHA-256 against the hash
recorded at stage time.

Reading exactly the ISO's byte length works because RS03-augmented
images are sector-aligned and xorriso burns the image byte-exact from
sector 0; drive padding beyond the image length is excluded by
construction.
"""

from __future__ import annotations

import hashlib
import logging

_logger = logging.getLogger(__name__)

_PROGRESS_EVERY = 1024 ** 3  # log read-back progress every 1 GiB


def read_device_sha256(
    device: str,
    length_bytes: int,
    chunk: int = 4 * 1024 * 1024,
) -> str:
    """Read exactly *length_bytes* from a block device; return hex SHA-256.

    Pure stdlib: ``open(device, 'rb')`` + ``hashlib``.  Raises
    ``OSError`` (carrying the device and offset) on read errors and on
    short reads — a disc shorter than the recorded image IS a verify
    failure, never a silent truncation.
    """
    if length_bytes <= 0:
        raise ValueError(
            f"length_bytes must be positive, got {length_bytes}"
        )
    _logger.info(
        "Reading back %.2f GB from %s for hash verification — "
        "this takes a while at optical read speeds...",
        length_bytes / 1e9, device,
    )
    digest = hashlib.sha256()
    offset = 0
    next_progress = _PROGRESS_EVERY
    with open(device, "rb") as fh:
        while offset < length_bytes:
            data = fh.read(min(chunk, length_bytes - offset))
            if not data:
                raise OSError(
                    f"Short read from {device}: got {offset:,} of "
                    f"{length_bytes:,} bytes (device ended early)"
                )
            digest.update(data)
            offset += len(data)
            if offset >= next_progress:
                _logger.info(
                    "  read-back progress: %.1f / %.1f GiB",
                    offset / _PROGRESS_EVERY,
                    length_bytes / _PROGRESS_EVERY,
                )
                next_progress += _PROGRESS_EVERY
    return digest.hexdigest()
