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
  * COMPLETENESS: every authored file under recovery/ HAS a row, and the
    manifest lists nothing that is not an authored file.

SCOPE -- WHAT COUNTS AS "AUTHORED" (issue #425)
------------------------------------------------
recovery/MANIFEST.sha256 is a CURATED "files-we-author" manifest, not a blind
full-tree sweep, and the curation rule is:

    every git-tracked file under recovery/, minus bin/ (built artifacts,
    covered by bin-parity and regenerated per-target at burn time by
    meta/builder.py _regenerate_recovery_manifest), minus the two
    MANIFEST.sha256 files themselves.

git is the oracle on purpose.  fuzz/corpus/ is a MIXED directory -- curated
seed_* inputs are committed, fuzzer-generated hex-named entries are gitignored
(fuzz/.gitignore: "Only the curated seed_* files are committed") -- so no
``find`` predicate can separate the two populations.  That is exactly why the
old find-based ``manifest`` target grew this file from 208 to 8,710 rows.

This section previously argued that no completeness half was possible because
there was "no deterministic, committed scope we could mirror".  There is one:
git's index.  The ``manifest`` Make target now generates from
``git ls-files``, and this gate mirrors that same scope, so the two cannot
disagree.

The forward-only gap this closes was real and had already been recorded in
docs/PROJECT_REVIEW_2026-07.md:289 ("new authored file with no row is
invisible"): when the completeness half was missing, the manifest had silently
gone stale by 161 files, including the ENTIRE src/lcsas-ecc/ C source and the
slip39/, rs03/ and tree/ fuzz corpora.

The completeness check uses ``--cached --others --exclude-standard`` rather
than the generator's ``--cached``, deliberately: an authored file that was
created but never ``git add``ed then fails this gate LOUDLY instead of quietly
being absent from both the index and the tamper record.

DOCS MANIFEST (recovery/docs/MANIFEST.sha256)
---------------------------------------------
The same freshness gate also covers the per-directory docs manifest.  Unlike
the curated top-level manifest, recovery/docs/ has a DETERMINISTIC scope --
every file in the directory except MANIFEST.sha256 itself (exactly what the
``manifest`` Make target sweeps, and the directory holds only authored docs).
So for the docs manifest we CAN and DO assert the reverse/completeness half:
every doc in the directory must have a row.  (Before this gate existed the
docs manifest drifted silently: three rows went stale across the #384 doc
edits and RESTORE_STANDARD_TOOLS.txt was never added at all.)

Mostly pure pathlib + hashlib; the completeness half shells to ``git
ls-files`` (as test_meta_bundling_completeness.py already does) and skips when
recovery/ is not inside a git work tree.  No optical hardware, fast.  Modelled
on the doc-contract tests in this directory (test_tiers_contract.py,
test_env_var_docs.py).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"
MANIFEST = RECOVERY_DIR / "MANIFEST.sha256"
DOCS_DIR = RECOVERY_DIR / "docs"
DOCS_MANIFEST = DOCS_DIR / "MANIFEST.sha256"

# Each row is "<64 hex sha256><two spaces><relpath>".  Paths are stored
# relative to the manifest's own directory with a leading "./"
# (e.g. "./docs/TIERS.txt" in the top-level manifest, "./TIERS.txt" in the
# docs manifest).
_DIGEST_LEN = 64


def _parse_manifest_rows(manifest: Path, label: str) -> list[tuple[int, str, str]]:
    """Return [(line_no, digest, relpath)] for every non-blank manifest row.

    Raises an assertion (caught by the parse test) on any malformed row so a
    corrupt manifest fails loud rather than silently skipping rows.
    """
    rows: list[tuple[int, str, str]] = []
    for line_no, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        # sha256sum format: digest, two spaces, path.  Split on the first run
        # of whitespace to be tolerant, then validate the digest shape.
        parts = line.split(maxsplit=1)
        assert len(parts) == 2, (
            f"{label} line {line_no} is not "
            f"'<sha256>  <path>': {line!r}"
        )
        digest, relpath = parts
        digest = digest.lower()
        assert len(digest) == _DIGEST_LEN and all(
            c in "0123456789abcdef" for c in digest
        ), (
            f"{label} line {line_no} has a malformed SHA-256 "
            f"digest: {digest!r}"
        )
        rows.append((line_no, digest, relpath))
    return rows


def _parse_rows() -> list[tuple[int, str, str]]:
    return _parse_manifest_rows(MANIFEST, "recovery/MANIFEST.sha256")


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


# Paths the manifest deliberately does NOT cover.  Keep in lockstep with the
# `manifest` target's pathspec in recovery/Makefile -- see the SCOPE section
# above for why each one is excluded.
_EXCLUDED_PREFIXES = ("./bin/",)
_EXCLUDED_PATHS = frozenset({"./MANIFEST.sha256", "./docs/MANIFEST.sha256"})


def _is_authored(relpath: str) -> bool:
    """True when `relpath` (a "./"-prefixed path) belongs in the manifest."""
    if relpath in _EXCLUDED_PATHS:
        return False
    return not relpath.startswith(_EXCLUDED_PREFIXES)


def _git_authored_files() -> set[str]:
    """The set of authored files under recovery/, per git.

    Uses ``--cached --others --exclude-standard`` so a NEW authored file that
    was never ``git add``ed still counts -- it must fail the completeness gate
    loudly rather than slip past both the index and the tamper record.
    ``--exclude-standard`` honours .gitignore, which is what keeps the
    fuzzer-generated corpus and build artifacts out.
    """
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree -- cannot determine the authored file set")
    proc = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            ".",
        ],
        cwd=RECOVERY_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        f"./{line}"
        for line in proc.stdout.splitlines()
        if line and _is_authored(f"./{line}")
    }


