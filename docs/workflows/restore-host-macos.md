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
   or `recovery/bin/x86_64-apple-darwin/` on Intel
   (see `recovery/scripts/restore.sh:230-270` for the dispatch
   table).
3. The script prompts for the encryption password.  Paste from the
   offline copy.  Alternative non-interactive forms:

   - `LCSAS_PASSWORD='...' sh restore.sh ~/Restored`
   - `LCSAS_PWFILE=/path/to/key.txt sh restore.sh ~/Restored`

4. Tier 1 (prebuilt `lcsas-restore`) runs.  If it succeeds, restored
   files land in `~/Restored/`.  Tier 2 (`rustic-static`) and
   tier 3 (`python3 standalone_restorer.py`, using the meta-disc's
   bundled CPython at `recovery/bin/<target>/python/bin/python3`)
   are reached only if a prior tier crashes; the cascade is
   transparent to the operator.

**Source refs:** `recovery/scripts/restore.sh:230` (dispatch table),
`recovery/scripts/restore.sh:378-460` (tier cascade).

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

This is `restore.sh:243` — the `LCSAS_TARGET` env var short-circuits
auto-detection.

## Gaps and known limitations

- **Boot directly from the meta-disc:** Mac firmware doesn't boot ISO
  9660 on USB optical drives well.  If your Mac is fully bricked,
  recovery from this meta-disc is *not* the path — use macOS Recovery
  Mode or a target-disk-mode dump first, then run the meta-disc
  restore on another Mac.
- **Single-drive multi-disc swap UX** (the `INSERT DISC:` flow in
  `tools/restore_single_drive.py`) is currently Linux-only.  On
  macOS, mount all discs first (Finder will hold them all in
  `/Volumes`) and run the non-interactive restore.
- **Quarantine attribute:** macOS may attach `com.apple.quarantine` to
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
| Full end-to-end macOS restore | [gap] | Requires a macOS CI runner with optical hardware; not currently in CI |
| `hdiutil` mount flow | [gap] | Manual operator step, not automated |
| Apple Silicon binary execution | [gap] | We don't run the bundled aarch64-apple-darwin binaries from x86_64 Linux CI |
