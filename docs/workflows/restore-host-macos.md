# Restore from a Running macOS Host

> **Tier:** Cross-tier — exercises HOT mirror metadata + COLD optical
> media on a macOS recovery host.

## Purpose

Walk an operator from "fresh macOS box with the meta-volume + data
discs in hand" to "files restored to `~/Restored/`".  Phase 21.1
unlocked this path by bundling per-target rustic + Python binaries on
the meta-volume; this doc is the operator-facing companion.

## Table of contents

- [Assumed environment](#assumed-environment)
- [Workflow: bootstrap from the meta-disc](#workflow-bootstrap-from-the-meta-disc)
- [Workflow: mounting data discs (`hdiutil` and the GUI)](#workflow-mounting-data-discs-hdiutil-and-the-gui)
- [Workflow: ISO verification without dvdisaster](#workflow-iso-verification-without-dvdisaster)
- [Apple Silicon vs Intel — which target wins](#apple-silicon-vs-intel--which-target-wins)
- [Gaps and known limitations](#gaps-and-known-limitations)
- [Variant axes that apply](#variant-axes-that-apply)
- [Test coverage summary](#test-coverage-summary)

## Assumed environment

- A working macOS Big Sur (11.x) or later.  Earlier macOS versions
  ship Python 2 by default; the bundled CPython 3.12 on the
  meta-volume covers them too, but `hdiutil` semantics changed across
  10.13/10.14 and we don't test pre-Big Sur.
- An external USB or Thunderbolt **optical drive** (the Mac itself
  hasn't shipped a built-in optical drive since 2012).  Any
  USB BD-R / DVD drive that macOS Finder can mount is enough — the
  recovery scripts never go below the filesystem layer.
- The LCSAS **meta-disc** burned via Phase 21.1's
  `make fetch-recovery` + `lcsas meta build` pipeline so the disc
  contains `recovery/bin/aarch64-apple-darwin/` and
  `recovery/bin/x86_64-apple-darwin/` subtrees.
- One or more LCSAS **data discs** burned to BD-R / M-Disc.
- The repository **encryption key** on an offline medium (paper
  printout, USB stick, etc. — see `docs/ESTATE_PLANNING.md`).

## Workflow: bootstrap from the meta-disc

1. Insert the meta-disc.  Finder mounts it under `/Volumes/LCSAS_META`
   (or similar — the label is whatever `lcsas meta build` produced).
2. Open Terminal and run:

   ```sh
   sh /Volumes/LCSAS_META/restore.sh ~/Restored
   ```

   The driver auto-detects the host arch via `uname -s` + `uname -m`
   and selects `recovery/bin/aarch64-apple-darwin/` on Apple Silicon
   or `recovery/bin/x86_64-apple-darwin/` on Intel (see the `Darwin`
   arm of the target-dispatch block in `recovery/scripts/restore.sh`).
3. The script prompts for the encryption password.  Paste from the
   offline copy.  Alternative non-interactive forms:

   - `LCSAS_PASSWORD='...' sh restore.sh ~/Restored`
   - `LCSAS_PWFILE=/path/to/key.txt sh restore.sh ~/Restored`

   If the archive's password was **split** into SLIP-0039 key shares
   (`KEY_INFO.txt` on the disc says so), rebuild it first with the
   bundled combiner — primary path is the static
   `recovery/bin/aarch64-apple-darwin/lcsas-keyshare` (or the
   `x86_64-apple-darwin` build on Intel); the Python fallback is
   `keyshare_combine.py` run under the meta-disc's bundled CPython at
   `recovery/bin/<platform>/python/bin/python3`.  See
   [`docs/KEY_SHARE_FORMAT.md`](../KEY_SHARE_FORMAT.md) for the share
   format and [`docs/guides/recovery-runbook.md`](../guides/recovery-runbook.md) for
   the full operator runbook.

4. Tier 1 (prebuilt C89 `lcsas-restore`) runs.  If it succeeds, restored
   files land in `~/Restored/`.  Tier 2 (`rustic-static`) and
   tier 3 (`python3 standalone_restorer.py`, using the meta-disc's
   bundled CPython at `recovery/bin/<target>/python/bin/python3`)
   are reached only if a prior tier crashes; the cascade is
   transparent to the operator.  Tier-1 fall-through to tier-2/3 is
   opt-in via `LCSAS_TIER_FALLBACK=1` (default `0` exec()s tier 1
   directly so a tier-1 regression surfaces loudly — see
   `recovery/docs/ENV_VARS.txt`).  The macOS tier-1 binaries are
   cross-built with `zig cc -target <arch>-macos` (Phase 21.12; no
   Apple SDK required).

**Source refs:** `recovery/scripts/restore.sh` (the `case "$OS" in`
Darwin dispatch arm selects the `*-apple-darwin` triple),
`src/lcsas/meta/builder.py` (`tier1_map`).

## Workflow: mounting data discs (`hdiutil` and the GUI)

The recovery scripts probe `/Volumes/*` for any mounted LCSAS data
discs and pass each as a `--pack-search` argument to
`lcsas-restore` (`recovery/scripts/restore.sh:349-356`).  There are
two ways for a disc to land in `/Volumes`:

### a. Finder GUI (default)

Insert the disc.  Finder mounts it automatically under `/Volumes/<LABEL>`
where `<LABEL>` is the volume label set at burn time (e.g.
`LCSAS_BD25_2026_0001`).  Nothing else to do.

### b. `hdiutil` for ISO files / DMG images / scripted flows

If you've received the data as `.iso` files rather than physical
discs (e.g. they were dd'd off the originals and sent over the
network), mount each one explicitly:

```sh
hdiutil mount -nobrowse -mountpoint /Volumes/LCSAS_BD25_2026_0001 LCSAS_BD25_2026_0001.iso
hdiutil mount -nobrowse -mountpoint /Volumes/LCSAS_BD25_2026_0002 LCSAS_BD25_2026_0002.iso
# ... etc, one per disc
sh /Volumes/LCSAS_META/restore.sh ~/Restored
```

With two or more data discs mounted, `restore.sh` may first ask which
repository to restore and list the same name several times; type `1`
(or see the repository-picker entry under
[Gaps](#gaps-and-known-limitations)).

`hdiutil` is in the macOS base install — no Homebrew or App Store
download needed.  When the restore is done, eject:

```sh
hdiutil detach /Volumes/LCSAS_BD25_2026_0001
```

## Workflow: ISO verification without dvdisaster

DVDisaster isn't bundled for the Darwin targets (cross-compiling it is
high effort and the recovery path doesn't strictly need it — see
[`CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q2).
On macOS, the verification fallback is a SHA-256 compare against the
hash recorded at burn time:

```python
from lcsas.restore.executor import verify_iso_sha256

# Expected SHA-256 lives in session_volumes.iso_sha256 for discs
# that came out of `lcsas burn`, or in a *.json receipt for discs
# burned via `lcsas burn-iso --emit-receipt`.
ok = verify_iso_sha256(Path("/Volumes/LCSAS_BD25_2026_0001.iso"),
                       expected_sha256="abc123...")
```

The shape is **detect-only** (Phase 21.2.b): if the SHA matches, the
ISO is byte-identical to what was burned.  If it doesn't, you've got
corruption and you need a redundant copy from another location — the
SHA verifier can tell you which volume is bad but it can't repair
like RS03 can.  For RS03-level repair, mount the disc on a Linux host
that has dvdisaster installed.

**Source refs:** `src/lcsas/restore/executor.py:verify_iso_sha256`,
`src/lcsas/restore/executor.py:RestoreExecutor.verify_iso`
(now accepts an `expected_sha256` kwarg).

## Apple Silicon vs Intel — which target wins

The arch dispatcher picks based on `uname -m`:

| `uname -m` reports | Target selected |
|---|---|
| `arm64` or `aarch64` | `aarch64-apple-darwin` |
| `x86_64` | `x86_64-apple-darwin` |

If you're on Apple Silicon but want to run the Intel binary under
Rosetta 2 (e.g. for compatibility testing), force it:

```sh
LCSAS_TARGET=x86_64-apple-darwin sh /Volumes/LCSAS_META/restore.sh ~/Restored
```

The `LCSAS_TARGET` env var short-circuits auto-detection in
`restore.sh` (the `if [ -n "${LCSAS_TARGET:-}" ]` branch of the
target-dispatch block); see `recovery/docs/ENV_VARS.txt` (LCSAS_TARGET).

## Gaps and known limitations

- **Tier 1 (`lcsas-restore`) IS bundled for macOS** as of Phase
  21.12.  Both Darwin targets (`aarch64-apple-darwin`,
  `x86_64-apple-darwin`) carry a prebuilt C89 `lcsas-restore`,
  cross-compiled with `zig cc -target <arch>-macos` — no osxcross or
  Apple-licensed SDK required.  The cascade on macOS is the same as
  on Linux: tier 1 (`lcsas-restore`) → tier 2 (`rustic-static`) →
  tier 3 (bundled Python).  The durability story (long-lived C89
  binary as the primary path) holds on macOS, not just on the build
  host (see
  [`CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q6).
  This is no longer a gap: `.github/workflows/macos-tier1.yml` executes
  the committed Mach-O binaries on `macos-14` (Apple Silicon) and
  `macos-15-intel` (Intel) via
  `tests/recovery_hardening/test_tier1_macos_native.py`.
- **Boot directly from the meta-disc (intentional: no LCSAS disc is
  bootable, BOOT-01):** Mac firmware doesn't boot ISO
  9660 on USB optical drives well.  If your Mac is fully bricked,
  recovery from this meta-disc is *not* the path — use macOS Recovery
  Mode or a target-disk-mode dump first, then run the meta-disc
  restore on another Mac.
- **Single-drive multi-disc swap:** tier 1 (`lcsas-restore`) has its
  own swap prompt: it names the missing disc, waits for ENTER and
  re-scans `/Volumes`, `/media`, `/mnt` and `/run/media` (since #455 it
  also picks up a disc that mounts by itself; `--auto-mount off`
  restores the Enter-only prompt).  This is exercised on macOS:
  `.github/workflows/macos-e2e.yml` (`macos-14` and `macos-15-intel`)
  `hdiutil attach`es the meta ISO and one data ISO, starts `restore.sh`
  on a terminal, and answers each prompt by attaching a wrong disc
  (ENTER: "still not found"), then the right disc (ENTER: the restore
  resumes), then the right disc with no input (the rescan finds it).
  The prompt only appears when `restore.sh` runs on a terminal; with
  stdin piped or redirected, a missing pack fails at once with
  "pack not found".  Seen in that run: the first prompt may not name a
  disc, and on macOS it can say "catalog could not be queried --
  likely written by a newer LCSAS".  The catalog on the disc is a
  WAL-mode SQLite file that cannot be opened in place on a read-only
  volume; nothing is wrong with your discs, insert them one at a time
  and the later prompts name the volume.  Mounting every disc first (Finder will hold them
  all in `/Volumes`) and running the restore remains the simplest path
  for a first run, subject to the repository-picker entry below.  The
  `INSERT DISC:` box is the older bash flow in `restore_legacy.sh`;
  `tools/restore_single_drive.py` on the disc (source:
  `src/lcsas/meta/restore_single_drive.py`) only builds the pick list
  and ingests packs, and has no prompt of its own.
- **Repository picker with two or more data discs mounted (open
  `restore.sh` bug, found by `macos-e2e.yml` and not macOS-specific):**
  every data disc carries `metadata/<tenant>/`, and repo discovery
  de-duplicates by path, so with N data discs mounted a single-tenant
  archive is listed N times ("Multiple repositories on this archive:
  1) win  2) win ...").  At a terminal, typing `1` is correct (the
  entries are the same repository).  Without a terminal the picker's
  read fails and `restore.sh` exits 1 before asking for the password.
  Name the repository to skip the picker: `sh restore.sh --repo NAME
  ...` (or `LCSAS_REPO=NAME`), where NAME is the directory name under
  `metadata/` on any data disc.  `macos-e2e.yml` runs the plain
  invocation as a named, non-blocking "KNOWN BUG" step, so it turns
  green by itself when this is fixed.
- **Quarantine attribute (intentional: OS behaviour, with a documented
  workaround; the heir-docs half is #490):** macOS may attach
  `com.apple.quarantine` to
  files copied off an optical disc.  Restored files inherit it.
  Strip with `xattr -rd com.apple.quarantine ~/Restored` if needed.

## Variant axes that apply

- **Media type** — TEST_TINY, BD25, BD50, BDXL100 (M-Disc variants
  alias).  All supported; the SHA-fallback is media-agnostic.
- **Multi-tenant** — single repo or multiple.  Same flow either way.
- **Multi-copy** — single or multiple locations.  Mount whichever
  copies the operator has on hand; `--pack-search` finds packs
  wherever they live.
- **Live distro** — not applicable (macOS host is already running).
- **Recovery tier** — tier 1 / 2 / 3, exactly as on Linux.

## Test coverage summary

| Variant | Status | Notes |
|---|---|---|
| Apple Silicon target selection | [covered] | `tests/unit/test_restore_sh_dispatcher.py` — Darwin + arm64 → aarch64-apple-darwin |
| Intel Mac target selection | [covered] | same file — Darwin + x86_64 → x86_64-apple-darwin |
| SHA-256 portable verifier | [covered] | `tests/unit/test_restore_executor.py::TestVerifyIsoSha256` (5 tests) |
| verify_iso ECC→SHA fallback | [covered] | `tests/unit/test_restore_executor.py::TestVerifyIsoFallback` (4 tests) |
| Full end-to-end macOS restore (`restore.sh` journey) | [covered] | `.github/workflows/macos-e2e.yml` (driver `tests/e2e/macos_restore_smoke.py`) runs the real `restore.sh` off `hdiutil`-attached fixture ISOs on `macos-14` (arm64) and `macos-15-intel` (x86_64): all discs attached with the read-only relocate-to-RAM re-exec (byte-exact against the manifest), wrong password (exit 77, no partial tree), and a multi-disc swap prompt (4 data discs, 3 swaps: wrong disc, right disc with ENTER, right disc with no input). One known `restore.sh` bug is shown there as a non-blocking step: the repository picker with 2+ data discs mounted (see Gaps). A stock-hardware pass by a person is still the manual real-Mac drill, #475 |
| `hdiutil` mount flow | [covered] | The same workflow attaches every ISO with `hdiutil attach -readonly -nobrowse -mountpoint /Volumes/<LABEL>`; the manual real-Mac drill is #475 |
| Apple Silicon binary execution | [covered] | `.github/workflows/macos-tier1.yml` runs the committed Mach-O binaries via `tests/recovery_hardening/test_tier1_macos_native.py` on `macos-14` (arm64) and `macos-15-intel` (x86_64) |
