"""Unit tests for the meta-volume builder and tool bundler."""

from __future__ import annotations

import dataclasses
import importlib as _importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import types as _types
from pathlib import Path

import pytest

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.meta import builder as _builder_mod
from lcsas.meta.builder import (
    MetaBuildError,
    MetaVolumeBuilder,
    _get_tool_version,
    pinned_dvdisaster_source_name,
)
from lcsas.meta.bundler import (
    ToolBundler,
    get_python_paths,
    get_shared_libs,
    resolve_binary,
)
from tests.unit.test_heir_doc_commands import _accepted_flags

# ── resolve_binary ───────────────────────────────────────────────


class TestResolveBinary:
    def test_finds_existing_binary(self):
        """Should find well-known system binaries."""
        p = resolve_binary("ls")
        assert p is not None
        assert p.is_file()

    def test_returns_none_for_nonexistent(self):
        assert resolve_binary("definitely_not_a_real_binary_xyz") is None

    def test_resolves_symlinks(self):
        p = resolve_binary("python3")
        if p is not None:
            # Should be the real path, not a symlink
            assert p == p.resolve()


# ── get_shared_libs ──────────────────────────────────────────────


class TestGetSharedLibs:
    def test_returns_list_for_known_binary(self):
        ls_path = resolve_binary("ls")
        if ls_path is None:
            pytest.skip("ls not found")
        libs = get_shared_libs(ls_path)
        assert isinstance(libs, list)

    def test_returns_empty_for_nonexistent(self):
        libs = get_shared_libs(Path("/nonexistent/binary"))
        assert libs == []

    def test_skips_glibc_family(self):
        """Should NOT include libc.so, ld-linux, etc."""
        ls_path = resolve_binary("ls")
        if ls_path is None:
            pytest.skip("ls not found")
        libs = get_shared_libs(ls_path)
        lib_names = {lib.name for lib in libs}
        for name in lib_names:
            assert not name.startswith("libc.so"), f"libc should not be bundled: {name}"
            assert not name.startswith("ld-linux"), "ld-linux should not be bundled"


# ── get_python_paths ─────────────────────────────────────────────


class TestGetPythonPaths:
    def test_finds_python_and_stdlib(self):
        exe, stdlib = get_python_paths()
        assert exe.is_file()
        assert stdlib.is_dir()
        assert (stdlib / "os.py").is_file()

    def test_stdlib_has_sqlite3(self):
        _, stdlib = get_python_paths()
        assert (stdlib / "sqlite3").is_dir()


# ── ToolBundler ──────────────────────────────────────────────────


