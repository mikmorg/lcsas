#!/usr/bin/env python3
"""GATE-08: rebuild-and-diff gate for the committed recovery/bin artifacts.

The meta disc bundles the *committed* binaries in ``recovery/bin/<arch>/``.
Almost every other gate builds a *fresh* binary from source, so a C fix can
land, pass every source-level gate, and still ship an *unfixed* committed
binary on every meta disc.  This gate closes that hole: for each committed
artifact it does a clean cross-rebuild with the pinned toolchain
(``recovery/TOOLCHAIN``) and ``SOURCE_DATE_EPOCH``, then byte-compares the
rebuild against the committed file.

zig+musl Linux targets are byte-reproducible; the gate enforces byte-identity
on them.  zig's Mach-O (macOS) and PE (Windows) linkers are not reproducible
with the current toolchain -- those targets are listed in
``recovery/BIN_PARITY_EXEMPT`` (each with a tracking issue), still clean-rebuilt
to prove they compile+link, but not byte-compared.  The exemption is printed
loudly so it can never rot silently.

Pure stdlib; the heavy lifting is `make` invocations.

Usage:
    python3 recovery/scripts/bin_parity.py
    make -C recovery bin-parity
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[1]
BIN = RECOVERY / "bin"
PARITY_BUILD = RECOVERY / "build" / "parity"
EXEMPT_FILE = RECOVERY / "BIN_PARITY_EXEMPT"
SOURCE_DATE_EPOCH = "1735689600"  # matches repro-check; keep in sync.

ZIG_CC = "python3 -m ziglang cc"
VENDOR_CFLAGS = (
    "-std=gnu99 -O2 -DNDEBUG "
    "-Wno-deprecated-declarations -Wno-unused-function"
)


@dataclass(frozen=True)
class Target:
    """One committed binary: how to rebuild it and where it lives in bin/."""

    arch: str  # bin/<arch>/ subdir
    exe: str  # file name within that subdir (incl. .exe on windows)
    zig_target: str  # zig cc -target <...>
    static: bool  # pass LDFLAGS=-static (Linux musl only)
    bin_ext: str = ""  # ".exe" for windows targets
    vendor_cflags: str | None = None  # override for cross targets

    @property
    def committed(self) -> Path:
        return BIN / self.arch / self.exe

    @property
    def build_dir(self) -> Path:
        return PARITY_BUILD / self.arch

    @property
    def rebuilt(self) -> Path:
        return self.build_dir / self.exe


# The committed-artifact matrix.  Mirrors the cross-build recipes in the
# Makefile (bin/<arch>/lcsas-restore, keyshare-arches, macos, windows).
# lcsas-init is gitignored (bin/*/lcsas-init in recovery/.gitignore) and so
# is intentionally absent here -- the gate only covers tracked artifacts.
TARGETS: list[Target] = [
    # ── Linux musl (byte-reproducible) ──
    Target("x86_64", "lcsas-restore", "x86_64-linux-musl", static=True),
    Target("x86_64", "lcsas-iso9660", "x86_64-linux-musl", static=True),
    Target("x86_64", "lcsas-keyshare", "x86_64-linux-musl", static=True),
    Target("x86_64", "lcsas-ecc", "x86_64-linux-musl", static=True),
    Target("aarch64", "lcsas-restore", "aarch64-linux-musl", static=True),
    Target("aarch64", "lcsas-iso9660", "aarch64-linux-musl", static=True),
    Target("aarch64", "lcsas-keyshare", "aarch64-linux-musl", static=True),
    Target("aarch64", "lcsas-ecc", "aarch64-linux-musl", static=True),
    Target("armv7", "lcsas-restore", "arm-linux-musleabihf", static=True),
    Target("armv7", "lcsas-iso9660", "arm-linux-musleabihf", static=True),
    Target("armv7", "lcsas-keyshare", "arm-linux-musleabihf", static=True),
    Target("armv7", "lcsas-ecc", "arm-linux-musleabihf", static=True),
    # ── macOS Mach-O (exempt; see BIN_PARITY_EXEMPT) ──
    Target("x86_64-macos", "lcsas-restore", "x86_64-macos", static=False),
    Target("x86_64-macos", "lcsas-keyshare", "x86_64-macos", static=False),
    Target("x86_64-macos", "lcsas-ecc", "x86_64-macos", static=False),
    Target("aarch64-macos", "lcsas-restore", "aarch64-macos", static=False),
    Target("aarch64-macos", "lcsas-keyshare", "aarch64-macos", static=False),
    Target("aarch64-macos", "lcsas-ecc", "aarch64-macos", static=False),
    # ── Windows PE (exempt; see BIN_PARITY_EXEMPT) ──
    Target(
        "x86_64-windows", "lcsas-restore.exe", "x86_64-windows-gnu",
        static=False, bin_ext=".exe",
    ),
    Target(
        "x86_64-windows", "lcsas-keyshare.exe", "x86_64-windows-gnu",
        static=False, bin_ext=".exe",
    ),
    Target(
        "x86_64-windows", "lcsas-ecc.exe", "x86_64-windows-gnu",
        static=False, bin_ext=".exe",
    ),
]

_EXEMPT_RE = re.compile(r"^(?P<path>\S+)\s+issue=#(?P<issue>\d+)\b")


def load_exempt() -> dict[str, str]:
    """rel-path -> 'issue=#N' for every exempt target."""
    out: dict[str, str] = {}
    if not EXEMPT_FILE.is_file():
        return out
    for raw in EXEMPT_FILE.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = _EXEMPT_RE.match(s)
        if not m:
            raise SystemExit(
                f"BIN_PARITY_EXEMPT: unparseable line (need "
                f"'<path> issue=#N ...'): {raw!r}"
            )
        out[m.group("path")] = f"issue=#{m.group('issue')}"
    return out


def rebuild(t: Target) -> None:
    """Clean cross-rebuild a single committed artifact into build/parity/."""
    if t.build_dir.exists():
        shutil.rmtree(t.build_dir)
    t.build_dir.mkdir(parents=True, exist_ok=True)
    cc = f"{ZIG_CC} -target {t.zig_target}"
    args = [
        "make",
        f"BUILD={t.build_dir}",
        f"CC={cc}",
    ]
    if t.static:
        # --strip-debug (#408): unstripped output embeds machine paths in
        # DWARF — zig's bundled-musl sources at the ziglang INSTALL path,
        # the checkout dir, and stale zig-cache comp dirs — so bytes were
        # only reproducible on the machine (and cache) that built them.
        # Link-time strip removes musl's DWARF too (compile -g0 would not)
        # and makes output byte-identical across checkout path, ziglang
        # install path, and cache state (all three proven empirically).
        # Keep in sync with the Makefile cross recipes + recovery/build.py.
        args.append("LDFLAGS=-static -Wl,--strip-debug")
    else:
        args.append("LDFLAGS=")
    args.append(f"VENDOR_CFLAGS={t.vendor_cflags or VENDOR_CFLAGS}")
    if t.bin_ext:
        args.append(f"BIN_EXT={t.bin_ext}")
    args.append(str(t.rebuilt))
    env = dict(os.environ, SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH)
    subprocess.run(args, cwd=RECOVERY, env=env, check=True)


@dataclass
class Result:
    mismatches: list[str] = field(default_factory=list)
    exempt_seen: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def main() -> int:
    exempt = load_exempt()
    result = Result()

    # Build-recipe sanity: every exempt entry must name a real target,
    # else the exemption is dead and silently protects nothing.
    known = {f"{t.arch}/{t.exe}" for t in TARGETS}
    for path in exempt:
        if path not in known:
            raise SystemExit(
                f"BIN_PARITY_EXEMPT names '{path}', which is not a "
                f"committed target. Known: {sorted(known)}"
            )

    for t in TARGETS:
        rel = f"{t.arch}/{t.exe}"
        if not t.committed.is_file():
            result.missing.append(rel)
            print(f"[bin-parity] MISSING committed artifact: {rel}")
            continue
        print(f"[bin-parity] rebuild {rel} (-target {t.zig_target})")
        rebuild(t)
        if rel in exempt:
            # Exempt: prove it builds, skip byte-compare, announce loudly.
            result.exempt_seen.append(rel)
            print(
                f"[bin-parity] EXEMPT  {rel}  ({exempt[rel]}) -- "
                f"rebuilt OK, byte-compare skipped (non-reproducible toolchain)"
            )
            continue
        if filecmp.cmp(t.rebuilt, t.committed, shallow=False):
            print(f"[bin-parity] OK      {rel}  byte-identical")
        else:
            result.mismatches.append(rel)
            print(
                f"[bin-parity] STALE   {rel}  committed binary != clean "
                f"rebuild -- regenerate it (see the Makefile cross-build "
                f"recipe for {t.arch})"
            )

    # Catch locally-rebuilt-but-uncommitted drift in the tracked bins.
    git_dirty = subprocess.run(
        ["git", "status", "--porcelain", "recovery/bin"],
        cwd=RECOVERY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    print("")
    if result.exempt_seen:
        print(
            f"[bin-parity] {len(result.exempt_seen)} target(s) EXEMPT from "
            f"byte-identity (tracked in BIN_PARITY_EXEMPT):"
        )
        for rel in result.exempt_seen:
            print(f"               - {rel}  ({exempt[rel]})")

    failed = False
    if result.missing:
        failed = True
        print(f"[bin-parity] FAIL: {len(result.missing)} committed "
              f"artifact(s) missing: {result.missing}")
    if result.mismatches:
        failed = True
        print(f"[bin-parity] FAIL: {len(result.mismatches)} committed "
              f"binary(ies) do not match a clean rebuild: {result.mismatches}")
    if git_dirty:
        failed = True
        print("[bin-parity] FAIL: recovery/bin has uncommitted changes "
              "(locally rebuilt but not committed):")
        print(git_dirty)

    if failed:
        return 1
    n_gated = len(TARGETS) - len(result.exempt_seen)
    print(f"[bin-parity] PASS: {n_gated} target(s) byte-identical to source, "
          f"{len(result.exempt_seen)} exempt, working tree clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