def test_manifest_is_complete_over_authored_files() -> None:
    """Reverse gate: every authored file has a row, and no row covers a
    non-authored file.

    Without this half the manifest is forward-only -- a newly added authored
    file is simply invisible to the tamper record, and an heir who verifies a
    disc gets a clean bill of health for a file nobody ever pinned.  That is
    how src/lcsas-ecc/ and three whole fuzz corpora went unpinned (#425).
    """
    listed = {relpath for _line_no, _digest, relpath in _parse_rows()}
    authored = _git_authored_files()

    unpinned = sorted(authored - listed)
    stray = sorted(listed - authored)

    problems: list[str] = []
    if unpinned:
        problems.append(
            f"{len(unpinned)} authored file(s) under recovery/ have NO manifest "
            f"row, so they are absent from the tamper-evidence record.  Run "
            f"`make -C recovery manifest` and commit the result:\n  "
            + "\n  ".join(unpinned)
        )
    if stray:
        problems.append(
            f"{len(stray)} manifest row(s) cover files that are not authored "
            f"content (generated output, build artifacts, or files under "
            f"bin/).  This is the blind-sweep corruption from #425 -- "
            f"regenerate with `make -C recovery manifest`:\n  "
            + "\n  ".join(stray)
        )
    assert not problems, (
        "recovery/MANIFEST.sha256 does not match the authored file set it "
        "claims to pin.\n\n" + "\n\n".join(problems)
    )


# ── recovery/docs/MANIFEST.sha256 — same gate + completeness half ────────


def test_docs_manifest_exists() -> None:
    """The per-directory docs manifest must exist alongside the docs."""
    assert DOCS_MANIFEST.is_file(), (
        f"recovery/docs/MANIFEST.sha256 is missing at {DOCS_MANIFEST}.  It is "
        f"the tamper-evidence record for the on-disc operator manual; without "
        f"it an heir cannot verify the docs they are following."
    )


def test_docs_manifest_rows_parse_and_are_unique() -> None:
    """Every docs-manifest row is well-formed and no path is listed twice."""
    rows = _parse_manifest_rows(DOCS_MANIFEST, "recovery/docs/MANIFEST.sha256")
    assert rows, (
        "recovery/docs/MANIFEST.sha256 has no rows -- the integrity record "
        "is empty."
    )
    seen: dict[str, int] = {}
    dups: list[str] = []
    for line_no, _digest, relpath in rows:
        if relpath in seen:
            dups.append(f"{relpath} (lines {seen[relpath]} and {line_no})")
        else:
            seen[relpath] = line_no
    assert not dups, (
        "recovery/docs/MANIFEST.sha256 lists the same path more than once -- "
        "ambiguous tamper record:\n  " + "\n  ".join(dups)
    )


def test_docs_manifest_listed_files_match_and_are_complete() -> None:
    """Docs-manifest gate, BOTH directions.

    Forward: every listed path exists in recovery/docs/ and its recomputed
    SHA-256 equals the committed digest (same staleness gate as the
    top-level manifest).

    Reverse (docs-only): every file in recovery/docs/ except
    MANIFEST.sha256 itself has a row.  The docs directory's scope is
    deterministic -- it holds only authored docs, exactly what the
    ``manifest`` Make target sweeps -- so a missing row is always a real
    omission (this is how RESTORE_STANDARD_TOOLS.txt went unpinned for
    weeks).
    """
    rows = _parse_manifest_rows(DOCS_MANIFEST, "recovery/docs/MANIFEST.sha256")
    missing: list[str] = []
    drifted: list[str] = []
    listed: set[Path] = set()

    for line_no, digest, relpath in rows:
        target = (DOCS_DIR / relpath).resolve()
        listed.add(target)
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

    unlisted = sorted(
        p.name
        for p in DOCS_DIR.iterdir()
        if p.is_file() and p.name != "MANIFEST.sha256" and p.resolve() not in listed
    )

    problems: list[str] = []
    if missing:
        problems.append(
            f"{len(missing)} docs-manifest row(s) point at files that no "
            f"longer exist under recovery/docs/ (delete/rename the row or "
            f"restore the file):\n  " + "\n  ".join(missing)
        )
    if drifted:
        problems.append(
            f"{len(drifted)} docs-manifest digest(s) no longer match the "
            f"file on disc -- refresh the row(s):\n  " + "\n  ".join(drifted)
        )
    if unlisted:
        problems.append(
            f"{len(unlisted)} file(s) in recovery/docs/ have NO manifest row "
            f"-- add them (regenerate with `make -C recovery manifest`, then "
            f"restore the curated top-level manifest if the blind sweep "
            f"clobbered it):\n  " + "\n  ".join(unlisted)
        )
    assert not problems, (
        "recovery/docs/MANIFEST.sha256 has drifted from the real docs tree; "
        "an heir verifying GOOD docs against it would get a spurious "
        "mismatch (or a gap in the tamper record).\n\n"
        + "\n\n".join(problems)
    )
