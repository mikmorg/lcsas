"""Volume label and UUID generation utilities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


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
