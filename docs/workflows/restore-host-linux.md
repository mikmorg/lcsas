# Restore from a Running Linux Host

This document covers the **easy-mode** restore path: LCSAS is already
installed on a working Linux host, the host can mount the cold-storage
discs (or has access to extracted ISO trees), and the `rustic` binary is
on `PATH`. The flow is:

1. `lcsas restore plan` — read the catalog, ask `rustic` which packs the
   target snapshot needs, then map those packs back to physical volumes
   and emit a disc pick list.
2. `lcsas restore exec` — re-run the same dry-run to learn the pack set,
   ingest packs from each volume (interactive prompt or `--volume-dir`),
   prepare a temporary rustic cache, then call `rustic restore` against
   the cache.

This is *not* the disaster-recovery path. Sibling docs cover the harder
modes:

- [`docs/workflows/restore-bare-metal.md`](restore-bare-metal.md) —
  recover when the LCSAS host itself is gone (boot the meta-volume).
- [`docs/workflows/restore-windows.md`](restore-windows.md) —
  recover from a Windows workstation.
- [`docs/workflows/restore-disc-only.md`](restore-disc-only.md) —
  recover from a single disc without the original catalog
  (`lcsas restore standalone`).

If the host is alive and the catalog is intact, **start here**.

## Table of contents

