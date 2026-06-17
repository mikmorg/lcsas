"""test_manifest_freshness.py -- tamper/staleness gate for recovery/MANIFEST.sha256.

FAILURE MODE CAUGHT
-------------------
recovery/MANIFEST.sha256 is presented to heirs as a pinned tamper-evidence
record: RECOVER_WINDOWS.txt tells an operator to ``findstr`` a file's name in
it and compare hashes before trusting the disc, and READINESS_CHECKLIST.txt
tells them to run ``sha256sum -c recovery/MANIFEST.sha256``.  Until this gate
existed NOTHING recomputed the real tree's hashes and asserted the committed
manifest still matched.  So it could silently drift: an authored file gets
edited but its manifest digest is not refreshed, or a file the manifest lists
is deleted/renamed.  A stale integrity manifest is worse than none -- a
diligent heir gets a spurious mismatch on a GOOD disc, or a false all-clear.

WHAT THIS GATE ASSERTS
----------------------
  * Every ``<sha256>  <relpath>`` row parses and its path exists under
    recovery/ -- no orphaned rows pointing at deleted/renamed files.
  * The recomputed SHA-256 of each listed file equals the committed digest --
    the core freshness/tamper gate.  Every drifted/missing row is reported.

SCOPE / WHY NO "COMPLETENESS" HALF
----------------------------------
This gate deliberately checks only the FORWARD direction (every listed row
still matches its file).  It does NOT assert the reverse -- that every
authored file has a row -- and here is why:

recovery/MANIFEST.sha256 is a CURATED "files-we-author" manifest, not a blind
full-tree sweep.  At time of writing it holds ~206 rows against 391 git-tracked
files under recovery/: it intentionally omits 149 of the 214 tracked fuzz
seeds, the built ``bin/`` artifacts, ``TOOLCHAIN``, ``BIN_PARITY_EXEMPT``, and
others.  The manifest carries no machine-readable header declaring that
inclusion rule, and the ``manifest`` Make target regenerates it with a blind
``find ... | sha256sum`` that sweeps in UNTRACKED fuzzer corpus + build
artifacts (a known prior corruption).  So there is no deterministic, committed
scope we could mirror to compute "the set of files that SHOULD have a row"
without either reintroducing the untracked-corpus problem or hard-coding a
brittle allow/deny list that would itself drift.  A flaky or guess-based
completeness assertion is worse than none; the hash-match half is the
high-value tamper/freshness gate and stands on its own.

Pure pathlib + hashlib -- no subprocess, no optical hardware, fast.  Modelled
on the doc-contract tests in this directory (test_tiers_contract.py,
test_env_var_docs.py).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"
MANIFEST = RECOVERY_DIR / "MANIFEST.sha256"

# Each row is "<64 hex sha256><two spaces><relpath>".  Paths are stored
# relative to recovery/ with a leading "./" (e.g. "./docs/TIERS.txt").
_DIGEST_LEN = 64


def _parse_rows() -> list[tuple[int, str, str]]:
    """Return [(line_no, digest, relpath)] for every non-blank manifest row.

    Raises an assertion (caught by the parse test) on any malformed row so a
    corrupt manifest fails loud rather than silently skipping rows.
    """
    rows: list[tuple[int, str, str]] = []
    for line_no, raw in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        # sha256sum format: digest, two spaces, path.  Split on the first run
        # of whitespace to be tolerant, then validate the digest shape.
        parts = line.split(maxsplit=1)
        assert len(parts) == 2, (
            f"recovery/MANIFEST.sha256 line {line_no} is not "
            f"'<sha256>  <path>': {line!r}"
        )
        digest, relpath = parts
        digest = digest.lower()
        assert len(digest) == _DIGEST_LEN and all(
            c in "0123456789abcdef" for c in digest
        ), (
            f"recovery/MANIFEST.sha256 line {line_no} has a malformed SHA-256 "
            f"digest: {digest!r}"
        )
        rows.append((line_no, digest, relpath))
    return rows


def test_manifest_exists() -> None:
    """The manifest heirs are told to verify against must exist."""
    assert MANIFEST.is_file(), (
        f"recovery/MANIFEST.sha256 is missing at {MANIFEST}.  Heirs are told "
        f"by RECOVER_WINDOWS.txt and READINESS_CHECKLIST.txt to verify disc "
        f"files against it; without it the tamper-evidence record is gone."
    )


def test_manifest_rows_parse_and_are_unique() -> None:
    """Every row is well-formed and no path is listed twice."""
    rows = _parse_rows()
    assert rows, "recovery/MANIFEST.sha256 has no rows -- the integrity record is empty."
    seen: dict[str, int] = {}
    dups: list[str] = []
    for line_no, _digest, relpath in rows:
        if relpath in seen:
            dups.append(f"{relpath} (lines {seen[relpath]} and {line_no})")
        else:
            seen[relpath] = line_no
    assert not dups, (
        "recovery/MANIFEST.sha256 lists the same path more than once -- "
        "ambiguous tamper record:\n  " + "\n  ".join(dups)
    )


def test_every_listed_file_exists_and_matches_digest() -> None:
    """Core gate: every listed path exists under recovery/ and its recomputed
    SHA-256 equals the committed digest.

    Catches both staleness directions a heir would hit:
      * MISSING -- the manifest points at a deleted/renamed file, so an heir
        gets a spurious mismatch on a good disc.
      * DRIFTED -- the file was edited but its manifest digest was not
        refreshed, so an heir gets a false mismatch (or, after a malicious
        edit, no warning that the recorded hash itself is stale).
    """
    missing: list[str] = []
    drifted: list[str] = []

    for line_no, digest, relpath in _parse_rows():
        target = (RECOVERY_DIR / relpath).resolve()
        if not target.is_file():
            missing.append(f"line {line_no}: {relpath}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            drifted.append(
                f"line {line_no}: {relpath}\n"
                f"      committed: {digest}\n"
                f"      actual:    {actual}"
            )

    problems: list[str] = []
    if missing:
        problems.append(
            f"{len(missing)} manifest row(s) point at files that no longer "
            f"exist under recovery/ (delete/rename the row or restore the "
            f"file):\n  " + "\n  ".join(missing)
        )
    if drifted:
        problems.append(
            f"{len(drifted)} manifest digest(s) no longer match the file on "
            f"disc -- refresh the row(s) for these AUTHORED files:\n  "
            + "\n  ".join(drifted)
        )
    assert not problems, (
        "recovery/MANIFEST.sha256 has drifted from the real tree; an heir "
        "verifying a GOOD disc against it would get a spurious mismatch.\n\n"
        + "\n\n".join(problems)
    )
