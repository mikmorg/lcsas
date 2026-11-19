"""recovery/VENDORED.sha256 pins every vendored sqlite and zstd source file.

FAILURE MODE CAUGHT
-------------------
Tier 1 compiles third-party C we keep in recovery/vendored/ (the sqlite
amalgamation and the zstd decoder).  Until #894 the committed
recovery/MANIFEST.sha256 pinned those files along with everything we
author; when it stopped being committed, a changed vendored file would no
longer show up as a changed pin in review.  recovery/VENDORED.sha256 is the
committed pin for that folder alone.

WHAT THIS GATE ASSERTS
----------------------
  * Every row of recovery/VENDORED.sha256 names a file under
    recovery/vendored/ whose SHA-256 matches.
  * Every file under recovery/vendored/ that git tracks or would track has
    a row.  Regenerate the pin with ``make -C recovery vendored-pin``.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
PIN = RECOVERY / "VENDORED.sha256"


def _rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in PIN.read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        assert len(digest) == 64, f"malformed row: {line!r}"
        assert rel.startswith("./vendored/"), f"row outside vendored/: {line!r}"
        assert rel not in rows, f"listed twice: {rel}"
        rows[rel] = digest
    return rows


def test_every_pinned_file_matches() -> None:
    rows = _rows()
    assert rows, "recovery/VENDORED.sha256 is empty"
    bad = [
        rel for rel, digest in rows.items()
        if not (RECOVERY / rel).is_file()
        or hashlib.sha256((RECOVERY / rel).read_bytes()).hexdigest() != digest
    ]
    assert not bad, (
        "vendored file(s) missing or changed without the pin; if the change "
        "is intended, run `make -C recovery vendored-pin`:\n  " + "\n  ".join(bad)
    )


def test_every_vendored_file_is_pinned() -> None:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree")
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "vendored"],
        cwd=RECOVERY, capture_output=True, text=True, check=True,
    )
    on_disk = {f"./{line}" for line in proc.stdout.splitlines() if line}
    unpinned = sorted(on_disk - set(_rows()))
    assert not unpinned, (
        "vendored file(s) with no row in recovery/VENDORED.sha256; run "
        "`make -C recovery vendored-pin`:\n  " + "\n  ".join(unpinned)
    )
