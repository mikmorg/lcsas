# Workflows: Meta Disc (Self-Contained Recovery Tools)

The **meta disc** is the *entry point* for a worst-case LCSAS restore: the
machine that owned the archive is gone, the operating system is gone, the
LCSAS source tree is gone, and the only artifacts left in the world are:

1. One or more **data discs** (BD25 / MDISC100 / BDXL100 — each with its
   own disc catalog).
2. The **meta disc** described in this document.
3. The owner's **password** (deliberately *not* on any disc).

A meta disc is a single optical disc (typically BD25 or MDISC100 — the
smaller media types, because the recovery tools are small) that carries
*everything else needed to drive a restore*: per-target static tool binaries
(`rustic`, `xorriso`, `python3`, `dvdisaster` if present, plus our own
tier-1 binaries), the LCSAS Python source tree, the Python reader
`standalone_restorer.py` (tier 3), the SLIP-0039 `keyshare_combine.py`,
and the orchestrating scripts: `restore.sh` (the one restore driver),
`restore.bat` (Windows) and `recovery/scripts/restore_auto.sh`
(automation).

**LCSAS discs are NOT bootable.** The Alpine live-boot stack was removed
(BOOT-07); there is no kernel, no initramfs, no GRUB/isolinux, and no boot
wizard. The meta disc is an ordinary *non-bootable* disc that the heir
mounts on an existing OS. If the target machine has no OS at all, the heir
uses any other computer — a friend's, a library's, a second-hand laptop —
or makes a live Linux USB and runs `restore.sh` from there (see
`recovery/docs/BOOT.txt`).

The meta disc *deliberately omits* `catalog.db`; see
[Catalog Policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc) below.

