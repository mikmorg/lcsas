# Workflows: Meta-Volume (Self-Contained Rescue Disc)

The **meta-volume** is the *entry point* for worst-case LCSAS recovery: the
machine that owned the archive is gone, the operating system is gone, the
LCSAS source tree is gone, and the only artifacts left in the world are:

1. One or more **data discs** (BD25 / MDISC100 / BDXL100 — each holographic).
2. The **meta-disc** described in this document.
3. The operator's **encryption key file** (deliberately *not* on any disc).

A meta-disc is a single optical (typically BD25 or MDISC100 — the smaller
media tiers, because the rescue payload is small) that carries *everything
else needed to drive a restore*: per-target static tool binaries
(`rustic`, `xorriso`, `python3`, `dvdisaster` if present, plus our own
tier-1 binaries), the LCSAS Python source tree, a pure-Python
`standalone_restorer.py` fallback, the `restore_single_drive.py` stdlib
helper, the SLIP-0039 `keyshare_combine.py`, and the orchestrating
`restore.sh` / `restore_auto.sh` / `restore.bat` scripts.

**LCSAS discs are NOT bootable.** The Alpine live-boot stack was removed
(BOOT-07); there is no kernel, no initramfs, no GRUB/isolinux, and no boot
wizard. The meta-disc is a *data ISO* that the operator mounts on an
existing OS. If the target machine has no OS at all, the operator uses any
other computer — a friend's, a library's, a second-hand laptop — or makes a
live Linux USB and runs `restore.sh` from there (see `recovery/docs/BOOT.txt`).

The meta-disc *deliberately omits* `catalog.db`; see
[Catalog Policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc) below.

