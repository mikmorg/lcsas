"""Unit tests for the standalone restorer builder."""

from __future__ import annotations

import ast
import re

import pytest

from lcsas.restore.standalone_builder import _strip_header, build_standalone

# ── _strip_header tests ────────────────────────────────────────────


class TestStripHeader:
    """Tests for the header-stripping helper."""

    def test_strips_single_line_docstring(self):
        src = '"""Module docstring."""\nimport os\n'
        result = _strip_header(src)
        assert result == ["import os"]

    def test_strips_multiline_docstring(self):
        src = '"""First line.\n\nMore info.\n"""\nimport os\n'
        result = _strip_header(src)
        assert result == ["import os"]

    def test_strips_future_import(self):
        src = "from __future__ import annotations\nimport os\n"
        result = _strip_header(src)
        assert result == ["import os"]

    def test_strips_leading_blank_lines(self):
        src = "\n\n\nimport os\n"
        result = _strip_header(src)
        assert result == ["import os"]

    def test_preserves_body_blank_lines(self):
        src = "import os\n\nimport sys\n"
        result = _strip_header(src)
        assert result == ["import os", "", "import sys"]

    def test_preserves_indentation(self):
        src = "def foo():\n    pass\n"
        result = _strip_header(src)
        assert result == ["def foo():", "    pass"]

    def test_triple_quote_inside_body_not_stripped(self):
        """A triple-quoted string in the body must NOT be stripped."""
        src = 'import os\n\nx = """hello"""\ny = 1\n'
        result = _strip_header(src)
        # The line should be preserved (may have escaping differences)
        assert any('hello' in line for line in result)
        assert "y = 1" in result

    def test_docstring_then_future_then_code(self):
        src = '"""Doc."""\nfrom __future__ import annotations\nimport os\n'
        result = _strip_header(src)
        assert result == ["import os"]

    def test_multiline_docstring_with_triple_quotes_inside(self):
        """Closing triple-quote of docstring should not re-enter skip mode."""
        src = '"""\nLine 1.\nLine 2.\n"""\nimport os\n'
        result = _strip_header(src)
        assert result == ["import os"]


# ── build_standalone tests ─────────────────────────────────────────


class TestBuildStandalone:
    """Tests for the full build_standalone() output."""

    @pytest.fixture(scope="class")
    def standalone_text(self):
        """Build the standalone text once for all tests in this class."""
        return build_standalone()

    def test_is_valid_python(self, standalone_text):
        """The output must compile without syntax errors."""
        ast.parse(standalone_text)

    def test_no_lcsas_imports(self, standalone_text):
        """The output must not reference the lcsas package."""
        matches = re.findall(r"^(?:from|import)\s+lcsas\b", standalone_text, re.MULTILINE)
        assert matches == [], f"Found lcsas imports: {matches}"

    def test_contains_aes_symbols(self, standalone_text):
        """Key symbols from _aes_pure.py must be present."""
        for symbol in ("aes_ctr", "aes_encrypt_block", "key_schedule"):
            assert symbol in standalone_text, f"Missing AES symbol: {symbol}"

    def test_contains_restorer_class(self, standalone_text):
        """PurePythonRestorer must be defined in the output."""
        assert "class PurePythonRestorer" in standalone_text

    def test_contains_cli_block(self, standalone_text):
        """The CLI entry point must be present."""
        assert "_cli_main" in standalone_text
        assert '__name__ == "__main__"' in standalone_text

    def test_starts_with_shebang(self, standalone_text):
        assert standalone_text.startswith("#!/usr/bin/env python3")

    def test_has_future_annotations(self, standalone_text):
        """Exactly one ``from __future__ import annotations`` line."""
        count = standalone_text.count("from __future__ import annotations")
        assert count == 1, f"Expected 1 __future__ import, found {count}"

    def test_minimum_length(self, standalone_text):
        """Sanity check: output should be substantial."""
        lines = standalone_text.splitlines()
        assert len(lines) > 500, f"Only {len(lines)} lines — too short"

    def test_no_duplicate_class_definitions(self, standalone_text):
        """Should not have duplicate class defs from bad concatenation."""
        class_defs = re.findall(r"^class (\w+)", standalone_text, re.MULTILINE)
        dupes = [c for c in class_defs if class_defs.count(c) > 1]
        assert dupes == [], f"Duplicate class definitions: {set(dupes)}"