class TestToolBundler:
    def test_bundle_binary(self, tmp_path: Path):
        """Create a simple binary and verify it's copied."""
        # Create a minimal script as a "binary"
        fake_bin = tmp_path / "fake_tool"
        fake_bin.write_text("#!/bin/sh\necho hello\n")
        os.chmod(str(fake_bin), 0o755)

        out_dir = tmp_path / "bundle"
        bundler = ToolBundler(out_dir)
        dest = bundler.bundle_binary("fake_tool", fake_bin)

        assert dest.is_file()
        assert dest == out_dir / "bin" / "fake_tool"
        assert os.access(str(dest), os.X_OK)
        assert "fake_tool" in bundler.bundled

    def test_bundle_binary_not_found(self, tmp_path: Path):
        bundler = ToolBundler(tmp_path / "bundle")
        with pytest.raises(FileNotFoundError):
            bundler.bundle_binary("totally_nonexistent_binary_xyz")

    @pytest.mark.skipif(
        not resolve_binary("rustic"), reason="rustic not installed"
    )
    def test_bundle_rustic(self, tmp_path: Path):
        """Bundle the real rustic binary."""
        bundler = ToolBundler(tmp_path / "bundle")
        dest = bundler.bundle_binary("rustic")
        assert dest.is_file()

        # The bundled binary should be executable
        result = subprocess.run(
            [str(dest), "--version"],
            capture_output=True,
            text=True,
            env={"LD_LIBRARY_PATH": str(bundler.lib_dir), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "rustic" in result.stdout.lower()

    def test_bundle_python(self, tmp_path: Path):
        """Bundle Python and verify the stdlib is present."""
        bundler = ToolBundler(tmp_path / "bundle")
        dest = bundler.bundle_python()

        assert dest.is_file()
        assert dest.name == "python3"
        assert os.access(str(dest), os.X_OK)

        # stdlib should be in lib/
        import sys
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        stdlib = bundler.lib_dir / version
        assert stdlib.is_dir()
        assert (stdlib / "os.py").is_file()
        assert (stdlib / "json").is_dir()
        assert (stdlib / "sqlite3").is_dir()
        assert (stdlib / "pathlib.py").is_file()

        # lib-dynload should have _sqlite3
        dynload = stdlib / "lib-dynload"
        assert dynload.is_dir()
        sqlite_sos = list(dynload.glob("_sqlite3*"))
        assert len(sqlite_sos) >= 1

        # ── verify bundled Python actually runs ──────────────────
        env = {
            "LD_LIBRARY_PATH": str(bundler.lib_dir),
            "PYTHONHOME": str(bundler.root),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(dest), "-c", "import sqlite3; print('ok')"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            f"Bundled Python failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.stdout.strip() == "ok"

        # ── verify test suites are stripped from stdlib ───────────
        assert not (stdlib / "test").exists(), "stdlib test/ should be excluded"
        assert not (stdlib / "tkinter").exists(), "tkinter should be excluded"


# ── MetaVolumeBuilder ───────────────────────────────────────────


@pytest.mark.skipif(
    not resolve_binary("rustic"), reason="rustic not installed"
)
@pytest.mark.skipif(
    not resolve_binary("xorriso"), reason="xorriso not installed"
)
class TestMetaVolumeBuilder:
    """Tests for the full meta-volume build.

    The build is expensive (~200 MB of bundled tools), so a single
    class-scoped fixture is shared across all tests instead of
    rebuilding per-test.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _build_meta(self, tmp_path_factory):
        """Build the meta-volume once for all tests in this class."""
        base = tmp_path_factory.mktemp("meta_builder")
        output = base / "meta"
        # allow_no_dvdisaster_source: this fixture asserts nothing about
        # the FMT-02 RS03 source tarball and must build on hosts with a
        # cold recovery cache. The dedicated bundling assertions live in
        # tests/recovery_hardening/test_meta_bundles_dvdisaster_source.py.
        builder = MetaVolumeBuilder(output, allow_no_dvdisaster_source=True)
        builder.build()
        # Store on the class so test methods can access them
        TestMetaVolumeBuilder._output = output
        TestMetaVolumeBuilder._builder = builder
        TestMetaVolumeBuilder._base = base
        yield
        # Eager cleanup — avoid retaining ~200 MB in pytest tmp dir
        import shutil
        shutil.rmtree(str(base), ignore_errors=True)

    @property
    def output(self) -> Path:
        return self._output

    def test_build_creates_directory_structure(self):
        """Build a meta-volume and verify all expected components."""
        # Restore script
        assert (self.output / "restore.sh").is_file()
        assert os.access(str(self.output / "restore.sh"), os.X_OK)

        # README
        assert (self.output / "README_RESTORE.md").is_file()

        # Volume info
        assert (self.output / "volume_info.json").is_file()
        vi = json.loads((self.output / "volume_info.json").read_text())
        assert vi["type"] == "meta"
        assert "rustic" in vi["contents"]["tools"]
        assert "xorriso" in vi["contents"]["tools"]
        assert "python3" in vi["contents"]["tools"]

        # Tool versions should be recorded
        assert "tool_versions" in vi["contents"]
        tv = vi["contents"]["tool_versions"]
        assert "python" in tv
        assert "rustic" in tv
        assert "xorriso" in tv

        # Tools
        assert (self.output / "tools" / "bin" / "rustic").is_file()
        assert (self.output / "tools" / "bin" / "xorriso").is_file()
        assert (self.output / "tools" / "bin" / "python3").is_file()
        assert (self.output / "tools" / "lib").is_dir()

        # LCSAS source
        assert (self.output / "lcsas" / "src" / "lcsas" / "__init__.py").is_file()
        assert (self.output / "lcsas" / "src" / "lcsas" / "meta" / "builder.py").is_file()

    def test_build_bundles_documentation(self):
        """Docs and README should be included."""
        # Project README
        if (self._builder.project_root / "README.md").is_file():
            assert (self.output / "README.md").is_file()

        # Architecture docs
        if (self._builder.project_root / "docs").is_dir():
            assert (self.output / "docs").is_dir()

    def test_restic_format_spec_bundled(self):
        """The restic format specification must be on every meta-volume."""
        spec = self.output / "docs" / "RESTIC_FORMAT_SPEC.md"
        assert spec.is_file(), "RESTIC_FORMAT_SPEC.md not bundled"
        content = spec.read_text()
        assert "AES-256-CTR" in content
        assert "scrypt" in content
        assert "Pack File Format" in content

    def test_bundled_rustic_works(self):
        """The bundled rustic binary should execute successfully."""
        rustic = self.output / "tools" / "bin" / "rustic"
        env = {
            "LD_LIBRARY_PATH": str(self.output / "tools" / "lib"),
            "HOME": str(self._base),
        }
        result = subprocess.run(
            [str(rustic), "--version"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0

    def test_bundled_xorriso_works(self):
        """The bundled xorriso binary should execute successfully."""
        xorriso = self.output / "tools" / "bin" / "xorriso"
        env = {
            "LD_LIBRARY_PATH": str(self.output / "tools" / "lib"),
            "HOME": str(self._base),
        }
        result = subprocess.run(
            [str(xorriso), "--version"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0

    def test_restore_script_is_valid_bash(self):
        """The generated restore.sh should pass bash syntax check."""
        result = subprocess.run(
            ["bash", "-n", str(self.output / "restore.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"restore.sh has syntax errors:\n{result.stderr}"
        )

    def test_restore_script_shows_help(self):
        """restore.sh --help should print usage and exit 0.

        The active driver is recovery/scripts/restore.sh (POSIX-sh, 3-tier
        cascade).  It prints a usage block to stderr and exits 0 when given
        --help; legacy --key / --isos flags are not part of the new
        contract.
        """
        result = subprocess.run(
            ["sh", str(self.output / "restore.sh"), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        out = result.stdout + result.stderr
        assert "restore.sh" in out
        # The new driver documents positional usage, recovery tree layout,
        # and the password env vars.
        assert "RECOVERY_ROOT" in out or "TARGET_DIR" in out
        assert "LCSAS_PASSWORD" in out or "LCSAS_PWFILE" in out

    def test_restore_script_has_cascades(self):
        """restore.sh should declare the 3-tier recovery cascade.

        Tier 1: prebuilt static lcsas-restore (C89).
        Tier 2: vendored rustic-static.
        Tier 3: Python fallback via standalone_restorer.py.
        """
        content = (self.output / "restore.sh").read_text()
        assert "lcsas-restore" in content, "Missing tier-1 lcsas-restore reference"
        assert "rustic-static" in content, "Missing tier-2 rustic-static reference"
        assert "standalone_restorer.py" in content, (
            "Missing tier-3 Python fallback reference"
        )
        assert "Tier 1" in content and "Tier 2" in content and "Tier 3" in content

    def test_no_pycache_in_source(self):
        """Bundled LCSAS source should not contain __pycache__."""
        pycache_dirs = list((self.output / "lcsas").rglob("__pycache__"))
        assert len(pycache_dirs) == 0, (
            f"Found __pycache__ in bundled source: {pycache_dirs}"
        )

    def test_start_here_generated(self):
        """Meta-volume should have a START_HERE.txt file.

        The fixture builds without an LCSASConfig, so the minimal
        START_HERE generator is used (no per-tenant key info).  We
        only validate the title block and that operating-system
        sections are present so the doc is usable.
        """
        path = self.output / "START_HERE.txt"
        assert path.is_file(), "START_HERE.txt not generated on meta-volume"
        content = path.read_text()
        assert "START HERE" in content
        # The minimal version covers OS-specific entry points.
        upper = content.upper()
        assert "WINDOWS" in upper
        assert "LINUX" in upper or "MACOS" in upper

    def test_start_here_boot_claim_matches_bootability(self):
        """UX-03: a default build (bootable=False) must not tell the
        heir to boot the disc — no build the CLI can produce is
        bootable — and must offer the borrow-a-computer + live-USB
        route instead.
        """
        content = (self.output / "START_HERE.txt").read_text()
        # Collapse the hard-wrapped 60-column layout so assertions are
        # not sensitive to where a phrase happens to wrap.
        flat = " ".join(content.split())
        assert "Boot directly from the disc" not in flat, (
            "START_HERE.txt promises a bootable disc on a non-bootable "
            "build (the boot-the-disc dead end of UX-03/BOOT-01)"
        )
        assert "NOT bootable" in flat, (
            "START_HERE.txt must state plainly that the disc is not "
            "bootable so the heir does not try the dead-end route"
        )
        assert "do NOT require a special computer" in flat, (
            "START_HERE.txt no-OS section must offer the "
            "borrow-a-computer route"
        )
        assert "live Linux USB" in flat, (
            "START_HERE.txt no-OS section must offer the live-USB route"
        )

    def test_readme_restore_txt_generated(self):
        """Meta-volume should have a plain-text README_RESTORE.txt."""
        path = self.output / "README_RESTORE.txt"
        assert path.is_file(), "README_RESTORE.txt not generated on meta-volume"
        content = path.read_text()
        # Should not contain Markdown formatting artifacts
        assert "## " not in content
        assert "**" not in content
        # Should have content from the Markdown version
        assert len(content) > 100

    def test_disc_care_txt_generated(self):
        """Meta-volume should have a DISC_CARE.txt file."""
        path = self.output / "DISC_CARE.txt"
        assert path.is_file(), "DISC_CARE.txt not generated on meta-volume"
        content = path.read_text()
        assert "DISC CARE" in content
        assert "M-DISC" in content

    def test_standalone_restorer_bundled(self):
        """standalone_restorer.py must be present at the meta-volume root."""
        sr = self.output / "standalone_restorer.py"
        assert sr.is_file(), "standalone_restorer.py not bundled on meta-volume"
        content = sr.read_text()
        # Should contain the CLI entry point
        assert "def _cli_main" in content
        assert "PurePythonRestorer" in content
        # Should be executable
        assert os.access(str(sr), os.X_OK)

    def test_restore_script_has_python_fallback(self):
        """restore.sh should reference the tier-3 Python fallback path."""
        content = (self.output / "restore.sh").read_text()
        assert "standalone_restorer.py" in content, (
            "restore.sh missing standalone_restorer.py reference"
        )
        # Tier 3 is opt-in via an explicit allow flag — the bare path
        # (tiers 1-2) stays Python-free.
        assert "LCSAS_ALLOW_PYTHON_TIER" in content, (
            "restore.sh missing Python tier gate"
        )
        assert "python3" in content, "restore.sh missing python3 invocation"

    def test_restore_script_has_pack_count_check(self):
        """restore_legacy.sh (kept for compat) checks pack count post-ingest.

        The active POSIX-sh driver no longer performs this check itself —
        each cascade tier exits non-zero on failure, which is sufficient.
        The legacy bash driver retains the explicit check.
        """
        legacy = self.output / "restore_legacy.sh"
        assert legacy.is_file(), "restore_legacy.sh not bundled on meta-volume"
        content = legacy.read_text()
        assert "ACTUAL_PACKS" in content, (
            "restore_legacy.sh missing post-ingest pack count check"
        )

    def test_inner_restore_sh_is_redirect_stub(self):
        """recovery/scripts/restore.sh on the meta disc must be a redirect stub.

        The canonical entry point is /restore.sh at the disc root.  The inner
        copy must exec that root script so agents who navigate into the recovery
        tree land on the same driver, not a silent duplicate.  Closes #94.
        """
        inner = self.output / "recovery" / "scripts" / "restore.sh"
        assert inner.is_file(), "recovery/scripts/restore.sh not present"
        content = inner.read_text()
        assert "exec" in content and "restore.sh" in content, (
            "recovery/scripts/restore.sh must redirect to root restore.sh"
        )
        root = self.output / "restore.sh"
        assert root.read_text() != content, (
            "recovery/scripts/restore.sh must differ from root restore.sh "
            "(should be redirect stub, not duplicate)"
        )

    def test_no_incomplete_marker_after_build(self):
        """After a successful build, .incomplete marker must be removed."""
        assert not (self.output / ".incomplete").exists(), (
            ".incomplete marker still present after successful build"
        )

    def test_single_drive_helper_bundled(self):
        """tools/restore_single_drive.py must be present and executable."""
        helper = self.output / "tools" / "restore_single_drive.py"
        assert helper.is_file(), "restore_single_drive.py not bundled in tools/"
        assert os.access(str(helper), os.X_OK)
        content = helper.read_text()
        # Sanity: subcommand names the bash wrapper depends on.
        assert "bootstrap" in content
        assert "ingest" in content
        assert "finalize" in content

    def test_keyshare_combiner_and_package_bundled(self):
        """K2.4(b): the production build ships the split-key combiner.

        Asserts the real ``build()`` output (not a direct helper call)
        carries (1) ``keyshare_combine.py`` at the meta-volume root, and
        (2) the ``keyshare`` package + its ``wordlist.txt`` under the
        bundled stdlib — the two halves the heir pre-step needs.
        """
        import sys as _sys

        combiner = self.output / "keyshare_combine.py"
        assert combiner.is_file(), "keyshare_combine.py not at meta-volume root"
        assert os.access(str(combiner), os.X_OK)

        version = f"python{_sys.version_info.major}.{_sys.version_info.minor}"
        pkg = self.output / "tools" / "lib" / version / "keyshare"
        assert pkg.is_dir(), "keyshare package not bundled under tools/lib/"
        assert (pkg / "wordlist.txt").is_file(), "SLIP-0039 wordlist not bundled"
        assert (pkg / "slip39.py").is_file()

    def test_restore_script_single_drive_default(self):
        """restore_legacy.sh drives the single-drive multi-disc UX.

        The single-drive disc-swap helper is part of the legacy bash
        driver; the new POSIX-sh driver delegates that responsibility
        to the C-based lcsas-restore binary.  We assert the legacy
        contract because that's where these markers still live.
        """
        legacy = self.output / "restore_legacy.sh"
        assert legacy.is_file()
        content = legacy.read_text()
        assert "INSERT DISC:" in content
        assert "restore_single_drive.py" in content
        assert 'MODE="single-drive"' in content
        assert "RESTORE COMPLETE" in content


# ── Lightweight tests for the single-drive bits (no rustic required) ──


class TestSingleDriveBitsStandalone:
    """Validate single-drive helper bundling and restore.sh content
    without invoking the full meta-volume build (which needs rustic).
    """

    def test_restore_script_constant_has_single_drive_dispatch(self):
        from lcsas.meta.builder import RESTORE_SCRIPT
        assert "INSERT DISC:" in RESTORE_SCRIPT
        assert 'MODE="single-drive"' in RESTORE_SCRIPT
        assert "restore_single_drive.py" in RESTORE_SCRIPT
        assert "RESTORE COMPLETE" in RESTORE_SCRIPT
        # Both modes still supported
        assert "--isos" in RESTORE_SCRIPT
        assert "--drive" in RESTORE_SCRIPT

    def test_restore_script_passes_bash_syntax(self, tmp_path):
        from lcsas.meta.builder import RESTORE_SCRIPT
        script = tmp_path / "restore.sh"
        script.write_text(RESTORE_SCRIPT)
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_bundle_restore_helper_writes_file(self, tmp_path):
        from lcsas.meta.builder import MetaVolumeBuilder
        b = MetaVolumeBuilder(tmp_path / "meta")
        (tmp_path / "meta").mkdir()
        b._bundle_restore_helper()
        dst = tmp_path / "meta" / "tools" / "restore_single_drive.py"
        assert dst.is_file()
        assert os.access(str(dst), os.X_OK)
        content = dst.read_text()
        assert "phase_bootstrap" in content
        assert "phase_ingest" in content
        assert "phase_finalize" in content


# ────────────────────────────────────────────────────────────────────
#  _bundle_upstream_binaries — Phase 21.1.b
# ────────────────────────────────────────────────────────────────────


class TestBundleUpstreamBinaries:
    """Tests for the per-target upstream-binary bundler.

    Uses a synthetic cache directory (no real rustic / python download
    required) to exercise the dispatch logic.
    """

    def _make_cache(self, root, target, *, with_rustic=True, with_python=True):
        """Populate root with fake cached files for one target."""
        if with_rustic:
            d = root / "rustic" / target
            d.mkdir(parents=True)
            (d / "rustic").write_text("#!/bin/sh\necho fake rustic\n")
            (d / "rustic").chmod(0o755)
        if with_python:
            d = root / "python" / target / "python" / "bin"
            d.mkdir(parents=True)
            (d / "python3").write_text("#!/bin/sh\necho fake python3\n")
            (d / "python3").chmod(0o755)
            # Add a stdlib placeholder so the tree looks real.
            (root / "python" / target / "python" / "lib").mkdir()

    def test_no_cache_dir_is_silent_skip(self, tmp_path, monkeypatch):
        """Missing cache root → the *bundler* returns without error or
        output.  This low-level tolerance is intentional: a partial cache
        must not crash the build.  Completeness is enforced one level up,
        by the RST-05 build gate in ``cmd_meta_build`` (default ON,
        escape hatch ``--allow-incomplete``) — see
        ``test_cli_comprehensive.TestCmdMetaBuildGate``."""
        from lcsas.meta.builder import MetaVolumeBuilder
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "nonexistent"))
        b = MetaVolumeBuilder(tmp_path / "meta")
        recovery_dst = tmp_path / "meta" / "recovery"
        recovery_dst.mkdir(parents=True)
        # Must not raise.
        b._bundle_upstream_binaries(recovery_dst)
        # And must not create any bin/ subdirectory.
        assert not (recovery_dst / "bin").exists()

    def test_single_target_cached(self, tmp_path, monkeypatch):
        """A cache holding one target produces bin/<target>/rustic-static
        and bin/<target>/python/."""
        from lcsas.meta.builder import MetaVolumeBuilder
        cache_root = tmp_path / "cache"
        self._make_cache(cache_root, "x86_64-unknown-linux-musl")
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        recovery_dst = tmp_path / "meta" / "recovery"
        recovery_dst.mkdir(parents=True)
        b = MetaVolumeBuilder(tmp_path / "meta")
        b._bundle_upstream_binaries(recovery_dst)

        target_dir = recovery_dst / "bin" / "x86_64-unknown-linux-musl"
        assert target_dir.is_dir()
        rustic = target_dir / "rustic-static"
        assert rustic.is_file()
        assert os.access(str(rustic), os.X_OK)
        py = target_dir / "python" / "bin" / "python3"
        assert py.is_file()
        assert os.access(str(py), os.X_OK)

    def test_unknown_targets_in_cache_are_ignored(self, tmp_path, monkeypatch):
        """Bundler iterates the *approved* target list, not the cache —
        random extra directories don't leak into the meta volume."""
        from lcsas.meta.builder import MetaVolumeBuilder
        cache_root = tmp_path / "cache"
        # Pollute the cache with an unapproved target.
        bogus = cache_root / "rustic" / "sparc64-unknown-linux-gnu"
        bogus.mkdir(parents=True)
        (bogus / "rustic").write_text("evil\n")
        # And an approved one to ensure the bundler does run.
        self._make_cache(cache_root, "aarch64-apple-darwin", with_python=False)
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        recovery_dst = tmp_path / "meta" / "recovery"
        recovery_dst.mkdir(parents=True)
        b = MetaVolumeBuilder(tmp_path / "meta")
        b._bundle_upstream_binaries(recovery_dst)

        # Approved target landed.
        assert (recovery_dst / "bin" / "aarch64-apple-darwin" / "rustic-static").is_file()
        # Unapproved target did NOT.
        assert not (recovery_dst / "bin" / "sparc64-unknown-linux-gnu").exists()

    def test_partial_cache_rustic_only(self, tmp_path, monkeypatch):
        """A target with only rustic (no python) still bundles rustic
        without creating a stray bin/<target>/python/ dir."""
        from lcsas.meta.builder import MetaVolumeBuilder
        cache_root = tmp_path / "cache"
        self._make_cache(
            cache_root, "armv7-unknown-linux-gnueabihf",
            with_rustic=True, with_python=False,
        )
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        recovery_dst = tmp_path / "meta" / "recovery"
        recovery_dst.mkdir(parents=True)
        b = MetaVolumeBuilder(tmp_path / "meta")
        b._bundle_upstream_binaries(recovery_dst)

        target_dir = recovery_dst / "bin" / "armv7-unknown-linux-gnueabihf"
        assert (target_dir / "rustic-static").is_file()
        assert not (target_dir / "python").exists()

    def test_all_six_targets_round_trip(self, tmp_path, monkeypatch):
        """When the cache holds every approved target, every target's
        bin/ subtree appears on the meta volume."""
        from lcsas.meta.builder import MetaVolumeBuilder
        cache_root = tmp_path / "cache"
        targets = [
            "x86_64-unknown-linux-musl",
            "aarch64-unknown-linux-musl",
            "armv7-unknown-linux-gnueabihf",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "x86_64-pc-windows-gnu",
        ]
        for t in targets:
            self._make_cache(cache_root, t)
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        recovery_dst = tmp_path / "meta" / "recovery"
        recovery_dst.mkdir(parents=True)
        b = MetaVolumeBuilder(tmp_path / "meta")
        b._bundle_upstream_binaries(recovery_dst)

        for t in targets:
            assert (recovery_dst / "bin" / t / "rustic-static").is_file(), t
            assert (recovery_dst / "bin" / t / "python" / "bin" / "python3").is_file(), t

    def test_orchestration_via_bundle_recovery_toolchain_artifacts(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end: call the orchestrating method, not the bundler
        helper directly, and confirm a synthetic multi-arch cache lands
        on the meta-volume in the expected location.

        This is the Phase 21.1.e integration check: proves the cache →
        ``_bundle_recovery_toolchain_artifacts`` → ``_bundle_upstream_binaries``
        wiring works, not just the leaf helper in isolation.
        """
        import shutil as _sh

        from lcsas.meta.builder import MetaVolumeBuilder

        cache_root = tmp_path / "cache"
        self._make_cache(cache_root, "aarch64-unknown-linux-musl")
        self._make_cache(cache_root, "x86_64-pc-windows-gnu", with_python=False)
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        # Use a COPY of the real recovery tree as the source.  We
        # delete its bin/ subtree first so the test sees a deterministic
        # state — the on-disk `recovery/bin/` may carry whatever the
        # developer or CI just cross-compiled (Phase 21.10.b–21.12),
        # which would otherwise leak into the test as "unexpected"
        # targets and make the assertion flaky.
        repo_root = Path(__file__).resolve().parents[2]
        src_recovery = tmp_path / "source_recovery"
        _sh.copytree(repo_root / "recovery", src_recovery, symlinks=True)
        bin_root = src_recovery / "bin"
        if bin_root.exists():
            _sh.rmtree(bin_root)

        out = tmp_path / "meta"
        out.mkdir()
        b = MetaVolumeBuilder(
            out,
            project_root=repo_root,
            recovery_dir=src_recovery,
        )
        b._bundle_recovery_toolchain_artifacts()

        # The recovery/ tree got copied to meta/recovery/...
        assert (out / "recovery" / "scripts" / "restore.sh").is_file()
        # ...AND the per-target binaries from our synthetic cache landed.
        arm = out / "recovery" / "bin" / "aarch64-unknown-linux-musl"
        assert arm.is_dir()
        assert (arm / "rustic-static").is_file()
        assert (arm / "python" / "bin" / "python3").is_file()
        win = out / "recovery" / "bin" / "x86_64-pc-windows-gnu"
        assert win.is_dir()
        assert (win / "rustic-static").is_file()
        # No python on the windows target (we asked for rustic-only).
        assert not (win / "python").exists()
        # And no other approved target slipped in.  (We cleared the
        # source bin/ above, so tier-1 bundling won't pick up any
        # locally-built lcsas-restore for these targets either.)
        for unexpected in (
            "x86_64-unknown-linux-musl",
            "armv7-unknown-linux-gnueabihf",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        ):
            assert not (out / "recovery" / "bin" / unexpected).exists(), unexpected


# ────────────────────────────────────────────────────────────────────
#  _bundle_tier1_binaries — Phase 21.10.b
# ────────────────────────────────────────────────────────────────────


class TestBundleTier1Binaries:
    """Tests for the tier-1 (lcsas-restore) cross-bundle step.

    Uses a synthetic source recovery/ tree (no real cross-compile
    required) to exercise the dispatch logic.
    """

    def _make_source_recovery(self, tmp_path, builds):
        """Synthesize a source recovery/ tree with pre-built binaries.

        ``builds`` is a dict mapping short-arch name → exe_name.
        Each entry creates ``<recovery>/bin/<short-arch>/<exe>``.
        """
        src = tmp_path / "source_recovery"
        src.mkdir()
        for short_arch, exe_name in builds.items():
            bin_dir = src / "bin" / short_arch
            bin_dir.mkdir(parents=True)
            bin_path = bin_dir / exe_name
            bin_path.write_text(f"#!/bin/sh\nfake {short_arch}\n")
            bin_path.chmod(0o755)
        return src

    def test_no_source_bin_dir_is_silent_skip(self, tmp_path):
        """No source `recovery/bin/` at all → no-op, no error."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = tmp_path / "source_recovery"
        src.mkdir()  # No bin/ subdir.
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)
        # No rust-triple dirs should appear under bin/.
        assert not (recovery_dst / "bin").exists() or not any(
            (recovery_dst / "bin").iterdir()
        )

    def test_linux_targets_mapped_and_copied(self, tmp_path):
        """Source has x86_64 + aarch64 short-arch builds → meta volume
        gets x86_64-unknown-linux-musl + aarch64-unknown-linux-musl
        copies."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "x86_64": "lcsas-restore",
            "aarch64": "lcsas-restore",
        })
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        assert (recovery_dst / "bin" / "x86_64-unknown-linux-musl" / "lcsas-restore").is_file()
        assert (recovery_dst / "bin" / "aarch64-unknown-linux-musl" / "lcsas-restore").is_file()

    def test_windows_target_mapped_with_exe_suffix(self, tmp_path):
        """Source has x86_64-windows/lcsas-restore.exe → meta volume
        gets bin/x86_64-pc-windows-gnu/lcsas-restore.exe."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "x86_64-windows": "lcsas-restore.exe",
        })
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        dst = recovery_dst / "bin" / "x86_64-pc-windows-gnu" / "lcsas-restore.exe"
        assert dst.is_file()

    def test_keyshare_combiner_relocated_alongside_tier1(self, tmp_path):
        """KEY-05: lcsas-keyshare[.exe] must land in the same rust-triple
        dir as lcsas-restore — the heir-facing Windows docs name a single
        on-disc bin dir (bin\\x86_64-pc-windows-gnu\\) for both binaries.
        Targets without a built keyshare binary are skipped silently."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "x86_64": "lcsas-restore",
            "x86_64-windows": "lcsas-restore.exe",
            "aarch64": "lcsas-restore",  # no keyshare build for this one
        })
        for short_arch, exe in (
            ("x86_64", "lcsas-keyshare"),
            ("x86_64-windows", "lcsas-keyshare.exe"),
        ):
            p = src / "bin" / short_arch / exe
            p.write_text(f"fake keyshare {short_arch}\n")
            p.chmod(0o755)
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        bin_root = recovery_dst / "bin"
        assert (bin_root / "x86_64-unknown-linux-musl" / "lcsas-keyshare").is_file()
        assert (bin_root / "x86_64-pc-windows-gnu" / "lcsas-keyshare.exe").is_file()
        # lcsas-restore still lands as before.
        assert (bin_root / "x86_64-pc-windows-gnu" / "lcsas-restore.exe").is_file()
        # Missing keyshare build → restore copied, keyshare skipped.
        assert (bin_root / "aarch64-unknown-linux-musl" / "lcsas-restore").is_file()
        assert not (bin_root / "aarch64-unknown-linux-musl" / "lcsas-keyshare").exists()

    def test_unmapped_short_arch_names_skipped(self, tmp_path):
        """Short-arch names NOT in the tier1_map (e.g. typo, or some
        future arch we haven't yet wired) are ignored even if the
        source recovery/bin had them.  Guards against an operator
        dropping a hand-built file into recovery/bin/<short> and
        being surprised it shipped without the cross-compile audit
        trail.

        Phase 21.12 promoted both macOS arches into the map; the
        only remaining unmapped names are typos or never-supported
        arches.  We use ``sparc64-linux`` here as an example —
        it's NOT in the map, so the bundler ignores it.
        """
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "sparc64-linux": "lcsas-restore",
            "ppc64-bsd": "lcsas-restore",
        })
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        # No rust-triple dirs populated.
        bin_root = recovery_dst / "bin"
        if bin_root.exists():
            assert not any(bin_root.iterdir())

    def test_macos_targets_now_mapped(self, tmp_path):
        """Phase 21.12 promotion guard: both macOS short-arch builds
        land at the right rust-triple paths."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "x86_64-macos": "lcsas-restore",
            "aarch64-macos": "lcsas-restore",
        })
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        assert (recovery_dst / "bin" / "x86_64-apple-darwin" / "lcsas-restore").is_file()
        assert (recovery_dst / "bin" / "aarch64-apple-darwin" / "lcsas-restore").is_file()

    def test_armv7_now_mapped(self, tmp_path):
        """Phase 21.11 promotion guard: armv7 source → armv7-unknown-
        linux-gnueabihf rust-triple landing on the meta volume.
        Symmetric with test_linux_targets_mapped_and_copied but
        ensures the new armv7 mapping specifically works."""
        from lcsas.meta.builder import MetaVolumeBuilder

        src = self._make_source_recovery(tmp_path, {
            "armv7": "lcsas-restore",
        })
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()

        b = MetaVolumeBuilder(out, recovery_dir=src)
        b._bundle_tier1_binaries(recovery_dst)

        dst = recovery_dst / "bin" / "armv7-unknown-linux-gnueabihf" / "lcsas-restore"
        assert dst.is_file()

    def test_orchestration_includes_tier1_and_manifest(self, tmp_path, monkeypatch):
        """End-to-end: _bundle_recovery_toolchain_artifacts runs
        _bundle_upstream_binaries → _bundle_tier1_binaries →
        _regenerate_recovery_manifest in that order.  The merged
        manifest must register the tier-1 binaries we put under
        bin/<rust-triple>/lcsas-restore[.exe]."""
        from lcsas.meta.builder import MetaVolumeBuilder

        # Synthesize an upstream cache with one target + a tier-1
        # build for the SAME target.
        cache = tmp_path / "cache"
        target = "x86_64-unknown-linux-musl"
        (cache / "rustic" / target).mkdir(parents=True)
        (cache / "rustic" / target / "rustic").write_text("#!fake\n")
        (cache / "rustic" / target / "rustic").chmod(0o755)
        (cache / "python" / target / "python" / "bin").mkdir(parents=True)
        (cache / "python" / target / "python" / "bin" / "python3").write_text("#!fake\n")
        (cache / "python" / target / "python" / "bin" / "python3").chmod(0o755)
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache))

        # Use the REAL recovery/ source tree so copytree finds
        # everything else (MANIFEST.sha256, etc.), but ALSO pre-seed
        # a fake bin/x86_64/lcsas-restore so the tier-1 bundler has
        # something to copy.  We do this by symlinking the real
        # tree and adding our fake binary on top.
        import shutil
        real_recovery = Path(__file__).resolve().parents[2] / "recovery"
        src_recovery = tmp_path / "source_recovery"
        shutil.copytree(real_recovery, src_recovery, symlinks=True)
        fake_bin_dir = src_recovery / "bin" / "x86_64"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_bin = fake_bin_dir / "lcsas-restore"
        fake_bin.write_text("#!/bin/sh\nfake C89 binary\n")
        fake_bin.chmod(0o755)

        out = tmp_path / "meta"
        out.mkdir()
        b = MetaVolumeBuilder(
            out,
            project_root=real_recovery.parent,
            recovery_dir=src_recovery,
        )
        b._bundle_recovery_toolchain_artifacts()

        # All three tier binaries landed under the rust-triple path.
        target_dir = out / "recovery" / "bin" / target
        assert (target_dir / "lcsas-restore").is_file(), "tier 1 missing"
        assert (target_dir / "rustic-static").is_file(), "tier 2 missing"
        assert (target_dir / "python" / "bin" / "python3").is_file(), "tier 3 missing"

        # And the merged manifest registers tier 1 too.
        manifest = (out / "recovery" / "MANIFEST.sha256").read_text()
        assert f"./bin/{target}/lcsas-restore" in manifest