Sibling docs: [`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md)
(how the tier-1 binaries are built),
[`docs/workflows/restore-bare-metal.md`](restore-bare-metal.md),
[`docs/workflows/verify-and-audit.md`](verify-and-audit.md), and
[`docs/architecture.md`](../architecture.md).

## Table of Contents

- [Workflow: `lcsas meta build` — produce the meta-volume tree](#workflow-lcsas-meta-build--produce-the-meta-volume-tree)
- [Cross-platform recovery (the six APPROVED_TARGETS)](#cross-platform-recovery-the-six-approved_targets)
- [Inventory: what lives on a meta-disc](#inventory-what-lives-on-a-meta-disc)
- [Workflow: `lcsas meta verify` — audit a built meta-volume](#workflow-lcsas-meta-verify--audit-a-built-meta-volume)
- [Workflow: Single-drive bootstrap (meta-disc occupies the only drive)](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive)
- [Catalog policy: why no `catalog.db` on the meta-disc](#catalog-policy-why-no-catalogdb-on-the-meta-disc)
- [Workflow: Refresh the meta-disc when LCSAS source changes](#workflow-refresh-the-meta-disc-when-lcsas-source-changes)
- [Variant axes summary](#variant-axes-summary)
- [Test coverage summary](#test-coverage-summary)

---

## Workflow: `lcsas meta build` — produce the meta-volume tree

**Purpose:** Assemble the self-contained rescue volume — bundled tools,
LCSAS source, documentation, restore scripts, the recovery toolchain
artifacts (per-target tier-1/2/3 binaries), optional Rustic per-repo
metadata, and the survivability docs — into an output directory ready
for ISO mastering.

**Synopsis:**

```bash
lcsas [--config lcsas.toml] [--db archive.db] meta build --output DIR \
      [--project-root DIR] [--allow-no-zstd] \
      [--allow-no-dvdisaster-source] [--allow-incomplete]
```

`--config` / `--db` are **global** flags and must come *before* the
subcommand. `--output` / `-o` is required.

**Prerequisites:**
- `rustic` and `xorriso` available on PATH (`_REQUIRED_TOOLS`,
  `src/lcsas/meta/builder.py`).
- `python3` (its runtime is bundled too).
- Optional: `dvdisaster` on PATH (auto-bundled if present, an
  `_OPTIONAL_TOOLS` member).
- Optional `--project-root` if not auto-detecting from the installed
  package.
- Optional `--config TOML` so survivability fields populate
  `START_HERE.txt`, `KEY_INFO.txt`, and `CONFIG_SUMMARY.txt`.
- Optional `--db` (or `config.db_path`) to seed per-repo Rustic metadata.
- The cross-built recovery binaries under `recovery/bin/<arch>/` and the
  pinned upstream cache (run `make build-recovery` / `make keyshare-arches`
  and `sh recovery/scripts/fetch_upstream.sh`) for a *complete* build —
  otherwise the RST-05 completeness gate fails loud unless
  `--allow-incomplete` is given.

**Steps:**

1. CLI parses `lcsas meta build` and dispatches to `cmd_meta_build`
   (`src/lcsas/cli/main.py`), which resolves `--output`, optional
   `--config`, and the catalog DB, then constructs `MetaVolumeBuilder`.
2. `MetaVolumeBuilder.build` creates the output directory, drops a
   `.incomplete` marker, and runs each stage in order
   (`MetaVolumeBuilder.build`, `src/lcsas/meta/builder.py`):
   1. `_bundle_tools` — bundle `rustic`, `xorriso`, and `python3` into
      `tools/bin/` + `tools/lib/` (plus `dvdisaster` if present and the
      native `zstandard` package when available).
   2. `_bundle_source` — copy `src/` from the project root into
      `lcsas/src/` (skipping `__pycache__`, `*.pyc`, `.git`).
   3. `_bundle_docs` — copy `docs/`, `README.md`, and `pyproject.toml`.
   4. `_bundle_dvdisaster_source` — bundle the pinned dvdisaster RS03
      source tarball (the format spec is only re-implementable against
      the source it transcribes; gated by `--allow-no-dvdisaster-source`).
   5. `_bundle_standalone_restorer` — generate the pure-Python single-file
      `standalone_restorer.py` at the meta root (tier-3 fallback).
   6. `_bundle_restore_helper` — copy `restore_single_drive.py` into
      `tools/`.
   7. `_bundle_keyshare_combiner` — copy the stdlib-only SLIP-0039
      `keyshare_combine.py` (+ wordlist) for the split-key heir pre-step.
   8. `_bundle_metadata` — if a catalog DB was provided, copy per-repo
      Rustic `config`, `keys`, `index`, and `snapshots` into
      `metadata/<repo_id>/`. This stage *does not* copy `catalog.db` —
      see the policy section.
   9. `_bundle_recovery_toolchain_artifacts` — copy the recovery `bin/`
      tree (per-target `lcsas-restore`, `rustic-static`, **`lcsas-ecc`**,
      and the bundled CPython) and merge `recovery/MANIFEST.sha256`
      onto the volume (`_bundle_upstream_binaries` + `_bundle_tier1_binaries`).
   10. `_write_restore_script` / `_write_restore_auto_script` — render
       `recovery/scripts/restore.sh` and `restore_auto.sh` (plus
       `restore.bat` on the Windows path) from the in-source script
       constants and set them executable.
   11. `_write_readme` / `_write_readme_txt` — render `README_RESTORE.md`
       and a plain-text twin.
   12. `_write_volume_info` — write `volume_info.json` with `type: "meta"`,
       bundled-tool list, tool versions, and timestamps.
   13. `_write_start_here` — write `START_HERE.txt`, plus `KEY_INFO.txt`,
       `CONFIG_SUMMARY.txt` (config builds), and `DISC_CARE.txt` via the
       `HolographicInjector`.
3. The `.incomplete` marker is removed once every stage succeeds.
4. **RST-05 completeness gate:** `cmd_meta_build` then calls
   `builder.missing_required_contents()` (the `required_meta_paths()`
   contract). If any per-target tier-1/2/3 binary, `lcsas-ecc`, CPython
   tree, or root artifact is missing, the build **fails loud** and lists
   every gap grouped by `APPROVED_TARGET` — unless `--allow-incomplete`
   was passed (single-arch developer builds).
5. The output directory is then handed to `xorriso` (out of band; not
   part of `meta build` itself) to master into an ISO, optionally with
   DVDisaster RS03 ECC, and burned the same way as data discs.

**Expected outcome:** `args.output` contains a complete meta-volume tree,
no `.incomplete` marker, and a `volume_info.json` whose `type` field is
`"meta"`. With a populated recovery `bin/` tree the RST-05 gate passes.
The directory is ready to feed to `xorriso` for ISO mastering.

**Variant axes that apply:**
- **Media type:** BD25 or MDISC100 in practice — the meta payload is small
  (a few hundred MB of tools + source + docs + per-target binaries).
- **Optical drive count:** *biggest behavior difference* at restore time.
  With multiple drives, leave the meta-disc in one drive and rotate data
  discs through the other(s); with a single drive, self-extract the
  meta-disc first — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive).
- **Recovery tier:** the meta-disc is always tier-2 cold storage; each
  refresh re-bundles tools and source from a hot project tree.
- **Completeness:** default builds enforce the full per-target contract
  (RST-05); `--allow-incomplete` permits a single-arch dev build.

**Test coverage:**
- `tests/unit/test_meta_builder.py` (build creates expected directory
  layout, bundled tools function, restore.sh parses as bash, cascades
  present, START_HERE / disc-care files generated, `standalone_restorer.py`
  shipped, `restore_single_drive.py` shipped, single-drive defaults wired,
  no `.incomplete` marker after build; `TestBundleUpstreamBinaries`).
- `tests/integration/test_meta_volume_restore.py` end-to-end (build
  meta-volume, nuke everything else, restore using ONLY the meta-volume's
  bundled tools, verify byte-for-byte file hashes).
- Contract gates: `tests/unit/test_doc_command_contract.py`
  (`test_meta_build_flags_in_docs_exist`) holds every doc-quoted
  `meta build` option to the real subparser.
- Gap: no test that builds with `--db` populated and asserts
  `metadata/<repo_id>/keys` is present.

**Source refs:** `cmd_meta_build` (`src/lcsas/cli/main.py`);
`MetaVolumeBuilder` + `build()` + `missing_required_contents()`
(`src/lcsas/meta/builder.py`); `required_meta_paths` / `APPROVED_TARGETS`
(`src/lcsas/meta/required_contents.py`).

---

## Cross-platform recovery (the six APPROVED_TARGETS)

A complete meta-volume bundles, **per approved target**, the full tier
stack: tier-1 `lcsas-restore` (our C89 reader), tier-2 `rustic-static`,
tier-3 a bundled CPython, and the in-house **`lcsas-ecc`** RS03
verify/repair tool (FMT-01). `required_meta_paths()`
(`src/lcsas/meta/required_contents.py`) is the contract; the six
`APPROVED_TARGETS` are:

| Target (rust triple) | Notes |
|---|---|
| `x86_64-unknown-linux-musl`     | Linux x86_64, static |
| `aarch64-unknown-linux-musl`    | Linux ARM64, static (Pi 4/5, Asahi, Graviton) |
| `armv7-unknown-linux-gnueabihf` | Linux 32-bit ARM (Pi 1/2/3/Zero) |
| `aarch64-apple-darwin`          | macOS Apple Silicon (built via `zig cc -target aarch64-macos`) |
| `x86_64-apple-darwin`           | macOS Intel (`zig cc -target x86_64-macos`) |
| `x86_64-pc-windows-gnu`         | Windows (`.exe`; POSIX-sh / `restore.bat` driver) |

**Workflow:**

```bash
make build-recovery               # cross-build tier-1 lcsas-* per arch (zig cc)
make keyshare-arches              # cross-build the keyshare combiner
sh recovery/scripts/fetch_upstream.sh   # pinned rustic + CPython cache
lcsas meta build --output ./meta  # bundle every target with a cached binary
```

`fetch_upstream.sh` verifies SHA-256 against `recovery/UPSTREAM.sha256` and
short-circuits when the cache is warm; air-gapped operators can rsync the
cache between hosts.

**At recovery time**, `recovery/scripts/restore.sh` auto-detects
`(uname -s, uname -m)` and picks the right `recovery/bin/<arch>/` subtree.
Override with `$LCSAS_TARGET=<arch>` if auto-detection misfires (chroot,
foreign-arch emulator, unusual uname). See
`tests/unit/test_restore_sh_dispatcher.py` for the full `(OS, machine)`
matrix and the explicit rejections, and
[`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md) for how the
binaries are built.

