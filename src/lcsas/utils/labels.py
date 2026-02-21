"""Volume label, UUID generation, and input sanitization utilities."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

_MAX_NAME_LENGTH = 128
_UNSAFE_PATTERN = re.compile(r'[/\\]|\.\.|[\x00]')


def sanitize_name(value: str, field: str = "name") -> str:
    """Sanitize a user-provided name for use in filenames or DB fields.

    Rejects:
      - Null bytes
      - Path separators (``/``, ``\\``)
      - Parent-directory traversals (``..``)
      - Empty strings
      - Strings exceeding 128 characters

    Args:
        value: The raw user input.
        field: Field name for error messages (e.g. ``"location"``).

    Returns:
        The stripped, validated string.

    Raises:
        ValueError: If the value is invalid.
    """
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty.")
    if len(value) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"{field} exceeds maximum length of {_MAX_NAME_LENGTH} characters."
        )
    if _UNSAFE_PATTERN.search(value):
        raise ValueError(
            f"{field} contains unsafe characters "
            f"(path separators, '..', or null bytes): {value!r}"
        )
    return value


def generate_volume_label(
    prefix: str = "LCSAS",
    media_type: str = "BD",
    seq_num: int = 1,
) -> str:
    """Generate a human-readable volume label.

    Format: {PREFIX}_{MEDIA}_{YYYY}_{SEQ:04d}
    Example: LCSAS_BD_2026_0001

    Sequence numbers use 4 digits (up to 9999) and grow automatically
    beyond that if needed.
    """
    year = datetime.now(UTC).strftime("%Y")
    media_short = media_type.replace("MDISC", "MD").replace("BDXL", "BX")
    width = max(4, len(str(seq_num)))
    return f"{prefix}_{media_short}_{year}_{seq_num:0{width}d}"


def generate_uuid() -> str:
    """Generate a new UUID v4 string for volume identification."""
    return str(uuid.uuid4())


def generate_session_id() -> str:
    """Generate a collision-safe session ID.

    Format: ISO timestamp (microseconds) + short UUID suffix.
    """
    ts = datetime.now(UTC).isoformat(timespec="microseconds")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{ts}-{short_uuid}"


def next_seq_num(existing_labels: list[str], prefix: str = "LCSAS") -> int:
    """Determine the next sequence number based on existing volume labels.

    Parses labels matching the format PREFIX_*_YYYY_NNN and returns max + 1.
    Returns 1 if no matching labels exist.
    """
    max_seq = 0
    for label in existing_labels:
        parts = label.split("_")
        if len(parts) >= 4 and parts[0] == prefix:
            try:
                seq = int(parts[-1])
                max_seq = max(max_seq, seq)
            except ValueError:
                continue
    return max_seq + 1
