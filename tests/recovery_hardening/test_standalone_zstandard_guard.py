"""Hardening test: standalone_restorer.py's `import zstandard` guard.

Issue #239 — the primary fix wires `PYTHONPATH` so the bundled CPython
in tier-3 can find the bundled zstandard.  This test is the
DEFENCE-IN-DEPTH layer: even when PYTHONPATH wiring fails on some
weird host (zstandard removed, ABI mismatch with the bundled python,
the meta builder skipped the bundle step…), the `import zstandard`
guard in restic_fallback.py must still let the standalone script
START.

The standalone script SHOULD:
  - degrade `_HAS_ZSTD` to False on a truly-missing zstandard,
  - reach the CLI / `_cli_main()`,
  - raise a clear RuntimeError mentioning "zstandard" only when a zstd
    blob is actually decompressed.

What this catches:
  - A future refactor that hoists `zstandard` to a top-level import
    (or `from zstandard import …`) above the try/except.
  - A `standalone_builder.py` regex change that accidentally strips
    the try/except wrapper.
  - Any other path where `import zstandard` becomes load-bearing
    BEFORE _cli_main().
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _generate_standalone(tmp_path: Path) -> Path:
    """Generate standalone_restorer.py via build_standalone()."""
    # Run build_standalone in a subprocess against the in-tree src/
    # so we don't depend on the test runner having lcsas pip-installed.
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src"
    out_path = tmp_path / "standalone_restorer.py"
    res = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(src_dir)!r})
            from lcsas.restore.standalone_builder import build_standalone
            import pathlib
            pathlib.Path({str(out_path)!r}).write_text(build_standalone())
        """)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, (
        f"build_standalone failed: {res.stderr}"
    )
    assert out_path.is_file()
    return out_path


def _empty_pythonpath(tmp_path: Path) -> Path:
    """An empty dir to point PYTHONPATH at -- nothing importable here."""
    d = tmp_path / "empty_pythonpath"
    d.mkdir()
    return d


def test_standalone_does_not_crash_when_zstandard_missing(tmp_path: Path) -> None:
    """`python3 standalone_restorer.py --info ...` must NOT raise
    ImportError when zstandard is not installed -- the try/except
    guard in restic_fallback.py is supposed to absorb that and set
    _HAS_ZSTD=False."""
    script = _generate_standalone(tmp_path)
    empty_pp = _empty_pythonpath(tmp_path)

    # A minimal "repo" that the CLI will accept far enough to fail on
    # missing keys (NOT on import).
    repo = tmp_path / "repo"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    pw = tmp_path / "pw"
    pw.write_text("test\n")
    target = tmp_path / "target"

    env = {
        **os.environ,
        # Force the interpreter to NOT find any user-site or env
        # zstandard.  PYTHONPATH points at an empty dir so any
        # pre-existing PYTHONPATH from the harness is overridden.
        "PYTHONPATH": str(empty_pp),
        "PYTHONNOUSERSITE": "1",
        "HOME": str(tmp_path),
    }

    res = subprocess.run(
        [sys.executable, str(script),
         "--info",
         "--repo", str(repo),
         "--password-file", str(pw),
         "--target", str(target)],
        capture_output=True, text=True, timeout=30, env=env,
    )

    # The script may exit non-zero -- the synthetic repo has no real
    # keys/index data -- but it MUST NOT crash with ImportError on
    # zstandard.  We assert the negative.
    combined = res.stdout + res.stderr
    assert "ImportError" not in combined or "zstandard" not in combined, (
        "standalone_restorer.py raised ImportError on zstandard -- "
        "the try/except guard in restic_fallback.py no longer survives "
        f"the build_standalone concatenation.\nstdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )
    # A bare ModuleNotFoundError on zstandard is also a regression.
    assert "ModuleNotFoundError" not in combined or "zstandard" not in combined, (
        "standalone_restorer.py raised ModuleNotFoundError on zstandard -- "
        "the import guard is broken.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_standalone_reports_zstd_dependency_only_on_decompress(
    tmp_path: Path,
) -> None:
    """When `--info` runs (no blob decompression needed), the script
    must NOT mention zstandard installation -- the runtime error must
    only fire on actual decompress calls, not at module load.

    Verifies: the guard's RuntimeError message
    ("zstandard Python package is not installed") is reachable only
    via _decompress_zstd, not via module-level execution.
    """
    script = _generate_standalone(tmp_path)
    empty_pp = _empty_pythonpath(tmp_path)

    repo = tmp_path / "repo"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    pw = tmp_path / "pw"
    pw.write_text("test\n")
    target = tmp_path / "target"

    env = {
        **os.environ,
        "PYTHONPATH": str(empty_pp),
        "PYTHONNOUSERSITE": "1",
        "HOME": str(tmp_path),
    }

    res = subprocess.run(
        [sys.executable, str(script),
         "--info",
         "--repo", str(repo),
         "--password-file", str(pw),
         "--target", str(target)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    combined = res.stdout + res.stderr
    # The fallback error message text must NOT appear -- we never
    # decompressed anything.
    assert "pip install zstandard" not in combined, (
        f"standalone_restorer.py surfaced the zstandard-missing error "
        f"during --info (no decompress should have happened).  Output:\n"
        f"{combined}"
    )


def test_build_standalone_preserves_import_guard() -> None:
    """Static check: the generated standalone script contains the
    try/except ImportError guard around `import zstandard`, and the
    `_HAS_ZSTD = False` fallback assignment is present.

    Pins the regex used by build_standalone() so a future refactor
    can't silently strip the guard via overzealous import-rewriting.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_sb",
        Path(__file__).resolve().parents[2]
        / "src" / "lcsas" / "restore" / "standalone_builder.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.build_standalone()

    # The try must appear BEFORE the import.
    try_idx = text.find("try:\n    # zstandard is an optional dependency")
    assert try_idx >= 0, (
        "the 'try: # zstandard is an optional dependency' block has "
        "been removed or rewritten -- the import guard is broken."
    )
    import_idx = text.find("import zstandard")
    assert import_idx > try_idx, (
        "`import zstandard` no longer sits inside the try block."
    )
    # The except branch must set _HAS_ZSTD = False (otherwise the
    # guard 'works' but downstream code raises NameError on _HAS_ZSTD).
    except_idx = text.find("except ImportError:\n    _HAS_ZSTD = False")
    assert except_idx > import_idx, (
        "the `except ImportError: _HAS_ZSTD = False` fallback no longer "
        "follows the import."
    )
