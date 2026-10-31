# Restore from a Running Linux Host

This document covers the **easy-mode** restore path: LCSAS is already
installed on a working Linux host, the host can mount the archive
discs (or has access to extracted ISO trees), and the `rustic` binary is
on `PATH`. The flow is:

1. `lcsas restore plan` — read the catalog, ask `rustic` which packs the
   target snapshot needs, then map those packs back to volumes
   and emit a disc pick list.
2. `lcsas restore exec` — re-run the same dry-run to learn the pack set,
   ingest packs from each volume (interactive prompt or `--volume-dir`),
   prepare a temporary rustic cache, then call `rustic restore` against
   the cache.

This is *not* the path for a restore after a disaster. Sibling docs
cover the harder modes:

- [`docs/workflows/restore-bare-metal.md`](restore-bare-metal.md) —
  restore when the LCSAS host itself is gone (its boot path is not
  built today; no LCSAS disc is bootable).
- [`docs/workflows/restore-windows.md`](restore-windows.md) —
  restore from a Windows workstation.
- [`docs/workflows/restore-disc-only.md`](restore-disc-only.md) —
  restore from a single disc without the master catalog
  (`lcsas restore standalone`).

If the host is alive and the master catalog is intact, **start here**.

## Table of contents

- [Assumed environment](#assumed-environment)
- [Workflow: lcsas restore plan](#workflow-lcsas-restore-plan)
- [Workflow: lcsas restore exec](#workflow-lcsas-restore-exec)
- [Workflow: mounting discs](#workflow-mounting-discs)
- [Workflow: password handling](#workflow-password-handling)
- [Workflow: partial restore (single file / subtree)](#workflow-partial-restore-single-file--subtree)
- [Workflow: restore with missing discs (degraded path)](#workflow-restore-with-missing-discs-degraded-path)
- [Gaps and known limitations](#gaps-and-known-limitations)

## Assumed environment

- LCSAS installed (`pip install -e .` from source, or wheel).
- `rustic >= 0.9.0` on `PATH` — checked up-front via
  `check_binary_version("rustic", min_version=(0, 9, 0))` in both
  `cmd_restore_plan()` and `cmd_restore_exec()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
- A populated master catalog at `config.db_path` (the same SQLite file
  the burn pipeline writes to). Schema v10
  ([`CLAUDE.md`](../../CLAUDE.md), Catalog schema section).
- A live rustic mirror for the repository at `repo_cfg.mirror_path` — the
  planner *and* the executor both call `rustic restore --dry-run`
  against the mirror to enumerate required pack IDs
  (`SubprocessRusticRunner.restore_dry_run()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)).
- The repository password file from `repo_cfg.password_file` (used by
  `plan`) or supplied via `--password-file` (required by `exec`).
- Enough free space under `config.staging_path` to hold every required
  pack — `restore exec` creates a `lcsas-restore-*` tempdir there when
  `--cache-dir` is not given
  (`cmd_restore_exec()` in
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).

If any of the above is *not* true (no live mirror, no config, no
catalog), drop to the disc-only path documented in
[`restore-disc-only.md`](restore-disc-only.md).

## Workflow: `lcsas restore plan`

**Purpose:** Tell the owner which discs to fetch from
storage before launching a restore. Pure read operation; no packs are
copied and `rustic` is invoked only for `--dry-run`.

**Prerequisites:**
- `--config` pointing at a valid LCSAS TOML config
  (checked at the top of `cmd_restore_plan()` in
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
- `--repo <name>` matching an entry in `config.repositories`
  (`cmd_restore_plan()`).
- `repo_cfg.password_file` set in the config (otherwise the command
  refuses to continue)
  (`cmd_restore_plan()`).
- The rustic mirror at `repo_cfg.mirror_path` is online and the
  snapshot ID resolves.
- `rustic >= 0.9.0` on `PATH`
  (`check_binary_version()` call in `cmd_restore_plan()`).

**Steps:**
1. The owner runs `lcsas --config /etc/lcsas/lcsas.toml restore plan
   <snapshot_id> --repo <name>` — parser definitions in the
   `restore plan` subparser of
   [`build_parser()`](../../src/lcsas/cli/main.py).
2. `cmd_restore_plan()` loads config and opens the catalog
   ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
3. A `SubprocessRusticRunner` runs `rustic restore <id> --dry-run --json
   /dev/null` against the live mirror to enumerate required pack IDs
   (`cmd_restore_plan()` →
   `SubprocessRusticRunner.restore_dry_run()` in
   [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)).
4. `RestorePlanner.generate_pick_list_v2()` joins those pack IDs against
   the `volume_packs` table to build `{volume_label -> [PackSource, ...]}`
   (each pack carrying its alternate volumes, RST-07) and flags missing
   or deprecated-only packs
   ([`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py)).
5. The CLI prints one line per volume with pack count + MB (plus
   "also on:" alternates), plus a total, plus warnings for
   `DEPRECATED`/`DESTROYED`-only packs and missing packs
   (display block in `cmd_restore_plan()`).

**Expected outcome:**
- Exit `0` and a printed pick list when every required pack lives on
  an active volume.
- Exit `1` when one or more required packs cannot be found on any
  volume in the catalog (the owner must locate or rebuild the disc; see
  the "missing discs" section).
- A non-fatal `WARNING` block when packs only live on `DEPRECATED` /
  `DESTROYED` volumes — the discs may still exist and be readable,
  and can be passed to `restore exec` once mounted
  (deprecated-discs warning block in `cmd_restore_plan()`).

**Variant axes that apply:**
- *Media type*: orthogonal — every burned media type stamps the
  catalog the same way.
- *Multi-repository*: yes — `--repo` selects exactly one repository;
  per-repository password files keep repositories apart.
- *OS*: Linux only for this doc (Windows/bare-metal handled by
  siblings).
- *Optical drive count*: irrelevant; `plan` doesn't touch discs.
- *Multi-copy*: both `plan` and `exec` use the alternates-aware
  `RestorePlanner.generate_pick_list_v2()`
  ([`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py));
  the printed pick list shows each volume's alternates.
  (`generate_pick_list()` remains as the single-choice variant.)
- *ECC*: irrelevant for planning (ECC is checked at ingest time).
- *Storage stage*: the mirror (the easiest case — host alive, catalog
  alive); no disc is read.

**Test coverage:** Existing —
- `tests/unit/test_cli_restore.py::TestRestoreParser::test_restore_plan_parser`
- `tests/unit/test_cli_restore.py::TestCmdRestorePlan::test_plan_displays_pick_list`
- `tests/unit/test_restore.py::TestRestorePlanner::test_basic_pick_list`,
  `test_missing_packs_detected`, `test_multi_volume_pick_list`,
  `test_empty_request`, `test_deprecated_volumes_excluded`.

  **Gaps:** No end-to-end test exercises `plan` against a real
  `rustic` binary; the integration tests skip when `rustic` is absent
  ([`CLAUDE.md`](../../CLAUDE.md), "Pytest writes temp files…" line).
  No test asserts the pretty-printed output format used for
  scripting.

**Source refs:**
- CLI handler: `cmd_restore_plan()` in [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)
- Parser: the `restore plan` subparser in `build_parser()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py))
- Planner: `RestorePlanner.generate_pick_list_v2()` in
  [`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py)
- Rustic dry-run: `SubprocessRusticRunner.restore_dry_run()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)

## Workflow: `lcsas restore exec`

**Purpose:** Re-create snapshot contents under a target directory by
assembling required packs into a local cache and running
`rustic restore`.

**Prerequisites:**
- Everything required by `restore plan`.
- `--password-file <path>` (required argument — it is checked for
  existence before any rustic call to fail fast)
  (early check in `cmd_restore_exec()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)),
  declared in the `restore exec` subparser of `build_parser()`).
- A writable `target_path` on the host
  (positional argument of the `restore exec` subparser).
- Either a TTY for the interactive disc prompt
  (interactive branch of `cmd_restore_exec()`)
  or `--volume-dir <dir>` for scripted runs
  (`--volume-dir` option of the `restore exec` subparser).
- Sufficient free space under `config.staging_path` (cache) or
  `--cache-dir`.

**Steps:**
1. The owner runs e.g. `lcsas --config /etc/lcsas/lcsas.toml restore exec
   <snapshot_id> /restore/target --repo family
   --password-file /root/keys/family.key`
   (the `restore exec` subparser in
   [`build_parser()`](../../src/lcsas/cli/main.py)).
2. Config + catalog load, password-file existence and `rustic` version are
   validated up-front
   (`cmd_restore_exec()` in
   [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
3. `rustic restore --dry-run` enumerates required packs
   (`cmd_restore_exec()` →
   `SubprocessRusticRunner.restore_dry_run()`).
4. `RestorePlanner.generate_pick_list_v2()` builds a pick list **with
   alternates** so corrupted packs can be retried from another volume
   ([`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py)).
5. If any required pack is missing from *every* volume, exit `1` —
   restore is impossible
   (missing-packs check in `cmd_restore_exec()`).
6. Cache dir is `--cache-dir` if given, else a `lcsas-restore-*`
   tempdir under `config.staging_path` registered with the
   `ShutdownManager` for clean exit
   (cache setup in `cmd_restore_exec()`).
7. `RestoreExecutor.prepare_cache()` seeds the cache with `index/`,
   `snapshots/`, `keys/`, and `config` from the live mirror — the
   repository metadata on the discs is *not* used here because the
   mirror is online
   ([`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py),
   called from `cmd_restore_exec()`).
8. For each volume in the pick list, packs are ingested either
   non-interactively from `<volume-dir>/<label>/` (or `<volume-dir>` if
   the per-label subdir is absent)
   (`--volume-dir` branch of `cmd_restore_exec()`)
   or interactively via a mount-path prompt
   (interactive branch of `cmd_restore_exec()`).
9. `RestoreExecutor.ingest_volume()` copies each pack into
   `cache/data/<prefix>/<sha256>`, checks its SHA-256 (unless
   `--skip-verify`), and records any failed pack for retry
   ([`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)).
10. Failed packs are retried from alternate volumes via
    `_retry_from_alternates_batch()` (non-interactive) or
    `_retry_from_alternates_interactive()`
    ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
11. `RestoreExecutor.verify_cache_completeness()` confirms every required
    pack is present before launching `rustic`, so the owner sees a clear
    LCSAS error instead of an opaque rustic crash
    (post-ingest check in `cmd_restore_exec()`,
    [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)).
12. `RestoreExecutor.execute_restore()` invokes
    `rustic -r <cache> --password-file <pw> restore <id> <target>` with
    a 6-hour timeout
    ([`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py),
    `SubprocessRusticRunner.restore()` in
    [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)).
13. Temp cache (if any) is cleaned up in `finally:`
    (end of `cmd_restore_exec()`).

**Expected outcome:**
- Exit `0` with `target_path/` populated with restored files.
- Exit `1` on: missing config, missing repository, missing password file,
  unmet rustic version, packs missing from every volume,
  uncorrectable pack corruption (raised as `PackCorruptionError`),
  cache completeness check failure, or interactive run without a TTY.

**Variant axes that apply:**
- *Media type*: orthogonal — `ingest_volume` reads `data/` from a
  generic mount point.
- *Multi-repository*: yes — pass `--password-file` for the chosen
  repository; the cache is per-restore and not shared across repositories.
- *OS*: Linux (loopback `mount -o loop`, optical drives, NFS/SMB
  shares; see next workflow).
- *Optical drive count*: a single drive works (interactive prompt lets
  you swap discs); multiple drives shrink wall-clock but aren't
  required.
- *Multi-copy*: alternates are pulled via `generate_pick_list_v2()` and
  retried automatically when a pack fails its check
  (alternates map + retry calls in `cmd_restore_exec()`).
- *ECC*: `restore exec` wires an ECC runner into the executor:
  `RestoreExecutor(runner, ecc_runner=select_ecc_runner())` (FMT-01 —
  real dvdisaster if installed, else the in-house decode-only
  `lcsas-ecc`, else `None` → SHA-256-only)
  (`cmd_restore_exec()`; `select_ecc_runner()` in
  [`src/lcsas/ecc/dvdisaster.py`](../../src/lcsas/ecc/dvdisaster.py)).
  ISO-level verify/repair only fires when the ISO file itself is
  reachable — a sibling `<label>.iso` next to the extracted tree in
  `--volume-dir`, or `--iso-dir` for interactive runs; packs read
  from a bare mounted directory are checked per pack by SHA-256 only
  (`RestoreExecutor.ingest_volume()`).
- *Storage stage*: the mirror and the discs — host alive, catalog
  alive, mirrors online.

**Test coverage:** Existing —
- `tests/unit/test_cli_restore.py::TestRestoreParser::test_restore_exec_parser`,
  `test_restore_exec_with_volume_dir`, `test_restore_exec_with_cache_dir`.
- `tests/unit/test_restore_executor.py` — full coverage of
  `prepare_cache`, `ingest_volume` (flat + two-level layout,
  corruption, collect-failures, idempotent re-runs),
  `execute_restore`, `verify_cache_completeness`.
- `tests/unit/test_multi_copy_restore.py` — alternate-volume retry,
  partial-volume ingest, full multi-disc workflows.
- `tests/integration/test_interactive_restore.py` — TTY prompt loop
  (skipped without binaries).

  **Gaps:**
  - No test asserts behaviour when `--volume-dir` exists but is empty
    (currently falls through to "pack not found" then alternate
    retry — the behaviour is correct but not regression-tested).
  - No test verifies the `ShutdownManager` cleans the temp cache on
    SIGINT mid-ingest (`ShutdownManager` itself is covered by
    `tests/unit/test_shutdown.py`; the `restore exec` wiring is not).
  - No CLI-level test covers the `select_ecc_runner()` wiring inside
    `restore exec` (the selector itself is unit-tested in
    `tests/unit/test_dvdisaster.py`).

**Source refs:**
- CLI handler: `cmd_restore_exec()` in [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)
- Parser: the `restore exec` subparser in `build_parser()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py))
- Executor: [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)
- Planner v2 + alternates: `RestorePlanner.generate_pick_list_v2()` in
  [`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py)
- Rustic restore: `SubprocessRusticRunner.restore()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)
- Pack layout helper: `find_pack_file()` / `pack_dest_path()` in
  [`src/lcsas/utils/pack_layout.py`](../../src/lcsas/utils/pack_layout.py)
  (imported at the top of `executor.py`).

## Workflow: mounting discs

**Purpose:** Make pack data available under a directory the executor
can read. `RestoreExecutor.ingest_volume()` reads `<mount>/data/...`
([`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)),
so any technique that exposes that tree works.

**Prerequisites:** Root or `udisks` privileges to mount; ISO images or
discs accessible to the host.

**Steps (loopback ISO mount):**
1. `sudo mkdir -p /mnt/lcsas/<label>`
2. `sudo mount -o loop,ro /path/to/<label>.iso /mnt/lcsas/<label>`
3. In the interactive `restore exec` prompt, type
   `/mnt/lcsas/<label>` when asked for the mount path
   (mount-path prompt in `cmd_restore_exec()`,
   [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).

**Steps (physical optical drive):**
1. Insert disc; udev/`udisks` auto-mounts it at
   `/run/media/<user>/<label>/` (or `/media/...`).
2. Confirm `<mount>/data/` exists (LCSAS-burned discs always do).
3. Type the mount path at the prompt, or pre-stage all discs and use
   `--volume-dir`.

**Steps (network share, multi-host):**
1. NFS: mount the share read-only,
   `mount -o ro nfs-host:/exports/lcsas /mnt/lcsas`.
2. SMB/CIFS: `mount.cifs //host/share /mnt/lcsas -o ro,guest`.
3. Lay out one subdirectory per volume label so `--volume-dir
   /mnt/lcsas` finds them via the `<volume-dir>/<label>/` lookup
   (`--volume-dir` branch of `cmd_restore_exec()`).
4. If the share is flat (all `data/<prefix>/<sha>` files in one tree),
   the executor falls back to `vol_path = vol_dir`
   (per-label fallback in the same branch of `cmd_restore_exec()`).

**Expected outcome:** `<mount>/data/<prefix>/<sha256>` resolvable for
every required pack; ingestion logs `ingested N packs` per volume.

**Variant axes that apply:**
- *Media type*: BD-25, M-DISC, or test images — once mounted, all
  look like a directory tree.
- *Optical drive count*: with one drive use the interactive prompt and
  swap discs between volumes; with N drives mount all simultaneously
  under a common parent and use `--volume-dir`.
- *OS*: Linux only here; Windows mount semantics live in the sibling
  doc.

**Test coverage:** Existing —
- `tests/unit/test_restore_executor.py::TestIngestVolume::test_flat_layout_ingest`
  and `test_two_level_layout_ingest` cover both directory layouts.
- `tests/unit/test_multi_copy_restore.py::test_full_restore_workflow`
  exercises the multi-volume directory layout.

  **Gaps:** No automated test for read-only NFS/SMB mounts or for
  `udisks` auto-unmounting a disc mid-ingest (intentional: both depend on
  host mount infrastructure a CI container does not have). The "single
  drive, swap discs" pattern is covered for the `restore.sh` path by
  `tests/integration/test_interactive_restore.py` (a PTY plus loop-device
  swaps; root, `rustic` and `xorriso` required) and for `restore exec`'s
  alternate-disc prompts by the mocked-`input()` tests in
  `tests/unit/test_cli_restore.py`.

**Source refs:**
- Ingest entry: `RestoreExecutor.ingest_volume()` in
  [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)
- `find_pack_file()` (flat + two-level): imported by `executor.py` from
  [`src/lcsas/utils/pack_layout.py`](../../src/lcsas/utils/pack_layout.py).
- CLI prompt loop: interactive branch of `cmd_restore_exec()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
- `--volume-dir` resolution: non-interactive branch of
  `cmd_restore_exec()`.

## Workflow: password handling

**Purpose:** Supply the repository password to both LCSAS and rustic
without leaking it into logs, argv, or environment dumps.

**Prerequisites:** A password file readable by the account LCSAS runs
as (a plain file whose first line is the repository password — exactly
the rustic `--password-file` contract).

**Steps:**
1. *`restore plan`*: reads the password from
   `config.repositories[<repo>].password_file`
   (password-file check + `restore_dry_run()` call in
   `cmd_restore_plan()`,
   [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
   The config loader resolves the path (the `resolve()` helper inside
   `load_config()`) and warns when the file is missing (missing-file
   warning in `load_config()`; also flagged by `validate_config()`)
   ([`src/lcsas/config/settings.py`](../../src/lcsas/config/settings.py)).
2. *`restore exec`*: requires `--password-file <path>` on the command
   line — there is **no** `LCSAS_PASSWORD` / `RUSTIC_PASSWORD`
   environment fallback, by design (see Gaps). The CLI checks the file
   exists before calling rustic
   (early check in `cmd_restore_exec()`).
3. *Subprocess*: `SubprocessRusticRunner._run()` passes
   `--password-file <path>` as an argv element and inherits the rest
   of the environment unchanged (only `TMPDIR` is set)
   ([`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py),
   `SubprocessRunnerBase._env()` in
   [`src/lcsas/utils/subprocess.py`](../../src/lcsas/utils/subprocess.py)).
4. *Log scrubbing*: on a rustic error, the wrapper masks the password
   path in both argv and stderr before re-raising — credentials never
   land in tracebacks
   (the `CalledProcessError` handler in
   `SubprocessRusticRunner._run()`).

**Expected outcome:** Rustic authenticates against the repository; the
password path appears as `***` in any LCSAS log line that quotes a
failing rustic invocation.

**Variant axes that apply:**
- *Multi-repository*: each repository carries its own `password_file`
  in the TOML config; restoring a different repository means changing
  `--repo` and `--password-file` together.
- *Interactive password entry*: **not supported** (decided against,
  #491). Rustic's own `--password-command` is not surfaced; an owner
  who keeps the password in an HSM must write it to a tmpfs file
  before invoking LCSAS.
- *OS*: file path conventions only — the actual file is opaque bytes.

**Test coverage:** Existing —
- `tests/unit/test_cli_restore.py::TestRestoreParser::test_restore_exec_parser`
  asserts `--password-file` parses into a `Path`.
- `tests/unit/test_cli_restore.py` exec tests pre-create a password
  file and rely on the existence check.

  **Gaps:**
  - The wrapper-level masking is covered:
    `tests/unit/test_rustic_wrapper.py::TestPasswordPathMasking::test_exception_masks_password_path_in_stderr`
    asserts the password path is masked in the raised exception. No
    CLI-level test asserts it end to end through `restore exec`.
  - No test covers a `LCSAS_PASSWORD` env var because the feature does
    not exist — if added it would need both a wrapper change and a
    dedicated test (decided against, #491).
  - No test rejects a password file with insecure permissions (e.g.
    world-readable `0644`). Today LCSAS just opens it.

**Source refs:**
- CLI argument: `--password-file` in the `restore exec` subparser of
  `build_parser()` ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py))
- Existence check: `cmd_restore_exec()` in
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)
- Config loader: `load_config()` in
  [`src/lcsas/config/settings.py`](../../src/lcsas/config/settings.py)
