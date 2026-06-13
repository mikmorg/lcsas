"""Bin-packer warns about multi-extent (>4 GiB) items at plan time (FMT-04).

A single file larger than the ISO 9660 single-extent limit (4 GiB − 2 KiB)
becomes multi-extent and is silently truncated by Windows' native CDFS
mount.  ISO mastering hard-rejects these; the bin-packer warns earlier so
the operator hears about it before staging completes — even when the item
comfortably fits the media.
"""

from __future__ import annotations

import logging

from lcsas.binpack.algorithm import _ISO_MAX_FILE_BYTES, first_fit_decreasing
from lcsas.config.media import MediaType


def test_warns_on_multiextent_item_that_fits_media(caplog):
    """A 5 GiB item fits BDXL100 but must still trigger a warning."""
    five_gib = 5 * 1024**3
    bdxl = MediaType.BDXL100.usable_bytes
    assert five_gib < bdxl  # the item fits the media — only the ISO limit bites

    items = [("bigpack", five_gib), ("small", 1000)]
    with caplog.at_level(logging.WARNING, logger="lcsas.binpack.algorithm"):
        selected, remaining = first_fit_decreasing(items, capacity=bdxl)

    assert ("bigpack", five_gib) in selected  # it still gets packed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("multi-extent" in r.getMessage() for r in warnings)
    assert any("bigpack" in r.getMessage() for r in warnings)


def test_no_warning_for_item_at_limit(caplog):
    """An item of exactly the single-extent limit must not warn."""
    items = [("ok", _ISO_MAX_FILE_BYTES)]
    with caplog.at_level(logging.WARNING, logger="lcsas.binpack.algorithm"):
        first_fit_decreasing(items, capacity=MediaType.BDXL100.usable_bytes)
    assert not any(
        "multi-extent" in r.getMessage() for r in caplog.records
    )
