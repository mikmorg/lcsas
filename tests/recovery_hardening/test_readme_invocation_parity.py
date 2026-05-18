"""Hardening test #2: README ↔ restore.sh invocation parity.

The README on the meta disc must document the same invocation form
that `restore.sh` actually accepts.  Pre-Phase-21 README documented a
flag-style UX (``./restore.sh --key X --target Y --repo Z``) that
``restore.sh`` no longer parses — operators followed the README,
restore.sh exited with a cryptic error.  This test fails the build
if the obsolete flag UX leaks back in.

Catches the failure mode where the meta-disc README and the script
shipped alongside it disagree on how to call the script.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_readme_source() -> str:
    """Pull the README_RESTORE heredoc out of the meta builder.

    The README is generated at build time from a string literal in
    `src/lcsas/meta/builder.py`.  We read it directly rather than
    building a meta disc just to inspect it.
    """
    builder = REPO_ROOT / "src" / "lcsas" / "meta" / "builder.py"
    src = builder.read_text()
    m = re.search(
        r"^README_RESTORE\s*=\s*(['\"])(.+?)\1\s*$"
        r"|^README_RESTORE\s*=\s*'''\\?\n(.+?)^'''",
        src, re.DOTALL | re.MULTILINE,
    )
    if m is None:
        m = re.search(
            r"^README_RESTORE\s*=\s*'''\\?\n(.+?)\n'''",
            src, re.DOTALL | re.MULTILINE,
        )
    assert m is not None, "could not locate README_RESTORE in builder.py"
    return m.group(m.lastindex)


# Patterns that indicate the obsolete flag-style UX is being documented
# for `restore.sh`.  The legacy bash driver (restore_legacy.sh) and the
# non-interactive `restore-auto.sh` legitimately accept these flags, so
# we only flag occurrences attached to bare `restore.sh`.
FORBIDDEN_INVOCATIONS = [
    r"\brestore\.sh\s+--key\b",
    r"\brestore\.sh\s+--repo\b",
    r"\brestore\.sh\s+--target\b",
    r"\brestore\.sh\s+--isos\b",
    r"\./restore\.sh\s+--key\b",
    r"\./restore\.sh\s+--repo\b",
    r"\./restore\.sh\s+--target\b",
    r"\./restore\.sh\s+--isos\b",
]


@pytest.mark.parametrize("pattern", FORBIDDEN_INVOCATIONS)
def test_readme_no_obsolete_flag_ux(pattern: str) -> None:
    readme = _read_readme_source()
    matches = re.findall(pattern, readme)
    assert not matches, (
        f"README_RESTORE documents the obsolete flag UX for restore.sh "
        f"({pattern!r}) — restore.sh only accepts positional args "
        f"(`sh restore.sh [TARGET_DIR] [SNAPSHOT_ID]`). "
        f"Update the README in src/lcsas/meta/builder.py."
    )


def test_readme_documents_positional_form() -> None:
    """Sanity: the README should actually show the positional invocation."""
    readme = _read_readme_source()
    # Match either `sh restore.sh ~/restored/` or `sh restore.sh /tmp/...`
    # or `restore.sh TARGET` (curly-brace placeholders welcome).
    assert re.search(
        r"\b(?:sh\s+)?\.?/?restore\.sh\s+[\$~/\w]", readme
    ), (
        "README_RESTORE does not show any positional invocation of "
        "restore.sh — operators won't know how to call it."
    )


def test_recovery_scripts_restore_takes_positional() -> None:
    """Sanity: the real script's usage line documents positional args."""
    script = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
    src = script.read_text()
    # The script's own usage line should mention TARGET_DIR positional.
    assert re.search(r"usage:.*TARGET_DIR", src), (
        "recovery/scripts/restore.sh no longer documents TARGET_DIR "
        "as a positional argument — its CLI may have changed in a way "
        "that contradicts what README_RESTORE.md documents."
    )
