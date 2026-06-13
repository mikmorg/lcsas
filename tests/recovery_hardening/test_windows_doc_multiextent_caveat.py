"""Hardening doc-pin: RECOVER_WINDOWS.txt documents the >4 GiB CDFS trap (FMT-04).

Windows' built-in CDFS driver cannot reassemble an ISO 9660 file stored
across multiple extents (single files ≥ 4 GiB), so it reads only the first
extent — the heir sees a silently truncated file.  LCSAS hard-rejects such
files at mastering, but the heir docs must still warn about pre-guard discs
and point to the 7-Zip extraction workaround.

These static assertions catch:
  * RECOVER_WINDOWS.txt being deleted or moved.
  * The >4 GiB / multi-extent caveat being stripped from the mount docs.
  * The 7-Zip workaround being removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVER_WINDOWS_TXT = REPO_ROOT / "recovery" / "docs" / "RECOVER_WINDOWS.txt"


def test_recover_windows_txt_exists() -> None:
    assert RECOVER_WINDOWS_TXT.is_file(), (
        f"recovery/docs/RECOVER_WINDOWS.txt is missing. Expected at: "
        f"{RECOVER_WINDOWS_TXT}"
    )


def test_documents_4gib_multiextent_limit() -> None:
    """The doc must warn that ≥4 GiB files cannot be read via native mount."""
    content = RECOVER_WINDOWS_TXT.read_text().lower()
    assert "4 gib" in content, (
        "RECOVER_WINDOWS.txt does not mention the 4 GiB single-file limit. "
        "Windows' native CDFS mount silently truncates multi-extent files; "
        "the heir must be warned to suspect this when a recovered file looks "
        "truncated without a hash mismatch."
    )
    assert "extent" in content, (
        "RECOVER_WINDOWS.txt does not mention multi-extent storage. The "
        "caveat must explain *why* large files truncate (ISO 9660 stores "
        "them across multiple extents the native driver cannot reassemble)."
    )


def test_documents_7zip_workaround() -> None:
    """The doc must point to the 7-Zip extraction workaround."""
    content = RECOVER_WINDOWS_TXT.read_text().lower()
    assert "7-zip" in content or "7zip" in content, (
        "RECOVER_WINDOWS.txt does not mention the 7-Zip workaround. Files "
        "over 4 GiB cannot be read through Windows' built-in mount; the heir "
        "must be told to copy the ISO to disk and extract it with 7-Zip, "
        "which reassembles multi-extent files correctly."
    )