# ────────────────────────────────────────────────────────────────────
#  _regenerate_recovery_manifest — Phase 21.4
# ────────────────────────────────────────────────────────────────────


class TestRegenerateRecoveryManifest:
    """Tests for the merged-manifest step that integrates bundled
    upstream binaries into recovery/MANIFEST.sha256 on the meta volume.
    """

    def _seed_meta_recovery(self, tmp_path, *, with_manifest=True):
        """Build a synthetic meta-volume recovery/ subtree.

        Returns (output, recovery_dst).  The source recovery/MANIFEST
        carries one pre-existing entry so we can assert it survives.
        """
        out = tmp_path / "meta"
        out.mkdir()
        recovery_dst = out / "recovery"
        recovery_dst.mkdir()
        if with_manifest:
            # Pre-existing entry (mimics a file authored by us — must
            # survive the merge step).
            (recovery_dst / "VERSION").write_text("1.0.0\n")
            import hashlib
            sha = hashlib.sha256(b"1.0.0\n").hexdigest()
            (recovery_dst / "MANIFEST.sha256").write_text(
                f"{sha}  ./VERSION\n"
            )
        return out, recovery_dst

    def _add_bundled_binary(self, recovery_dst, target, rel_path, content=b"\xca\xfe"):
        """Drop a synthetic bundled file under bin/<target>/<rel_path>."""
        dst = recovery_dst / "bin" / target / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)
        return dst

    def test_no_manifest_is_silent_skip(self, tmp_path):
        """If recovery/MANIFEST.sha256 doesn't exist, the step is a no-op
        rather than erroring."""
        from lcsas.meta.builder import MetaVolumeBuilder

        out, recovery_dst = self._seed_meta_recovery(tmp_path, with_manifest=False)
        b = MetaVolumeBuilder(out)
        # Must not raise; must not create a manifest out of thin air.
        b._regenerate_recovery_manifest(recovery_dst)
        assert not (recovery_dst / "MANIFEST.sha256").exists()

    def test_existing_entries_preserved(self, tmp_path):
        """Source-tree entries (./VERSION) survive even when there are
        no bundled binaries to merge."""
        from lcsas.meta.builder import MetaVolumeBuilder

        out, recovery_dst = self._seed_meta_recovery(tmp_path)
        b = MetaVolumeBuilder(out)
        b._regenerate_recovery_manifest(recovery_dst)

        content = (recovery_dst / "MANIFEST.sha256").read_text()
        assert "./VERSION" in content

    def test_bundled_binaries_added(self, tmp_path):
        """After bundling a per-target file under bin/<target>/, the
        manifest regen picks it up with the correct SHA-256."""
        from lcsas.meta.builder import MetaVolumeBuilder

        out, recovery_dst = self._seed_meta_recovery(tmp_path)
        self._add_bundled_binary(
            recovery_dst, "x86_64-unknown-linux-musl",
            "rustic-static", content=b"fake rustic body",
        )
        b = MetaVolumeBuilder(out)
        b._regenerate_recovery_manifest(recovery_dst)

        import hashlib
        expected_sha = hashlib.sha256(b"fake rustic body").hexdigest()
        content = (recovery_dst / "MANIFEST.sha256").read_text()
        assert (
            f"{expected_sha}  ./bin/x86_64-unknown-linux-musl/rustic-static"
            in content
        )
        # Source entry unchanged.
        assert "./VERSION" in content

    def test_idempotent(self, tmp_path):
        """Running the regen twice with no intervening change produces
        byte-identical output."""
        from lcsas.meta.builder import MetaVolumeBuilder

        out, recovery_dst = self._seed_meta_recovery(tmp_path)
        self._add_bundled_binary(
            recovery_dst, "aarch64-apple-darwin", "rustic-static",
            content=b"darwin arm rustic",
        )
        b = MetaVolumeBuilder(out)
        b._regenerate_recovery_manifest(recovery_dst)
        first = (recovery_dst / "MANIFEST.sha256").read_text()
        b._regenerate_recovery_manifest(recovery_dst)
        second = (recovery_dst / "MANIFEST.sha256").read_text()
        assert first == second

    def test_stale_bin_entries_replaced(self, tmp_path):
        """If MANIFEST already carries an entry under ./bin/, regen drops
        the old one and writes the current SHA — no stale row survives.
        """
        import hashlib

        from lcsas.meta.builder import MetaVolumeBuilder

        out, recovery_dst = self._seed_meta_recovery(tmp_path)
        # Pre-seed a stale bin entry that doesn't match what we'll bundle.
        old_text = (recovery_dst / "MANIFEST.sha256").read_text()
        (recovery_dst / "MANIFEST.sha256").write_text(
            old_text +
            "deadbeef" * 8 + "  ./bin/x86_64-unknown-linux-musl/rustic-static\n"
        )

        # Now bundle a file with DIFFERENT content.
        self._add_bundled_binary(
            recovery_dst, "x86_64-unknown-linux-musl",
            "rustic-static", content=b"the real binary",
        )
        b = MetaVolumeBuilder(out)
        b._regenerate_recovery_manifest(recovery_dst)

        content = (recovery_dst / "MANIFEST.sha256").read_text()
        # Stale row is gone.
        assert "deadbeef" * 8 not in content
        # Real SHA is present.
        real_sha = hashlib.sha256(b"the real binary").hexdigest()
        assert (
            f"{real_sha}  ./bin/x86_64-unknown-linux-musl/rustic-static"
            in content
        )

    def test_orchestration_writes_merged_manifest(self, tmp_path, monkeypatch):
        """End-to-end: _bundle_recovery_toolchain_artifacts (the public
        orchestrator) calls into the merger and produces a manifest
        covering both source files AND per-target bundled binaries.
        """
        from lcsas.meta.builder import MetaVolumeBuilder

        cache_root = tmp_path / "cache"
        # One target with rustic + python.
        target = "x86_64-unknown-linux-musl"
        (cache_root / "rustic" / target).mkdir(parents=True)
        (cache_root / "rustic" / target / "rustic").write_text("#!fake\n")
        (cache_root / "rustic" / target / "rustic").chmod(0o755)
        (cache_root / "python" / target / "python" / "bin").mkdir(parents=True)
        (cache_root / "python" / target / "python" / "bin" / "python3").write_text(
            "#!fake\n"
        )
        (cache_root / "python" / target / "python" / "bin" / "python3").chmod(0o755)

        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        repo_root = Path(__file__).resolve().parents[2]
        out = tmp_path / "meta"
        out.mkdir()
        b = MetaVolumeBuilder(
            out,
            project_root=repo_root,
            recovery_dir=repo_root / "recovery",
        )
        b._bundle_recovery_toolchain_artifacts()

        manifest_text = (out / "recovery" / "MANIFEST.sha256").read_text()
        # Source entry survived (./.gitattributes is in the real
        # recovery/ tree on every checkout).
        assert "./.gitattributes" in manifest_text
        # Bundled rustic entry written.
        assert (
            f"./bin/{target}/rustic-static" in manifest_text
        )
        # Bundled python tree entry written.
        assert (
            f"./bin/{target}/python/bin/python3" in manifest_text
        )


