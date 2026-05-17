# Cross-Platform Meta-Volume — Design RFC

**Status:** **APPROVED** (2026-05-16). All §6 open questions resolved; see
"Decisions" lines under each question.  Implementation kick-off scope in §9.
**Author:** Claude Opus 4.7 (drafted from a session with Michael Morgan).
**Date:** 2026-05-16 (drafted), 2026-05-16 (approved).
**Supersedes:** PR #78 (`feat(recovery): C89 + POSIX-sh recovery toolchain`),
which was closed unmerged because it reintroduced the 5-tier recovery
cascade that PR #30 had deliberately collapsed to 3 tiers.

---

## 1. Problem

The meta-volume bundles Linux x86_64 ELF binaries (`rustic`, `xorriso`,
`python3`) plus their shared-library dependencies.  This is documented in
`README.md` §"Platform Limitations" (≈ line 525):

> The meta-volume bundles **Linux x86_64 ELF binaries** and their shared
> libraries. It will only work on recovery machines that are:
>
> - **Architecture:** x86_64 (AMD64)
> - **OS:** Linux with compatible glibc …
> - **Kernel:** 3.x+

This means a recipient running ARM64 (Apple Silicon, Raspberry Pi, AWS
Graviton, modern Android dev boards), RISC-V (HiFive, VisionFive), or
non-Linux Unix (FreeBSD, OpenBSD, macOS as the recovery host) cannot
restore from cold start without first locating an x86_64 Linux machine
or building the toolchain from source.

