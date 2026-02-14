"""Unit tests for the meta-volume builder and tool bundler."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.meta.bundler import (
    ToolBundler,
    get_python_paths,
    get_shared_libs,
    resolve_binary,
)
from lcsas.meta.builder import MetaVolumeBuilder


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
            assert not name.startswith("ld-linux"), f"ld-linux should not be bundled"


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
        not resolve_binary("restic"), reason="restic not installed"
    )
    def test_bundle_restic(self, tmp_path: Path):
        """Bundle the real restic binary."""
        bundler = ToolBundler(tmp_path / "bundle")
        dest = bundler.bundle_binary("restic")
        assert dest.is_file()

        # The bundled binary should be executable
        result = subprocess.run(
            [str(dest), "version"],
            capture_output=True,
            text=True,
            env={"LD_LIBRARY_PATH": str(bundler.lib_dir)},
        )
        assert result.returncode == 0
        assert "restic" in result.stdout

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

    def test_bundled_python_executes(self, tmp_path: Path):
        """The bundled Python should actually run."""
        bundler = ToolBundler(tmp_path / "bundle")
        py = bundler.bundle_python()

        import sys
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"

        env = {
            "LD_LIBRARY_PATH": str(bundler.lib_dir),
            "PYTHONHOME": str(bundler.root),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(py), "-c", "import sqlite3; print('ok')"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            f"Bundled Python failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.stdout.strip() == "ok"

    def test_skips_test_suites_in_stdlib(self, tmp_path: Path):
        """The bundled stdlib should NOT contain test suites (saves ~30 MB)."""
        bundler = ToolBundler(tmp_path / "bundle")
        bundler.bundle_python()

        import sys
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        stdlib = bundler.lib_dir / version

        # Top-level test/ directory should be excluded
        assert not (stdlib / "test").exists()
        # tkinter should be excluded
        assert not (stdlib / "tkinter").exists()


# ── MetaVolumeBuilder ───────────────────────────────────────────


@pytest.mark.skipif(
    not resolve_binary("restic"), reason="restic not installed"
)
@pytest.mark.skipif(
    not resolve_binary("xorriso"), reason="xorriso not installed"
)
class TestMetaVolumeBuilder:
    def test_build_creates_directory_structure(self, tmp_path: Path):
        """Build a meta-volume and verify all expected components."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        # Restore script
        assert (output / "restore.sh").is_file()
        assert os.access(str(output / "restore.sh"), os.X_OK)

        # README
        assert (output / "README_RESTORE.md").is_file()

        # Volume info
        assert (output / "volume_info.json").is_file()
        vi = json.loads((output / "volume_info.json").read_text())
        assert vi["type"] == "meta"
        assert "restic" in vi["contents"]["tools"]
        assert "xorriso" in vi["contents"]["tools"]
        assert "python3" in vi["contents"]["tools"]

        # Tools
        assert (output / "tools" / "bin" / "restic").is_file()
        assert (output / "tools" / "bin" / "xorriso").is_file()
        assert (output / "tools" / "bin" / "python3").is_file()
        assert (output / "tools" / "lib").is_dir()

        # LCSAS source
        assert (output / "lcsas" / "src" / "lcsas" / "__init__.py").is_file()
        assert (output / "lcsas" / "src" / "lcsas" / "meta" / "builder.py").is_file()

    def test_build_bundles_documentation(self, tmp_path: Path):
        """Docs and README should be included."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        # Project README
        if (builder.project_root / "README.md").is_file():
            assert (output / "README.md").is_file()

        # Architecture docs
        if (builder.project_root / "docs").is_dir():
            assert (output / "docs").is_dir()

    def test_bundled_restic_works(self, tmp_path: Path):
        """The bundled restic binary should execute successfully."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        restic = output / "tools" / "bin" / "restic"
        env = {
            "LD_LIBRARY_PATH": str(output / "tools" / "lib"),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(restic), "version"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0

    def test_bundled_xorriso_works(self, tmp_path: Path):
        """The bundled xorriso binary should execute successfully."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        xorriso = output / "tools" / "bin" / "xorriso"
        env = {
            "LD_LIBRARY_PATH": str(output / "tools" / "lib"),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(xorriso), "--version"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0

    def test_restore_script_is_valid_bash(self, tmp_path: Path):
        """The generated restore.sh should pass bash syntax check."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        result = subprocess.run(
            ["bash", "-n", str(output / "restore.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"restore.sh has syntax errors:\n{result.stderr}"
        )

    def test_restore_script_shows_help(self, tmp_path: Path):
        """restore.sh --help should work."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        result = subprocess.run(
            ["bash", str(output / "restore.sh"), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--key" in result.stdout
        assert "--isos" in result.stdout

    def test_no_pycache_in_source(self, tmp_path: Path):
        """Bundled LCSAS source should not contain __pycache__."""
        output = tmp_path / "meta"
        builder = MetaVolumeBuilder(output)
        builder.build()

        pycache_dirs = list((output / "lcsas").rglob("__pycache__"))
        assert len(pycache_dirs) == 0, (
            f"Found __pycache__ in bundled source: {pycache_dirs}"
        )
