"""Integration tests for the Python orchestrator -> recovery toolchain bridge.

Covers:

* ``RecoveryBuilder.build_host()`` produces a working lcsas-restore binary.
* ``RecoveryBuilder.run_tests()`` invokes the C test suite.
* ``RecoveryBuilder.write_manifest()`` produces a stable SHA-256 listing.
* ``MetaVolumeBuilder`` with ``bundle_recovery_toolchain=True`` copies
  the recovery tree onto the meta-volume.

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


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RECOVERY_DIR.is_dir() or shutil.which("cc") is None,
        reason="recovery/ tree or cc compiler not available",
    ),
]


@pytest.fixture(scope="session")
def isolated_recovery_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway, per-session copy of the live ``recovery/`` tree.

    Cross-build tests that PRODUCE per-arch binaries must not write into the
    committed ``recovery/bin/`` tree.  Doing so:

    * leaves the working tree dirty after every gate — zig's Mach-O / PE
      output is non-deterministic, so the macOS/Windows bins change bytes on
      each build (issue #320); and
    * races a concurrent ``meta build`` test that ``copytree``s the live
      ``recovery/`` tree — one run rewrites ``bin/<arch>/`` while the other
      walks it, a non-deterministic gate failure (issue #327).

    Building in a per-session copy keeps the tracked tree immutable, so gates
    are deterministic under concurrency and never dirty the checkout.  The
    copy excludes regenerable build artifacts to stay lean; ``make`` rebuilds
    them in the copy.
    """
    dst = tmp_path_factory.mktemp("recovery_build") / "recovery"
    shutil.copytree(
        RECOVERY_DIR,
        dst,
        ignore=shutil.ignore_patterns(
            "build", "build-*", "__pycache__", "*.o", "*.a", "*.pyc",
        ),
    )
    return dst


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


def test_recovery_builder_supports_armv7():
    """Phase 21.11: armv7 is in SUPPORTED_ARCHES and has a sensible
    default CC (the musl-cross-make hardfloat EABI prefix)."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    assert "armv7" in rb.SUPPORTED_ARCHES
    # Should NOT be in the Windows arch set.
    assert "armv7" not in rb._WINDOWS_ARCHES
    # The per-arch default CC should reflect the hardfloat EABI naming
    # convention (musleabihf, NOT plain musl-gcc which would fail).
    assert rb._DEFAULT_CC["armv7"] == "armv7-linux-musleabihf-gcc"


def test_recovery_builder_armv7_unknown_cc_raises_filenotfound():
    """When the cross-compiler for armv7 is not on PATH (typical on
    CI hosts), cross_build raises FileNotFoundError with the binary
    name so operators know what to install or override with --cc."""
    import pytest

    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    with pytest.raises(FileNotFoundError) as exc_info:
        rb.cross_build("armv7", cc="definitely-not-a-real-compiler-xyz")
    assert "definitely-not-a-real-compiler-xyz" in str(exc_info.value)


def test_recovery_builder_supports_macos_arches():
    """Phase 21.12: x86_64-macos and aarch64-macos are in
    SUPPORTED_ARCHES and recognized as macOS arches (handled by
    the dedicated zig-cc -target X-macos Makefile path)."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    assert "x86_64-macos" in rb.SUPPORTED_ARCHES
    assert "aarch64-macos" in rb.SUPPORTED_ARCHES
    assert "x86_64-macos" in rb._MACOS_ARCHES
    assert "aarch64-macos" in rb._MACOS_ARCHES
    # macOS arches must NOT be in the Windows set (different naming
    # convention, different output suffix).
    assert "x86_64-macos" not in rb._WINDOWS_ARCHES


def _ziglang_available() -> bool:
    import importlib.util as _u
    return _u.find_spec("ziglang") is not None


@pytest.mark.skipif(
    not _ziglang_available(),
    reason="ziglang module required for macOS cross-build",
)
def test_recovery_builder_cross_builds_macos(isolated_recovery_tree):
    """End-to-end: cross-compile the macOS Mach-O binary via
    `zig cc -target X-macos` (no Apple SDK needed).  Skipped when
    ziglang isn't installed.

    Builds in an isolated recovery copy so the non-deterministic Mach-O
    output never dirties the committed tree (#320/#327)."""
    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(isolated_recovery_tree)
    # Remove any stale artifact so we know this run produced it.
    stale = isolated_recovery_tree / "bin" / "x86_64-macos" / "lcsas-restore"
    if stale.exists():
        stale.unlink()

    a = rb.cross_build("x86_64-macos", verbose=False)
    assert a.arch == "x86_64-macos"
    assert a.lcsas_restore.is_file()
    # Isolation guard (#320/#327): the artifact lives in the throwaway
    # copy, never the committed recovery/ tree.
    assert RECOVERY_DIR not in a.lcsas_restore.parents
    # Mach-O binaries don't get the .exe suffix.
    assert a.lcsas_restore.name == "lcsas-restore"
    # iso9660 + init not produced for macOS (same as Windows).
    assert a.lcsas_iso9660 is None
    assert a.lcsas_init is None
    # And it really is a Mach-O binary (4-byte magic).
    with a.lcsas_restore.open("rb") as f:
        magic = f.read(4)
    # x86_64 Mach-O magic is CF FA ED FE (little-endian).
    assert magic == b"\xcf\xfa\xed\xfe", f"not Mach-O 64-bit: {magic!r}"


def test_recovery_builder_multi_token_cc_probes_first_word():
    """--cc 'zig cc -target X' should probe only the 'zig' binary,
    not the full string (which would never be on PATH).  Lets
    operators use zig cc with -target flags as the cross compiler."""
    import pytest

    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(RECOVERY_DIR)
    with pytest.raises(FileNotFoundError) as exc_info:
        rb.cross_build(
            "armv7",
            cc="definitely-not-zig -target armv7-linux-musleabihf",
        )
    # The error names just the binary, not the full multi-token string.
    msg = str(exc_info.value)
    assert "definitely-not-zig" in msg
    assert "-target" not in msg


@pytest.mark.skipif(
    not __import__("shutil").which("python3"),
    reason="ziglang module / python3 required for windows cross-build",
)
def test_recovery_builder_cross_builds_windows(isolated_recovery_tree):
    """End-to-end: cross-compile the Windows .exe via RecoveryBuilder.

    Builds in an isolated recovery copy so the non-deterministic PE output
    never dirties the committed tree (#320/#327)."""
    import importlib.util
    if importlib.util.find_spec("ziglang") is None:
        pytest.skip("ziglang not installed")

    from lcsas.recovery import RecoveryBuilder

    rb = RecoveryBuilder(isolated_recovery_tree)
    # Remove any stale artifact so we know this run produced it.
    stale = (
        isolated_recovery_tree / "bin" / "x86_64-windows" / "lcsas-restore.exe"
    )
    if stale.exists():
        stale.unlink()

    a = rb.cross_build("x86_64-windows", verbose=False)
    assert a.arch == "x86_64-windows"
    assert a.lcsas_restore.is_file()
    # Isolation guard (#320/#327): the artifact lives in the throwaway copy.
    assert RECOVERY_DIR not in a.lcsas_restore.parents
    assert a.lcsas_restore.name == "lcsas-restore.exe"
    # Windows binary isn't expected to ship lcsas-init (Linux PID 1 only).
    assert a.lcsas_init is None
