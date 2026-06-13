"""Unit tests for HolographicInjector.write_standalone_restorer."""

from __future__ import annotations

import ast
import shutil
import subprocess

import pytest

from lcsas.restore.standalone_builder import build_standalone
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


class TestStandaloneFloor:
    """The generated script must stay within its advertised 3.10 floor."""

    # Names that only exist in Python > 3.10 and would crash a true 3.10
    # interpreter at import-time.  The generated script is a stdlib-only
    # tier-3 last resort whose whole pitch is "runs on whatever Python is
    # already here" — RST-09.
    _POST_310_FROM_IMPORTS = {
        "datetime": {"UTC"},          # added 3.11
        "typing": {"Self", "Never", "assert_never", "LiteralString"},  # 3.11
    }
    _POST_310_MODULES = {"tomllib"}   # added 3.11
    # dotted attribute access markers (e.g. datetime.UTC used unqualified)
    _POST_310_BUILTINS = {"ExceptionGroup", "BaseExceptionGroup"}  # 3.11

    def test_generated_script_has_no_post_310_apis(self):
        """AST-walk the generated script for known >3.10 markers."""
        text = build_standalone()
        tree = ast.parse(text)
        offenders: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                banned = self._POST_310_FROM_IMPORTS.get(node.module or "")
                if banned:
                    for alias in node.names:
                        if alias.name in banned:
                            offenders.append(
                                f"from {node.module} import {alias.name}"
                            )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self._POST_310_MODULES:
                        offenders.append(f"import {alias.name}")
            elif isinstance(node, ast.Name):
                if node.id in self._POST_310_BUILTINS:
                    offenders.append(node.id)

        assert not offenders, (
            "Generated standalone script uses post-3.10 APIs "
            f"(violates advertised 3.10 floor): {sorted(set(offenders))}"
        )

    def test_generated_script_compiles_under_python310(self, tmp_path):
        """If a python3.10 binary is on PATH, py_compile the script under it."""
        py310 = shutil.which("python3.10")
        if py310 is None:
            pytest.skip("python3.10 not on PATH")
        script = tmp_path / "standalone_restorer.py"
        script.write_text(build_standalone())
        result = subprocess.run(
            [py310, "-m", "py_compile", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"py_compile under python3.10 failed:\n{result.stderr}"
        )
