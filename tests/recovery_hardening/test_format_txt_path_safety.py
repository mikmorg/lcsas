"""Hardening test: FORMAT.txt PATH SAFETY must match binary behaviour.

FORMAT.txt is the durable on-disc spec an heir's future technician
audits the tier-1 binary against.  Two PATH SAFETY claims drifted from
the code (T1C-03):

  (a) The spec said symlinks with ABSOLUTE linktargets are REJECTED, but
      lcsas_path_safe_symlink deliberately *allows* any absolute target
      (issue #187, for rustic/tier-2 parity).  The doc must say absolute
      targets are restored as-is with NO containment guarantee, not that
      they are rejected.
  (b) The spec said names containing NUL bytes are REJECTED.  This is now
      enforced (decode_path_component length-checks the decoded string),
      so the NUL-rejection claim must REMAIN in the spec.

This is the FORMAT.txt-specific leg of the system-wide docs-vs-reality
contract gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FORMAT_TXT = REPO_ROOT / "recovery" / "docs" / "FORMAT.txt"


def _path_safety_section() -> str:
    """Return the PATH SAFETY section body (up to the next ALL-CAPS header)."""
    text = FORMAT_TXT.read_text()
    assert "PATH SAFETY" in text, "FORMAT.txt has no PATH SAFETY section"
    after = text.split("PATH SAFETY", 1)[1]
    # The next top-level header is an all-caps word at column 0.
    m = re.search(r"\n[A-Z][A-Z ]+\n", after)
    return after[: m.start()] if m else after


def test_format_txt_exists() -> None:
    assert FORMAT_TXT.is_file(), (
        f"recovery/docs/FORMAT.txt is missing.  This is the on-disc spec "
        f"the tier-1 binary is audited against.  Expected at: {FORMAT_TXT}"
    )


def test_absolute_symlinks_documented_as_restored_not_rejected() -> None:
    """PATH SAFETY must say absolute symlinks are restored as-is (#187),
    NOT that they are rejected — the binary allows them."""
    section = _path_safety_section().lower()
    assert "restored as-is" in section, (
        "FORMAT.txt PATH SAFETY no longer states that absolute symlink "
        "targets are 'restored as-is'.  The binary allows absolute "
        "targets (path.c, issue #187); the spec must say so."
    )
    assert "containment" in section and "not" in section, (
        "FORMAT.txt PATH SAFETY must note that containment is NOT "
        "guaranteed for absolute symlink targets."
    )
    # The drift this gate guards against: claiming absolute targets are
    # rejected.  The legitimate wording rejects only RELATIVE escapes, so
    # flag a line that pairs 'absolute' with 'reject' UNLESS it also
    # scopes the rejection to 'relative'.
    for line in _path_safety_section().splitlines():
        low = line.lower()
        if "absolute" in low and "reject" in low and "relative" not in low:
            pytest.fail(
                "FORMAT.txt PATH SAFETY still claims absolute symlink "
                f"targets are rejected: {line.strip()!r}.  The binary "
                "allows them (issue #187)."
            )


def test_nul_in_names_still_rejected() -> None:
    """PATH SAFETY must still claim NUL bytes in names are rejected —
    decode_path_component now enforces this (T1C-03)."""
    section = _path_safety_section().lower()
    assert "nul" in section and "reject" in section, (
        "FORMAT.txt PATH SAFETY must still state that names containing "
        "NUL bytes are REJECTED — the binary enforces this via "
        "decode_path_component (T1C-03)."
    )