- [Assumed environment](#assumed-environment)
- [Workflow: lcsas restore plan](#workflow-lcsas-restore-plan)
- [Workflow: lcsas restore exec](#workflow-lcsas-restore-exec)
- [Workflow: mounting volumes](#workflow-mounting-volumes)
- [Workflow: password handling](#workflow-password-handling)
- [Workflow: partial restore (single file / subtree)](#workflow-partial-restore-single-file--subtree)
- [Workflow: restore with missing discs (degraded path)](#workflow-restore-with-missing-discs-degraded-path)
- [Gaps and known limitations](#gaps-and-known-limitations)

## Assumed environment

- LCSAS installed (`pip install -e .` from source, or wheel).
- `rustic >= 0.9.0` on `PATH`
  ([`src/lcsas/cli/main.py:1780`](../../src/lcsas/cli/main.py#L1780),
  [`src/lcsas/cli/main.py:1892`](../../src/lcsas/cli/main.py#L1892)).
- A populated catalog at `config.db_path` (the same SQLite the burn
  pipeline writes to). Schema v5
  ([`CLAUDE.md`](../../CLAUDE.md), Database schema section).
- A live rustic mirror for the repo at `repo_cfg.mirror_path` — the
  planner *and* the executor both call `rustic restore --dry-run`
  against the mirror to enumerate required pack hashes
  ([`src/lcsas/rustic/wrapper.py:150-158`](../../src/lcsas/rustic/wrapper.py#L150-L158)).
- The repository password file from `repo_cfg.password_file` (used by
  `plan`) or supplied via `--password-file` (required by `exec`).
- Enough free space under `config.staging_path` to hold every required
  pack — `restore exec` creates a `lcsas-restore-*` tempdir there when
  `--cache-dir` is not given
  ([`src/lcsas/cli/main.py:1932-1938`](../../src/lcsas/cli/main.py#L1932-L1938)).

If any of the above is *not* true (no live mirror, no config, no
catalog), drop to the disc-only path documented in
[`restore-disc-only.md`](restore-disc-only.md).

## Workflow: `lcsas restore plan`

**Purpose:** Tell the operator which physical discs to fetch from
storage before launching a restore. Pure read operation; no packs are
copied and `rustic` is invoked only for `--dry-run`.

**Prerequisites:**
- `--config` pointing at a valid LCSAS TOML config
  ([`src/lcsas/cli/main.py:1753-1758`](../../src/lcsas/cli/main.py#L1753-L1758)).
- `--repo <name>` matching a key in `config.repositories`
  ([`src/lcsas/cli/main.py:1764-1768`](../../src/lcsas/cli/main.py#L1764-L1768)).
- `repo_cfg.password_file` set in the config (otherwise the command
  refuses to continue)
  ([`src/lcsas/cli/main.py:1771-1775`](../../src/lcsas/cli/main.py#L1771-L1775)).
- The rustic mirror at `repo_cfg.mirror_path` is online and the
  snapshot ID resolves.
- `rustic >= 0.9.0` on `PATH`
  ([`src/lcsas/cli/main.py:1780`](../../src/lcsas/cli/main.py#L1780)).

**Steps:**
1. Operator runs `lcsas --config /etc/lcsas/lcsas.toml restore plan
   <snapshot_id> --repo <name>` — parser definitions at
   ([`src/lcsas/cli/main.py:248-251`](../../src/lcsas/cli/main.py#L248-L251)).
2. `cmd_restore_plan` loads config and opens the catalog
   ([`src/lcsas/cli/main.py:1756-1761`](../../src/lcsas/cli/main.py#L1756-L1761)).
3. A `SubprocessRusticRunner` runs `rustic restore <id> --dry-run --json
   /dev/null` against the live mirror to enumerate required pack hashes
   ([`src/lcsas/cli/main.py:1785-1790`](../../src/lcsas/cli/main.py#L1785-L1790),
   [`src/lcsas/rustic/wrapper.py:150-158`](../../src/lcsas/rustic/wrapper.py#L150-L158)).
4. `RestorePlanner.generate_pick_list` joins those hashes against the
   `volume_packs` table to build `{volume_label -> [Pack, ...]}` and
   flags missing or deprecated-only packs
   ([`src/lcsas/restore/planner.py:66-93`](../../src/lcsas/restore/planner.py#L66-L93)).
5. The CLI prints one line per volume with pack count + MB, plus a
   total, plus warnings for `DEPRECATED`/`DESTROYED`-only packs and
   missing packs
   ([`src/lcsas/cli/main.py:1799-1846`](../../src/lcsas/cli/main.py#L1799-L1846)).

**Expected outcome:**
- Exit `0` and a printed pick list when every required pack lives on
  an active volume.
- Exit `1` when one or more required packs cannot be located in any
  catalog volume (the operator must locate or rebuild the disc; see
  the "missing discs" section).
- A non-fatal `WARNING` block when packs only live on `DEPRECATED` /
  `DESTROYED` volumes — the discs may still be physically recoverable
  and can be passed to `restore exec` once mounted
  ([`src/lcsas/cli/main.py:1814-1827`](../../src/lcsas/cli/main.py#L1814-L1827)).

**Variant axes that apply:**
- *Media type*: orthogonal — every burned media type stamps the
  catalog the same way.
- *Multi-tenant*: yes — `--repo` selects exactly one tenant; per-repo
  password files keep tenants isolated.
- *OS*: Linux only for this doc (Windows/bare-metal handled by
  siblings).
- *Optical drive count*: irrelevant; `plan` doesn't touch discs.
- *Multi-copy*: `generate_pick_list` picks one volume per pack; for
  alternates use `generate_pick_list_v2`
  ([`src/lcsas/restore/planner.py:95-136`](../../src/lcsas/restore/planner.py#L95-L136))
  which `restore exec` uses internally.
- *ECC*: irrelevant for planning (ECC is verified at ingest time).
- *Recovery tier*: T0 (the easiest tier — host alive, catalog alive).

**Test coverage:** Existing —
- `tests/unit/test_cli_restore.py::TestRestoreParser::test_restore_plan_parser`
- `tests/unit/test_cli_restore.py::TestCmdRestorePlan::test_plan_displays_pick_list`
- `tests/unit/test_restore.py::TestRestorePlanner::test_basic_pick_list`,
  `test_missing_packs_detected`, `test_multi_volume_pick_list`,
  `test_empty_request`, `test_deprecated_volumes_excluded`.

  **Gaps:** No end-to-end test exercises `plan` against a real
  `rustic` binary; the integration tier skips when `rustic` is absent
  ([`CLAUDE.md`](../../CLAUDE.md), "Pytest writes temp files…" line).
  No test asserts the pretty-printed output format used for operator
  scripting.

**Source refs:**
- CLI handler: [`src/lcsas/cli/main.py:1745-1848`](../../src/lcsas/cli/main.py#L1745-L1848)
- Parser: [`src/lcsas/cli/main.py:248-251`](../../src/lcsas/cli/main.py#L248-L251)
- Planner: [`src/lcsas/restore/planner.py:60-93`](../../src/lcsas/restore/planner.py#L60-L93)
- Rustic dry-run: [`src/lcsas/rustic/wrapper.py:150-158`](../../src/lcsas/rustic/wrapper.py#L150-L158)

## Workflow: `lcsas restore exec`

**Purpose:** Re-create snapshot contents under a target directory by
assembling required packs into a local cache and running
`rustic restore`.

**Prerequisites:**
- Everything required by `restore plan`.
- `--password-file <path>` (required argument — it is checked for
  existence before any rustic call to fail fast)
  ([`src/lcsas/cli/main.py:1881-1887`](../../src/lcsas/cli/main.py#L1881-L1887),
  parser at
  [`src/lcsas/cli/main.py:267-268`](../../src/lcsas/cli/main.py#L267-L268)).
- A writable `target_path` on the host
  ([`src/lcsas/cli/main.py:264`](../../src/lcsas/cli/main.py#L264)).
- Either a TTY for the interactive disc prompt
  ([`src/lcsas/cli/main.py:1999-2006`](../../src/lcsas/cli/main.py#L1999-L2006))
  or `--volume-dir <dir>` for scripted runs
  ([`src/lcsas/cli/main.py:271-273`](../../src/lcsas/cli/main.py#L271-L273)).
- Sufficient free space under `config.staging_path` (cache) or
  `--cache-dir`.

**Steps:**
1. Operator runs e.g. `lcsas --config /etc/lcsas/lcsas.toml restore exec
   <snapshot_id> /restore/target --repo family
   --password-file /root/keys/family.key`
   ([`src/lcsas/cli/main.py:253-275`](../../src/lcsas/cli/main.py#L253-L275)).
2. Config + DB load, password-file existence and `rustic` version are
   validated up-front
   ([`src/lcsas/cli/main.py:1866-1895`](../../src/lcsas/cli/main.py#L1866-L1895)).
3. `rustic restore --dry-run` enumerates required packs
   ([`src/lcsas/cli/main.py:1900-1904`](../../src/lcsas/cli/main.py#L1900-L1904)).
4. `RestorePlanner.generate_pick_list_v2` builds a pick list **with
   alternates** so corrupted packs can be retried from another volume
   ([`src/lcsas/cli/main.py:1907-1908`](../../src/lcsas/cli/main.py#L1907-L1908),
   [`src/lcsas/restore/planner.py:95-136`](../../src/lcsas/restore/planner.py#L95-L136)).
5. If any required pack is missing from *every* volume, exit `1` —
   restore is impossible
   ([`src/lcsas/cli/main.py:1919-1922`](../../src/lcsas/cli/main.py#L1919-L1922)).
6. Cache dir is `--cache-dir` if given, else a `lcsas-restore-*`
   tempdir under `config.staging_path` registered with the
   `ShutdownManager` for clean exit
   ([`src/lcsas/cli/main.py:1931-1947`](../../src/lcsas/cli/main.py#L1931-L1947)).
7. `RestoreExecutor.prepare_cache` seeds the cache with `index/`,
   `snapshots/`, `keys/`, and `config` from the live mirror — the
   "holographic" copy is *not* used here because the mirror is online
   ([`src/lcsas/restore/executor.py:75-120`](../../src/lcsas/restore/executor.py#L75-L120),
   [`src/lcsas/cli/main.py:1953-1954`](../../src/lcsas/cli/main.py#L1953-L1954)).
8. For each volume in the pick list, packs are ingested either
   non-interactively from `<volume-dir>/<label>/` (or `<volume-dir>` if
   the per-label subdir is absent)
   ([`src/lcsas/cli/main.py:1962-1996`](../../src/lcsas/cli/main.py#L1962-L1996))
   or interactively via a mount-path prompt
   ([`src/lcsas/cli/main.py:1998-2046`](../../src/lcsas/cli/main.py#L1998-L2046)).
9. `RestoreExecutor.ingest_volume` copies each pack into
   `cache/data/<prefix>/<sha256>`, verifies SHA-256 (unless
   `--skip-verify`), and records any failed pack for retry
   ([`src/lcsas/restore/executor.py:122-235`](../../src/lcsas/restore/executor.py#L122-L235)).
10. Failed packs are retried from alternate volumes via
    `_retry_from_alternates_batch` (non-interactive) or
    `_retry_from_alternates_interactive`
    ([`src/lcsas/cli/main.py:2551-2640`](../../src/lcsas/cli/main.py#L2551-L2640)).
11. `RestoreExecutor.verify_cache_completeness` confirms every required
    pack is present before launching `rustic`, so the user sees a clear
    LCSAS error instead of an opaque rustic crash
    ([`src/lcsas/cli/main.py:2048-2068`](../../src/lcsas/cli/main.py#L2048-L2068),
    [`src/lcsas/restore/executor.py:255-290`](../../src/lcsas/restore/executor.py#L255-L290)).
12. `RestoreExecutor.execute_restore` invokes
    `rustic -r <cache> --password-file <pw> restore <id> <target>` with
    a 6-hour timeout
    ([`src/lcsas/restore/executor.py:237-253`](../../src/lcsas/restore/executor.py#L237-L253),
    [`src/lcsas/rustic/wrapper.py:160-168`](../../src/lcsas/rustic/wrapper.py#L160-L168)).
13. Temp cache (if any) is cleaned up in `finally:`
    ([`src/lcsas/cli/main.py:2080-2084`](../../src/lcsas/cli/main.py#L2080-L2084)).

**Expected outcome:**
- Exit `0` with `target_path/` populated with restored files.
- Exit `1` on: missing config, missing repo, missing password file,
  unmet rustic version, packs missing from every volume,
  uncorrectable pack corruption (raised as `PackCorruptionError`),
  cache completeness check failure, or interactive run without a TTY.

**Variant axes that apply:**
- *Media type*: orthogonal — `ingest_volume` reads `data/` from a
  generic mount point.
- *Multi-tenant*: yes — pass `--password-file` for the chosen repo;
  the cache is per-restore and not shared across tenants.
- *OS*: Linux (loopback `mount -o loop`, optical drives, NFS/SMB
  shares; see next workflow).
- *Optical drive count*: a single drive works (interactive prompt lets
  you swap discs); multiple drives shrink wall-clock but aren't
  required.
- *Multi-copy*: alternates are pulled via `generate_pick_list_v2` and
  retried automatically when verification fails
  ([`src/lcsas/cli/main.py:1924-1929`](../../src/lcsas/cli/main.py#L1924-L1929),
  [`src/lcsas/cli/main.py:1986-1996`](../../src/lcsas/cli/main.py#L1986-L1996)).
- *ECC*: `RestoreExecutor.verify_iso` is plumbed but the `exec`
  command does **not** wire an `ECCRunner` in
  ([`src/lcsas/cli/main.py:1950`](../../src/lcsas/cli/main.py#L1950) —
  `RestoreExecutor(runner)`, no ECC argument). ECC verification is
  the operator's responsibility today (e.g. via `dvdisaster -t` before
  mounting).
- *Recovery tier*: T0 — host alive, catalog alive, mirrors online.

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
    SIGINT mid-ingest.
  - No test wires an ECC runner into `restore exec` (the constructor
    accepts one, but the CLI never does).

**Source refs:**
- CLI handler: [`src/lcsas/cli/main.py:1851-2086`](../../src/lcsas/cli/main.py#L1851-L2086)
- Parser: [`src/lcsas/cli/main.py:253-275`](../../src/lcsas/cli/main.py#L253-L275)
- Executor: [`src/lcsas/restore/executor.py`](../../src/lcsas/restore/executor.py)
- Planner v2 + alternates: [`src/lcsas/restore/planner.py:95-136`](../../src/lcsas/restore/planner.py#L95-L136)
- Rustic restore: [`src/lcsas/rustic/wrapper.py:160-168`](../../src/lcsas/rustic/wrapper.py#L160-L168)
- Pack layout helper: [`src/lcsas/utils/pack_layout.py`](../../src/lcsas/utils/pack_layout.py)
  (referenced from `executor.py:14`).

## Workflow: mounting volumes

**Purpose:** Make pack data available under a directory the executor
can read. `RestoreExecutor.ingest_volume` reads `<mount>/data/...`
([`src/lcsas/restore/executor.py:148`](../../src/lcsas/restore/executor.py#L148)),
so any technique that exposes that tree works.

**Prerequisites:** Root or `udisks` privileges to mount; ISO files or
physical media accessible to the host.

**Steps (loopback ISO mount):**
1. `sudo mkdir -p /mnt/lcsas/<label>`
2. `sudo mount -o loop,ro /path/to/<label>.iso /mnt/lcsas/<label>`
3. In the interactive `restore exec` prompt, type
   `/mnt/lcsas/<label>` when asked for the mount path
   ([`src/lcsas/cli/main.py:2010-2017`](../../src/lcsas/cli/main.py#L2010-L2017)).

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
   ([`src/lcsas/cli/main.py:1967-1969`](../../src/lcsas/cli/main.py#L1967-L1969)).
4. If the share is flat (all `data/<prefix>/<sha>` files in one tree),
   the executor falls back to `vol_path = vol_dir`
   ([`src/lcsas/cli/main.py:1968-1969`](../../src/lcsas/cli/main.py#L1968-L1969)).

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

  **Gaps:** No automated test for read-only NFS/SMB mounts; no test for
  the "single drive, swap discs" pattern (would require a TTY
  fixture); no test for handling auto-unmount when `udisks` releases a
  disc mid-ingest.

**Source refs:**
- Ingest entry: [`src/lcsas/restore/executor.py:122-235`](../../src/lcsas/restore/executor.py#L122-L235)
- `find_pack_file` (flat + two-level): referenced at
  [`src/lcsas/restore/executor.py:14`](../../src/lcsas/restore/executor.py#L14)
  → [`src/lcsas/utils/pack_layout.py`](../../src/lcsas/utils/pack_layout.py).
- CLI prompt loop: [`src/lcsas/cli/main.py:2007-2032`](../../src/lcsas/cli/main.py#L2007-L2032).
- `--volume-dir` resolution: [`src/lcsas/cli/main.py:1962-1996`](../../src/lcsas/cli/main.py#L1962-L1996).

## Workflow: password handling

**Purpose:** Supply the rustic repo password to both LCSAS and rustic
without leaking it into logs, argv, or environment dumps.

**Prerequisites:** A password file readable by the LCSAS user (a plain
file whose first line is the rustic password — exactly the rustic
`--password-file` contract).

**Steps:**
1. *`restore plan`*: reads the password from
   `config.repositories[<repo>].password_file`
   ([`src/lcsas/cli/main.py:1771-1775`](../../src/lcsas/cli/main.py#L1771-L1775),
   [`src/lcsas/cli/main.py:1789`](../../src/lcsas/cli/main.py#L1789)).
   The config loader resolves the path and warns when the file is
   missing
   ([`src/lcsas/config/settings.py:189-197`](../../src/lcsas/config/settings.py#L189-L197),
   [`src/lcsas/config/settings.py:350-356`](../../src/lcsas/config/settings.py#L350-L356)).
2. *`restore exec`*: requires `--password-file <path>` on the command
   line — there is **no** `LCSAS_PASSWORD` / `RUSTIC_PASSWORD`
   environment fallback in the wrapper today. The CLI checks the file
   exists before calling rustic
   ([`src/lcsas/cli/main.py:1881-1887`](../../src/lcsas/cli/main.py#L1881-L1887)).
3. *Subprocess*: `SubprocessRusticRunner._run` passes
   `--password-file <path>` as an argv element and inherits the rest
   of the environment unchanged (only `TMPDIR` is set)
   ([`src/lcsas/rustic/wrapper.py:84-98`](../../src/lcsas/rustic/wrapper.py#L84-L98),
   [`src/lcsas/utils/subprocess.py:115-118`](../../src/lcsas/utils/subprocess.py#L115-L118)).
4. *Log scrubbing*: on a rustic error, the wrapper masks the password
   path in both argv and stderr before re-raising — credentials never
   land in tracebacks
   ([`src/lcsas/rustic/wrapper.py:103-118`](../../src/lcsas/rustic/wrapper.py#L103-L118)).

**Expected outcome:** Rustic authenticates against the repo; the
password path appears as `***` in any LCSAS log line that quotes a
failing rustic invocation.

**Variant axes that apply:**
- *Multi-tenant*: each repo carries its own `password_file` in the
  TOML config; cross-tenant restores must change `--repo` and
  `--password-file` together.
- *Interactive password entry*: **not supported** today. Rustic's own
  `--password-command` is not surfaced; operators with HSM-backed
  keys must write to a tmpfs file before invoking LCSAS.
- *OS*: file path conventions only — the actual file is opaque bytes.

**Test coverage:** Existing —
- `tests/unit/test_cli_restore.py::TestRestoreParser::test_restore_exec_parser`
  asserts `--password-file` parses into a `Path`.
- `tests/unit/test_cli_restore.py` exec tests pre-create a password
  file and rely on the existence check.

  **Gaps:**
  - No test asserts that the masked-stderr path is actually applied
    (the masking code is exercised only indirectly when rustic
    fails).
  - No test covers a `LCSAS_PASSWORD` env var because the feature does
    not exist — if added it would need both a wrapper change and a
    dedicated test.
  - No test rejects a password file with insecure permissions (e.g.
    world-readable `0644`). Today LCSAS just opens it.

**Source refs:**
- CLI argument: [`src/lcsas/cli/main.py:267-268`](../../src/lcsas/cli/main.py#L267-L268)
- Existence check: [`src/lcsas/cli/main.py:1881-1887`](../../src/lcsas/cli/main.py#L1881-L1887)
- Config loader: [`src/lcsas/config/settings.py:165-198`](../../src/lcsas/config/settings.py#L165-L198)
- Rustic argv: [`src/lcsas/rustic/wrapper.py:84-98`](../../src/lcsas/rustic/wrapper.py#L84-L98)
- Log masking: [`src/lcsas/rustic/wrapper.py:103-118`](../../src/lcsas/rustic/wrapper.py#L103-L118)

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
   ([`src/lcsas/cli/main.py:263-275`](../../src/lcsas/cli/main.py#L263-L275)) —
   the executor unconditionally passes only the snapshot id and target
   to rustic
   ([`src/lcsas/restore/executor.py:248-253`](../../src/lcsas/restore/executor.py#L248-L253),
   [`src/lcsas/rustic/wrapper.py:160-168`](../../src/lcsas/rustic/wrapper.py#L160-L168)).
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
   a valid rustic repo because `prepare_cache` copied `index/`,
   `snapshots/`, `keys/`, `config`
   ([`src/lcsas/restore/executor.py:75-120`](../../src/lcsas/restore/executor.py#L75-L120)).

**Expected outcome:** Only the requested file/subtree appears under the
final destination. Note that **all packs the snapshot needs** are still
materialised in the cache because the dry-run enumerates the full
snapshot's pack set; partial restore saves time on the
rustic-decompression side, not on the disc-ingest side.

**Variant axes that apply:**
- *Multi-tenant*: pick the right repo's password file.
- *Recovery tier*: T0 only.
- All other axes: orthogonal.

**Test coverage:** Existing —
- `tests/unit/test_multi_copy_restore.py::test_restore_photos_only_from_mixed_volume`
  and `test_restore_docs_only_from_mixed_volume` cover the
  conceptually-related case of restoring a *per-repo* subset from a
  multi-tenant volume.

  **Gaps:**
  - No first-class `--include` / `--path` / `--filter` flag on
    `lcsas restore exec` — partial restore is a workaround today.
  - No test exercising the "rustic-against-LCSAS-cache" pattern.

**Source refs:**
- CLI arguments (no filter): [`src/lcsas/cli/main.py:263-275`](../../src/lcsas/cli/main.py#L263-L275)
- Restore call (no filter): [`src/lcsas/restore/executor.py:237-253`](../../src/lcsas/restore/executor.py#L237-L253)
- Rustic argv: [`src/lcsas/rustic/wrapper.py:160-168`](../../src/lcsas/rustic/wrapper.py#L160-L168)
- Cache preparation (full rustic repo on disk):
  [`src/lcsas/restore/executor.py:75-120`](../../src/lcsas/restore/executor.py#L75-L120)

## Workflow: restore with missing discs (degraded path)

**Purpose:** Recover as much as physically possible when at least one
disc that holds required packs is unavailable (lost, destroyed, or
unreadable).

**Prerequisites:** Catalog reports the destination volume but no copy
is mountable. Multi-copy redundancy is the only realistic way out: the
volume(s) holding the same pack on a *different* disc must still be
readable.

**Steps:**
1. Run `lcsas restore plan` first. If `missing_packs` is non-empty,
   restore is *impossible* — every alternate volume for those packs
   is gone
   ([`src/lcsas/cli/main.py:1829-1846`](../../src/lcsas/cli/main.py#L1829-L1846),
   [`src/lcsas/restore/planner.py:79`](../../src/lcsas/restore/planner.py#L79)).
2. If `deprecated_disc_labels` lists volumes, the catalog believes
   those discs were retired — but if you can physically locate and
   read them, the executor will accept them as `--volume-dir`
   sources or via the interactive prompt
   ([`src/lcsas/cli/main.py:1814-1827`](../../src/lcsas/cli/main.py#L1814-L1827),
   [`src/lcsas/restore/planner.py:80`](../../src/lcsas/restore/planner.py#L80)).
3. Run `lcsas restore exec`. Pack-level fallback to alternates is
   automatic when a primary volume is corrupt *and* an alternate is
   indexed in the catalog
   ([`src/lcsas/cli/main.py:1924-1929`](../../src/lcsas/cli/main.py#L1924-L1929),
   [`src/lcsas/cli/main.py:2551-2594`](../../src/lcsas/cli/main.py#L2551-L2594)).
4. For an *interactive* session, type `skip` at the mount prompt to
   move past a missing disc; failed/missing packs are collected and
   retried from alternates afterwards
   ([`src/lcsas/cli/main.py:2014-2016`](../../src/lcsas/cli/main.py#L2014-L2016),
   [`src/lcsas/cli/main.py:2034-2046`](../../src/lcsas/cli/main.py#L2034-L2046)).
5. If alternates do not cover the gap,
   `verify_cache_completeness` reports the surviving holes and the
   command exits `1` *before* invoking rustic — the operator can then
   try the catalog rebuild path
   (`lcsas catalog validate --disc /mnt/disc`, recommended by the
   error message at
   [`src/lcsas/cli/main.py:1843-1845`](../../src/lcsas/cli/main.py#L1843-L1845)).

**Expected outcome:**
- Best case: alternates fill every gap, restore completes normally.
- Partial-loss case: command exits `1` with the list of unrecoverable
  pack SHA-256s; rustic is not invoked.
- Worst case (catalog says "missing on all volumes"): no point in
  starting `restore exec`; investigate the catalog and physical
  inventory first.

**Variant axes that apply:**
- *Multi-copy*: this entire workflow only works when packs were
  burned to >= 2 volumes. With single-copy archives a missing disc
  is unrecoverable.
- *Recovery tier*: T0 (catalog alive); when the catalog itself is the
  problem, drop to T1/T2 paths in the sibling docs.

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
  - No test exercises the interactive `skip` keyword followed by
    alternate-retry as one flow.
  - No test asserts the exact wall-clock behaviour of
    `--volume-dir` when an alternate label's directory is also
    missing — the code logs a warning and falls back to the parent
    `vol_dir`
    ([`src/lcsas/cli/main.py:2575-2583`](../../src/lcsas/cli/main.py#L2575-L2583)),
    but no regression test pins that.
  - No end-to-end test of the "catalog rebuild after physical disc
    loss" recovery loop suggested by the error text.

**Source refs:**
- Planner missing/deprecated detection: [`src/lcsas/restore/planner.py:79-92`](../../src/lcsas/restore/planner.py#L79-L92)
- DB queries: `get_missing_packs`, `get_deprecated_only_packs`,
  `get_pick_list_with_alternates` — referenced at
  [`src/lcsas/restore/planner.py:9-14`](../../src/lcsas/restore/planner.py#L9-L14).
- Alternate retry helpers: [`src/lcsas/cli/main.py:2551-2655`](../../src/lcsas/cli/main.py#L2551-L2655)
- Completeness gate: [`src/lcsas/restore/executor.py:255-290`](../../src/lcsas/restore/executor.py#L255-L290)

## Gaps and known limitations

- **No `LCSAS_PASSWORD` env var.** `restore exec` requires
  `--password-file` and rustic is always called with
  `--password-file <argv>`. Adding env-var support requires changes
  to both `SubprocessRusticRunner._run`
  ([`src/lcsas/rustic/wrapper.py:84-98`](../../src/lcsas/rustic/wrapper.py#L84-L98))
  and the `exec_p.add_argument("--password-file", ..., required=True)`
  declaration
  ([`src/lcsas/cli/main.py:267-268`](../../src/lcsas/cli/main.py#L267-L268)).
- **No path-filter flag for partial restore.** Snapshot id and target
  are the only arguments forwarded to rustic
  ([`src/lcsas/rustic/wrapper.py:160-168`](../../src/lcsas/rustic/wrapper.py#L160-L168)).
  Users must drive rustic directly against the LCSAS-built cache.
- **No ECC verification wired into `restore exec`.** The constructor
  accepts an `ECCRunner` ([`src/lcsas/restore/executor.py:38-47`](../../src/lcsas/restore/executor.py#L38-L47))
  but the CLI never supplies one
  ([`src/lcsas/cli/main.py:1950`](../../src/lcsas/cli/main.py#L1950)).
- **Missing-disc + missing-catalog combination** is out of scope here
  and handled by `lcsas restore standalone` — see
  [`restore-disc-only.md`](restore-disc-only.md).
- **No interactive password prompt** (rustic's `--password-command` is
  not surfaced).
- **No regression test for cache cleanup on SIGINT** during ingestion
  (despite the `ShutdownManager` wiring at
  [`src/lcsas/cli/main.py:1943-1947`](../../src/lcsas/cli/main.py#L1943-L1947)).
