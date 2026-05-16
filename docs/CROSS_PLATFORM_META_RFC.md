# Cross-Platform Meta-Volume — Design RFC

**Status:** DRAFT — awaiting approval before implementation.
**Author:** Claude Opus 4.7 (drafted from a session with Michael Morgan).
**Date:** 2026-05-16.
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

**Target matrix (proposed minimum):**

| Arch / OS | Rationale |
|---|---|
| `x86_64-linux-gnu` | Today's only target.  Most lab/server hosts. |
| `aarch64-linux-gnu` | Apple Silicon-via-Asahi, Raspberry Pi 4/5, AWS Graviton, modern Android dev. |
| `x86_64-darwin` | Recovery from a Mac that has not yet been replaced. |
| `aarch64-darwin` | Apple Silicon native macOS. |
| `x86_64-w64-mingw32` | Already partially covered via `restore.bat` + the `rustic-static` Windows binary; this just makes it explicit. |

**RISC-V is excluded for now** — upstream Rustic does not yet ship a
release artifact for `riscv64gc-unknown-linux-gnu`, so we'd have to
cross-compile ourselves.  Defer.

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

**Option A (multi-arch prebuilt bundling)** for these targets:
`x86_64-linux-gnu`, `aarch64-linux-gnu`, `aarch64-darwin`,
`x86_64-darwin`, `x86_64-w64-mingw32`.  Defer RISC-V until upstream
Rustic ships a release artifact.

This is the only option that simultaneously satisfies the cold-start
requirement, the 50-year survivability story, and the existing
3-tier architecture.

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

## 6. Open questions for the human

These are the decision points where I'd want sign-off before doing any
implementation work:

1. **Is the target arch matrix above the right one?**  Specifically:
   - Do we care about RISC-V *now* (cross-compile in CI) or wait for
     upstream Rustic?
   - Do we care about FreeBSD / OpenBSD?  They have very different
     binary formats and would meaningfully expand the matrix.
   - Should we support the older 32-bit ARM (`armv7-linux-gnueabihf`)
     for Raspberry Pi 1/2/Zero?  Coverage is low but the hardware is
     still in use.

2. **How do we handle xorriso on non-x86_64?**  The
   "kernel-`mount -o loop` primary" pattern works on Linux only.  On
   macOS we lean on `hdiutil`; on Windows we lean on
   `Mount-DiskImage`.  Is that an acceptable degradation, or do we
   want to cross-compile xorriso too (significantly more work)?

3. **Where does the per-target binary cache live?**  Options:
   - In-repo under `recovery/bin/` (git-LFS recommended — total payload
     is several hundred MB).
   - Downloaded at meta-volume build time from a known-good cache (Git
     release artifact, S3 bucket, etc.) — requires net access at build
     but not at restore.
   - Built into CI; the cache lives in GitHub Actions artifacts and
     gets baked into meta-volume builds.

4. **Manifest format.**  Do we want the per-arch binaries listed in
   the existing `recovery/MANIFEST.sha256`, or in a parallel
   `recovery/UPSTREAM.sha256` that distinguishes "we built this" from
   "we downloaded this and pinned the hash"?

5. **Tier 3 (Python) on non-x86_64.**  The pure-Python fallback works
   on any arch that has a Python 3 interpreter, but `zstandard` is C
   extension code that may not be importable on all targets.  Do we:
   - Bundle a CPython interpreter per arch (much larger payload), or
   - Document that recipient must `pip install zstandard` if their
     host's python3 doesn't have it (back to the package-manager
     dependency we were trying to avoid), or
   - Accept that on exotic arches tier 3 may degrade to "uncompressed
     blobs only" (already the case in `restic_fallback.py:83`)?

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

**Next step:** human approval of the target-arch matrix and the four
open questions in §6 before any implementation work begins.
