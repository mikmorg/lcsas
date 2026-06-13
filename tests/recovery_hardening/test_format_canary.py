"""FMT-03 format canary: round-trip LATEST rustic through the pinned readers.

Unlike ``test_tier1_vs_tier2_differential`` (which pins the writer to rustic
0.11.2 by construction and so can only validate the readers), this canary is
driven by a workflow that installs the **latest** upstream rustic — the writer
is deliberately UNPINNED.  It writes a fixture tree with that latest rustic,
then restores it with BOTH pinned readers — tier-1 (the C ``lcsas-restore``
binary) and tier-3 (``PurePythonRestorer``) — and byte-compares each restore
against the source.

If a future rustic bumps the repository format past what the pinned readers
understand (a new MAC, KDF, or compression framing), this canary goes red —
giving years of warning before any operator upgrades the rustic on their NAS
and starts burning discs no shipped reader can open.

Opt-in: set ``LCSAS_FORMAT_CANARY=1`` (the weekly workflow does).  Requires
``rustic`` on PATH and a built ``lcsas-restore`` (``make -C recovery``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from lcsas.restore.restic_fallback import PurePythonRestorer
from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    diff_trees,
    find_restore_bin,
    find_restored_root,
    restore_with_tier1,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LCSAS_FORMAT_CANARY") != "1",
    reason="format canary is opt-in (set LCSAS_FORMAT_CANARY=1)",
)

requires_rustic = pytest.mark.skipif(
    not shutil.which("rustic"), reason="rustic not installed",
)


def _make_fixture(src: Path) -> None:
    """A small but representative tree: nested dirs, varied sizes, unicode."""
    (src / "top.txt").write_bytes(b"hello format canary\n")
    sub = src / "nested" / "deeper"
    sub.mkdir(parents=True)
    (sub / "small.bin").write_bytes(os.urandom(64))
    (sub / "medium.bin").write_bytes(os.urandom(200_000))
    (src / "uniéçñ.txt").write_bytes(b"unicode body\n")
    empty = src / "empty_dir"
    empty.mkdir()


@requires_rustic
def test_latest_rustic_roundtrips_through_tier1(tmp_path):
    """Latest rustic → tier-1 C reader → byte-identical to source."""
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    _make_fixture(src)
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("format-canary-password\n")

    build_rustic_repo(src, repo, pwfile)

    out = tmp_path / "tier1_out"
    restore_with_tier1(repo, out, pwfile, bin_path)
    restored = find_restored_root(out)

    diffs = diff_trees(src, restored)
    assert not diffs, (
        "tier-1 could not faithfully restore data written by the LATEST "
        "rustic — the upstream repository format may have drifted past the "
        "pinned reader's v1/v2 contract:\n" + "\n".join(diffs)
    )


@requires_rustic
def test_latest_rustic_roundtrips_through_tier3(tmp_path):
    """Latest rustic → tier-3 PurePythonRestorer → byte-identical to source."""
    src = tmp_path / "src"
    src.mkdir()
    _make_fixture(src)
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("format-canary-password\n")

    build_rustic_repo(src, repo, pwfile)

    out = tmp_path / "tier3_out"
    out.mkdir()
    restorer = PurePythonRestorer(
        repo, password_file=pwfile, interactive=False, strict=True,
    )
    restorer.restore(out)
    restored = find_restored_root(out)

    diffs = diff_trees(src, restored)
    assert not diffs, (
        "tier-3 (PurePythonRestorer) could not faithfully restore data "
        "written by the LATEST rustic — the upstream repository format may "
        "have drifted past the pinned reader's v1/v2 contract:\n"
        + "\n".join(diffs)
    )
