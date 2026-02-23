"""Unit tests for the ToolBundler.bundle_python_package method."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from lcsas.meta.bundler import ToolBundler

# ── _find_installed_package tests ──────────────────────────────────


class TestFindInstalledPackage:
    """Tests for ``ToolBundler._find_installed_package``."""

    def test_finds_known_package(self):
        """Should locate a package that exists (pytest)."""
        result = ToolBundler._find_installed_package("pytest")
        assert result is not None
        assert result.is_dir()
        assert result.name == "pytest" or "pytest" in result.name

    def test_returns_none_for_missing_package(self):
        result = ToolBundler._find_installed_package("nonexistent_fake_pkg_12345")
        assert result is None

    def test_returns_none_when_import_fails(self):
        with patch("builtins.__import__", side_effect=ImportError("no")):
            result = ToolBundler._find_installed_package("os")
            assert result is None


# ── bundle_python_package tests ────────────────────────────────────


class TestBundlePythonPackage:
    """Tests for ``ToolBundler.bundle_python_package``."""

    @pytest.fixture
    def bundler(self, tmp_path):
        """Create a ToolBundler pointed at tmp_path."""
        return ToolBundler(tmp_path / "meta")

    def test_returns_none_for_missing_package(self, bundler):
        result = bundler.bundle_python_package("nonexistent_fake_pkg_12345")
        assert result is None

    def test_bundles_real_package(self, bundler, tmp_path):
        """Bundle a known installed package and check it lands correctly."""
        # Create minimal bundled-Python lib dir structure so the method
        # can deposit the package
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        lib_dir = tmp_path / "meta" / "tools" / "lib"
        lib_dir.mkdir(parents=True)
        bundler._lib_dir = lib_dir

        # Use 'json' as a test target — it ships with stdlib and has
        # a well-known package directory
        # But json is not a package with __path__... Let's create a fake one.
        fake_pkg = tmp_path / "fakepkg"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("# fake\n")
        (fake_pkg / "module.py").write_text("x = 1\n")

        with patch.object(
            ToolBundler,
            "_find_installed_package",
            return_value=fake_pkg,
        ):
            result = bundler.bundle_python_package("fakepkg")

        assert result is not None
        assert result.is_dir()
        assert (result / "__init__.py").exists()
        assert (result / "module.py").exists()
        # Should be under lib/pythonX.Y/fakepkg/
        assert version in str(result)

    def test_skips_pycache(self, bundler, tmp_path):
        """__pycache__ directories should be excluded."""
        lib_dir = tmp_path / "meta" / "tools" / "lib"
        lib_dir.mkdir(parents=True)
        bundler._lib_dir = lib_dir

        fake_pkg = tmp_path / "pkg2"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("")
        pycache = fake_pkg / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.pyc").write_bytes(b"\x00")

        with patch.object(
            ToolBundler,
            "_find_installed_package",
            return_value=fake_pkg,
        ):
            result = bundler.bundle_python_package("pkg2")

        assert result is not None
        assert not (result / "__pycache__").exists()

    def test_idempotent(self, bundler, tmp_path):
        """Calling twice for the same package should not error."""
        lib_dir = tmp_path / "meta" / "tools" / "lib"
        lib_dir.mkdir(parents=True)
        bundler._lib_dir = lib_dir

        fake_pkg = tmp_path / "pkg3"
        fake_pkg.mkdir()
        (fake_pkg / "__init__.py").write_text("")

        with patch.object(
            ToolBundler,
            "_find_installed_package",
            return_value=fake_pkg,
        ):
            r1 = bundler.bundle_python_package("pkg3")
            r2 = bundler.bundle_python_package("pkg3")

        assert r1 is not None
        assert r2 is not None
        assert r1 == r2
