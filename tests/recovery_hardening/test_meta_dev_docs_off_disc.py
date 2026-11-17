"""test_meta_dev_docs_off_disc.py -- developer-process records stay off the meta disc (#724).

FAILURE MODE CAUGHT
-------------------
recovery/docs/ held nine developer-process records (audit ledgers, a
Windows design plan, the owner's readiness checklist, two redirect stubs)
next to RECOVER.txt.  Every meta disc burned them, so a non-technical heir
browsing the disc met hundreds of lines of developer noise.

WHAT THIS GATE ASSERTS
----------------------
  * None of the nine files is anywhere in a built meta tree, whether
    recovery/ is copied from a git checkout or from a plain folder.
  * The audit-gate files that stay in recovery/docs/ have no row in
    either on-disc manifest, and every row left points at a real file
    with the listed hash, so `meta verify` and an heir's `sha256sum -c`
    still pass.
  * The other records live in docs/development/recovery/ and the two
    stubs are gone, so no reference can point at the old place.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from lcsas.meta.builder import DEV_ONLY_RECOVERY_FILES, MetaVolumeBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]

DEV_RECORDS = (
    "WINDOWS_RECOVERY_PLAN.txt",
    "AUDIT.md",
    "EXEMPTIONS.md",
    "STATUS_LEDGER.md",
    "UX_CONCERNS.txt",
    "READINESS_CHECKLIST.txt",
    "SANITIZER_SUPPRESSIONS.txt",
    "DEFERRED_WORK.txt",
    "AUDIT_FINDINGS.md",
)
MOVED = ("WINDOWS_RECOVERY_PLAN.txt", "UX_CONCERNS.txt", "READINESS_CHECKLIST.txt")
DELETED = ("DEFERRED_WORK.txt", "AUDIT_FINDINGS.md")


def _build_docs_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_dir: Path,
) -> Path:
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
    out = tmp_path / "meta"
    out.mkdir()
    builder = MetaVolumeBuilder(
        out, project_root=REPO_ROOT, recovery_dir=recovery_dir,
    )
    builder._bundle_docs()
    builder._bundle_recovery_toolchain_artifacts()
    return out


def _check_manifest(manifest: Path) -> None:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        sha, rel = line.split("  ", 1)
        if rel.startswith("./bin/"):
            continue  # built binaries: covered by the bin tests
        name = rel.rsplit("/", 1)[-1]
        assert name not in DEV_RECORDS, f"{manifest.name} still lists {rel}"
        path = manifest.parent / rel
        assert path.is_file(), f"{manifest} lists {rel}, absent on the disc"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, rel


@pytest.mark.parametrize("from_git", [True, False], ids=["git", "plain-folder"])
def test_no_developer_record_on_the_disc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_git: bool,
) -> None:
    recovery_dir = REPO_ROOT / "recovery"
    if not from_git:
        # Outside git the builder copies the whole folder; the exclusion
        # must hold there too.
        recovery_dir = tmp_path / "plain-recovery"
        shutil.copytree(REPO_ROOT / "recovery", recovery_dir, symlinks=True,
                        ignore=shutil.ignore_patterns("build", "bin"))
    out = _build_docs_and_recovery(tmp_path, monkeypatch, recovery_dir)

    found = sorted(
        p.relative_to(out).as_posix() for p in out.rglob("*")
        if p.name in DEV_RECORDS
    )
    assert found == [], f"developer records on the meta disc: {found}"
    assert not (out / "docs" / "development" / "recovery").exists()
    # The heir manuals are still there.
    assert (out / "recovery" / "docs" / "RECOVER.txt").is_file()
    assert (out / "docs" / "development" / "roadmap.md").is_file()

    _check_manifest(out / "recovery" / "MANIFEST.sha256")
    _check_manifest(out / "recovery" / "docs" / "MANIFEST.sha256")


def test_records_live_where_the_references_point() -> None:
    dev_dir = REPO_ROOT / "docs" / "development" / "recovery"
    for name in MOVED:
        assert (dev_dir / name).is_file(), name
        assert not (REPO_ROOT / "recovery" / "docs" / name).exists(), name
    for name in DELETED:
        assert not (REPO_ROOT / "recovery" / "docs" / name).exists(), name
    # The audit-gate family stays where the audit gate reads it.
    for rel in DEV_ONLY_RECOVERY_FILES:
        assert (REPO_ROOT / "recovery" / rel).is_file(), rel
    assert {r.rsplit("/", 1)[-1] for r in DEV_ONLY_RECOVERY_FILES} | set(
        MOVED
    ) | set(DELETED) == set(DEV_RECORDS)
