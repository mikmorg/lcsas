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

Two tiers (issue #367):

* **Required** (``required_meta_paths`` + per-repo ``metadata/<repo>/keys``)
  — restore-BLOCKING.  Missing any of these fails ``--require-complete``.
* **Recommended** (``recommended_meta_paths``: stock restic tier-2b hedge,
  lcsas-keyshare) — WARNS but does not fail: a disc with tier-1 + tier-3 is
  still recoverable, and stock restic legitimately absents on a cold cache.
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


def _ecc_name(target: str) -> str:
    # FMT-01: the in-house RS03 ECC verify/repair tool, bundled per
    # target so the heir-facing "scratched disc" repair path
    # (restore.sh --check-disc → lcsas-ecc) works off the meta disc with
    # no externally-installed dvdisaster.  Required, not optional.
    return "lcsas-ecc.exe" if target in _WINDOWS_TARGETS else "lcsas-ecc"


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
        f"recovery/bin/{target}/{_ecc_name(target)}",
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


def _restic_name(target: str) -> str:
    return "restic.exe" if target in _WINDOWS_TARGETS else "restic"


def _keyshare_name(target: str) -> str:
    return "lcsas-keyshare.exe" if target in _WINDOWS_TARGETS else "lcsas-keyshare"


def recommended_target_paths(target: str) -> list[str]:
    """Relative paths a complete meta-volume is RECOMMENDED to bundle for
    one target (issue #367).

    Unlike ``required_target_paths`` these are hedges, not restore-blockers:
    a disc with the tier-1 ``lcsas-restore`` and the tier-3 CPython path is
    still fully recoverable without them, so their absence WARNS rather than
    fails the completeness gate.

    * stock ``restic`` — the tier-2b "standard tools" hedge, sourced from the
      upstream cache (``recovery/UPSTREAM.sha256``), so it is legitimately
      absent on a cold-cache build.
    * ``lcsas-keyshare`` — the SLIP-0039 combiner, only needed to reconstruct
      a K-of-N split repo password.
    """
    return [
        f"recovery/bin/{target}/{_restic_name(target)}",
        f"recovery/bin/{target}/{_keyshare_name(target)}",
    ]


def recommended_meta_paths() -> list[str]:
    """Every relative path a complete meta-volume is recommended to bundle.

    Absence WARNS but does not fail ``--require-complete`` (see
    ``recommended_target_paths``).
    """
    paths: list[str] = []
    for target in APPROVED_TARGETS:
        paths.extend(recommended_target_paths(target))
    return paths


def is_per_repo_keys_gap(path: str) -> bool:
    """True if ``path`` names a per-repo ``metadata/<repo>/keys`` entry.

    ``MetaVolumeBuilder.missing_required_contents()`` appends these
    (issue #367) when a bundled repo's metadata carries no ``keys/``
    subtree — a survivability gap distinct from the rest of that list
    (missing per-target binaries / root restore artifacts). Issue #443:
    ``cmd_meta_build`` uses this predicate to route the two kinds of gap
    to different overrides — ``--allow-incomplete`` for everything else,
    ``--allow-missing-metadata`` for this one, matching the #437 key gate
    it shares its failure mode with ("this meta-volume cannot decrypt
    repo X").

    Matched precisely rather than by substring: exactly three path
    segments, ``metadata`` / ``<repo>`` / ``keys`` — a repo id cannot
    itself contain ``/`` in practice, but this stays exact rather than
    relying on that.
    """
    parts = path.split("/")
    return (
        len(parts) == 3
        and parts[0] == "metadata"
        and parts[1] != ""
        and parts[2] == "keys"
    )


# Human-facing note printed by both gates so an operator understands an
# ABSENT verdict against an older disc is expected, not a defect.
VINTAGE_NOTE = (
    "this check reflects the 2026-06 required-contents contract; older "
    "discs may predate it."
)
