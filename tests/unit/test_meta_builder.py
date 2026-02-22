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
        """restore.sh --help should work."""
        result = subprocess.run(
            ["bash", str(self.output / "restore.sh"), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--key" in result.stdout
        assert "--isos" in result.stdout

    def test_restore_script_has_cascades(self):
        """restore.sh should contain cascading extraction and rustic resolution."""
        content = (self.output / "restore.sh").read_text()
        # ISO extraction cascade
        assert "extract_iso" in content, "Missing extract_iso cascade function"
        assert "mount -o loop" in content, "Missing mount fallback"
        assert "7z x" in content, "Missing 7z fallback"
        # Rustic resolution cascade
        assert "rustic-static" in content, "Missing static rustic fallback"

    def test_no_pycache_in_source(self):
        """Bundled LCSAS source should not contain __pycache__."""
        pycache_dirs = list((self.output / "lcsas").rglob("__pycache__"))
        assert len(pycache_dirs) == 0, (
            f"Found __pycache__ in bundled source: {pycache_dirs}"
        )

    def test_start_here_generated(self):
        """Meta-volume should have a START_HERE.txt file."""
        path = self.output / "START_HERE.txt"
        assert path.is_file(), "START_HERE.txt not generated on meta-volume"
        content = path.read_text()
        assert "START HERE" in content
        assert "ENCRYPTION KEY" in content.upper() or "encryption key" in content.lower()

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
