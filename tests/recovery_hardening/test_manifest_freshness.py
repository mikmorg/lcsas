"""The meta disc's recovery manifests list every file on it, with the right hash.

FAILURE MODE CAUGHT
-------------------
recovery/MANIFEST.sha256 and recovery/docs/MANIFEST.sha256 on a meta disc
are the heir's tamper-evidence record: RESTORE_WINDOWS.txt tells an
operator to ``findstr`` a file's name in it and compare hashes before
trusting the disc, and READINESS_CHECKLIST.txt tells them to run
``sha256sum -c recovery/MANIFEST.sha256``.  A stale manifest is worse than
none: a diligent heir gets a spurious mismatch on a GOOD disc, or a false
all-clear for a file nobody pinned.

Until #894 both manifests were committed files that had to be regenerated
by hand after every change under recovery/, and this gate checked the
committed files against the work tree.  Now ``lcsas meta build`` computes
them from the files it puts on the disc (``recovery_manifests``), so this
gate checks a built recovery/ tree instead.

WHAT THIS GATE ASSERTS
----------------------
On a recovery/ tree bundled the way the builder does it
(``_copy_recovery_tree`` + ``_regenerate_recovery_manifest``):

  * Both manifests exist, every row parses, and no path is listed twice.
  * Every listed file exists and its SHA-256 equals the listed digest.
  * Every file on the disc has a row: under recovery/ in the top-level
    manifest (except the two manifests), under docs/ in the docs manifest.
  * COMPLETENESS (#425, #432): the source rows are exactly the authored
    files under recovery/ -- what git lists (``--cached --others
    --exclude-standard``), minus bin/ and the developer records that stay
    off the disc (``DEV_ONLY_RECOVERY_FILES``, #724).  ``--others`` is on
    purpose: an authored file that was created but never ``git add``ed is
    not bundled, and this gate fails LOUDLY instead of the file being
    quietly absent from both the disc and its tamper record.  fuzz/corpus/
    is a MIXED directory (curated seed_* inputs are committed, fuzzer
    output is gitignored), which is why git, not ``find``, is the oracle.

The completeness half shells to ``git ls-files`` and skips when recovery/ is
not inside a git work tree.  No optical hardware, fast.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from lcsas.meta.builder import DEV_ONLY_RECOVERY_FILES, MetaVolumeBuilder

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"

# Each row is "<64 hex sha256><two spaces><relpath>".  Paths are relative to
# the manifest's own directory with a leading "./" (e.g. "./docs/TIERS.txt"
# in the top-level manifest, "./TIERS.txt" in the docs manifest).
_DIGEST_LEN = 64
_MANIFESTS = frozenset({"./MANIFEST.sha256", "./docs/MANIFEST.sha256"})


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """recovery/ bundled and manifested the way ``lcsas meta build`` does it."""
    out = tmp_path_factory.mktemp("meta")
    dst = out / "recovery"
    builder = MetaVolumeBuilder(out, project_root=REPO_ROOT, recovery_dir=RECOVERY_DIR)
    builder._copy_recovery_tree(RECOVERY_DIR, dst)
    builder._regenerate_recovery_manifest(dst)
    return dst


def _parse_manifest_rows(manifest: Path, label: str) -> list[tuple[int, str, str]]:
    """Return [(line_no, digest, relpath)] for every non-blank manifest row.

    Fails on any malformed row so a corrupt manifest fails loud rather than
    silently skipping rows.
    """
    rows: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        assert len(parts) == 2, f"{label} line {line_no} is not '<sha256>  <path>': {line!r}"
        digest, relpath = parts
        assert len(digest) == _DIGEST_LEN and all(
            c in "0123456789abcdef" for c in digest
        ), f"{label} line {line_no} has a malformed SHA-256 digest: {digest!r}"
        rows.append((line_no, digest, relpath))
    return rows


def _check(manifest: Path, label: str, root: Path, on_disc: set[str]) -> None:
    """Every row unique, present and matching; every file in *on_disc* listed."""
    assert manifest.is_file(), f"{label} is missing from the built meta disc"
    rows = _parse_manifest_rows(manifest, label)
    assert rows, f"{label} has no rows -- the integrity record is empty."

    seen: dict[str, int] = {}
    problems: list[str] = []
    for line_no, digest, relpath in rows:
        if relpath in seen:
            problems.append(f"listed twice: {relpath} (lines {seen[relpath]}, {line_no})")
            continue
        seen[relpath] = line_no
        target = root / relpath
        if not target.is_file():
            problems.append(f"listed but not on the disc: {relpath}")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            problems.append(f"hash differs: {relpath}")
    problems += [f"on the disc but not listed: {rel}" for rel in sorted(on_disc - set(seen))]
    assert not problems, (
        f"{label} on a built meta disc does not match the files there; an "
        f"heir running `sha256sum -c` would get a false alarm or a gap:\n  "
        + "\n  ".join(problems)
    )


def _files(root: Path) -> set[str]:
    return {
        f"./{p.relative_to(root).as_posix()}" for p in root.rglob("*") if p.is_file()
    }


def test_top_manifest_lists_every_file_on_the_disc(built: Path) -> None:
    _check(
        built / "MANIFEST.sha256", "recovery/MANIFEST.sha256", built,
        _files(built) - _MANIFESTS,
    )


def test_docs_manifest_lists_every_doc_on_the_disc(built: Path) -> None:
    docs = built / "docs"
    _check(
        docs / "MANIFEST.sha256", "recovery/docs/MANIFEST.sha256", docs,
        {rel for rel in _files(docs) if not rel.endswith("/MANIFEST.sha256")},
    )


def _git_authored_files() -> set[str]:
    """The authored files under recovery/ that belong on the disc, per git."""
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree -- cannot determine the authored file set")
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
        cwd=RECOVERY_DIR, capture_output=True, text=True, check=True,
    )
    return {
        f"./{line}"
        for line in proc.stdout.splitlines()
        if line
        and not line.startswith("bin/")
        and line not in DEV_ONLY_RECOVERY_FILES
        and f"./{line}" not in _MANIFESTS
        and (RECOVERY_DIR / line).is_file()
    }


def test_source_rows_are_exactly_the_authored_files(built: Path) -> None:
    """Every authored file is on the disc and listed; nothing else is (#425)."""
    listed = {
        rel for _n, _d, rel in _parse_manifest_rows(built / "MANIFEST.sha256", "manifest")
        if not rel.startswith("./bin/")
    }
    authored = _git_authored_files()
    unpinned = sorted(authored - listed)
    stray = sorted(listed - authored)
    problems: list[str] = []
    if unpinned:
        problems.append(
            f"{len(unpinned)} authored file(s) under recovery/ are not on the "
            f"meta disc.  If the file IS authored content, `git add` it (the "
            f"builder bundles only tracked files).  If it is not, delete it or "
            f"add it to a .gitignore:\n  " + "\n  ".join(unpinned)
        )
    if stray:
        problems.append(
            f"{len(stray)} file(s) on the disc are not authored content "
            f"(generated output or build artifacts, #425):\n  " + "\n  ".join(stray)
        )
    assert not problems, "\n\n".join(problems)
