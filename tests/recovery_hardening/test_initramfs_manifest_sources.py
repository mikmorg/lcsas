"""BOOT-02: build_initramfs.sh must hard-fail on missing sources.

The pre-fix script wrote zero-byte placeholders for missing manifest
sources and exited 0 — producing a 'successful' initramfs whose
/bin/busybox (and every shell symlink onto it) was empty: a black
screen for the heir at boot.  These tests pin the inverted behavior:

* missing source  -> non-zero exit, ERROR on stderr, no output file;
* complete tree   -> exit 0 and an archive with no zero-byte regular
  file (validated by the same newc walker the ISO builder uses);
* placeholder archive -> rejected by ``_assert_no_empty_regular_files``.

The script lives under ``experimental/boot/`` post-BOOT-01; the locator
glob below survives either layout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lcsas.meta.bootable import _assert_no_empty_regular_files

pytestmark = pytest.mark.skipif(
    shutil.which("cpio") is None or shutil.which("gzip") is None,
    reason="requires cpio and gzip binaries",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_script() -> Path:
    """Locate build_initramfs.sh in either pre- or post-BOOT-01 layout."""
    for base in ("experimental", "recovery"):
        candidate = _REPO_ROOT / base / "boot" / "initramfs" / "build_initramfs.sh"
        if candidate.is_file():
            return candidate
    pytest.skip("build_initramfs.sh not present in this tree")


def _run_script(
    script: Path, arch: str, out: Path, root: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "RECOVERY_ROOT"}
    if root is not None:
        env["RECOVERY_ROOT"] = str(root)
    return subprocess.run(
        ["sh", str(script), arch, str(out)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_missing_source_fails_loud(tmp_path: Path) -> None:
    """Against the real repo tree (busybox vendored for no arch), the
    build must exit non-zero with an ERROR and create no output."""
    script = _build_script()
    out = tmp_path / "out.cpio.gz"
    proc = _run_script(script, "x86_64", out)
    assert proc.returncode != 0
    assert "ERROR: manifest source missing" in proc.stderr
    assert "placeholder" not in proc.stdout
    assert not out.exists()


def test_complete_tree_builds_clean(tmp_path: Path) -> None:
    """With every manifest `f` source present and non-empty, the build
    succeeds and the archive contains no zero-byte regular file."""
    script = _build_script()
    manifest = script.parent / "manifest.txt"
    root = tmp_path / "recovery-root"
    synth_manifest = root / "boot" / "initramfs" / "manifest.txt"
    synth_manifest.parent.mkdir(parents=True)
    shutil.copy2(manifest, synth_manifest)

    # Synthesize every `f` source as a 1-byte file.
    for raw in manifest.read_text().splitlines():
        line = raw.replace("{{ARCH}}", "x86_64").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] != "f":
            continue
        src = root / parts[1]
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"x")

    out = tmp_path / "initramfs-x86_64.cpio.gz"
    proc = _run_script(script, "x86_64", out, root=root)
    assert proc.returncode == 0, proc.stderr
    assert out.is_file() and out.stat().st_size > 0

    # The archive really contains the staged tree (guards against a
    # vacuously-empty cpio from a failed pipeline)...
    listing = subprocess.run(
        f"gzip -dc {out} | cpio -t",
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = {name.lstrip("./") for name in listing.splitlines()}
    assert "init" in entries
    assert "bin/busybox" in entries
    # ...and no zero-byte regular file survives (same newc walker the
    # bootable-ISO builder uses to reject placeholder archives).
    _assert_no_empty_regular_files(out)


def test_validate_inputs_rejects_placeholder_cpio(tmp_path: Path) -> None:
    """A cpio.gz containing a 0-byte regular file must be rejected."""
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bin").mkdir()
    (staging / "bin" / "busybox").touch()  # the placeholder
    (staging / "init").write_bytes(b"\x7fELF")

    archive = tmp_path / "initramfs-x86_64.cpio.gz"
    subprocess.run(
        f"find . | LC_ALL=C sort | cpio -o -H newc 2>/dev/null | gzip -n > {archive}",
        shell=True,
        cwd=staging,
        check=True,
    )

    with pytest.raises(ValueError, match="zero-byte regular files") as excinfo:
        _assert_no_empty_regular_files(archive)
    assert "busybox" in str(excinfo.value)

    # A clean archive of the same shape passes.
    (staging / "bin" / "busybox").write_bytes(b"x")
    clean = tmp_path / "clean.cpio.gz"
    subprocess.run(
        f"find . | LC_ALL=C sort | cpio -o -H newc 2>/dev/null | gzip -n > {clean}",
        shell=True,
        cwd=staging,
        check=True,
    )
    _assert_no_empty_regular_files(clean)


def test_not_a_newc_archive_rejected(tmp_path: Path) -> None:
    """Garbage (non-newc) gzipped input fails loud, not silent."""
    bogus = tmp_path / "bogus.cpio.gz"
    import gzip as _gzip

    bogus.write_bytes(_gzip.compress(b"definitely not a cpio archive...." * 8))
    with pytest.raises(ValueError, match="not a newc cpio archive"):
        _assert_no_empty_regular_files(bogus)


def test_no_placeholder_branch_in_script() -> None:
    """Acceptance: the placeholder-creation branch is gone for good."""
    script = _build_script()
    text = script.read_text()
    assert "placeholder zero-byte file" not in text
    assert "ERROR: manifest source missing or empty" in text