- Rustic argv: `SubprocessRusticRunner._run()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)
- Log masking: the `CalledProcessError` handler in
  `SubprocessRusticRunner._run()`

## Workflow: partial restore (single file / subtree)

**Purpose:** Bring back one file or one directory subtree from a
snapshot without unpacking the entire snapshot.

**Prerequisites:** A successful `restore exec` of the full snapshot
*to a scratch directory*, then copy out what you need; **or** drop to
the rustic CLI directly using the cache LCSAS just built.

**Steps (LCSAS-driven, current behaviour):**
1. Run `lcsas restore exec <snapshot> /scratch/restore --repo <name>
   --password-file <pw> --cache-dir /scratch/cache`. LCSAS does not
   accept a path filter on its own
   (no filter option exists on the `restore exec` subparser in
   [`build_parser()`](../../src/lcsas/cli/main.py)) —
   the executor unconditionally passes only the snapshot id and target
   to rustic
   (`RestoreExecutor.execute_restore()` in
   [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py),
   `SubprocessRusticRunner.restore()` in
   [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)).
2. After completion, `cp -a /scratch/restore/<path-inside-snapshot>
   /final/destination`.

**Steps (advanced, drive rustic directly against the LCSAS cache):**
1. Run `lcsas restore exec ... --cache-dir /scratch/cache` and let the
   ingestion phase complete (it stops at step 12 of the
   `restore exec` workflow once packs are in place).
2. Stop before/after the final rustic call (currently no flag — see
   Gaps), or simply re-run rustic yourself:
   `rustic -r /scratch/cache --password-file /root/keys/family.key
   restore <snapshot>:/path/inside /final/destination`. The cache is
   a valid rustic repository because `prepare_cache` copied `index/`,
   `snapshots/`, `keys/`, `config`
   (`RestoreExecutor.prepare_cache()` in
   [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)).

**Expected outcome:** Only the requested file/subtree appears under the
final destination. Note that **all packs the snapshot needs** are still
materialised in the cache because the dry-run enumerates the full
snapshot's pack set; partial restore saves time on the
rustic-decompression side, not on the disc-ingest side.

**Variant axes that apply:**
- *Multi-repository*: pick the right repository's password file.
- *Storage stage*: the mirror and the discs only (host alive).
- All other axes: orthogonal.

**Test coverage:** Existing —
- `tests/unit/test_multi_copy_restore.py::test_restore_photos_only_from_mixed_volume`
  and `test_restore_docs_only_from_mixed_volume` cover the
  conceptually-related case of restoring one repository's subset from a
  volume shared by several repositories.

  **Gaps:**
  - No first-class `--include` / `--path` / `--filter` flag on
    `lcsas restore exec` — partial restore uses the workaround above
    (decided against, #491).
  - No test exercising the "rustic-against-LCSAS-cache" pattern.

**Source refs:**
- CLI arguments (no filter): the `restore exec` subparser in
  `build_parser()` ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py))
- Restore call (no filter): `RestoreExecutor.execute_restore()` in
  [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)
- Rustic argv: `SubprocessRusticRunner.restore()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)
- Cache preparation (full rustic repository on disk):
  `RestoreExecutor.prepare_cache()` in
  [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)

