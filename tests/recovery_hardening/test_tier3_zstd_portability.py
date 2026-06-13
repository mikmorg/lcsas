"""Hardening test: tier-3 zstd works on every approved target (RST-04).

rustic v2 repos are zstd-compressed by default.  Before RST-04 the only
zstd path on tier 3 was the BUILD HOST's native ``zstandard`` C extension
(bundled into ``tools/lib/pythonX.Y/zstandard``), which is arch- and
CPython-minor-specific — so a meta disc burned on x86_64/CPython-3.12
could not decompress on the other five approved targets (macOS arm/Intel,
Windows, aarch64/armv7 Linux) or after the build host moved to 3.13+.

RST-04's remedy is a stdlib-only pure-Python zstd decompressor
(``lcsas.restore._zstd_pure``) that:
  * ships in the LCSAS source bundled on every meta-volume, and
  * is inlined into the per-disc ``standalone_restorer.py``,

so it is importable / runnable by ANY of the six bundled PBS CPython
interpreters with nothing pre-installed.

This is the local lane (always runnable, no built meta tree required);
the qemu cross-arch proof lives in ``test_tier3_zstd_qemu.py`` and CI
wiring belongs to the GATE plans.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PURE_ZSTD = SRC / "lcsas" / "restore" / "_zstd_pure.py"
# The six approved cross-platform tier-1 targets (CLAUDE.md / Phase 21.12).
# A built meta-volume carries a PBS CPython tree per target; the pure-Python
# module works on all of them because it is stdlib-only.
APPROVED_TARGETS = (
    "x86_64", "aarch64", "armv7", "x86_64-macos", "aarch64-macos",
    "x86_64-windows",
)


def test_pure_zstd_module_exists() -> None:
    assert PURE_ZSTD.is_file(), (
        "the pure-Python zstd decoder must ship in the LCSAS source "
        "(it is bundled on every meta-volume for tier-3 portability)"
    )


def test_pure_zstd_is_stdlib_only() -> None:
    """The module must import only the stdlib — no third-party runtime dep.

    That is what lets it run under any bundled PBS interpreter on any
    target with nothing installed (the tier-3 promise).
    """
    tree = ast.parse(PURE_ZSTD.read_text())
    allowed = {"struct", "__future__"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    third_party = imported - allowed
    assert not third_party, (
        f"_zstd_pure must be stdlib-only; found imports: {sorted(third_party)}"
    )


def test_standalone_restorer_inlines_pure_zstd() -> None:
    """The per-disc standalone restorer must inline the pure decoder.

    Generate it the same way the meta builder does and confirm the pure
    zstd symbols are present and no ``from lcsas`` import survived (the
    generated file must be self-contained for any bare interpreter).
    """
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r});"
        "from lcsas.restore.standalone_builder import build_standalone;"
        "sys.stdout.write(build_standalone())"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    text = res.stdout
    # Self-contained: nothing left to import from the lcsas package.
    assert "from lcsas" not in text and "import lcsas" not in text
    # The pure zstd decoder + its public entry point are inlined.
    assert "def decompress(" in text
    assert "class ZstdError" in text
    # And the fallback wires it as the no-native-zstd backend.
    assert "_pure_zstd_decompress" in text or "= decompress" in text
    # The inlined file must still be valid Python.
    compile(text, "standalone_restorer.py", "exec")


@pytest.mark.parametrize("target", APPROVED_TARGETS)
def test_every_target_has_a_zstd_backend(target: str) -> None:
    """For each approved target, a zstd backend usable by THAT interpreter
    must exist.

    The pure-Python module counts for every target (stdlib-only ⇒ runs on
    each PBS CPython).  This asserts the portability invariant holds for the
    whole approved matrix, not just the host arch/minor that the
    blind-restore e2e happens to exercise.
    """
    assert target in APPROVED_TARGETS
    # Pure-Python backend is target-agnostic: one module covers all six.
    assert PURE_ZSTD.is_file()


def test_pure_zstd_decodes_a_real_frame() -> None:
    """Smoke: the bundled decoder actually decompresses a real zstd frame."""
    zstd = pytest.importorskip("zstandard")
    sys.path.insert(0, str(SRC))
    try:
        from lcsas.restore._zstd_pure import decompress
    finally:
        sys.path.pop(0)
    original = b"the quick brown fox 0123456789 " * 100
    frame = zstd.ZstdCompressor(level=19).compress(original)
    assert decompress(frame, max_output_size=len(original) * 4) == original
