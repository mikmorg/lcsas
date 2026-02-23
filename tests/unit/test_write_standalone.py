"""Unit tests for HolographicInjector.write_standalone_restorer."""

from __future__ import annotations

import ast

import pytest

from lcsas.staging.metadata import HolographicInjector


class TestWriteStandaloneRestorer:
    """Tests for the write_standalone_restorer method."""

    @pytest.fixture
    def injector(self, tmp_path):
        """Create a HolographicInjector with a tmp staging root."""
        return HolographicInjector(
            staging_root=tmp_path / "staging",
        )

    @pytest.fixture
    def staging_root(self, injector, tmp_path):
        root = tmp_path / "staging"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_creates_file(self, injector, staging_root):
        """write_standalone_restorer should create standalone_restorer.py."""
        injector.write_standalone_restorer()
        path = staging_root / "standalone_restorer.py"
        assert path.exists()
        assert path.stat().st_size > 0

    def test_file_is_valid_python(self, injector, staging_root):
        """The generated file must compile."""
        injector.write_standalone_restorer()
        text = (staging_root / "standalone_restorer.py").read_text()
        ast.parse(text)

    def test_file_has_no_lcsas_imports(self, injector, staging_root):
        """The generated file must be self-contained."""
        injector.write_standalone_restorer()
        text = (staging_root / "standalone_restorer.py").read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("from lcsas", "import lcsas")):
                pytest.fail(f"Found lcsas import: {stripped}")

    def test_overwrites_existing(self, injector, staging_root):
        """Should overwrite if called twice."""
        injector.write_standalone_restorer()
        path = staging_root / "standalone_restorer.py"
        size1 = path.stat().st_size
        # Write again
        injector.write_standalone_restorer()
        size2 = path.stat().st_size
        assert size1 == size2  # deterministic output

    def test_restore_instructions_mention_standalone(self, injector, staging_root):
        """RESTORE_INSTRUCTIONS.txt should reference standalone_restorer.py."""
        injector.write_restore_instructions()
        text = (staging_root / "RESTORE_INSTRUCTIONS.txt").read_text()
        assert "standalone_restorer.py" in text