## Workflow: restore with missing discs (degraded path)

**Purpose:** Restore as much as physically possible when at least one
disc that holds required packs is unavailable (lost, destroyed, or
unreadable).

**Prerequisites:** Catalog reports the destination volume but none of
its discs is mountable. Multi-copy redundancy is the only realistic
way out: the volume(s) holding the same pack on a *different* disc
must still be readable.

**Steps:**
1. Run `lcsas restore plan` first. If `missing_packs` is non-empty,
   restore is *impossible* — every alternate volume for those packs
   is gone
   (missing-packs error block in `cmd_restore_plan()`
   ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py));
   detection via `get_missing_packs()` inside
   `RestorePlanner.generate_pick_list_v2()`).
2. If `deprecated_disc_labels` lists volumes, the catalog believes
   those discs were taken out of service — but if you can physically
   locate and read them, the executor will accept them as `--volume-dir`
   sources or via the interactive prompt
   (deprecated-discs warning block in `cmd_restore_plan()`;
   detection via `get_deprecated_only_packs()` inside
   `generate_pick_list_v2()`).
3. Run `lcsas restore exec`. Pack-level fallback to alternates is
   automatic when a primary volume is corrupt *and* an alternate is
   indexed in the catalog
   (alternates map in `cmd_restore_exec()` +
   `_retry_from_alternates_batch()` in
   [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
4. For an *interactive* run, type `skip` at the mount prompt to
   move past a missing disc; failed/missing packs are collected and
   retried from alternates afterwards
   (`skip` handling and interactive retry in `cmd_restore_exec()`).
5. If alternates do not cover the gap,
   `verify_cache_completeness` reports the surviving holes and the
   command exits `1` *before* invoking rustic — the owner can then
   try the catalog rebuild path
   (`lcsas catalog validate /mnt/disc` — the disc path is positional —
   as recommended by the missing-packs error text in
   `cmd_restore_plan()`).

**Expected outcome:**
- Best case: alternates fill every gap, restore completes normally.
- Partial-loss case: command exits `1` with the list of missing
  pack IDs; rustic is not invoked.
- Worst case (catalog says "missing on all volumes"): no point in
  starting `restore exec`; investigate the catalog and physical
  inventory first.

**Variant axes that apply:**
- *Multi-copy*: this entire workflow only works when packs were
  burned to >= 2 volumes. With single-copy archives the packs on a
  missing disc are lost.
- *Storage stage*: the mirror and the discs (catalog alive); when the
  catalog itself is the problem, drop to the paths in the sibling docs
  that need only the discs.

**Test coverage:** Existing —
- `tests/unit/test_multi_copy_restore.py::test_deprecate_vol_a`,
  `test_destroy_vol_a_and_vol_d`,
  `test_missing_packs_after_destruction`,
  `test_deprecate_one_of_three_copies`,
  `test_pick_list_with_degraded_volumes`,
  `test_viable_combinations_exist`,
  `test_all_4_volumes_is_viable`,
  `test_no_single_volume_is_viable`,
  `test_enumerate_all_viable_2vol_combinations` exhaustively cover
  catalog-side degraded planning.
- `tests/unit/test_restore_executor.py::TestIngestVolume::test_missing_pack_not_counted`,
  `test_collect_failures_returns_result`,
  `test_corrupt_pack_collected_not_raised`,
  `test_mixed_good_and_corrupt` cover the per-pack collect-and-retry
  contract.

  **Gaps:**
  - The interactive `skip` keyword followed by alternate-retry as one
    flow is covered by
    `tests/unit/test_cli_restore.py::TestCmdRestoreExec::test_skip_primary_volume_triggers_alternate_prompt`
    (RST-07; skipped when `rustic` is not installed), with
    `::test_final_missing_error_names_volume_labels` covering the
    all-skipped failure.
  - No test asserts the exact wall-clock behaviour of
    `--volume-dir` when an alternate label's directory is also
    missing — the code logs a warning and falls back to the parent
    `vol_dir`
    (the missing-directory fallback in
    `_retry_from_alternates_batch()`,
    [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)),
    but no regression test pins that.
  - No end-to-end test of the "catalog rebuild after disc loss" loop
    suggested by the error text.

**Source refs:**
- Planner missing/deprecated detection:
  `RestorePlanner.generate_pick_list_v2()` in
  [`src/lcsas/restore/planner.py`](../../src/lcsas/restore/planner.py)
- Catalog queries: `get_missing_packs()`, `get_deprecated_only_packs()`,
  `get_pick_list_with_alternates()` in
  [`src/lcsas/db/queries.py`](../../src/lcsas/db/queries.py)
  (imported at the top of `planner.py`).
- Alternate retry helpers: `_retry_from_alternates_batch()` /
  `_retry_from_alternates_interactive()` in
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)
- Completeness gate: `RestoreExecutor.verify_cache_completeness()` in
  [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)

## Gaps and known limitations

- **No `LCSAS_PASSWORD` env var.** `restore exec` requires
  `--password-file` and rustic is always called with
  `--password-file <argv>`. Adding env-var support requires changes
  to both `SubprocessRusticRunner._run()`
  ([`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py))
  and the `exec_p.add_argument("--password-file", ..., required=True)`
  declaration in the `restore exec` subparser of `build_parser()`
  ([`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
  Decided against (#491): the CLI deliberately takes no password from
  the environment (see
  [README.md](../../README.md#how-lcsas-passes-passwords-to-rustic));
  `restore.sh` on the meta disc is the one exception.
- **No path-filter flag for partial restore.** Snapshot id and target
  are the only arguments forwarded to rustic
  (`SubprocessRusticRunner.restore()` in
  [`src/lcsas/rustic/wrapper.py`](../../src/lcsas/rustic/wrapper.py)).
  The owner must drive rustic directly against the LCSAS-built cache.
  Decided against (#491): rustic run against that cache already
  restores one path (see
  [partial restore](#workflow-partial-restore-single-file--subtree)).
- **ECC verification in `restore exec` needs an ISO file handle
  (intentional: RS03 verify/repair operates on a raw image, which a bare
  mounted directory does not provide).**
  The CLI wires `RestoreExecutor(runner,
  ecc_runner=select_ecc_runner())` (FMT-01,
  `cmd_restore_exec()` in
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)), but ECC
  verify/repair only fires when a `<label>.iso` is reachable — a
  sibling file under `--volume-dir`, or `--iso-dir` for interactive
  runs (`RestoreExecutor.ingest_volume()` in
  [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)).
  Packs ingested from a bare mounted directory get only the per-pack
  SHA-256 check.
- **Missing-disc + missing-catalog combination** is out of scope here
  (intentional) and handled by `lcsas restore standalone` — see
  [`restore-disc-only.md`](restore-disc-only.md).
- **No interactive password prompt** (rustic's `--password-command` is
  not surfaced). Decided against (#491): like the rest of the CLI,
  `restore exec` reads the password from a file. The `restore.sh`
  tier-1 path does prompt.
- **No regression test for cache cleanup on SIGINT** during ingestion
  (despite the `ShutdownManager` wiring in `cmd_restore_exec()`,
  [`src/lcsas/cli/main.py`](../../src/lcsas/cli/main.py)).
  `ShutdownManager` itself is unit-tested (`tests/unit/test_shutdown.py`),
  but nothing signals a running `restore exec`. (Still open.)
