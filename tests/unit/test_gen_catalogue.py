"""Unit tests for tools/gen_hardening_catalogue.py (prints; never writes)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

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


def test_all_test_files_present_in_printed_table(capsys) -> None:
    expected_files = sorted(_TEST_DIR.glob("test_*.py"))
    assert expected_files, "No test_*.py files found; is the path correct?"
    assert GEN.run(_TEST_DIR) == 0
    out = capsys.readouterr().out
    assert out.startswith("| File | Catches |")
    for py_path in expected_files:
        assert f"`{py_path.name}`" in out


def test_run_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text('"""Catches a."""\n', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    GEN.run(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_no_docstring_placeholder(tmp_path: Path) -> None:
    (tmp_path / "test_has_doc.py").write_text(
        '"""Catches something important."""\n\nimport os\n', encoding="utf-8"
    )
    (tmp_path / "test_no_doc.py").write_text(
        "import os\n\ndef test_something():\n    pass\n", encoding="utf-8"
    )
    table = GEN.build_table(tmp_path)
    assert "| `test_no_doc.py` | (no description) |" in table
    assert "| `test_has_doc.py` | Catches something important. |" in table


def test_extract_description_takes_first_nonblank_line(tmp_path: Path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text('"""\n\nFirst line.\n\nSecond.\n"""\n', encoding="utf-8")
    assert GEN.extract_description(f) == "First line."


class TestArgs:
    def test_unknown_arg_fails_without_running(self, monkeypatch) -> None:
        def _no_run(*_args, **_kwargs):
            raise AssertionError("run() must not be reached on a bad argv")

        monkeypatch.setattr(GEN, "run", _no_run)
        for argv in (["--chek"], ["--dry-run"], ["--check"]):
            assert GEN.main(argv) != 0, argv

    def test_help_prints_usage(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(GEN, "run", lambda *_a, **_k: pytest.fail("ran"))
        assert GEN.main(["--help"]) == 0
        assert "stdout" in capsys.readouterr().out


def test_first_docstring_line_is_a_whole_sentence() -> None:
    """The printed catalogue shows only line 1, so it must stand alone (#894)."""
    bad = []
    for py_path in sorted(_TEST_DIR.glob("test_*.py")):
        doc = ast.get_docstring(ast.parse(py_path.read_text(encoding="utf-8")))
        first = next((ln.strip() for ln in (doc or "").splitlines() if ln.strip()), "")
        if not first.endswith((".", ".)", "?", "!")) or len(first) > 100:
            bad.append(f"{py_path.name}: {first!r}")
    assert not bad, "first docstring line must be one whole sentence:\n" + "\n".join(bad)
