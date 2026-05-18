"""Hardening test #1: meta-disc tier-1 bundling completeness.

`MetaVolumeBuilder._bundle_tier1_binaries` (in
`src/lcsas/meta/builder.py`) maps every approved tier-1 target to a
`(short_arch_dir, exe_name)` pair under `recovery/bin/<short>/`.  At
bundle-time it silently skips any target whose source binary doesn't
exist on disk — the design choice was intentional (a developer can
build one arch and still produce a meta disc), but it lets a "shipping"
meta disc go out without a binary for the operator's actual platform.

This is what happened in the blind run that exposed the production
fragility: Phase 21.x claimed all six targets were bundled, but the
operator's Linux x86_64 host had no `recovery/bin/x86_64/lcsas-restore`
because nobody had built it.  restore.sh fell through tier 1 / tier 2
and tripped over the broken tier-3 path.

This test fails the build if a tier-1 target is "approved" (listed in
`tier1_map`) but missing from `recovery/bin/<short>/`.  Operators who
deliberately want to ship a partial meta disc can list missing arches
in `OPTIONAL_TARGETS` below to silence the gate.

What it catches:
  - Forgetting to run `lcsas recovery build --arch <X>` before
    `lcsas meta build`.
  - A future Phase that adds a new approved target without building
    its binary in CI.
  - Accidental deletion / rename of `recovery/bin/<short>/lcsas-restore`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_BIN = REPO_ROOT / "recovery" / "bin"


# Targets the codebase claims to support (docs/CROSS_PLATFORM_META_RFC.md
# §6 Q6 enumerates these as the six approved triples).  Keep this in
# lock-step with `_bundle_tier1_binaries.tier1_map`.
APPROVED_TIER1_TARGETS: list[tuple[str, str, str]] = [
    # (rust_triple, short_arch_dir, exe_name)
    ("x86_64-unknown-linux-musl",     "x86_64",        "lcsas-restore"),
    ("aarch64-unknown-linux-musl",    "aarch64",       "lcsas-restore"),
    ("armv7-unknown-linux-gnueabihf", "armv7",         "lcsas-restore"),
    ("aarch64-apple-darwin",          "aarch64-macos", "lcsas-restore"),
    ("x86_64-apple-darwin",           "x86_64-macos",  "lcsas-restore"),
    ("x86_64-pc-windows-gnu",         "x86_64-windows", "lcsas-restore.exe"),
]

# Targets a developer is allowed to skip on a host that lacks the
# cross-toolchain.  Empty by default — zig 0.16+ handles every approved
# triple (Linux musl, macOS via bundled libSystem, Windows-gnu), so
# `snap install zig --classic --beta` is the standard fix.
#
# Set LCSAS_OPTIONAL_ARCHES=arch1,arch2 in your shell to allow a local
# skip; CI runs unset and therefore requires all six.
OPTIONAL_TARGETS: tuple[str, ...] = tuple(
    t.strip() for t in os.environ.get("LCSAS_OPTIONAL_ARCHES", "").split(",")
    if t.strip()
)


def test_tier1_map_matches_approved_targets() -> None:
    """The mapping in `_bundle_tier1_binaries` must match the approved
    list above; if Phase 22 adds a 7th target this test fails loudly
    so we update both places."""
    builder = REPO_ROOT / "src" / "lcsas" / "meta" / "builder.py"
    src = builder.read_text()
    for rust_triple, _short_arch, _exe in APPROVED_TIER1_TARGETS:
        marker = f'"{rust_triple}"'
        assert marker in src, (
            f"Approved tier-1 target {rust_triple!r} is missing from "
            f"`_bundle_tier1_binaries.tier1_map` in "
            f"src/lcsas/meta/builder.py.  Either add it to the map or "
            f"remove it from APPROVED_TIER1_TARGETS in this test."
        )


@pytest.mark.parametrize(
    "rust_triple,short_arch,exe",
    APPROVED_TIER1_TARGETS,
    ids=[t[0] for t in APPROVED_TIER1_TARGETS],
)
def test_tier1_source_binary_present(
    rust_triple: str, short_arch: str, exe: str
) -> None:
    """A pre-built lcsas-restore must exist for every approved target,
    so `lcsas meta build` produces a complete meta disc.

    Skipped (not failed) for targets listed in OPTIONAL_TARGETS — those
    require a cross-toolchain we don't assume every developer has.  CI
    should set OPTIONAL_TARGETS = () and require all six.
    """
    if rust_triple in OPTIONAL_TARGETS:
        pytest.skip(
            f"{rust_triple} is OPTIONAL on this host; build with "
            f"`lcsas recovery build --arch {short_arch}` to enable."
        )
    src_bin = RECOVERY_BIN / short_arch / exe
    assert src_bin.is_file(), (
        f"approved tier-1 target {rust_triple} has no pre-built "
        f"binary at {src_bin}.  `lcsas meta build` will silently "
        f"omit it from the meta disc.  Build it with:\n\n"
        f"    lcsas recovery build --arch {short_arch}\n\n"
        f"(or, for cross-targets without a host toolchain:\n"
        f"    lcsas recovery build --arch {short_arch} "
        f"--cc 'zig cc -target {rust_triple}'  )."
    )


def test_meta_build_bundles_every_present_target(tmp_path: Path) -> None:
    """End-to-end: build a meta disc, assert that every target whose
    source binary exists shows up in the bundled `bin/<rust-triple>/`
    tree.

    This catches the *bundling* bug — i.e. a source binary exists but
    `_bundle_tier1_binaries` for some reason doesn't copy it onto the
    disc.  Independent of OPTIONAL_TARGETS: we don't require absence
    here, only that whatever was available got bundled.
    """
    from lcsas.meta.builder import MetaVolumeBuilder

    out_dir = tmp_path / "meta_stage"
    out_dir.mkdir()
    builder = MetaVolumeBuilder(out_dir, catalog_db_path=None)
    builder.build()

    bundled_bin = out_dir / "recovery" / "bin"
    assert bundled_bin.is_dir(), (
        "meta build produced no recovery/bin/ tree at all — the "
        "bundling step is silently no-op."
    )

    for rust_triple, short_arch, exe in APPROVED_TIER1_TARGETS:
        src_bin = RECOVERY_BIN / short_arch / exe
        if not src_bin.is_file():
            continue  # nothing to bundle for this arch on this host
        bundled = bundled_bin / rust_triple / exe
        assert bundled.is_file(), (
            f"source binary {src_bin} exists but the bundler "
            f"did not copy it to {bundled} on the meta disc.  "
            f"Inspect `_bundle_tier1_binaries.tier1_map` in "
            f"src/lcsas/meta/builder.py."
        )
        assert bundled.stat().st_mode & 0o111, (
            f"bundled {bundled} is not executable — the +x bit "
            f"was lost during copy."
        )