# ────────────────────────────────────────────────────────────────────
#  START_HERE.txt — one generator for both variants (UX-05)
# ────────────────────────────────────────────────────────────────────


def _survivability_config(base: Path) -> LCSASConfig:
    """A production-shaped config exercising every survivability field."""
    return LCSASConfig(
        mirror_base_path=base / "mirror",
        staging_path=base / "staging",
        db_path=base / "db.db",
        archive_owner="Alice Archivist",
        archive_description="family photo and document backups",
        key_storage_hints="Check the fire safe.\nCheck the bank deposit box.",
        technical_contact="Bob the IT person <bob@example.org>",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        repositories={
            "family": RepositoryConfig(
                name="family",
                mirror_path=base / "mirror" / "family",
            ),
        },
    )


class TestMetaStartHereVariants:
    """UX-05: production (config) builds must get the per-OS START_HERE.

    Before the fix, the config branch reused the DATA-disc generator,
    whose text claimed Linux was required, carried no runnable command,
    and pointed at RESTORE_INSTRUCTIONS.txt — a file the meta builder
    never writes.  These tests run a partial build (only the steps that
    produce START_HERE.txt and the files it references), skipping the
    ~200 MB tool bundling so they stay always-on (no rustic/xorriso).
    """

    @pytest.fixture(scope="class")
    def meta_tree(self, tmp_path_factory):
        base = tmp_path_factory.mktemp("meta_start_here")
        out = base / "meta"
        out.mkdir()
        builder = MetaVolumeBuilder(out, config=_survivability_config(base))
        # Same relative order as build(): the recovery toolchain bundle
        # must land before _write_restore_script so the root restore.sh
        # is the POSIX driver and recovery/docs/ exists on the tree.
        builder._bundle_docs()
        builder._bundle_keyshare_combiner()
        builder._bundle_recovery_toolchain_artifacts()
        builder._write_restore_script()
        builder._write_start_here()
        yield out, builder
        shutil.rmtree(str(base), ignore_errors=True)

    def test_start_here_with_config_has_os_dispatch(self, meta_tree):
        """The config (production) variant must carry the per-OS dispatch,
        a path-qualified restore.sh command, the owner string, and no
        Linux-only claim."""
        out, builder = meta_tree
        content = (out / "START_HERE.txt").read_text(encoding="utf-8")
        flat = " ".join(content.split())

        assert "restore.bat" in content, "Windows route missing"
        assert re.search(r"sh +/\S+/restore\.sh", flat), (
            "no runnable restore.sh command with a path separator — a "
            "bare 'sh restore.sh' fails from a Terminal opened in $HOME"
        )
        assert "Alice Archivist" in content, "owner survivability field missing"
        assert "computer running Linux," not in flat, (
            "data-disc Linux-only claim leaked into the meta START_HERE"
        )
        assert "RESTORE_INSTRUCTIONS.txt" not in content, (
            "META text references RESTORE_INSTRUCTIONS.txt, which is "
            "only written to data discs"
        )

        # The no-config render keeps the same structure minus the
        # config fields.
        bare = builder._render_meta_start_here(None)
        for marker in (
            ">>> Windows 10 or 11 <<<",
            ">>> macOS <<<",
            ">>> Linux <<<",
            ">>> No working computer at all <<<",
        ):
            assert marker in content, f"config variant missing {marker!r}"
            assert marker in bare, f"no-config variant missing {marker!r}"
        assert "Alice Archivist" not in bare

    def test_start_here_references_only_files_present(self, meta_tree):
        """Generic guard: every file START_HERE.txt names must exist on
        the built meta tree (catches the RESTORE_INSTRUCTIONS.txt class
        of dangling reference)."""
        out, _builder = meta_tree
        content = (out / "START_HERE.txt").read_text(encoding="utf-8")

        refs: set[str] = set()
        for pattern in (
            # Doc paths: recovery/docs/RECOVER.txt, docs/KEY_SHARE_FORMAT.md,
            # and bare UPPER_CASE.txt files at the disc root.
            r"(?:[A-Za-z0-9_./-]+/)?[A-Z][A-Z0-9_]*\.(?:txt|md)",
            r"(?:[A-Za-z0-9_./$-]+/)?restore\.(?:sh|bat)",
            r"keyshare_combine\.py",
            r"standalone_restorer\.py",
        ):
            refs.update(re.findall(pattern, content))
        assert refs, "expected START_HERE.txt to reference on-disc files"

        missing = []
        for ref in sorted(refs):
            rel = ref.lstrip("/")
            # Mount-prefixed commands (/Volumes/<LABEL>/restore.sh,
            # /media/$USER/<LABEL>/restore.sh, /mnt/restore.sh) all
            # resolve to the disc root.
            if rel.endswith(("restore.sh", "restore.bat")):
                rel = rel.rsplit("/", 1)[-1]
            if not (out / rel).exists():
                missing.append(ref)
        assert not missing, (
            f"START_HERE.txt references files absent from the meta tree: {missing}"
        )

    def test_start_here_split_block_present_when_key_split(self, meta_tree):
        """key_split=True → the split-key pre-step appears, free of
        phantom restore.sh flags (UX-02 contract); single-key configs
        must not show it."""
        out, builder = meta_tree
        content = (out / "START_HERE.txt").read_text(encoding="utf-8")

        assert "SPLIT KEY — YOUR PASSWORD IS IN SHARE CARDS" in content
        assert "keyshare_combine.py" in content

        # Every --flag attached to a restore.sh command must be one
        # recovery/scripts/restore.sh actually accepts.
        accepted = _accepted_flags()
        flag_refs = {
            flag
            for line in content.splitlines()
            if "restore.sh" in line
            for flag in re.findall(r"--[a-z][a-z-]*", line)
        }
        phantom = flag_refs - accepted
        assert not phantom, (
            f"META START_HERE tells the heir to use restore.sh flag(s) "
            f"{sorted(phantom)} that restore.sh does not accept"
        )

        single_key = dataclasses.replace(
            _survivability_config(out), key_split=False
        )
        bare = builder._render_meta_start_here(single_key)
        assert "SHARE CARDS" not in bare, (
            "single-key archive must not show split-key instructions"
        )


