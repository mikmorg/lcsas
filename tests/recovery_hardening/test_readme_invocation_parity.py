"""Hardening test #2: README ↔ restore.sh invocation parity.

The README on the meta disc must document the same invocation form
that `restore.sh` actually accepts.  A README that documents a flag
``restore.sh`` no longer parses sends operators into an error.  This
test fails the build if a retired flag leaks back in.

Catches the failure mode where the meta-disc README and the script
shipped alongside it disagree on how to call the script.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README_ASSET = REPO_ROOT / "src" / "lcsas" / "meta" / "assets" / "README_RESTORE.md"


def _read_readme_source() -> str:
    """The README_RESTORE.md text the meta builder writes to the disc.

    It is copied verbatim from ``src/lcsas/meta/assets/README_RESTORE.md``
    (#689), so we read that file rather than build a meta disc just to
    inspect it.
    """
    return README_ASSET.read_text(encoding="utf-8")


# Patterns that indicate a retired flag is being documented for
# `restore.sh`.  --key, --target and --repo were renamed in #934 (the
# script now refuses them); the legacy bash drivers that accepted
# --isos (restore_legacy.sh, restore-auto.sh) were retired in #689.
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
        f"README_RESTORE documents a retired flag for restore.sh "
        f"({pattern!r}) -- restore.sh takes --restore-folder DIR, "
        f"--snapshot ID, --backup-set NAME and --password-file FILE. "
        f"Update src/lcsas/meta/assets/README_RESTORE.md."
    )


def test_readme_documents_restore_folder_form() -> None:
    """Sanity: the README should actually show the --restore-folder invocation."""
    readme = _read_readme_source()
    assert re.search(
        r"\b(?:sh\s+)?\.?/?restore\.sh\s+--restore-folder\b", readme
    ), (
        "README_RESTORE does not show a restore.sh --restore-folder "
        "invocation -- operators won't know how to call it."
    )


def test_recovery_scripts_restore_documents_restore_folder() -> None:
    """Sanity: the real script's usage line documents --restore-folder."""
    script = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
    src = script.read_text()
    # The script's own usage line should mention --restore-folder.
    assert re.search(r"usage:.*--restore-folder", src), (
        "recovery/scripts/restore.sh no longer documents --restore-folder "
        "in its usage line -- its CLI may have changed in a way "
        "that contradicts what README_RESTORE.md documents."
    )
