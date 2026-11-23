"""Hardening test: README_RESTORE simplification (Unit 5).

Two friction points in the blind-restore transcript came from the
operator-facing recovery README:

1. The recipe told users to ``sudo mount`` then ``cp -r /mnt/meta
   /tmp/lcsas-meta`` then ``cd`` then ``umount`` before running
   ``restore.sh``.  That manual relocation step is redundant —
   ``recovery/scripts/restore.sh`` has its own ``relocate_to_ram``
   logic that handles the read-only-media case automatically (see
   ``find_meta_mount`` + ``relocate_to_ram`` near line 68 of that
   script).  The 4-line recipe collapses to a 2-line one.

2. The test rig and the agent prompt treat tmux as the expected
   driver, which makes operators with a single terminal think they
   need to install tmux before they can swap discs.  A second
   terminal window works fine.  (#966: the ``Ctrl+Z`` + ``fg`` advice
   is gone: after ``fg`` at the hidden password question the terminal
   showed the password as it was typed.)

This file fails the build if either regression returns:

* The obsolete ``cp -r /mnt`` recipe creeps back into the README, OR
* The ``LCSAS_NO_RELOCATE`` override stops being documented, OR
* The ``Ctrl+Z`` advice comes back, or the second-terminal advice
  goes, OR
* The Single-Drive quick-start grows back beyond two shell commands.

Catches the failure mode where operators copy a longer recipe than
necessary off the disc, then waste time trying to debug a step
``restore.sh`` would have done for them automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README_ASSET = REPO_ROOT / "src" / "lcsas" / "meta" / "assets" / "README_RESTORE.md"


def _read_readme_source() -> str:
    """The README_RESTORE.md text the meta builder writes to the disc.

    It is copied verbatim from ``src/lcsas/meta/assets/README_RESTORE.md``
    (#689), so we read that file rather than build a meta disc just to
    inspect it.
    """
    return README_ASSET.read_text(encoding="utf-8")


def test_readme_no_manual_cp() -> None:
    """The old 4-step ``cp -r /mnt/meta /tmp/lcsas-meta`` recipe is gone."""
    readme = _read_readme_source()
    assert "cp -r /mnt" not in readme, (
        "README_RESTORE still documents the obsolete `cp -r /mnt ...` "
        "manual-relocation recipe — restore.sh's relocate_to_ram logic "
        "handles this automatically.  Drop the recipe from "
        "src/lcsas/meta/assets/README_RESTORE.md."
    )


def test_readme_mentions_relocate_override() -> None:
    """Advanced users need to know about the ``LCSAS_NO_RELOCATE`` escape hatch."""
    readme = _read_readme_source()
    assert "LCSAS_NO_RELOCATE" in readme, (
        "README_RESTORE does not mention the LCSAS_NO_RELOCATE override "
        "— operators who need to skip the auto-relocation (tests, dev, "
        "running from a pre-copied dir) will not know it exists."
    )


def test_readme_swaps_discs_from_a_second_terminal() -> None:
    """#966 item 8: Ctrl+Z, a disc swap, then ``fg`` at the hidden
    password question left the terminal showing the password as it was
    typed.  The README says to swap from a second terminal window, and
    no longer suggests Ctrl+Z (nor tmux)."""
    readme = _read_readme_source()
    # "Ctrl+Z", "Ctrl-Z", "ctrl z", and the like.
    assert not re.search(r"ctrl[\s+\-]?z", readme, re.IGNORECASE), (
        "README_RESTORE suggests Ctrl+Z again: after fg at the password "
        "question the terminal echoes the password."
    )
    flat = " ".join(readme.split())
    assert "second terminal window" in flat, (
        "README_RESTORE does not say how to swap discs with one "
        "terminal open: from a second terminal window."
    )


def test_readme_quick_start_is_two_commands() -> None:
    """The Single-Drive Mode quick-start is a 2-command recipe, not 4.

    The first fenced block under the ``Single-Drive Mode`` heading
    should contain at most two shell commands (mount + run).  The old
    recipe was ``mount`` / ``cp -r`` / ``cd`` / ``umount`` followed by
    a second block with ``sh restore.sh ...``.
    """
    readme = _read_readme_source()
    # Slice the Single-Drive Mode section.
    sd = re.search(
        r"##\s+Single-Drive Mode.*?(?=^##\s+\S|\Z)",
        readme, re.DOTALL | re.MULTILINE,
    )
    assert sd is not None, (
        "README_RESTORE no longer has a `## Single-Drive Mode` section."
    )
    section = sd.group(0)

    # First fenced ``` block in that section.
    block = re.search(r"```[a-zA-Z]*\n(.*?)\n```", section, re.DOTALL)
    assert block is not None, (
        "Single-Drive Mode section has no fenced code block — "
        "operators have no copyable recipe."
    )
    body = block.group(1)

    # Count non-blank, non-comment shell command lines.
    cmd_lines = [
        line for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(cmd_lines) <= 2, (
        f"Single-Drive Mode quick-start has {len(cmd_lines)} command "
        f"lines; the simplified recipe is at most 2 (mount + run). "
        f"Found:\n  " + "\n  ".join(cmd_lines)
    )
    # Sanity: the surviving recipe should actually mount and run.
    joined = "\n".join(cmd_lines)
    assert "mount" in joined, (
        "Single-Drive Mode quick-start no longer contains a `mount` "
        "command — operators need to mount the disc."
    )
    assert "restore.sh" in joined, (
        "Single-Drive Mode quick-start no longer invokes `restore.sh`."
    )