# ── RST-04: loud failure when native zstandard is unbundleable ───


class TestZstdBundleGuard:
    """`_bundle_tools` must fail loud when native zstandard is absent.

    The native ``zstandard`` C extension is the FAST tier-3 path; the
    pure-Python decoder (lcsas.restore._zstd_pure) always ships, so the
    failure is about not silently shipping a slow-only disc when the
    operator could have had the fast path.
    """

    @staticmethod
    def _patch_heavy(monkeypatch):
        """No-op the binary/python bundling so the test stays light."""
        monkeypatch.setattr(ToolBundler, "bundle_binary", lambda self, t: None)
        monkeypatch.setattr(ToolBundler, "bundle_python", lambda self: None)

    def test_build_fails_when_zstandard_unbundleable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from lcsas.meta.builder import MetaBuildError

        self._patch_heavy(monkeypatch)
        # Pretend NOTHING is installed on the build host.
        monkeypatch.setattr(
            ToolBundler, "_find_installed_package", staticmethod(lambda name: None)
        )
        builder = MetaVolumeBuilder(tmp_path / "meta")
        with pytest.raises(MetaBuildError, match="zstandard"):
            builder._bundle_tools()

    def test_allow_no_zstd_suppresses_the_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._patch_heavy(monkeypatch)
        monkeypatch.setattr(
            ToolBundler, "_find_installed_package", staticmethod(lambda name: None)
        )
        builder = MetaVolumeBuilder(tmp_path / "meta", allow_no_zstd=True)
        # Should not raise; records that native zstd was not bundled.
        builder._bundle_tools()
        assert builder._native_zstd_bundled is False

    def test_volume_info_records_zstd_capability(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._patch_heavy(monkeypatch)
        monkeypatch.setattr(
            ToolBundler, "_find_installed_package", staticmethod(lambda name: None)
        )
        builder = MetaVolumeBuilder(tmp_path / "meta", allow_no_zstd=True)
        (tmp_path / "meta").mkdir()
        builder._bundle_tools()
        builder._write_volume_info()
        info = json.loads(
            (tmp_path / "meta" / "volume_info.json").read_text()
        )
        zstd = info["zstd_support"]
        # Pure-Python tier-3 zstd always available; native absent here.
        assert zstd["pure_python_zstd"] is True
        assert zstd["native_zstd"] is False
        assert zstd["native_zstd_arch"] is None


# ── Builder private-helper branch coverage (cycle 17) ────────────────
#
# These exercise the small error/edge branches of MetaVolumeBuilder's
# private bundling helpers directly, with crafted tmp fixtures and no
# rustic/xorriso on PATH — they pin the failure/skip paths that the
# full build() integration test never reaches.


class TestPinnedDvdisasterSourceName:
    def test_returns_none_when_manifest_absent(self, tmp_path: Path):
        # No UPSTREAM.sha256 in the recovery dir -> None (builder.py:64).
        assert pinned_dvdisaster_source_name(tmp_path) is None

    def test_skips_malformed_and_non_dvdisaster_lines(self, tmp_path: Path):
        # A comment, a blank line, a single-field (malformed) line, and a
        # valid non-dvdisaster pin -> the loop falls through to None
        # (builder.py:71 the <2-parts continue, builder.py:75 the return).
        (tmp_path / "UPSTREAM.sha256").write_text(
            "# a comment\n"
            "\n"
            "onlyonefield\n"
            "deadbeef  rustic/bin/rustic\n"
        )
        assert pinned_dvdisaster_source_name(tmp_path) is None

    def test_returns_basename_for_dvdisaster_pin(self, tmp_path: Path):
        (tmp_path / "UPSTREAM.sha256").write_text(
            "deadbeef  dvdisaster/src/dvdisaster-0.79.5.tar.bz2\n"
        )
        assert (
            pinned_dvdisaster_source_name(tmp_path)
            == "dvdisaster-0.79.5.tar.bz2"
        )


class TestGetToolVersionFailure:
    def test_unknown_when_binary_unrunnable(self, tmp_path: Path):
        # subprocess.run on a nonexistent path raises FileNotFoundError
        # (an OSError) for both --version and bare version attempts, so
        # the helper returns "unknown" (builder.py:175-177).
        assert _get_tool_version(tmp_path / "nope" / "ghost-bin") == "unknown"


class TestBuilderProperties:
    def test_output_and_project_root_properties(self, tmp_path: Path):
        b = MetaVolumeBuilder(tmp_path / "out", project_root=tmp_path)
        assert b.output_dir == tmp_path / "out"   # builder.py:1732
        assert b.project_root == tmp_path.resolve()


class TestMissingRequiredContents:
    def test_empty_output_reports_tools_dir_missing(self, tmp_path: Path):
        # tools is a directory-contract entry: an empty output tree makes
        # it (and every file entry) missing — hits the tools is_dir guard
        # (builder.py:1785) plus the file elif.
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(out, project_root=tmp_path)
        missing = b.missing_required_contents()
        assert "tools" in missing


class TestBundleDvdisasterSource:
    def _builder(self, tmp_path: Path, *, allow: bool) -> MetaVolumeBuilder:
        recovery = tmp_path / "recovery"
        recovery.mkdir()
        # UPSTREAM.sha256 present but with NO dvdisaster/src pin.
        (recovery / "UPSTREAM.sha256").write_text(
            "deadbeef  rustic/bin/rustic\n"
        )
        out = tmp_path / "out"
        out.mkdir()
        return MetaVolumeBuilder(
            out,
            project_root=tmp_path,
            recovery_dir=recovery,
            allow_no_dvdisaster_source=allow,
        )

    def test_returns_when_unpinned_and_allowed(self, tmp_path: Path):
        # name is None + allow flag -> silent return (builder.py:1929-1930).
        b = self._builder(tmp_path, allow=True)
        b._bundle_dvdisaster_source()  # must not raise

    def test_raises_when_unpinned_and_not_allowed(self, tmp_path: Path):
        # name is None + not allowed -> MetaBuildError (builder.py:1931).
        b = self._builder(tmp_path, allow=False)
        with pytest.raises(MetaBuildError, match="no dvdisaster source"):
            b._bundle_dvdisaster_source()


class TestBundleSourceAndDocs:
    def test_bundle_source_overwrites_existing_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # project_root with a src/ dir and a top-level file item; running
        # twice exercises the rmtree-of-existing-dir branch (builder.py:1878)
        # and the file-item branch (builder.py:1889-1891).
        proj = tmp_path / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "a.py").write_text("x = 1\n")
        (proj / "afile.txt").write_text("hello\n")
        monkeypatch.setattr(_builder_mod, "_SOURCE_ITEMS", ("src", "afile.txt"))
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(out, project_root=proj)
        b._bundle_source()
        b._bundle_source()  # second pass hits the rmtree-existing branch
        assert (out / "lcsas" / "src" / "a.py").read_text() == "x = 1\n"
        assert (out / "lcsas" / "afile.txt").read_text() == "hello\n"

    def test_bundle_docs_overwrites_existing_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # docs/ dir copied twice -> rmtree-existing branch (builder.py:1900).
        proj = tmp_path / "proj"
        (proj / "docs").mkdir(parents=True)
        (proj / "docs" / "guide.md").write_text("# guide\n")
        monkeypatch.setattr(_builder_mod, "_DOC_ITEMS", ("docs",))
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(out, project_root=proj)
        b._bundle_docs()
        b._bundle_docs()
        assert (out / "docs" / "guide.md").read_text() == "# guide\n"


