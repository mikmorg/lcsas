"""Unit tests for tools/gen_hardening_catalogue.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the generator module without having it on sys.path already.
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
_GEN_SCRIPT = _TOOLS_DIR / "gen_hardening_catalogue.py"
_TEST_DIR = Path(__file__).parent.parent / "recovery_hardening"


def _load_gen():
    """Dynamically import tools/gen_hardening_catalogue as a module."""
    spec = importlib.util.spec_from_file_location("gen_hardening_catalogue", _GEN_SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


GEN = _load_gen()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllTestFilesPresent:
    """gen-catalogue table must contain every test_*.py in recovery_hardening."""

    def test_all_test_files_present(self, tmp_path: Path) -> None:
        # Copy the real README to a temp location so we don't mutate the real one.
        real_readme = _TEST_DIR / "README.md"
        temp_readme = tmp_path / "README.md"
        temp_readme.write_bytes(real_readme.read_bytes())

        GEN.run(_TEST_DIR, temp_readme, check_mode=False)
        generated = temp_readme.read_text(encoding="utf-8")

        expected_files = sorted(_TEST_DIR.glob("test_*.py"))
        assert expected_files, "No test_*.py files found — is the path correct?"

        for py_path in expected_files:
            assert py_path.name in generated, (
                f"{py_path.name} is missing from the generated catalogue table"
            )


class TestIdempotent:
    """Running the generator twice must produce bit-identical output."""

    def test_idempotent(self, tmp_path: Path) -> None:
        real_readme = _TEST_DIR / "README.md"
        temp_readme = tmp_path / "README.md"
        temp_readme.write_bytes(real_readme.read_bytes())

        GEN.run(_TEST_DIR, temp_readme, check_mode=False)
        first_pass = temp_readme.read_bytes()

        GEN.run(_TEST_DIR, temp_readme, check_mode=False)
        second_pass = temp_readme.read_bytes()

        assert first_pass == second_pass, "Generator output is not idempotent"


class TestCheckModeFailsWhenStale:
    """--check must exit non-zero when the README is out of date."""

    def test_check_mode_fails_when_stale(self, tmp_path: Path) -> None:
        real_readme = _TEST_DIR / "README.md"
        temp_readme = tmp_path / "README.md"

        # Write a README that is missing the last test file's row.
        content = real_readme.read_text(encoding="utf-8")
        # Remove one filename from the table section to make it stale.
        test_files = sorted(_TEST_DIR.glob("test_*.py"))
        assert test_files, "No test_*.py files found"
        # Remove the last file's row.
        last_name = test_files[-1].name
        stale_content = content.replace(f"| `{last_name}` |", "| `_removed_` |")
        # If the original didn't have that row (e.g. README has not been generated),
        # force-write a minimal stale table.
        if stale_content == content:
            # Just strip all table rows (keep header + separator only).
            lines = content.splitlines(keepends=True)
            out_lines = []
            in_table_rows = False
            for line in lines:
                if line.startswith("| File | Catches |"):
                    out_lines.append(line)
                    in_table_rows = True
                    continue
                if in_table_rows and line.startswith("|------"):
                    out_lines.append(line)
                    continue
                if in_table_rows and line.startswith("|"):
                    continue  # drop all data rows
                else:
                    in_table_rows = False
                    out_lines.append(line)
            stale_content = "".join(out_lines)

        temp_readme.write_text(stale_content, encoding="utf-8")

        exit_code = GEN.run(_TEST_DIR, temp_readme, check_mode=True)
        assert exit_code != 0, "--check should exit non-zero when README is stale"
        # The file must not have been modified in check mode.
        assert temp_readme.read_text(encoding="utf-8") == stale_content, (
            "--check mode must not modify the file"
        )


class TestNoDocstringPlaceholder:
    """A test file with no module docstring should produce '(no description)'."""

    def test_no_docstring_placeholder(self, tmp_path: Path) -> None:
        # Create a synthetic test dir with one file that has no docstring.
        synth_dir = tmp_path / "recovery_hardening"
        synth_dir.mkdir()

        # File with docstring.
        (synth_dir / "test_has_doc.py").write_text(
            '"""Catches something important."""\n\nimport os\n', encoding="utf-8"
        )
        # File without docstring.
        (synth_dir / "test_no_doc.py").write_text(
            "import os\n\ndef test_something():\n    pass\n", encoding="utf-8"
        )

        # We need a README.md in the temp dir with the Catalogue header.
        synth_readme = tmp_path / "README.md"
        synth_readme.write_text(
            "# Header\n\n## Catalogue\n\n| File | Catches |\n|------|---------|"
            "\n| `test_placeholder.py` | old |"
            "\n\n## Next Section\n\nContent.\n",
            encoding="utf-8",
        )

        GEN.run(synth_dir, synth_readme, check_mode=False)
        generated = synth_readme.read_text(encoding="utf-8")

        assert "test_no_doc.py" in generated
        assert "(no description)" in generated
        assert "test_has_doc.py" in generated
        assert "Catches something important." in generated