**Building the tier-1 binaries** is `lcsas recovery build --arch <arch>`
(see the recovery-toolchain doc); `lcsas meta build` then picks the
results up from `recovery/bin/<arch>/` automatically. Two extra targets
(`riscv64`, `aarch64-windows`) build via `RecoveryBuilder.SUPPORTED_ARCHES`
but are not part of the required-contents contract. See
[`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q6.

**Source refs:** `APPROVED_TARGETS` / `required_target_paths`
(`src/lcsas/meta/required_contents.py`);
`_bundle_recovery_toolchain_artifacts` / `_bundle_upstream_binaries` /
`_bundle_tier1_binaries` (`src/lcsas/meta/builder.py`);
`recovery/UPSTREAM.sha256`; `recovery/scripts/fetch_upstream.sh`;
`recovery/scripts/restore.sh`.

---

## Inventory: what lives on a meta-disc

Authoritative sources: `MetaVolumeBuilder.build` and the layout docstring
on `MetaVolumeBuilder` (`src/lcsas/meta/builder.py`), plus the
required-contents contract (`required_meta_paths`,
`src/lcsas/meta/required_contents.py`).

| Path on disc | Origin | What it is |
|---|---|---|
| `tools/bin/rustic` | `_bundle_tools` | Rustic backup binary (host arch). |
| `tools/bin/xorriso` | `_bundle_tools` | ISO authoring / extraction. |
| `tools/bin/dvdisaster` (optional) | `_bundle_tools` | RS03 ECC tool (only if found on PATH). |
| `tools/bin/python3` + `tools/lib/...` | `_bundle_tools` | Portable CPython interpreter + stdlib + shared libs. |
| `tools/lib/python/zstandard/` (optional) | `_bundle_tools` | Native zstd decoder for rustic v2 repos. |
| `tools/restore_single_drive.py` | `_bundle_restore_helper` | stdlib-only single-drive disc-swap helper. |
| `lcsas/src/lcsas/` | `_bundle_source` | LCSAS Python package source (no external deps). |
| `docs/`, `README.md`, `pyproject.toml` | `_bundle_docs` | Project docs including the restic-format spec for last-resort decoding. |
| `recovery/scripts/restore.sh`, `restore_auto.sh`, `restore.bat` | `_write_restore_script` / `_write_restore_auto_script` | Tier dispatchers (single-drive default + scripted + Windows). |
| `standalone_restorer.py` | `_bundle_standalone_restorer` | Pure-Python restic decoder (tier-3) when no binary works. |
| `keyshare_combine.py` | `_bundle_keyshare_combiner` | stdlib-only SLIP-0039 share combiner (split-key heir pre-step). |
| `README_RESTORE.md` / `README_RESTORE.txt` | `_write_readme` / `_write_readme_txt` | Human-readable restore instructions. |
| `START_HERE.txt`, `KEY_INFO.txt`, `CONFIG_SUMMARY.txt`, `DISC_CARE.txt` | `_write_start_here` (via `HolographicInjector`) | Plain-language guidance for non-technical recovery operators. |
| `recovery/bin/<target>/lcsas-restore` | `_bundle_tier1_binaries` | Tier-1 C89 reader, per APPROVED_TARGET (`.exe` on Windows). |
| `recovery/bin/<target>/rustic-static` | `_bundle_upstream_binaries` | Tier-2 pinned upstream rustic, per target. |
| `recovery/bin/<target>/lcsas-ecc` | `_bundle_tier1_binaries` | **FMT-01:** in-house RS03 verify/repair, per target. Required, not optional. |
| `recovery/bin/<target>/python/...` | `_bundle_upstream_binaries` | Tier-3 bundled CPython tree, per target. |
| `recovery/MANIFEST.sha256` | merged by `_bundle_recovery_toolchain_artifacts` | SHA-256 of the bundled recovery source/scripts (audited by `lcsas meta verify`). |
| `metadata/<repo_id>/{config,keys,index,snapshots}` (optional) | `_bundle_metadata` | Per-repo Rustic state that *doesn't* go stale (keys decrypt any future pack). |
| `volume_info.json` | `_write_volume_info` | `type: "meta"` + bundled-tool inventory + tool versions. |
| **NOT present:** `catalog.db` | — | Deliberately absent — see [Catalog policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc). |
| **NOT present:** any kernel / initramfs / GRUB / isolinux / boot wizard | — | LCSAS discs are NOT bootable (the Alpine live stack was removed, BOOT-07). |

---

## Workflow: `lcsas meta verify` — audit a built meta-volume

**Purpose:** Confirm a built meta-volume directory (or a mounted meta-disc)
is intact and complete before — or long after — it is mastered into an ISO.

**Synopsis:**

```bash
lcsas meta verify <output-dir> [--strict]
```

**Steps:**

1. CLI dispatches to `cmd_meta_verify` (`src/lcsas/cli/main.py`).
2. It reads `<output>/recovery/MANIFEST.sha256` and recomputes the
   SHA-256 of every listed file, reporting `MISSING` and `MISMATCH`.
3. With `--strict`, files present under `recovery/` but *absent* from the
   manifest are reported as `EXTRA`.
4. Independent of the manifest, it runs the **RST-05 required-contents
   check** (`required_meta_paths()`), reporting any `ABSENT` contract
   artifact (a never-bundled target the manifest can't catch). The
   `VINTAGE_NOTE` explains that an ABSENT verdict against an older disc may
   be expected.
5. Exit 0 when the meta-volume is intact and complete; exit 1 on any
   issue.

**Expected outcome:** `Meta-volume verify PASSED: N files match …; all
required-contents artifacts present.` on a clean volume; a per-issue
listing and exit 1 otherwise.

This mirrors `make verify-recovery` (which audits the *build-host* upstream
cache) but operates on the output of `lcsas meta build` — useful for
catching bit-rot on a meta-volume before mastering, or periodic
disc-health checks on a mounted meta-disc. See
[`verify-and-audit.md`](verify-and-audit.md) for the full integrity story.

**Source refs:** `cmd_meta_verify` (`src/lcsas/cli/main.py`);
`required_meta_paths` / `VINTAGE_NOTE` (`src/lcsas/meta/required_contents.py`).

---

## Workflow: Single-drive bootstrap (meta-disc occupies the only drive)

**Purpose:** Handle the worst-case hardware configuration: one optical
drive, a stack of data discs, and the meta-disc. The meta-disc occupies
the drive at the start, so it must self-extract everything it needs onto
disk *before* the drive is freed to accept data discs. (This is a software
extraction step on a running OS — the disc is not bootable.)

**Prerequisites:**
- A meta-disc and the host's existing OS (or a live Linux USB).
- Enough RAM/local disk to hold a working copy of the meta-volume payload
  (a few hundred MB).
- The encryption key file from external media (USB stick, paper-typed key).

**Steps:**

1. Mount the meta-disc at `/mnt/meta` on the running OS.
2. **Copy the meta-volume off the disc** so the drive can be freed
   (`README_RESTORE.md` Step 1 of single-drive mode):
   ```bash
   sudo mount /dev/sr0 /mnt/meta
   cp -r /mnt/meta /tmp/lcsas-meta
   cd /tmp/lcsas-meta
   sudo umount /mnt/meta
   ```
3. **Eject the meta-disc.** `restore.sh` uses the optical drive as the
   data-disc loader; its prompt loop unmounts + ejects before asking the
   operator to swap discs.
4. **Insert any data disc** (the highest-numbered, if known, minimises the
   chance of needing a catalog upgrade mid-loop).
5. Run from the extracted copy (single-drive mode is the default;
   interactive prompts):
   ```bash
   sh recovery/scripts/restore.sh ~/restored/ latest
   # script prompts: Repository: REPO_NAME
   #                 Password:   <type the password>
   ```
   For scripted runs use `restore_auto.sh`; on Windows use `restore.bat`.
6. Phase 1 (bootstrap) mounts the inserted disc, reads its `catalog.db`,
   invokes `tools/restore_single_drive.py bootstrap …`, and emits a pick
   list describing every data disc the restore will visit.
7. Phase 2 (ingest) walks the volume list. For each volume it ejects the
   current disc, prompts for the wanted disc, then copies every needed pack
   into the cache, verifying SHA-256 on each copy. If a newly-inserted disc
   carries a *fresher* catalog (`MAX(created_at)`), it re-runs
   `bootstrap --reseed` and refreshes the pick list mid-loop.
8. Phase 3 (finalize) verifies every required pack is present and intact in
   the cache, classifying any missing pack as recoverable (alternate disc
   available) or unrecoverable.
9. After finalize succeeds, the wrapper invokes `rustic restore` (or, if no
   rustic binary works, `standalone_restorer.py`) against the assembled
   cache to write files into the target.

**Expected outcome:** The restore completes against the assembled cache;
the optical drive holds the meta-disc only at the very start and is free
for data discs afterward. State persists in
`$CACHE_DIR/restore-state.json` so an interrupted restore can be resumed by
re-running the same command.

**Variant axes that apply:**
- **Media type:** the data-disc media type determines swap count, not the
  bootstrap mechanism.
- **Optical drive count:** **single-drive is the only configuration where
  this bootstrap matters** — with multiple drives the meta-disc never needs
  to leave its drive.
- **Recovery tier:** the cache lives on tier-1 (warm) local disk and is
  populated from tier-2 (cold) discs.
- **Repository selection:** running without a repo makes
  `restore_single_drive.py bootstrap` list available repositories and exit
  so the operator can pick one.

**Test coverage:**
- `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder::test_single_drive_helper_bundled`
  and `::test_restore_script_single_drive_default`.
- `tests/unit/test_meta_builder.py::TestSingleDriveBitsStandalone`
  (dispatcher, bash syntax, helper write).
- `tests/integration/test_meta_volume_restore.py` covers the directory-mode
  (`--isos`) path end-to-end; the interactive single-drive prompt loop is
  not yet simulated (gap).

**Source refs:** `RESTORE_SCRIPT` / `RESTORE_AUTO_SCRIPT` constants and
`_write_restore_script` (`src/lcsas/meta/builder.py`);
`src/lcsas/meta/restore_single_drive.py`.

---

## Catalog policy: why no `catalog.db` on the meta-disc

**Policy:** `_bundle_metadata` deliberately copies Rustic per-repo metadata
(`config`, `keys`, `index`, `snapshots`) but **never** copies the LCSAS
catalog (`catalog.db`). The decision is documented in the docstring of
`_bundle_metadata` (`src/lcsas/meta/builder.py`):

> The meta disc does NOT carry a catalog.db — it would always be stale
> (pre-dating data discs burned after the meta disc). Instead, the restore
> script bootstraps from the catalog on the first data disc the operator
> inserts, and upgrades organically when it encounters a fresher catalog on
> a later disc. We do bundle Rustic metadata (keys, config, index,
> snapshots) because keys are needed to decrypt packs and don't go stale.

**Why it works:** every data disc is *holographic* — the
`HolographicInjector` (`src/lcsas/staging/metadata.py`) burns a complete
`catalog.db` snapshot onto every data disc at burn time. Therefore:

1. Any data disc is sufficient to seed the restore. The bootstrap phase of
   `restore.sh` reads `catalog.db` off whichever disc the operator inserts
   first.
2. Older discs carry older catalogs; newer discs carry newer catalogs. A
   freshness token (`MAX(created_at) FROM volumes`) orders them.
3. During Phase 2 (ingest), every newly-inserted disc's freshness token is
   compared against the bootstrap catalog's. If a disc has a *fresher*
   catalog, `restore.sh` re-runs `bootstrap --reseed` to replace the
   in-cache metadata and re-emit the pick list. Volumes burned *after* the
   meta-disc appear in the updated list and get visited later in the same
   loop.
4. The non-interactive `restore_auto.sh` performs the same organic upgrade,
   optionally auto-selecting the highest-labeled disc first to minimise the
   number of upgrades needed.

**Net effect:** the meta-disc never goes stale in a way that matters.
Rustic keys never go stale (a 2020-burnt meta-disc can still decrypt a
2030-burnt pack with the same key file), and the catalog gap is bridged by
the holographic copy on the freshest data disc.

**Source refs:** `_bundle_metadata` (`src/lcsas/meta/builder.py`);
`RESTORE_SCRIPT` / `RESTORE_AUTO_SCRIPT` (`src/lcsas/meta/builder.py`,
catalog-upgrade glue); `src/lcsas/meta/restore_single_drive.py`
(`phase_bootstrap`, `--reseed`); `HolographicInjector`
(`src/lcsas/staging/metadata.py`).

---

## Workflow: Refresh the meta-disc when LCSAS source changes

**Purpose:** Re-mint the meta-disc so it carries the latest LCSAS code,
bumped tool versions, freshly cross-built tier-1 binaries, and (if
newly-added repos exist) updated per-repo Rustic metadata.

**Prerequisites:**
- A development checkout of LCSAS with the desired source revision.
- Same tool prerequisites as `lcsas meta build` (and the cross-built
  `recovery/bin/<arch>/` tree + upstream cache for a complete build).

**Steps:**

1. Update the LCSAS source tree (git pull, version bump, etc.) and run
   `make lint`, `make test-unit`, `make typecheck` to confirm the build
   inputs are healthy.
2. Rebuild the recovery toolchain if it changed (`make build-recovery`,
   `make keyshare-arches`, `sh recovery/scripts/fetch_upstream.sh`).
3. Run `lcsas [--config etc/lcsas.toml] [--db /var/lib/lcsas/archive.db]
   meta build --output /tmp/meta-NEW` (`--config`/`--db` are global flags
   and must come *before* the subcommand). The full pipeline regenerates
   `lcsas/src/` from the current project root and re-renders all in-source
   script constants.
4. Master the directory into an ISO with `xorriso` (optionally RS03-ECC it)
   and burn it the same way as a data disc.
5. Record the new meta-disc as a `volumes.type='meta'` row in the catalog
   so the holographic catalog on future data discs knows about it. (This is
   a manual `INSERT` or external script — `meta build` itself does not
   write to `catalog.db`.)
6. Retire the previous meta-disc(s) per estate-planning policy
   (`docs/ESTATE_PLANNING.md`).

**Expected outcome:** A new meta-disc carrying the current LCSAS source, the
same `volume_info.json` schema with a newer `created_at`, and the same
catalog policy (no `catalog.db`).

**Variant axes that apply:**
- **Media type:** unchanged — BD25/MDISC100 in practice.
- **Optical drive count:** unaffected — refresh runs on a workstation.
- **Recovery tier:** source artifact is hot (dev tree); output is tier-2.
- **Cadence:** driven by LCSAS releases, key rotation, or per-disc
  `volume_events` review; not a per-burn-session activity.

**Test coverage:**
- `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder::test_no_pycache_in_source`
  ensures the refreshed source is clean of `__pycache__`.
- `test_no_incomplete_marker_after_build` confirms a successful rebuild
  leaves no `.incomplete` flag.
- Gap: no idempotency / overwrite test on re-running `build()` over an
  existing output directory.

**Source refs:** `cmd_meta_build` (`src/lcsas/cli/main.py`);
`MetaVolumeBuilder.build` + `_bundle_source` (`src/lcsas/meta/builder.py`).

---

## Variant axes summary

| Axis | Values | Where it matters |
|---|---|---|
| Media type | BD25, MDISC100 (typical), TEST_TINY (CI) | `lcsas meta build` output size and mastering choice (`src/lcsas/config/media.py`). |
| Optical drive count | 1 (must self-extract before swap) vs ≥2 (meta-disc stays loaded) | **Biggest behavior difference** — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive). |
| Recovery tier | Tier-2 cold (the disc), Tier-1 warm (extracted copy + cache) | Drives how the operator stages tools before swapping discs. |
| Completeness (RST-05) | full per-target contract vs `--allow-incomplete` dev build | `missing_required_contents()` (`src/lcsas/meta/builder.py`). |
| Target arch at restore | one of the six APPROVED_TARGETS | `restore.sh` `(uname -s, uname -m)` dispatch; `$LCSAS_TARGET` override. |
| Catalog freshness | Bootstrap-from-first-disc vs organic upgrade on fresher discs | `src/lcsas/meta/restore_single_drive.py`. |

---

## Test coverage summary

| Workflow | Unit | Integration | Gaps |
|---|---|---|---|
| `lcsas meta build` | `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder`, `::TestBundleUpstreamBinaries` | `tests/integration/test_meta_volume_restore.py` | No `--db`-populated `metadata/` assertion. |
| `lcsas meta verify` | `tests/unit/test_meta_builder.py` (manifest + required-contents) | — | None significant. |
| Meta-disc inventory | `test_build_creates_directory_structure`, `test_bundled_*_works`, `test_start_here_generated`, `test_standalone_restorer_bundled`, `test_single_drive_helper_bundled` | `TestMetaVolumeRestore::test_meta_volume_has_all_tools` / `_has_source` / `_has_docs` | None significant. |
| Cross-platform bundling | `TestBundleUpstreamBinaries` (5 tests), `test_restore_sh_dispatcher.py` | — | Per-target restore exercised in recovery-toolchain CI. |
| Single-drive bootstrap | `test_single_drive_helper_bundled`, `test_restore_script_single_drive_default`, `TestSingleDriveBitsStandalone` | `TestMetaVolumeRestore` (directory mode only) | No interactive prompt-loop simulation. |
| Catalog policy | Code inspection | `TestMetaVolumeRestore` exercises the holographic catalog implicitly | No direct `--reseed` precedence test. |
| Source refresh | `test_no_pycache_in_source`, `test_no_incomplete_marker_after_build` | — | No idempotency / re-run test. |
| Docs-vs-reality | `tests/unit/test_doc_command_contract.py` (`test_meta_build_flags_in_docs_exist`) | — | — |