class TestBundleRecoveryToolchainArtifacts:
    def test_returns_when_recovery_dir_absent(self, tmp_path: Path):
        # recovery_dir is not a directory -> silent return (builder.py:2053).
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(
            out, project_root=tmp_path, recovery_dir=tmp_path / "ghost"
        )
        b._bundle_recovery_toolchain_artifacts()  # must not raise
        assert not (out / "recovery").exists()

    def test_overwrites_existing_recovery_dst(self, tmp_path: Path):
        # An existing output recovery/ tree is rmtree'd then recopied
        # (builder.py:2057).
        recovery = tmp_path / "recovery"
        (recovery / "scripts").mkdir(parents=True)
        (recovery / "scripts" / "restore.sh").write_text("#!/bin/sh\n")
        out = tmp_path / "out"
        (out / "recovery").mkdir(parents=True)
        (out / "recovery" / "stale.txt").write_text("old\n")
        b = MetaVolumeBuilder(
            out, project_root=tmp_path, recovery_dir=recovery
        )
        b._bundle_recovery_toolchain_artifacts()
        # Stale file gone (tree was replaced), real script present.
        assert not (out / "recovery" / "stale.txt").exists()
        assert (out / "recovery" / "scripts" / "restore.sh").is_file()


class TestRegenerateRecoveryManifestComments:
    def test_preserves_comments_drops_stale_bin_rows(self, tmp_path: Path):
        # A MANIFEST with a comment, a kept source row, and a stale ./bin/
        # row: comment is preserved (builder.py:2318-2319), the bin row is
        # dropped and regenerated from the actual bin/ tree.
        out = tmp_path / "out"
        recovery_dst = out / "recovery"
        (recovery_dst / "bin" / "x86_64").mkdir(parents=True)
        (recovery_dst / "bin" / "x86_64" / "lcsas-restore").write_bytes(b"ELF")
        (recovery_dst / "MANIFEST.sha256").write_text(
            "# recovery manifest\n"
            "abc123  ./scripts/restore.sh\n"
            "stale99  ./bin/x86_64/old-binary\n"
        )
        b = MetaVolumeBuilder(out, project_root=tmp_path)
        b._regenerate_recovery_manifest(recovery_dst)
        text = (recovery_dst / "MANIFEST.sha256").read_text()
        assert "# recovery manifest" in text            # comment preserved
        assert "./scripts/restore.sh" in text           # source row kept
        assert "old-binary" not in text                 # stale bin dropped
        assert "./bin/x86_64/lcsas-restore" in text      # regenerated


