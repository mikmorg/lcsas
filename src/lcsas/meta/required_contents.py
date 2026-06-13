"""Required-contents contract for the meta-volume (RST-05).

A meta-volume is the rescue disc: per-target tier-1 binaries
(``lcsas-restore``), the upstream ``rustic-static`` hedge, bundled
CPython trees, the key-share combiner, and the restore scripts an heir
double-clicks.  Historically every per-target artifact was treated as
*optional* — a missing binary was a silent ``continue``, and
``_regenerate_recovery_manifest`` then rebuilt the manifest from whatever
happened to be present, so ``lcsas meta verify`` validated an incomplete
bundle as PASS.  An operator could burn a "rescue" disc missing the
Windows / macOS / aarch64 binaries with zero failing gate.

This module is the single source of truth for *what a complete
meta-volume must contain*.  It is imported by:

* ``meta/builder.py`` — the ``_bundle_tier1_binaries`` target map, and
  the post-build completeness gate (``cmd_meta_build --require-complete``).
* ``cli/main.py`` — ``cmd_meta_verify`` runs the same existence check so
  verify catches both "never bundled" and "manifest self-referentially
  regenerated".
* ``tests/recovery_hardening/test_meta_bundling_completeness.py`` — drives
  its per-target assertions from ``required_meta_paths`` so the test and
  the builder cannot drift.

The contract reflects the 2026-06 required-contents agreement: all six
approved rust triples (``docs/CROSS_PLATFORM_META_RFC.md`` §6 Q6) plus the
root-level restore artifacts.  Older meta discs that predate this contract
will (correctly) report ABSENT for targets they never shipped — that is
the desired honest signal, not a false alarm.
"""

from __future__ import annotations

# The six approved rust triples — single source of truth.  Mirrored into
# `_bundle_tier1_binaries.tier1_map` (which also carries the short-arch
# directory + exe name) and the recovery-hardening test.
APPROVED_TARGETS: tuple[str, ...] = (
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "armv7-unknown-linux-gnueabihf",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-gnu",
)

# Windows targets carry .exe suffixes; everything else is bare.
_WINDOWS_TARGETS = frozenset({"x86_64-pc-windows-gnu"})

# Root-level artifacts every complete meta-volume must carry.  These live
# outside recovery/ (so the recovery/ manifest never covers them) but are
# load-bearing for the restore flow an heir actually uses.
_ROOT_ARTIFACTS: tuple[str, ...] = (
    "standalone_restorer.py",
    "keyshare_combine.py",
    "START_HERE.txt",
    "recovery/scripts/restore.sh",
    "restore.bat",
    "tools",
)


def _tier1_exe(target: str) -> str:
    return "lcsas-restore.exe" if target in _WINDOWS_TARGETS else "lcsas-restore"


def _rustic_name(target: str) -> str:
    return "rustic-static.exe" if target in _WINDOWS_TARGETS else "rustic-static"


def python_tree_marker(target: str) -> str:
    """Relative path proving the bundled CPython tree for ``target`` is real.

    python-build-standalone extracts a ``bin/python3`` on Linux/macOS and
    a top-level ``python.exe`` on Windows; the bundler copies the whole
    tree under ``recovery/bin/<target>/python/`` either way.
    """
    base = f"recovery/bin/{target}/python"
    if target in _WINDOWS_TARGETS:
        return f"{base}/python.exe"
    return f"{base}/bin/python3"


def required_target_paths(target: str) -> list[str]:
    """Relative paths a complete meta-volume must contain for one target."""
    return [
        f"recovery/bin/{target}/{_tier1_exe(target)}",
        f"recovery/bin/{target}/{_rustic_name(target)}",
        python_tree_marker(target),
    ]


def required_meta_paths() -> list[str]:
    """Every relative path a complete meta-volume must contain.

    Ordered targets-first (grouped per triple), then root artifacts, so
    callers can report missing items grouped by target.
    """
    paths: list[str] = []
    for target in APPROVED_TARGETS:
        paths.extend(required_target_paths(target))
    paths.extend(_ROOT_ARTIFACTS)
    return paths


# Human-facing note printed by both gates so an operator understands an
# ABSENT verdict against an older disc is expected, not a defect.
VINTAGE_NOTE = (
    "this check reflects the 2026-06 required-contents contract; older "
    "discs may predate it."
)
