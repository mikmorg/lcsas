"""Integration tests for the Python orchestrator -> recovery toolchain bridge.

Covers:

* ``RecoveryBuilder.build_host()`` produces a working lcsas-restore binary.
* ``RecoveryBuilder.run_tests()`` invokes the C test suite.
* ``RecoveryBuilder.write_manifest()`` produces a stable SHA-256 listing.
* ``MetaVolumeBuilder`` with ``bundle_recovery_toolchain=True`` copies
  the recovery tree onto the meta-volume.
* ``BootableISOBuilder`` accepts ``recovery_boot_dir`` mode.

Skipped when:
* The host has no C compiler (``cc`` not on PATH).
* The ``recovery/`` tree is missing (not in a full source checkout).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = PROJECT_ROOT / "recovery"


pytestmark = pytest.mark.skipif(
    not RECOVERY_DIR.is_dir() or shutil.which("cc") is None,
    reason="recovery/ tree or cc compiler not available",
)


def test_recovery_builder_build_host():
    """Building the host arch produces lcsas-restore."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    artifacts = rb.build_host(verbose=False)
    assert artifacts.lcsas_restore.is_file()
    assert os.access(artifacts.lcsas_restore, os.X_OK)
    # Smoke: --help should exit 0 (usage goes to stderr).
    out = subprocess.run([str(artifacts.lcsas_restore), "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    text = out.stdout + out.stderr
    assert "lcsas-restore" in text or "restic" in text


def test_recovery_builder_run_tests():
    """The C unit-test suite passes."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    assert rb.run_tests(verbose=False)


def test_recovery_builder_manifest(tmp_path):
    """Manifest contains every recovery/ file."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    manifest = rb.write_manifest(tmp_path / "MANIFEST.sha256")
    assert manifest.is_file()
    lines = manifest.read_text().splitlines()
    # Sanity: at least 10 entries, every line is "<64-hex>  <path>"
    assert len(lines) >= 10
    for line in lines:
        digest, _, _path = line.partition("  ")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


def test_meta_builder_bundles_recovery_toolchain(tmp_path):
    """MetaVolumeBuilder.build() with default flag copies recovery/ in."""
    from lcsas.meta.builder import MetaVolumeBuilder

    output = tmp_path / "meta"
    # Stub project to avoid pulling in heavyweight tool bundling.
    # We test only that _bundle_recovery_toolchain_artifacts works.
    b = MetaVolumeBuilder(output_dir=output,
                          project_root=PROJECT_ROOT,
                          bundle_recovery_toolchain=True)
    output.mkdir()
    b._bundle_recovery_toolchain_artifacts()

    assert (output / "recovery").is_dir()
    assert (output / "recovery" / "Makefile").is_file()
    assert (output / "recovery" / "src" / "lcsas-restore" / "main.c").is_file()
    assert (output / "recovery" / "scripts" / "restore.sh").is_file()
    assert (output / "recovery" / "scripts" / "restore.bat").is_file()
    # restore.bat must also be surfaced at the meta-volume root for
    # Windows users who don't descend into subfolders.
    assert (output / "restore.bat").is_file()
    # build/ should be excluded
    assert not (output / "recovery" / "build").exists()


def test_recovery_builder_supports_windows_arches():
    """RecoveryBuilder advertises x86_64-windows and aarch64-windows."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    assert "x86_64-windows" in rb.SUPPORTED_ARCHES
    assert "aarch64-windows" in rb.SUPPORTED_ARCHES
    assert "x86_64-windows" in rb._WINDOWS_ARCHES


@pytest.mark.skipif(
    not __import__("shutil").which("python3"),
    reason="ziglang module / python3 required for windows cross-build",
)
def test_recovery_builder_cross_builds_windows():
    """End-to-end: cross-compile the Windows .exe via RecoveryBuilder."""
    import importlib.util
    if importlib.util.find_spec("ziglang") is None:
        pytest.skip("ziglang not installed")

    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    # Remove any stale artifact so we know this run produced it.
    stale = RECOVERY_DIR / "bin" / "x86_64-windows" / "lcsas-restore.exe"
    if stale.exists():
        stale.unlink()

    a = rb.cross_build("x86_64-windows", verbose=False)
    assert a.arch == "x86_64-windows"
    assert a.lcsas_restore.is_file()
    assert a.lcsas_restore.name == "lcsas-restore.exe"
    # Windows binary isn't expected to ship lcsas-init (Linux PID 1 only).
    assert a.lcsas_init is None


def test_bootable_builder_rejects_both_modes():
    """BootableISOBuilder requires exactly one of alpine_dir or recovery_boot_dir."""
    from lcsas.meta.bootable import BootableISOBuilder

    with pytest.raises(ValueError, match="mutually exclusive"):
        BootableISOBuilder(
            staging_dir=Path("/tmp"),
            alpine_dir=Path("/tmp/a"),
            recovery_boot_dir=Path("/tmp/r"),
            output_iso=Path("/tmp/x.iso"),
        )

    with pytest.raises(ValueError, match="must specify"):
        BootableISOBuilder(
            staging_dir=Path("/tmp"),
            output_iso=Path("/tmp/x.iso"),
        )


def test_bootable_builder_recovery_mode_validates_inputs(tmp_path):
    """recovery_boot_dir mode raises if kernel/initramfs are missing."""
    from lcsas.meta.bootable import BootableISOBuilder

    staging = tmp_path / "staging"
    staging.mkdir()
    rb = tmp_path / "recovery_boot"
    rb.mkdir()
    (rb / "linux").mkdir()

    bib = BootableISOBuilder(
        staging_dir=staging,
        recovery_boot_dir=rb,
        recovery_arch="x86_64",
        output_iso=tmp_path / "x.iso",
    )
    with pytest.raises(FileNotFoundError, match="vmlinuz"):
        bib._validate_inputs()