class TestBundleMetadata:
    def test_returns_when_catalog_absent(self, tmp_path: Path):
        # catalog_db_path set but the file does not exist -> return
        # (builder.py:2369).
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(
            out, project_root=tmp_path, catalog_db_path=tmp_path / "ghost.db"
        )
        b._bundle_metadata()  # must not raise
        assert not (out / "metadata").exists()

    def test_copies_per_repo_rustic_metadata(self, tmp_path: Path):
        # A catalog with one repository whose mirror has config/keys
        # files and an index/ dir -> the copy loop runs (builder.py:2385-2396).
        mirror = tmp_path / "mirror" / "family"
        mirror.mkdir(parents=True)
        (mirror / "config").write_text("repo-config\n")
        (mirror / "keys").write_text("key-material\n")
        (mirror / "index").mkdir()
        (mirror / "index" / "idx0").write_text("idx\n")
        # snapshots intentionally absent -> exercises the skip path too.

        db = tmp_path / "catalog.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE repositories (repo_id TEXT, mirror_path TEXT)"
        )
        conn.execute(
            "INSERT INTO repositories VALUES (?, ?)",
            ("family", str(mirror)),
        )
        # A second repo whose mirror_path is not a directory -> skipped
        # (builder.py:2387), proving a stale catalog row can't crash the
        # bundle.
        conn.execute(
            "INSERT INTO repositories VALUES (?, ?)",
            ("gone", str(tmp_path / "no-such-mirror")),
        )
        conn.commit()
        conn.close()

        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(
            out, project_root=tmp_path, catalog_db_path=db
        )
        b._bundle_metadata()
        repo_dst = out / "metadata" / "family"
        assert (repo_dst / "config").read_text() == "repo-config\n"
        assert (repo_dst / "keys").read_text() == "key-material\n"
        assert (repo_dst / "index" / "idx0").read_text() == "idx\n"
        assert not (repo_dst / "snapshots").exists()


