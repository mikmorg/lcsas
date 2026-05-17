"""Unit tests for the meta-volume builder and tool bundler."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.meta.bundler import (
    ToolBundler,
    get_python_paths,
    get_shared_libs,
    resolve_binary,
)

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
        builder = MetaVolumeBuilder(output)
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
        """Missing cache root → bundler returns without error or output."""
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
        from lcsas.meta.builder import MetaVolumeBuilder

        cache_root = tmp_path / "cache"
        self._make_cache(cache_root, "aarch64-unknown-linux-musl")
        self._make_cache(cache_root, "x86_64-pc-windows-gnu", with_python=False)
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

        # The recovery source tree must exist for the parent method
        # to do its main copytree work.  Use the real repo's recovery/.
        repo_root = Path(__file__).resolve().parents[2]
        out = tmp_path / "meta"
        out.mkdir()
        b = MetaVolumeBuilder(
            out,
            project_root=repo_root,
            recovery_dir=repo_root / "recovery",
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
        # And no other approved target slipped in.
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

    def test_deferred_targets_skipped(self, tmp_path):
        """armv7, macOS targets are not in the mapping → even if
        source has analogous short-arch builds, they don't land
        on the meta volume.  Guards against an operator dropping
        a hand-built file into recovery/bin/<short> and being
        surprised it shipped without the cross-compile audit
        trail."""
        from lcsas.meta.builder import MetaVolumeBuilder

        # These short-arch names AREN'T in the TIER1_MAP — even
        # if the source recovery/bin had them, the bundler ignores
        # them by design.
        src = self._make_source_recovery(tmp_path, {
            "armv7": "lcsas-restore",
            "aarch64-darwin": "lcsas-restore",
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