The current §"Platform Limitations" workaround ("install `rustic`,
`xorriso`, and Python from the target platform's package manager")
assumes:

- The package manager exists and has working network access (false for
  cold-start scenarios — power's on but the network is out).
- The recipient knows which versions are compatible (the LCSAS catalog
  was written by a specific Rustic version; mismatched versions can
  refuse to open).
- The recipient is technically capable of `pip install -e .` from the
  bundled `lcsas_src/` after building the binaries.

For a 50-year-survivability archive (see `docs/SURVIVABILITY.md`), this
is a real risk.  ARM is already dominant in consumer devices; in 30
years it may be the only commodity host architecture available.

## 2. Constraints carried forward from prior decisions

These are non-negotiable inputs to the design:

1. **3 tiers only** (PR #30).  No source-rebuild tiers.  Recovery cascade
   stays at: tier 1 = prebuilt static `lcsas-restore`, tier 2 = vendored
   `rustic-static`, tier 3 = pure-Python `standalone_restorer.py`.
2. **Always-on ECC** (PR #36).  Production media gets RS03; no toggle.
3. **No new bundled compilers.**  PR #78 vendored ~330 KLOC of C source
   (sqlite, zstd) plus a C89 reimplementation to support a tier-3
   "rebuild from source" path.  The user rejected this approach.  Any
   solution must not require the recipient to *compile* anything.
4. **Optical-only media** (PR #27).  No LTO.
5. **Single password mechanism: `password_file`** (PR #66 doc; PR #37
   removed `--key-file`).  No further auth indirection.
6. **No runtime pip dependencies** (architectural).  `zstandard` remains
   an optional pure-Python tier-3 dependency only.

## 3. Design options

### Option A — Multi-arch prebuilt bundling

Stuff the meta-volume with prebuilt binaries for several arch/OS pairs.
At restore time, `restore.sh` detects the host arch and picks the right
`bin/<arch>/` subtree.

**Target matrix (verified against upstream release listings, 2026-05-16):**

| Target | Rustic upstream | Python (PBS) | Rationale |
|---|---|---|---|
| `x86_64-unknown-linux-musl` | ✓ | ✓ | **Static** — no host glibc dependency.  Replaces today's `x86_64-linux-gnu` path. |
| `aarch64-unknown-linux-musl` | ✓ | ✓ | **Static** — Raspberry Pi 4/5, Apple Silicon via Asahi, AWS Graviton, modern Android dev boards. |
| `armv7-unknown-linux-gnueabihf` | ✓ | ✓ | 32-bit ARM — Raspberry Pi 1/2/3/Zero, still extremely common in homelab / preservation. |
| `aarch64-apple-darwin` | ✓ | ✓ | Apple Silicon native macOS. |
| `x86_64-apple-darwin` | ✓ | ✓ | Intel Mac (recovery target until they age out). |
| `x86_64-pc-windows-gnu` | ✓ | ✓ (msvc variant) | Windows; already partially covered by `restore.bat` + the existing `rustic-static` Windows binary — this formalizes it. |

**Key change vs the original DRAFT:** prefer **musl-static** over
**glibc-dynamic** for Linux targets.  Rustic ships both flavors upstream;
the musl-static binary has zero host-libc dependency, which is the right
choice for cold-start recovery.

**Excluded for now (decisions under §6):**

- `riscv64gc-*` — no upstream rustic artifact.  Defer until upstream
  ships.
- `i686-*` (32-bit x86) — upstream ships it, but the cold-storage
  recovery audience for 32-bit x86 in 2026+ is vanishingly small.
- `*-freebsd / *-openbsd` — no upstream rustic artifact.  Defer.
- `aarch64-pc-windows-msvc` (Windows ARM64) — no upstream rustic
  artifact.  Defer.

**Where the binaries come from:**

- **Rustic** ships upstream releases for every target above except
  `aarch64-darwin` and `riscv64gc` (as of v0.11.2; check on each release
  refresh).  The meta-builder downloads + SHA-256-verifies the tarball
  per target, extracts the binary, and stores it under
  `tools/bin/<arch-os>/rustic`.
- **xorriso** is a moderately ugly cross-compile target (autoconf,
  libburn family).  Pragmatic answer: bundle xorriso ONLY for
  `x86_64-linux-gnu` (the existing path) and reuse the kernel
  `mount -o loop` primary path (already implemented per Phase 11.1)
  for every other arch.  On macOS, `hdiutil mount -nobrowse` plays the
  same role.  On Windows, the existing `restore.bat` mounts via
  PowerShell `Mount-DiskImage`.  No xorriso needed at restore time on
  non-Linux hosts.
- **python3** for tier-3 is bundled per arch when available.  This
  is the heaviest item by far (~30 MB per platform with stdlib).  We
  rely on the host system's `python3` if present (any 3.6+); the
  bundled copy is the fallback.

**Sizing:** A meta-volume with binaries for the 5 targets above grows
from today's ~120 MB to roughly 600–800 MB.  This still fits comfortably
on a BD25 (25 GB) meta-disc.  Manifest hash list grows linearly.

**Pros:**
- Zero work for the recipient: insert disc, run `restore.sh` or `restore.bat`.
- No compilation, no network, no package manager.
- Each target's binary is a single artifact whose provenance we can pin
  via SHA-256 in the manifest.

**Cons:**
- More upstream-release plumbing in the meta-builder (download + verify
  + cache 5 tarballs per build).
- Larger meta-volume.
- We commit to a maintenance burden: when a target arch gains traction
  (RISC-V eventually) we add it; when one fades (sparc64?) we drop it.

### Option B — "Bring your own host" (status quo + better docs)

Don't change the meta-volume.  Document precisely what the recipient
needs to do on each non-x86_64 host:

```
ARM64 Linux:
  apt install rustic xorriso python3
  python3 -m pip install /mnt/meta/lcsas_src  # if cmd_restore_standalone is wanted
  bash /mnt/meta/restore_legacy.sh --key ~/key.txt --isos /mnt/data/ --target ~/restored/

macOS (Intel or Apple Silicon):
  brew install rustic xorriso
  # …
```

**Pros:**
- Zero new code or vendored binaries.
- Each target gets the "real" package-manager build of its tools.

**Cons:**
- Fails the cold-start test: package managers need network, hosts may
  not have rustic packaged at all (rustic is in nixpkgs and AUR but not
  Debian/Ubuntu stable as of this writing).
- Survivability degrades over time: a recipient in 2050 will not have a
  `brew` or `apt` that knows about rustic.
- Pushes complexity onto the recipient, exactly the audience least
  equipped to handle it.

### Option C — Container/Flatpak bundle

Ship a single OCI image or AppImage that contains the full
restore toolchain plus an init script.  The recipient runs
`podman run` / `appimage` and the image picks the right arch
automatically (manifest list).

**Pros:**
- Single artifact handles all arches.
- Industry-standard distribution mechanism.

**Cons:**
- Recipient must have `podman` / `docker` / `flatpak` installed.  That's
  another runtime to bootstrap.
- Container runtimes themselves are a moving target on a 50-year
  horizon.  AppImage requires FUSE; Docker is contractor-owned.
- Defeats the "static binary, kernel + libc, done" survivability story.

### Option D — Pure-Python everything

Promote `standalone_restorer.py` (pure-Python restic decoder) from
tier-3 fallback to tier-1 primary.  Drop the bundled rustic entirely
on non-x86_64 targets.

**Pros:**
- Python 3 is far more portable than rustic prebuilts and ships with
  most modern Unix distributions.
- We already maintain this code path.
- Eliminates the cross-compile problem entirely.

**Cons:**
- Slower than rustic (no parallelism, pure-Python AES, single-threaded
  zstd) — by ~10–50x for large repositories.
- `zstandard` is an optional dep that may not be importable on every
  host without a C extension (it has a pure-Python fallback but the
  compressed zstd content path is much slower).
- Reverses Phase 11's "C-first, Python-last" hierarchy.

## 4. Recommendation

**Option A (multi-arch prebuilt bundling)** for the six targets in §3's
table:

- `x86_64-unknown-linux-musl`
- `aarch64-unknown-linux-musl`
- `armv7-unknown-linux-gnueabihf`
- `aarch64-apple-darwin`
- `x86_64-apple-darwin`
- `x86_64-pc-windows-gnu`

This is the only option that simultaneously satisfies the cold-start
requirement, the 50-year survivability story, and the existing 3-tier
architecture.  Every target is independently verified to have both an
upstream rustic release artifact and a python-build-standalone release
artifact, so we never have to cross-compile anything ourselves.

### Why not the others

- **Option B** loses the cold-start property and pushes failure into
  the recipient's hands.  Acceptable as the *current* limitation, not
  as the *target* limitation.
- **Option C** adds a container-runtime dependency on the recovery
  host, which is exactly the kind of dependency we've spent four
  phases removing.
- **Option D** is a real survivability strategy but a coverage one,
  not a speed one.  Tier 3 already does this for x86_64 hosts when
  the rustic binary fails.  Promoting it to tier 1 would slow down
  the *fast* path for a problem (host arch mismatch) we can solve
  by bundling more binaries.

## 5. Proposed implementation plan

These are sketch-level steps; each one would be its own PR with full
tests before merge.

1. **Multi-arch downloader.**  Add `recovery/scripts/fetch_upstream.sh`
   that downloads the rustic release tarball for each target, verifies
   its SHA-256 against a pinned manifest, extracts the binary, and
   places it under `recovery/bin/<arch-os>/`.  Hash list lives in a
   new `recovery/UPSTREAM.sha256` file under version control.

2. **Meta-builder bundling extension.**  Today
   `MetaVolumeBuilder._bundle_recovery_toolchain()` copies
   `recovery/bin/<arch>/lcsas-restore` (the only target).  Extend to
   walk all subdirectories of `recovery/bin/` and copy each one,
   updating the bundled MANIFEST.sha256 accordingly.

3. **restore.sh arch dispatcher.**  Currently
   `recovery/scripts/restore.sh:202` reads `uname -m` and normalizes to
   one of `x86_64 / aarch64 / riscv64`.  Extend to also probe `uname -s`
   so the dispatcher picks `x86_64-darwin` vs `x86_64-linux-gnu` etc.

4. **restore.bat parity for Windows.**  PR #38 already pruned the
   Python-fallback chain from restore.bat; Phase 11.2 already
   established the `rustic-static` path.  Add `bin/x86_64-w64-mingw32/`
   to the search list and ensure the SHA-256 verification step matches
   the manifest format used by the POSIX driver.

5. **macOS support gap.**  The Phase 11 cascade
   (`mount -o loop` → `7z x` → bundled xorriso) needs a macOS
   equivalent.  Suggest: `hdiutil mount -nobrowse` as primary, fall
   through to `7z x` (Homebrew package widely available), no
   bundled-xorriso tier.  Documented in
   `docs/workflows/restore-host-linux.md` or a new
   `restore-host-macos.md`.

6. **Test coverage.**  Per-arch unit tests for the dispatcher logic
   (mock `uname -m` / `uname -s`).  An integration test that
   builds a meta-volume containing all 5 targets and asserts the
   directory tree and MANIFEST.sha256 are correct.  Cross-arch
   *execution* testing is out of scope — we can only test on
   `x86_64-linux-gnu` from CI; the other targets are verified by
   matching their SHA-256 against the pinned upstream manifest.

7. **README + workflow doc refresh.**  Replace the §"Platform
   Limitations" section in `README.md` with the new supported matrix.
   Add a "Cross-platform recovery" subsection to
   `docs/workflows/meta-volume.md`.

## 6. Resolved questions

All five open questions resolved 2026-05-16 after upstream-release
verification.  Each question retains its original framing for context;
the **Decision:** line below it is binding.

### Q1: Is the target arch matrix above the right one?

Sub-questions:
- Do we care about RISC-V *now* (cross-compile in CI) or wait for
  upstream Rustic?
- Do we care about FreeBSD / OpenBSD?  They have very different binary
  formats and would meaningfully expand the matrix.
- Should we support the older 32-bit ARM (`armv7-linux-gnueabihf`) for
  Raspberry Pi 1/2/Zero?  Coverage is low but the hardware is still
  in use.

**Decision:** Six targets, all verified to have both upstream rustic and
python-build-standalone release artifacts: `x86_64-unknown-linux-musl`,
`aarch64-unknown-linux-musl`, `armv7-unknown-linux-gnueabihf`,
`aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-pc-windows-gnu`.
**Prefer musl-static over glibc-dynamic** on Linux targets — eliminates
host-libc compatibility risk, which matches the cold-start recovery
story.  Skip RISC-V (no upstream), 32-bit x86 (audience effectively
zero in 2026+), FreeBSD/OpenBSD (no upstream), Windows ARM64 (no
upstream).  See the revised target matrix in §3.

### Q2: How do we handle xorriso on non-x86_64?

The "kernel-`mount -o loop` primary" pattern works on Linux only.  On
macOS we lean on `hdiutil`; on Windows we lean on `Mount-DiskImage`.
Is that an acceptable degradation, or do we want to cross-compile
xorriso too (significantly more work)?

**Decision:** Accept the degraded pattern.  No bundled xorriso outside
`x86_64-linux-musl`.  Justifications: (a) Linux `mount -o loop` is in
the kernel and works on every arch with zero new binaries; this is
already Phase 11.1's primary path.  (b) macOS `hdiutil mount -nobrowse`
is in the base OS.  (c) Windows `Mount-DiskImage` is in PowerShell on
Win 8+.  Cross-compiling xorriso (autoconf + libburn + libisofs) is a
significant maintenance burden for a tier already covered by host
facilities.

### Q3: Where does the per-target binary cache live?

Options were: in-repo under `recovery/bin/` (git-LFS recommended;
several hundred MB total payload), downloaded at meta-volume build
time from a known-good cache, or baked into CI artifacts.

**Decision:** Three-stage approach.  (1) `recovery/UPSTREAM.sha256`
pins each upstream artifact's SHA-256 — version-controlled, tiny
(~40 lines), no LFS.  (2) The meta-builder downloads the rustic +
python tarball for each target at meta-volume build time, verifies
against the pinned hash, and caches locally under
`~/.cache/lcsas/recovery-binaries/v<version>/`.  (3) A `make
fetch-recovery` target downloads everything ahead of time for
air-gapped builds.  Rejected: in-repo git-LFS (clone bloat, friction
for every contributor) and CI-artifacts-only (locks builds to a
specific CI provider's retention).

### Q4: Manifest format

Do we want the per-arch binaries listed in the existing
`recovery/MANIFEST.sha256`, or in a parallel `recovery/UPSTREAM.sha256`
that distinguishes "we built this" from "we downloaded this and
pinned the hash"?

**Decision:** Two separate files.
- `recovery/MANIFEST.sha256` — files **we author** (existing C source,
  scripts, docs, anything we ship from this repo).
- `recovery/UPSTREAM.sha256` — files **we trust from upstream**
  (rustic release tarballs, python-build-standalone release tarballs).

When upstream rustic releases a new version, only `UPSTREAM.sha256`
changes; `MANIFEST.sha256` stays stable.  Security review becomes
easier: anyone auditing what we trust from outside can read one file.

### Q5: Tier 3 (Python) on non-x86_64

The pure-Python fallback works on any arch that has a Python 3
interpreter, but `zstandard` is a C extension that may not be
importable on all targets.

**Decision:** Bundle per-arch CPython from python-build-standalone for
every target in §3's matrix.  PBS provides install_only_stripped
builds (~25 MB compressed per arch) covering all six targets we ship.
This eliminates the "host has no Python" failure mode entirely.  For
arches not covered by PBS (currently none in our matrix), tier 3
degrades to "uncompressed blobs only" — already implemented in
`src/lcsas/restore/restic_fallback.py:83`.

### Q6: Tier 1 (`lcsas-restore`) cross-compilation  (POST-FACTO)

Not part of the original §6 questions — surfaced 2026-05-17 after
Phase 21.1–21.9 shipped.  Documented here for honesty before the
fix lands.

**The gap.**  `recovery/scripts/restore.sh` declares
`bin/<target>/lcsas-restore` (our C89 binary against vendored
sqlite/zstd) as the **primary** recovery tool (tier 1), with
`rustic-static` as the tier-2 fallback.  Phase 21.1.b bundled
upstream rustic + python for every approved target, but did NOT
cross-compile our own `lcsas-restore`.  So on any host whose arch
isn't the build host's, the cascade skips straight to tier 2 —
exactly inverting the survivability ranking (rustic + Python are
moving targets; the C89 binary was meant to be the long-lived
durable artifact).

A separate naming bug compounds it: `restore.sh`'s dispatcher
(Phase 21.1.c) probes `bin/<rust-target-triple>/lcsas-restore`,
while `RecoveryBuilder.cross_build` (`src/lcsas/recovery/build.py:99`)
writes to `bin/<short-arch>/lcsas-restore`.  Result: tier 1 fails
to fire **even on the host arch** until the path conventions are
reconciled.

**Why Phase 21 ducked it.**  The §5 implementation plan took the
shortcut: "we never have to cross-compile anything ourselves"
because upstream rustic + python-build-standalone ship release
matrices.  The C89 binary inherited the same shortcut by
omission.  Phase 21 prioritized getting *any* cross-platform
recovery story shipped, not the fully-optimal one.

**What we already have.**  `RecoveryBuilder.cross_build()`
supports three reachable targets for the C89 binary today:

- `x86_64-unknown-linux-musl` — `<arch>-linux-musl-gcc` or `zig cc`
- `aarch64-unknown-linux-musl` — same
- `x86_64-pc-windows-gnu` — `zig cc -target x86_64-windows-gnu`
  (Makefile already encodes this; see `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`)

**Still missing toolchains.**

- `armv7-unknown-linux-gnueabihf` — needs adding to
  `SUPPORTED_ARCHES` (`zig cc` covers it cleanly).
- `aarch64-apple-darwin`, `x86_64-apple-darwin` — needs
  [`osxcross`](https://github.com/tpoechtrager/osxcross) or an
  Apple-licensed SDK in CI.  Real follow-up cost.

**Decision** (binding the Phase 21.10 plan):

1. **Phase 21.10.a** — ✅ SHIPPED 2026-05-17 (commit `000dee2`).
   Docs honesty pass: README, this RFC, meta-volume.md, and
   restore-host-macos.md all acknowledged the gap.
2. **Phase 21.10.b** — ✅ SHIPPED 2026-05-17.  `MetaVolumeBuilder._bundle_tier1_binaries`
   maps the 3 reachable rust-triples to `cross_build`'s short-arch
   directory names and copies the binary at bundle time.  The
   meta-builder now knows both conventions; `cross_build` stays
   unchanged.  `make build-recovery` drives the cross-builds.
3. **Phase 21.11** — ✅ SHIPPED 2026-05-17.  Added `armv7` to
   `RecoveryBuilder.SUPPORTED_ARCHES` and the
   `_bundle_tier1_binaries` rust-triple map.  Default CC for
   armv7 is `armv7-linux-musleabihf-gcc` (the canonical
   hardfloat-EABI musl-cross-make prefix); operators can override
   with `--cc "zig cc -target armv7-linux-musleabihf"` if zig is
   preferred.  Also extended the multi-token CC support so
   `--cc "zig cc ..."` probes only the `zig` binary on PATH.
   Incidental: fixed the `lcsas recovery build --arch` CLI
   choices to expose every entry in `SUPPORTED_ARCHES`
   (previously omitted the Windows arches).
4. **Phase 21.12** — ✅ SHIPPED 2026-05-17.  Solved without
   osxcross: `zig cc -target <arch>-macos` bundles enough
   libSystem definitions to link Mach-O executables for both
   `aarch64-apple-darwin` and `x86_64-apple-darwin` without
   needing an Apple SDK download.  Two new Makefile targets
   (`bin/x86_64-macos/lcsas-restore`,
   `bin/aarch64-macos/lcsas-restore`) drive zig; the
   `RecoveryBuilder` Windows code path was generalized into
   a "needs dedicated Makefile target" branch that handles both
   Windows (.exe suffix) and macOS (no suffix).  The
   `_bundle_tier1_binaries` map promotes both Darwin triples
   from None to their short-arch macOS counterparts.  Integration
   test (`test_recovery_builder_cross_builds_macos`) actually
   compiles and runs the build end-to-end when ziglang is
   installed.  No Apple licensing required; operators are
   responsible for notarization/codesigning if they want
   Gatekeeper to bless the binary.

**All six approved targets now have tier-1 coverage.** The Phase 21
cross-platform meta-volume RFC is fully implemented.

## 7. Non-goals

- Adding source-rebuild tiers (rejected with PR #78).
- Cross-compiling rustic ourselves from source.  Upstream's release
  matrix is what we ship.
- Booting from the meta-volume on non-x86_64 (the live boot environment
  uses Alpine, which has its own arch story; that's a separate effort).
- Restoring on Windows ARM64.  Marginal target, can be added when
  rustic upstream ships it.

## 8. Acceptance criteria (for the eventual implementation PRs)

- A meta-volume built on x86_64 Linux contains a working
  `lcsas-restore` for every target in the matrix.
- `restore.sh` on a hypothetical aarch64 Linux host successfully picks
  the `aarch64-linux-gnu/` binary and runs through tier 1.
- `restore.bat` on a Windows ARM64 host successfully picks the
  appropriate binary (deferred — Windows ARM64 is non-goal #4 above).
- `make test-unit` exercises the new arch dispatcher with mocked
  `uname` output.
- The integration test that builds a meta-volume from CI asserts the
  full per-arch directory tree and MANIFEST.sha256 are present.
- README §"Platform Limitations" rewritten to reflect the new
  supported matrix; old caveats moved to a "Limitations and known
  gaps" subsection.

---

## 9. Implementation kick-off

The first PR's scope (call it Phase 21.1) is the smallest end-to-end
slice that proves the design:

1. **`recovery/UPSTREAM.sha256`** — new file pinning the SHA-256 of the
   rustic v0.11.2 release artifact for every target in §3, plus the
   python-build-standalone `install_only_stripped` archive for each
   target.  Comments name the upstream release URL each hash comes
   from for future audit.
2. **`recovery/scripts/fetch_upstream.sh`** — POSIX-sh downloader.
   For each `UPSTREAM.sha256` line, download to
   `~/.cache/lcsas/recovery-binaries/<version>/<target>/`, verify the
   SHA, extract.  Idempotent: re-runs are no-ops when the cache is
   warm.  Air-gapped operators can rsync the cache directory between
   machines.
3. **`Makefile`** — add `fetch-recovery` target that invokes
   `fetch_upstream.sh` once for every pinned target.
4. **`src/lcsas/meta/builder.py`** — extend
   `_bundle_recovery_toolchain()` to walk every cached target
   directory and copy `bin/<target>/` into the meta volume.  Skip
   targets whose cache directory is missing (with a warning) so
   single-arch developer builds still work without the full fetch.
5. **`recovery/scripts/restore.sh`** — extend the existing
   `uname -m` arch dispatcher (currently lines 202–218) to also
   consult `uname -s`.  Add a normalization table mapping `(machine,
   os)` to one of the six §3 targets, with a clear failure mode for
   unknown combinations.
6. **Unit tests** — mock `uname -m` / `uname -s`, assert the
   dispatcher picks the right target for every (machine, os) pair
   in the matrix.  No cross-arch *execution* testing — that's
   gated by SHA-256 verification against the pinned upstream.
7. **Integration test** — build a meta-volume on CI (which is
   `x86_64-linux-gnu`) with the full `make fetch-recovery` having
   run.  Assert: every `bin/<target>/` subtree is present, every
   file's SHA-256 matches the pinned manifest, the meta MANIFEST
   has been regenerated to include the new per-target trees.
8. **Docs refresh** — rewrite `README.md` §"Platform Limitations" to
   describe the new supported matrix.  Add a "Cross-platform
   recovery" subsection to `docs/workflows/meta-volume.md`.  Update
   `docs/architecture.md` if its meta-volume section drifts.
9. **`recovery/MANIFEST.sha256` regen** — the meta-builder must
   merge `MANIFEST.sha256` + `UPSTREAM.sha256` into a single
   per-meta-volume `MANIFEST.sha256` so restore-time verification
   on the meta volume itself stays one file.

Subsequent PRs (Phase 21.2, 21.3, …) add macOS-specific cascade
nuances, a per-target `restore.bat` parity pass for Windows, and the
Phase 11-style xorriso-free verification helpers for macOS/Windows.

**Estimated PR count:** ~4 PRs.  Phase 21.1 above is the largest
(~6-10 commits).  Phase 21.2 and beyond are progressively smaller
slices.

---

**Status:** APPROVED 2026-05-16.  Ready to begin Phase 21.1
implementation on a separate branch.