class TestWriteRestoreScriptFallback:
    def test_legacy_fallback_when_no_recovery_driver(self, tmp_path: Path):
        # No recovery/scripts/restore.sh in the output -> the else branch
        # writes the legacy bash heredoc as restore.sh (builder.py:2456-2457).
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(out, project_root=tmp_path)
        b._write_restore_script()
        script = out / "restore.sh"
        assert script.is_file()
        assert os.access(script, os.X_OK)

    def test_build_sha_unknown_when_git_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A bundled recovery driver is present, but git rev-parse raises ->
        # the SHA falls back to "unknown" (builder.py:2429-2430).
        out = tmp_path / "out"
        (out / "recovery" / "scripts").mkdir(parents=True)
        (out / "recovery" / "scripts" / "restore.sh").write_text(
            "#!/bin/sh\n# build @@BUILD_SHA@@ @@BUILD_DATE@@\n"
        )

        def _boom(*_a, **_k):
            raise OSError("git unavailable")

        monkeypatch.setattr(subprocess, "check_output", _boom)
        b = MetaVolumeBuilder(out, project_root=tmp_path)
        b._write_restore_script()
        text = (out / "restore.sh").read_text()
        assert "unknown" in text                  # SHA placeholder filled
        assert "@@BUILD_SHA@@" not in text         # placeholder replaced


class TestBundleUpstreamBinariesStaging:
    def test_returns_when_cache_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "ghost"))
        out = tmp_path / "out"
        out.mkdir()
        b = MetaVolumeBuilder(out, project_root=tmp_path)
        b._bundle_upstream_binaries(out / "recovery")  # cache missing -> noop
        assert not (out / "recovery" / "bin").exists()

    def test_stages_linux_tree_and_windows_flat_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Build a fake recovery-binaries cache:
        #  * a Linux target with a python/<target>/python/ TREE
        #    (exercises the directory-copy branch + its rmtree on re-run,
        #     builder.py:2161-2169 incl. 2165),
        #  * a Windows target with a flat python.exe install
        #    (exercises the alt-install branch builder.py:2176-2191).
        cache = tmp_path / "cache"
        # Linux tree install.
        lin = cache / "python" / "x86_64-unknown-linux-musl" / "python"
        (lin / "bin").mkdir(parents=True)
        (lin / "bin" / "python3").write_text("#!/bin/sh\n")
        # Windows flat install: python.exe at the target root, plus a
        # sibling dir + file, and tarball/marker files that must be skipped.
        win = cache / "python" / "x86_64-pc-windows-gnu"
        win.mkdir(parents=True)
        (win / "python.exe").write_bytes(b"MZ")
        (win / "Lib").mkdir()
        (win / "Lib" / "os.py").write_text("# stdlib\n")
        (win / "vcruntime.dll").write_bytes(b"\x00")
        (win / "cpython.tar.gz").write_bytes(b"skip-me")
        (win / ".extracted").write_text("marker\n")

        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache))
        out = tmp_path / "out"
        recovery_dst = out / "recovery"
        recovery_dst.mkdir(parents=True)
        b = MetaVolumeBuilder(out, project_root=tmp_path)

        b._bundle_upstream_binaries(recovery_dst)
        b._bundle_upstream_binaries(recovery_dst)  # re-run -> rmtree branches

        lin_dst = recovery_dst / "bin" / "x86_64-unknown-linux-musl" / "python"
        assert (lin_dst / "bin" / "python3").is_file()

        win_dst = recovery_dst / "bin" / "x86_64-pc-windows-gnu" / "python"
        assert (win_dst / "python.exe").is_file()
        assert (win_dst / "Lib" / "os.py").is_file()
        assert (win_dst / "vcruntime.dll").is_file()
        # Tarball + extraction marker are not copied into the meta tree.
        assert not (win_dst / "cpython.tar.gz").exists()
        assert not (win_dst / ".extracted").exists()


# ── ToolBundler edge-branch coverage (cycle 19) ──────────────────────


class TestBundlerEdgeBranches:
    def test_get_shared_libs_bare_absolute_path_lib(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # ldd output where a bundleable lib appears as a BARE absolute path
        # (no "=>", like the loader line) -> the elif branch appends it
        # (bundler.py:87-90).  The real loader is glibc-filtered, so this
        # path only fires for a synthetic non-glibc bare-path entry.
        from lcsas.meta import bundler as _bnd

        fake_so = tmp_path / "libwidget.so.1"
        fake_so.write_bytes(b"\x7fELF")

        class _Result:
            stdout = f"\t{fake_so} (0x00007f0000000000)\n"

        monkeypatch.setattr(_bnd.subprocess, "run", lambda *a, **k: _Result())
        libs = get_shared_libs(tmp_path / "anybin")
        assert fake_so.resolve() in libs

    def test_get_python_paths_sysconfig_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # base_prefix with no lib/<ver>/os.py -> fall through to the
        # sysconfig stdlib path (bundler.py:114-120).
        from lcsas.meta import bundler as _bnd

        monkeypatch.setattr(
            _bnd.sys, "base_prefix", "/nonexistent-prefix-xyz"
        )
        _exe, stdlib = get_python_paths()
        assert (stdlib / "os.py").is_file()

    def test_find_installed_package_single_file_module(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A bundleable name that imports to a single-file MODULE (has
        # __file__, no __path__) -> returns its parent (bundler.py:338-339).
        name = sorted(ToolBundler._BUNDLEABLE_PACKAGES)[0]
        fake = _types.ModuleType(name)
        fake.__file__ = "/tmp/fake_pkg/mod.py"  # no __path__
        monkeypatch.setattr(_importlib, "import_module", lambda _n: fake)
        assert ToolBundler._find_installed_package(name) == Path("/tmp/fake_pkg")

    def test_find_installed_package_no_path_no_file(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Module with neither __path__ nor __file__ -> None (bundler.py:341).
        name = sorted(ToolBundler._BUNDLEABLE_PACKAGES)[0]
        fake = _types.ModuleType(name)
        fake.__dict__.pop("__file__", None)
        monkeypatch.setattr(_importlib, "import_module", lambda _n: fake)
        assert ToolBundler._find_installed_package(name) is None

    def test_find_installed_package_rejects_unlisted(self):
        with pytest.raises(ValueError, match="not in the allowed bundle list"):
            ToolBundler._find_installed_package("requests")