Sibling docs: [`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md)
(how the tier-1 binaries are built),
[`docs/workflows/restore-live-usb.md`](restore-live-usb.md),
[`docs/workflows/verify-and-audit.md`](verify-and-audit.md), and
[`docs/architecture.md`](../architecture.md).

## Table of Contents

- [Workflow: `lcsas meta build` — produce the meta disc tree](#workflow-lcsas-meta-build--produce-the-meta-disc-tree)
- [Cross-platform recovery tools (the six APPROVED_TARGETS)](#cross-platform-recovery-tools-the-six-approved_targets)
- [Inventory: what lives on a meta disc](#inventory-what-lives-on-a-meta-disc)
- [Workflow: `lcsas meta verify` — audit a built meta disc](#workflow-lcsas-meta-verify--audit-a-built-meta-disc)
- [Workflow: Single-drive bootstrap (meta disc occupies the only drive)](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive)
- [Catalog policy: why no `catalog.db` on the meta disc](#catalog-policy-why-no-catalogdb-on-the-meta-disc)
- [Workflow: Refresh the meta disc when LCSAS source changes](#workflow-refresh-the-meta-disc-when-lcsas-source-changes)
- [Variant axes summary](#variant-axes-summary)
- [Test coverage summary](#test-coverage-summary)

---

## Workflow: `lcsas meta build` — produce the meta disc tree

**Purpose:** Assemble the self-contained meta disc — bundled tools,
LCSAS source, documentation, restore scripts, the recovery toolchain
artifacts (per-target tier-1/2/3 binaries), optional repository
metadata, and the survivability docs — into an output directory ready
for mastering into an ISO image.

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
  `START_HERE.txt`, `PASSWORD_INFO.txt`, and `CONFIG_SUMMARY.txt`.
- Optional `--db` (or `config.db_path`) to seed the repository metadata.
- The cross-built recovery binaries under `recovery/bin/<arch>/` and the
  pinned upstream cache (run `make build-recovery` — which also runs the
  keyshare cross-builds — and `sh recovery/scripts/fetch_upstream.sh`) for a
  *complete* build —
  otherwise the RST-05 completeness gate fails loud unless
  `--allow-incomplete` is given.

**Steps:**

1. CLI parses `lcsas meta build` and dispatches to `cmd_meta_build`
   (`src/lcsas/cli/commands/meta.py`), which resolves `--output`, optional
   `--config`, and the catalog, then constructs `MetaVolumeBuilder`.
2. `MetaVolumeBuilder.build` checks that `recovery/scripts/restore.sh`
   exists (else a `MetaBuildError`, #689), creates the output directory, drops a
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
   5. `_bundle_standalone_restorer` — generate the single-file Python
      reader `standalone_restorer.py` at the meta root (tier 3).
   6. `_bundle_keyshare_combiner` — copy the stdlib-only SLIP-0039
      `keyshare_combine.py` (+ wordlist) for the heir's share-combining
      pre-step when a password was split.
   7. `_bundle_metadata` — if a catalog was provided, copy each
      repository's metadata (Rustic `config`, `keys`, `index`, and
      `snapshots`) into `metadata/<repo_id>/`. This stage *does not* copy
      `catalog.db` — see the policy section. A repository whose
      `mirror_path` is unreachable is **skipped, not fatal** (a stale
      catalog row must not block a meta disc) — but never silently (#435):
      the skip is warned with the repository ID and path, counted in the
      build summary
      (`Rustic metadata bundled for N of M catalogued repo(s)`), and
      recorded on the disc under `volume_info.json` →
      `repo_metadata.not_bundled`. Reachability is probed in a
      short-lived child process with a 5 s bound, so a stale NFS/CIFS
      mount costs five seconds instead of hanging the build (the
      catalog-side twin of #427). This stage also records, per
      repository, whether it is `retired` and how many of its packs sit
      on volumes that still exist — the two facts the key gate below
      decides on.
   8. `_bundle_recovery_toolchain_artifacts` — copy `recovery/` onto the
      meta disc, add the per-target binaries (`lcsas-restore`,
      **`lcsas-ecc`**, `rustic-static`, and the bundled CPython) and merge
      `recovery/MANIFEST.sha256` (`_bundle_upstream_binaries` +
      `_bundle_tier1_binaries`). From a git checkout it copies only the
      git-tracked files under `recovery/` (the set the manifest is built
      from), plus `recovery/bin/` as it stands, so untracked files such as the
      fuzzer output a local `make audit-gate` leaves in
      `recovery/fuzz/corpus/` stay off the disc (#562). Outside a git
      checkout it copies the whole folder minus build debris and fuzzer
      output, and warns that any other stray file will fail
      `lcsas meta verify --strict`.
   9. `_write_restore_script` — copy `recovery/scripts/restore.sh` to the
      disc root as `restore.sh`, the disc's one restore driver, stamped
      with the build SHA and date, and leave a two-line redirect stub in
      `recovery/scripts/` (its `recovery/MANIFEST.sha256` row is
      refreshed). The automation wrapper `recovery/scripts/restore_auto.sh`
      came over with the rest of `recovery/` in step 8. #689 retired the
      legacy bash driver (`restore_legacy.sh`), its Python helper
      (`tools/restore_single_drive.py`), the bash `restore-auto.sh` and
      the root copy `restore_c89.sh`; a rebuild into an old output folder
      deletes them. With no `recovery/` tree there is no driver to ship,
      so `build()` stops with a `MetaBuildError` before any bundling: run
      `lcsas meta build` from a source checkout (or pass
      `--project-root <checkout>`), not from a wheel or the host-A Docker
      image.
   10. `_write_readme` / `_write_readme_txt` — write `README_RESTORE.md`
       (from `src/lcsas/meta/assets/README_RESTORE.md`) and a plain-text
       twin. The README is a short pointer to `recovery/docs/RECOVER.txt`
       and `RECOVER_WINDOWS.txt`, where the restore steps live.
       `tests/unit/test_meta_assets_golden.py` pins the SHA-256 of both
       files (#689).
   11. `_write_volume_info` — write `volume_info.json` with `type: "meta"`,
       bundled-tool list, tool versions, timestamps, and `repo_metadata`
       (which catalogued repositories' keys this disc does — and does
       not — carry), plus its plain-text twin `VOLUME_INFO.txt` (this is
       the meta disc, when it was built, the LCSAS version, and that it
       holds the tools but no catalog).
   12. `_write_start_here` — write `START_HERE.txt`, plus `PASSWORD_INFO.txt`,
       `CONFIG_SUMMARY.txt` (config builds), and `DISC_CARE.txt` via the
       `HolographicInjector`.
3. The `.incomplete` marker is removed once every stage succeeds.
   `cmd_meta_build` then warns about any file in the built `recovery/`
   that `recovery/MANIFEST.sha256` does not list: the files
   `lcsas meta verify --strict` would report as `EXTRA` (#562).
4. **RST-05 completeness gate:** `cmd_meta_build` then calls
   `builder.missing_required_contents()` (the `required_meta_paths()`
   contract). If any per-target tier-1/2/3 binary, `lcsas-ecc`, CPython
   tree, or root artifact is missing, the build **fails loud** and lists
   every gap grouped by `APPROVED_TARGET` — unless `--allow-incomplete`
   was passed (single-arch developer builds).
5. The output directory is then handed to `xorriso` (out of band; not
   part of `meta build` itself) to master into an ISO image, optionally
   with DVDisaster RS03 ECC, and burned the same way as data discs.
   The volume label is always `LCSAS_META`, whatever the archive's
   `label_prefix`: the estate card, `START_HERE.txt` and every restore
   guide tell the heir to look for that label (#588).
   `xorriso -as mkisofs -r -J -joliet-long -iso-level 3 -V LCSAS_META -o meta.iso <output>`

**Expected outcome:** `args.output` contains a complete meta disc tree,
no `.incomplete` marker, and a `volume_info.json` whose `type` field is
`"meta"`. With a populated recovery `bin/` tree the RST-05 gate passes.
The directory is ready to feed to `xorriso` for ISO mastering.

**Variant axes that apply:**
- **Media type:** BD25 or MDISC100 in practice — the meta payload is small
  (a few hundred MB of tools + source + docs + per-target binaries).
- **Optical drive count:** *biggest behavior difference* at restore time.
  With multiple drives, leave the meta disc in one drive and rotate data
  discs through the other(s); with a single drive, self-extract the
  meta disc first — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive).
- **Storage stage:** the meta disc is always one of the discs; each
  refresh re-bundles tools and source from the current project tree.
- **Completeness:** default builds enforce the full per-target contract
  (RST-05); `--allow-incomplete` permits a single-arch dev build.

**Test coverage:**
- `tests/unit/test_meta_builder.py` (build creates expected directory
  layout, bundled tools function, restore.sh parses as bash, cascades
  present, START_HERE / disc-care files generated, `standalone_restorer.py`
  shipped, one restore driver and none of the retired files (#689),
  no `.incomplete` marker after build; `TestBundleUpstreamBinaries`;
  `TestRecoveryBundleTrackedOnly`: only git-tracked `recovery/` files
  are bundled and a checkout with fuzzer output still passes
  `meta verify --strict`).
- `tests/integration/test_meta_volume_restore.py` end-to-end (build a
  meta disc, nuke everything else, run its `restore.sh` against the
  unpacked data discs, check byte-for-byte file hashes).
- Contract gates: `tests/unit/test_doc_command_contract.py`
  (`test_meta_build_flags_in_docs_exist`) holds every doc-quoted
  `meta build` option to the real subparser.
- Gap: no test that builds with `--db` populated and asserts
  `metadata/<repo_id>/keys` is present.

**Source refs:** `cmd_meta_build` (`src/lcsas/cli/commands/meta.py`);
`MetaVolumeBuilder` + `build()` + `missing_required_contents()`
(`src/lcsas/meta/builder.py`); `required_meta_paths` / `APPROVED_TARGETS`
(`src/lcsas/meta/required_contents.py`).

---

## Cross-platform recovery tools (the six APPROVED_TARGETS)

A complete meta disc bundles, **per approved target**, the full tier
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
make -C recovery keyshare-arches  # cross-build the keyshare combiner
                                  # (also run as part of build-recovery)
sh recovery/scripts/fetch_upstream.sh   # pinned rustic + CPython cache
lcsas meta build --output ./meta  # bundle every target with a cached binary
```

`fetch_upstream.sh` verifies SHA-256 against `recovery/UPSTREAM.sha256` and
short-circuits when the cache is warm; air-gapped owners can rsync the
cache between hosts.

**At restore time**, `recovery/scripts/restore.sh` auto-detects
`(uname -s, uname -m)` and picks the right `recovery/bin/<arch>/` subtree.
Override with `$LCSAS_TARGET=<arch>` if auto-detection misfires (chroot,
foreign-arch emulator, unusual uname). See
`tests/unit/test_restore_sh_dispatcher.py` for the full `(OS, machine)`
matrix and the explicit rejections, and
[`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md) for how the
binaries are built.

**Building the tier-1 binaries** is `lcsas recovery build --arch <arch>`
(see the recovery-toolchain doc); `lcsas meta build` then picks the
results up from `recovery/bin/<arch>/` automatically.
`RecoveryBuilder.SUPPORTED_ARCHES` names only the six approved targets of the
required-contents contract. See
[`../history/CROSS_PLATFORM_META_RFC.md`](../history/CROSS_PLATFORM_META_RFC.md) §6 Q6.

**Source refs:** `APPROVED_TARGETS` / `required_target_paths`
(`src/lcsas/meta/required_contents.py`);
`_bundle_recovery_toolchain_artifacts` / `_bundle_upstream_binaries` /
`_bundle_tier1_binaries` (`src/lcsas/meta/builder.py`);
`recovery/UPSTREAM.sha256`; `recovery/scripts/fetch_upstream.sh`;
`recovery/scripts/restore.sh`.

---

## Inventory: what lives on a meta disc

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
| `tools/lib/python/zstandard/` (optional) | `_bundle_tools` | Native zstd decoder for rustic v2 repositories. |
| `lcsas/src/lcsas/` | `_bundle_source` | LCSAS Python package source (no external deps). |
| `docs/`, `README.md`, `pyproject.toml` | `_bundle_docs` | Project docs including the restic-format spec for last-resort decoding. |
| `restore.sh` (root), `recovery/scripts/restore.sh` (redirect stub) | `_write_restore_script` | The one restore driver: the tier dispatcher heirs run (single-drive default). |
| `restore.bat`, `recovery/scripts/restore.bat`, `recovery/scripts/restore_auto.sh` | `_bundle_recovery_toolchain_artifacts` (copied from the source tree) | Windows dispatcher; POSIX-sh wrapper that runs `restore.sh` once per backup set it finds in the disc root's `metadata/`, with no questions (automation only). |
| `standalone_restorer.py` | `_bundle_standalone_restorer` | The Python reader (tier 3), for when no binary works. |
| `keyshare_combine.py` | `_bundle_keyshare_combiner` | stdlib-only SLIP-0039 share combiner (heir pre-step when a password was split). |
| `README_RESTORE.md` / `README_RESTORE.txt` | `_write_readme` / `_write_readme_txt` | Short pointer to the restore manuals, `recovery/docs/RECOVER.txt` and `RECOVER_WINDOWS.txt`. |
| `START_HERE.txt`, `PASSWORD_INFO.txt`, `CONFIG_SUMMARY.txt`, `DISC_CARE.txt` | `_write_start_here` (via `HolographicInjector`) | Plain-language guidance for non-technical heirs. |
| `recovery/bin/<target>/lcsas-restore` | `_bundle_tier1_binaries` | Tier-1 C89 reader, per APPROVED_TARGET (`.exe` on Windows). |
| `recovery/bin/<target>/rustic-static` | `_bundle_upstream_binaries` | Tier-2 pinned upstream rustic, per target. |
| `recovery/bin/<target>/lcsas-ecc` | `_bundle_tier1_binaries` | **FMT-01:** in-house RS03 verify/repair, per target. Required, not optional. |
| `recovery/bin/<target>/python/...` | `_bundle_upstream_binaries` | Tier-3 bundled CPython tree, per target. |
| `recovery/MANIFEST.sha256` | merged by `_bundle_recovery_toolchain_artifacts` | SHA-256 of the bundled recovery source/scripts (audited by `lcsas meta verify`). |
| `metadata/<repo_id>/{config,keys,index,snapshots}` (optional) | `_bundle_metadata` | Per-repository Rustic state that *doesn't* go stale (keys decrypt any future pack). A repository whose mirror was unreachable at build time is absent here — and named in the build summary and in `volume_info.json` (#435), and fails the build outright if it is a live repository with packs on discs (#437). |
| `VOLUME_INFO.txt` | `_write_volume_info` | Plain-text twin of `volume_info.json` for heirs: this is the meta disc, when it was built, the LCSAS version, and that it holds the tools but no catalog. |
| `volume_info.json` | `_write_volume_info` | `type: "meta"` + bundled-tool inventory + tool versions + `repo_metadata` (repositories whose keys are / are not on this disc, each with its `retired` flag and `packs_on_discs` count, plus `acknowledged` — whether the build was waved through with `--allow-missing-metadata`). |
| **NOT present:** `catalog.db` | — | Deliberately absent — see [Catalog policy](#catalog-policy-why-no-catalogdb-on-the-meta-disc). |
| **NOT present:** any kernel / initramfs / GRUB / isolinux / boot wizard | — | LCSAS discs are NOT bootable (the Alpine live stack was removed, BOOT-07). |

---

## Workflow: `lcsas meta verify` — audit a built meta disc

**Purpose:** Confirm a built meta disc directory (or a mounted meta disc)
is intact and complete before — or long after — it is mastered into an
ISO image.

**Synopsis:**

```bash
lcsas meta verify <output-dir> [--strict]
```

**Steps:**

1. CLI dispatches to `cmd_meta_verify` (`src/lcsas/cli/commands/meta.py`).
2. It reads `<output>/recovery/MANIFEST.sha256` and recomputes the
   SHA-256 of every listed file, reporting `MISSING` and `MISMATCH`.
3. With `--strict`, files present under `recovery/` but *absent* from the
   manifest are reported as `EXTRA`. `lcsas meta build` warns about the
   same files when it builds the tree.
4. Independent of the manifest, it runs the **RST-05 required-contents
   check** (`required_meta_paths()`), reporting any `ABSENT` contract
   artifact (a never-bundled target the manifest can't catch). The
   `VINTAGE_NOTE` explains that an ABSENT verdict against an older disc may
   be expected.
5. Exit 0 when the meta disc is intact and complete; exit 1 on any
   issue.

**Expected outcome:** `Meta disc verify PASSED: N files match …; all
required-contents artifacts present.` on a clean meta disc; a per-issue
listing and exit 1 otherwise.

This mirrors `make verify-recovery` (which audits the *build-host* upstream
cache) but operates on the output of `lcsas meta build` — useful for
catching bit-rot on a meta disc tree before mastering, or periodic
disc-health checks on a mounted meta disc. See
[`verify-and-audit.md`](verify-and-audit.md) for the full integrity story.

**Source refs:** `cmd_meta_verify` (`src/lcsas/cli/commands/meta.py`);
`required_meta_paths` / `VINTAGE_NOTE` (`src/lcsas/meta/required_contents.py`).

---

## Workflow: Single-drive bootstrap (meta disc occupies the only drive)

**Purpose:** Handle the worst-case hardware configuration: one optical
drive, a stack of data discs, and the meta disc. The meta disc occupies
the drive at the start, so `restore.sh` copies everything it needs onto
local disk *before* the drive is freed to accept data discs. (This is a
software step on a running OS — the disc is not bootable.)

**Prerequisites:**
- A meta disc and the host's existing OS (or a live Linux USB).
- Room in the home folder for the tools folder (about 130 MB for one
  platform's binaries, plus the pack cache: about the size of the data
  discs read).
- The password (a password file on a USB stick, or typed in from paper).

**Steps:**

1. Mount the meta disc and run the restore from it:
   ```bash
   sudo mount /dev/sr0 /mnt
   sh /mnt/restore.sh ~/restored/ latest
   ```
2. `restore.sh` sees it runs off a read-only disc and copies itself,
   this computer's `recovery/bin/<triple>/`, the docs and the disc's
   `metadata/` into its tools folder, `~/lcsas-restore-tools`, then runs
   that copy. Nothing on the disc is in use any more.
3. It asks for the **newest data disc**: eject the meta disc, insert the
   data disc with the latest year and then the highest number in its
   label, mount it if it does not open by itself, and press Enter. That
   disc's snapshots and index replace the meta disc's copies in the
   tools folder, and its `catalog.db` is copied there too.
4. It asks for the backup set (`Choose a repository (number or name):`)
   and its password (or the set's share cards).
5. Tier 1 runs. At each disc box, eject, insert the named data disc and
   press Enter (a disc that mounts by itself is found without Enter).
   The first time it sees a data disc it copies all of it into the pack
   cache, so each disc goes in about once.
6. After a restore that succeeds it asks whether to restore another
   backup set from the same discs; when you decline, the tools folder
   goes.

For scripted runs use `recovery/scripts/restore_auto.sh`; on Windows use
`restore.bat` (`docs/workflows/restore-windows.md`). (The older Bash
driver `restore_legacy.sh`, with its `INSERT DISC:` prompt and
"Directory Mode", was retired in #689.)

**Expected outcome:** The restore completes; the optical drive holds the
meta disc only at the very start and is free for data discs afterward.
If the restore stops (`q` at a disc box, Ctrl-C), the tools folder stays
with the discs already copied, and `sh ~/lcsas-restore-tools/resume.sh`
carries it on with no meta disc.

**Variant axes that apply:**
- **Media type:** the data-disc media type determines swap count, not the
  bootstrap mechanism.
- **Optical drive count:** **single-drive is the only configuration where
  this bootstrap matters** — with multiple drives the meta disc never needs
  to leave its drive.
- **Storage stage:** the cache lives on the restoring computer's local disk
  and is filled from the discs.
- **Repository selection:** with more than one backup set, `restore.sh`
  lists them by name and asks; `LCSAS_REPO` (or `--repo`) picks one
  without the list.

**Test coverage:**
- `tests/recovery_hardening/test_restore_sh_journey.py` and
  `test_restore_sh_tools_folder.py`: the newest-disc step, the set list,
  the password, another set, the tools folder and resume.sh.
- `tests/unit/test_meta_builder.py::TestMovedRestoreScripts` (one
  restore driver; `restore_auto.sh` finds the sets on the meta disc).
- `tests/integration/test_meta_volume_restore.py` runs the meta disc's
  `restore.sh` end-to-end against unpacked data discs. Tier 1's own
  swap prompt is covered by
  `recovery/tests/test_multidisc.py::case_interactive_swap`.
- Not proven yet: `tests/integration/test_interactive_restore.py` (a
  pseudoterminal plus loop-mount disc swaps, root only) and
  `scripts/smoke_single_drive.py` (a cdemu drive, root only) were
  ported to the current `restore.sh` (#756): no retired flag, discs
  found where they are mounted (`LCSAS_MOUNT_DIRS`), the newest-disc
  step and tier 1's real disc question. Neither has been run as root
  since, so neither counts as working coverage until it passes. Both
  self-skip without root; `tests/unit/test_restore_sh_driver.py`
  checks their shared driver against `restore.sh` and tier 1 on every
  run.

**Source refs:** `recovery/scripts/restore.sh` and `_write_restore_script`
(`src/lcsas/meta/builder.py`).

---

## Key gate: when a missing repo key FAILS the build

**Policy:** `lcsas meta build` exits **1** when a catalogued repository
satisfies all three of:

1. it has packs on at least one volume whose `status != 'DESTROYED'`, **and**
2. it is not `retired` (`repositories.status`, schema v10), **and**
3. no Rustic keys were bundled for it.

Everything else keeps the #436 behaviour exactly — reported, exit 0.

**Why a gate at all:** #436 made the gap loud, but a build script that
only checks the exit code would still master and burn a disc that cannot
decrypt a repository's packs. Burning a disc you cannot prove restorable is
the thing this gate exists to prevent.

**Why it is narrowed, not blanket:** LCSAS cannot tell "mirror
temporarily unmounted" from "mirror gone for good" using `mirror_path`
alone. A blanket rule would fire on every routine build in an estate
with one long-dead repository, get pasted into the build script permanently,
and thereafter read as "checked" while checking nothing. So the gate
derives from *packs on discs* — data that exists right now and needs
these keys — mirroring `HolographicInjector.inject_metadata`'s existing
rule (`src/lcsas/staging/metadata.py`): a repository with data *here* hard-fails,
everything else skips.

**What is deliberately NOT gated:**

| Case | Why exempt |
|---|---|
| No catalog at all | `_bundle_metadata` returns early, so a build without a catalog bundles **zero** repositories — a supported, tested configuration (the blind-restore fixture builds one). A rule that failed 2-of-3 repositories while passing 0-of-3 would not be defensible. |
| Retired repository | The owner has stated the mirror is gone for good via `lcsas repo retire` — see [multi-tenant.md](multi-tenant.md#retire-a-repository-lcsas-repo-retire). |
| Repository with zero packs on discs | Nothing out there needs its keys yet (empty or freshly registered repository). |
| Packs only on `DESTROYED` volumes | Same filter the tier-1 C reader uses (`TIER1_FROZEN_SURFACE` in `src/lcsas/db/schema.py`), so gate and reader agree on what "data that still exists" means. |

`packs.is_pruned` is deliberately **not** filtered: `is_pruned` is not part
of the tier-1 frozen surface, so tier-1 will restore a pruned pack that is
still on a disc. A repository whose on-disc packs are all pruned still
needs its keys.

**The way out:** the error names the offending repositories and offers all three
remedies — mount the mirror and rebuild, `lcsas repo retire <repo>`, or
`--allow-missing-metadata` for this build. That last one is a **new flag,
deliberately not `--allow-incomplete`**: `--allow-incomplete` means "dev
build lacking per-target binaries", it lives habitually in build scripts,
and letting it double as the override would silence a survivability gap by
accident. When it is used, `volume_info.json` records
`repo_metadata.acknowledged: true`, so the **disc itself** carries the fact
that the gap was accepted — years later an heir can tell an accepted gap
from a defect, long after the build log is gone.

**Note:** `lcsas repo remove --force` is *not* a remedy. It deletes the
repository's packs and snapshots from the catalog; an owner whose data is on
burned discs must never be pushed toward it to quiet a gate. That is why
retirement exists.

**The other shape of the same gap (#367 / #443):** this gate covers a
mirror that was **unreachable** at build time, so the repository never gets
bundled at all. But the RST-05 completeness gate (`missing_required_contents()`,
[above](#workflow-lcsas-meta-build--produce-the-meta-disc-tree)) can
*also* fail a build for a mirror that **was** reachable and bundled but
whose `keys/` subtree was itself absent — `metadata/<repo>/keys`. Both
are the identical failure ("this meta disc cannot decrypt repository X"),
just reached by different routes, so both answer to the same valve:
`--allow-missing-metadata` only. `--allow-incomplete` covers neither.

**Source refs:** `_bundle_metadata`, `_read_retired_repos`,
`_read_packs_on_discs`, `RepoMetadataResult` (`src/lcsas/meta/builder.py`);
the exit-code decision in `cmd_meta_build` (`src/lcsas/cli/commands/meta.py`).

**Test coverage:** `tests/unit/test_cli_comprehensive.py::TestCmdMetaBuildKeyGate`;
`tests/unit/test_meta_builder.py::TestBundleMetadata`;
`tests/unit/test_cli_comprehensive.py::TestCmdMetaBuildPerRepoKeysNotAllowIncomplete`
(the `metadata/<repo>/keys` variant).

---

## Catalog policy: why no `catalog.db` on the meta disc

**Policy:** `_bundle_metadata` deliberately copies each repository's
metadata (`config`, `keys`, `index`, `snapshots`) but **never** copies the
LCSAS catalog (`catalog.db`). The decision is documented in the docstring of
`_bundle_metadata` (`src/lcsas/meta/builder.py`):

> The meta disc does NOT carry a catalog.db — it would always be stale
> (pre-dating data discs burned after the meta disc). Instead, the restore
> script reads the catalog on the data discs. We do bundle repository
> metadata (keys, config, index, snapshots) because keys are needed to
> decrypt packs and don't go stale.

**Why it works:** every data disc carries a disc catalog — the
`HolographicInjector` (`src/lcsas/staging/metadata.py`) burns a complete
copy of the catalog (`catalog.db`) onto every data disc at burn time.
Therefore:

1. Any data disc is sufficient to seed the restore. `restore.sh` reads
   `catalog.db` off the mounted data discs, picking the freshest, and
   hands tier 1 a copy in the tools folder.
2. Discs of older volumes carry older disc catalogs; discs of newer
   volumes carry newer ones (a disc burned later to replace a disc of an
   old volume carries that old volume's catalog).
3. So `restore.sh`, run off the meta disc, first asks for the NEWEST data
   disc (the latest year, then the highest number, in its label): its
   catalog and its copy of each backup set's metadata are the fullest,
   including volumes burned *after* the meta disc. The metadata copies
   on the data discs rank above the meta disc's own (C2, #605).
4. Tier 1 never swaps to a catalog older than the one it started with.
   `recovery/scripts/restore_auto.sh` runs `restore.sh` per backup set
   with the newest-disc question off, so mount the newest data disc
   before running it.

**Net effect:** the meta disc never goes stale in a way that matters.
Rustic keys never go stale (a 2020-burnt meta disc can still decrypt a
2030-burnt pack with the same key file), and the catalog gap is bridged by
the disc catalog on the freshest data disc.

**Source refs:** `_bundle_metadata` (`src/lcsas/meta/builder.py`); the
catalog pick and the newest-disc step in `recovery/scripts/restore.sh`;
`HolographicInjector` (`src/lcsas/staging/metadata.py`).

---

## Workflow: Refresh the meta disc when LCSAS source changes

**Purpose:** Re-mint the meta disc so it carries the latest LCSAS code,
bumped tool versions, freshly cross-built tier-1 binaries, and (if
newly-added repositories exist) updated repository metadata.

**Prerequisites:**
- A development checkout of LCSAS with the desired source revision. For a
  meta disc you will burn, use a clean git checkout of that revision (a
  `git worktree` of the tag, say). The builder bundles only the
  git-tracked files under `recovery/`, so untracked files cannot reach
  the disc, but it copies tracked files as they are in the work tree,
  local edits included; a clean checkout makes the disc reproducible.
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
   script constants. Then run `lcsas meta verify --strict /tmp/meta-NEW`,
   which must pass.
4. Master the directory into an ISO image with `xorriso`, volume label
   `LCSAS_META` (`-V LCSAS_META`; see the build steps above), optionally
   RS03-ECC it, and burn it the same way as a data disc.
5. Nothing is recorded in the catalog: the meta disc is not a volume, and
   `meta build` never writes to the master catalog.
6. Keep the previous meta disc(s) with the other discs: an heir uses the
   newest, and if its tools fail, the one burned closest in date to the
   data discs (`docs/ESTATE_PLANNING.md`).

**Expected outcome:** A new meta disc carrying the current LCSAS source, the
same `volume_info.json` schema with a newer `created_at`, and the same
catalog policy (no `catalog.db`).

**Variant axes that apply:**
- **Media type:** unchanged — BD25/MDISC100 in practice.
- **Optical drive count:** unaffected — refresh runs on a workstation.
- **Storage stage:** the input is the development tree; the output is one
  of the discs.
- **Cadence:** driven by LCSAS releases, key rotation, or per-disc
  `volume_events` review; not a per-burn-session activity.

**Test coverage:**
- `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder::test_no_pycache_in_source`
  ensures the refreshed source is clean of `__pycache__`.
- `test_no_incomplete_marker_after_build` confirms a successful rebuild
  leaves no `.incomplete` flag.
- Gap: no idempotency / overwrite test on re-running `build()` over an
  existing output directory.

**Source refs:** `cmd_meta_build` (`src/lcsas/cli/commands/meta.py`);
`MetaVolumeBuilder.build` + `_bundle_source` (`src/lcsas/meta/builder.py`).

---

## Variant axes summary

| Axis | Values | Where it matters |
|---|---|---|
| Media type | BD25, MDISC100 (typical), TEST_TINY (CI) | `lcsas meta build` output size and mastering choice (`src/lcsas/config/media.py`). |
| Optical drive count | 1 (must self-extract before swap) vs ≥2 (meta disc stays loaded) | **Biggest behavior difference** — see [Single-drive bootstrap](#workflow-single-drive-bootstrap-meta-disc-occupies-the-only-drive). |
| Storage stage | The discs (the meta disc itself), local disk (extracted copy + cache) | Drives how the heir copies tools off before swapping discs. |
| Completeness (RST-05) | full per-target contract vs `--allow-incomplete` dev build | `missing_required_contents()` (`src/lcsas/meta/builder.py`). |
| Target arch at restore | one of the six APPROVED_TARGETS | `restore.sh` `(uname -s, uname -m)` dispatch; `$LCSAS_TARGET` override. |
| Catalog freshness | Newest data disc first vs the meta disc's older copies | The newest-disc step and catalog pick in `recovery/scripts/restore.sh`. |

---

## Test coverage summary

| Workflow | Unit | Integration | Gaps |
|---|---|---|---|
| `lcsas meta build` | `tests/unit/test_meta_builder.py::TestMetaVolumeBuilder`, `::TestBundleUpstreamBinaries` | `tests/integration/test_meta_volume_restore.py` | No `--db`-populated `metadata/` assertion. |
| `lcsas meta verify` | `tests/unit/test_meta_builder.py` (manifest + required-contents) | — | None significant. |
| Meta disc inventory | `test_build_creates_directory_structure`, `test_bundled_*_works`, `test_start_here_generated`, `test_standalone_restorer_bundled`, `TestMovedRestoreScripts` (the two scripts under `recovery/scripts/` run from there, manifest rows), `TestMetaVolumeInfoTxt` | `TestMetaVolumeRestore::test_meta_volume_has_all_tools` / `_has_source` / `_has_docs` | None significant. |
| Cross-platform bundling | `TestBundleUpstreamBinaries` (5 tests), `test_restore_sh_dispatcher.py` | — | Per-target restore exercised in recovery-toolchain CI. |
| Single-drive bootstrap | `test_restore_sh_journey.py` (the newest-disc step), `recovery/tests/test_multidisc.py::case_interactive_swap` (tier 1's swap prompt) | `TestMetaVolumeRestore` (directory mode) | No test proven to swap discs in one drive through `restore.sh`: `test_interactive_restore.py` was ported (#756) but not yet run as root. |
| Catalog policy | Code inspection | `TestMetaVolumeRestore` exercises the disc catalog implicitly | No direct `--reseed` precedence test. |
| Source refresh | `test_no_pycache_in_source`, `test_no_incomplete_marker_after_build` | — | No idempotency / re-run test. |
| Docs-vs-reality | `tests/unit/test_doc_command_contract.py` (`test_meta_build_flags_in_docs_exist`) | — | — |
