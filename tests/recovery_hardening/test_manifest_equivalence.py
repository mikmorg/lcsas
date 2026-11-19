"""test_manifest_equivalence.py -- the builder computes the committed manifests exactly (#894).

FAILURE MODE CAUGHT
-------------------
The meta disc builder used to copy recovery/MANIFEST.sha256 and
recovery/docs/MANIFEST.sha256 from the repository and patch them: drop
the rows of the developer records it leaves off the disc, append rows
for the binaries it bundles.  #894 moves to computing both manifests
from the files actually copied onto the disc, so that the two files can
stop being committed.  If the computed manifests differed from the
committed ones in any row, order or byte, discs built after the switch
would carry manifests that no longer match the record heirs and the
drill logs have been checked against, and nobody would notice.

WHAT THIS GATE ASSERTS
----------------------
  * Copying recovery/ the way the builder does (``_copy_recovery_tree``)
    and computing its manifests (``recovery_manifests``) gives, outside
    ``./bin/``, exactly the committed recovery/MANIFEST.sha256 minus the
    rows of the developer-only files (``DEV_ONLY_RECOVERY_FILES``), byte
    for byte.
  * The computed docs manifest is exactly the committed
    recovery/docs/MANIFEST.sha256 minus those rows, byte for byte.

This gate needs the committed manifests, so it lives only as long as
they do: it proves the switch before they are removed from git.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.meta.builder import (
    DEV_ONLY_RECOVERY_FILES,
    MetaVolumeBuilder,
    recovery_manifests,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"


def _without(text: str, gone: set[str]) -> str:
    return "".join(
        line for line in text.splitlines(keepends=True)
        if line.rstrip("\n").split("  ", 1)[-1] not in gone
    )


def _computed(tmp_path: Path) -> dict[str, str]:
    out = tmp_path / "meta"
    out.mkdir()
    dst = out / "recovery"
    MetaVolumeBuilder(
        out, project_root=REPO_ROOT, recovery_dir=RECOVERY,
    )._copy_recovery_tree(RECOVERY, dst)
    return recovery_manifests(dst)


def test_top_manifest_matches_the_committed_one(tmp_path: Path) -> None:
    committed = (RECOVERY / "MANIFEST.sha256").read_text(encoding="utf-8")
    expected = _without(
        committed, {f"./{rel}" for rel in DEV_ONLY_RECOVERY_FILES},
    )
    computed = _computed(tmp_path)["MANIFEST.sha256"]
    source_rows = "".join(
        line for line in computed.splitlines(keepends=True)
        if not line.split("  ", 1)[1].startswith("./bin/")
    )
    assert source_rows == expected
    # The bin/ rows all come after the source rows.
    assert computed.startswith(source_rows)


def test_docs_manifest_matches_the_committed_one(tmp_path: Path) -> None:
    committed = (RECOVERY / "docs" / "MANIFEST.sha256").read_text(
        encoding="utf-8",
    )
    expected = _without(committed, {
        f"./{rel.removeprefix('docs/')}"
        for rel in DEV_ONLY_RECOVERY_FILES if rel.startswith("docs/")
    })
    assert _computed(tmp_path)["docs/MANIFEST.sha256"] == expected
