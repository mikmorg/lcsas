# Stage & Burn Pipeline (Session-Based)

The Stage & Burn pipeline is the heart of LCSAS: the workflow that turns
unarchived pack files in the Rustic mirror into permanent, self-describing
optical or tape volumes. It is also the highest-traffic surface of the tool —
every routine archival cycle runs through these two commands, and almost every
"variant axis" (media type, multi-tenant, multi-copy, ECC on/off, etc.)
crosses through them.

The pipeline is implemented as a single orchestrator
(`src/lcsas/burn/orchestrator.py::BurnOrchestrator`) with two entry points.
`stage()` plans volumes via First-Fit-Decreasing bin-packing
(`src/lcsas/binpack/algorithm.py:10`), builds a hardlinked staging tree
(`src/lcsas/staging/builder.py:61`), injects the **holographic catalog** —
SQLite catalog + per-repo Rustic metadata — onto every disc
(`src/lcsas/staging/metadata.py:35`), masters an ISO via xorriso
(`src/lcsas/iso/xorriso.py:98`), and (for optical media only) augments it with
DVDisaster RS03 ECC (`src/lcsas/ecc/dvdisaster.py:46`). `burn_session()` then
streams each ISO to the optical device, reads the disc back to verify, and
records a copy in the catalog. Tape media (LTO8/LTO9) skips ECC because the
LTO drive provides its own correction.

A burn **session** is the unit of resumability. Staging emits a session record
with status `STAGED`, plus one row per volume with the volume in `STAGING`
state. Burning advances each volume `STAGING → BURNING → BURNED → VERIFIED`
(or rolls back to `STAGING` on failure), and finalises the session as
`COMPLETE` or `PARTIAL`. Sessions live until `clean_session()` is invoked,
which deletes the staged ISOs but leaves the catalog records intact so the
volumes remain referenceable.

## Table of contents

- [Pipeline overview](#pipeline-overview)
- [`lcsas stage` — plan and stage volumes only](#lcsas-stage--plan-and-stage-volumes-only)
- [`lcsas burn` — full pipeline (legacy stage + burn)](#lcsas-burn--full-pipeline-legacy-stage--burn)
- [`lcsas burn --session <id>` — burn a previously staged session](#lcsas-burn---session-id--burn-a-previously-staged-session)
- [`lcsas burn --dry-run` — plan + ISO build without burning](#lcsas-burn---dry-run--plan--iso-build-without-burning)
- [`lcsas stage --dry-run` — plan-only (no side effects)](#lcsas-stage---dry-run--plan-only-no-side-effects)
- [`lcsas burn --for-location <name>` — delta burn for a specific location](#lcsas-burn---for-location-name--delta-burn-for-a-specific-location)
- [Per-media-type variants](#per-media-type-variants)
- [Session lifecycle and resuming an interrupted burn](#session-lifecycle-and-resuming-an-interrupted-burn)
- [Variant-axis matrix](#variant-axis-matrix)
- [Test coverage summary](#test-coverage-summary)

## Pipeline overview

The end-to-end flow inside a single session:

1. **Gather packs** — `BurnOrchestrator._gather_packs_for_staging`
   (`src/lcsas/burn/orchestrator.py:838`) queries the catalog for unarchived
   packs, optionally filtered by repo and/or "missing at location".
2. **Bin-pack** — `_multi_bin_pack` (`src/lcsas/burn/orchestrator.py:867`)
   wraps `first_fit_decreasing` (`src/lcsas/binpack/algorithm.py:10`),
   producing one volume plan per disc until all packs are placed. Oversize
   packs (larger than `usable_bytes - metadata_reserve_bytes`) raise
   `ValueError` because they can never fit on the chosen media.
3. **Disk-space pre-flight** — `stage()` computes
   `total_data_bytes × (1.05 × (1 + ecc%/100) + 1)` and refuses if
   `staging_path` does not have that much free space
   (`src/lcsas/burn/orchestrator.py:568-583`).
4. **Create session** — `create_session` writes a `sessions` row with status
   `STAGED` and a per-session staging directory under `staging_path`
   (`src/lcsas/burn/orchestrator.py:586-595`).
5. **For each volume plan:**
   - `StagingBuilder.initialize` + `stage_packs` hardlinks pack files from
     the Rustic mirror into `staging_root/data/<aa>/<aabbcc…>`
     (`src/lcsas/staging/builder.py:56`, `src/lcsas/staging/builder.py:61`).
   - `HolographicInjector.inject_metadata` copies each repo's `index/`,
     `snapshots/`, `keys/`, and `config` into `metadata/<repo_id>/`
     (`src/lcsas/staging/metadata.py:35`).
   - `create_volume` + `bulk_link_packs` + `update_used_bytes` register the
     volume in catalog with status `STAGING`
     (`src/lcsas/burn/orchestrator.py:387-401`).
   - `wal_checkpoint(TRUNCATE)` + `inject_catalog` flush the SQLite catalog
     and copy it into `staging_root/catalog.db` — the catalog is **always
     injected after volume registration is committed** so the disc reflects
     its own existence (`src/lcsas/burn/orchestrator.py:407-420`).
   - `write_volume_info`, `write_restore_instructions`,
     `write_standalone_restorer`, `write_lcsas_source` (skipped for
     `TEST_*` media), `write_start_here`, `write_key_info`,
     `write_config_summary`, `write_disc_care`
     (`src/lcsas/burn/orchestrator.py:422-432`).
   - `xorriso create_iso` masters the staging tree → ISO
     (`src/lcsas/iso/xorriso.py:98`).
   - If the media is optical and `--skip-ecc` was not passed,
     `dvdisaster -mRS03 -n <pct>` augments the ISO in-place via a
     temporary copy + atomic rename
     (`src/lcsas/ecc/dvdisaster.py:46`).
   - Post-ECC validation: rejects an ISO larger than
     `media_type.capacity_bytes`
     (`src/lcsas/burn/orchestrator.py:468-474`).
   - `sha256_file(iso)` is computed and stored on the session-volume row.
6. **Write session manifest** — `session.json` lists volume ids, labels,
   uuids, ISO paths, and pack ids
   (`src/lcsas/burn/orchestrator.py:934-965`).
7. **Burn (separate phase, `burn_session`)** — for each session-volume:
   - Transition `STAGING → BURNING`
     (`src/lcsas/burn/orchestrator.py:696-699`).
   - `xorriso burn_iso` writes the ISO to `/dev/srN`
     (`src/lcsas/iso/xorriso.py:272`).
   - `verify_disc` reads the disc back and compares against the ISO
     (`src/lcsas/iso/xorriso.py:307`); on pass, transition `BURNED →
     VERIFIED`; on fail, stop at `BURNED` and emit a `VERIFY_FAIL` event.
   - `add_volume_copy` records the location + burn timestamp
     (`src/lcsas/burn/orchestrator.py:744-750`).
   - Emit a `BurnReceipt` JSON to `<session_dir>/receipts/`
     (`src/lcsas/burn/orchestrator.py:967-1003`).
   - On success, delete the ISO file to reclaim staging space
     (`src/lcsas/burn/orchestrator.py:791-802`).
8. **Finalize session** — `update_session_status(... "COMPLETE")` on success,
   `PARTIAL` if any volume failed mid-session
   (`src/lcsas/burn/orchestrator.py:786-805`).

The catalog volume lifecycle is `STAGING → BURNING → BURNED → VERIFIED →
DEPRECATED → DESTROYED`. The session lifecycle is `STAGED → PARTIAL/COMPLETE
→ CLEANED`.

---

## `lcsas stage` — plan and stage volumes only

**Purpose:** Run the full plan + staging + ISO + ECC pipeline, but do **not**
burn anything to physical media. Produces a session that can be burned later
(possibly on a different machine) via `lcsas burn --session <id>` or
`lcsas burn-iso`. Useful when ISO creation and burning are split across
machines, or when staging in advance for an unattended burn.

**Prerequisites:**

- `--config <path>` pointing at a validated TOML config; `cmd_stage` exits
  early if config is missing (`src/lcsas/cli/main.py:887-892`).
- Initialised catalog (`lcsas init`).
- Registered repositories (`lcsas repo add`) and a recent `lcsas scan` so the
  catalog reflects current mirror state.
- `xorriso ≥ 1.4.0` and (for non-tape, non-`--skip-ecc`) `dvdisaster ≥ 0.79`
  on `PATH`. Version checks happen lazily inside `BurnOrchestrator.execute`,
  but the same binaries are invoked by `stage` for ISO + ECC.
- `staging_path` filesystem with enough free space; pre-flight requires
  `total_data_bytes × (1.05 × (1 + ecc%/100) + 1)`
  (`src/lcsas/burn/orchestrator.py:570-583`).

**Steps:**

1. `lcsas stage --config <path>` — invokes `cmd_stage`
   (`src/lcsas/cli/main.py:876`).
2. `--media <type>` — optional override of `default_media_type`; validated
   against `MediaType` enum (`src/lcsas/cli/main.py:913-921`).
3. `--repo <id>...` — optional restriction to specific repositories
   (`src/lcsas/cli/main.py:924`).
4. `--for-location <name>` — stage packs missing at that location only;
   routes through `get_unarchived_or_missing_at_location`
   (`src/lcsas/burn/orchestrator.py:848-851`).
5. `--skip-ecc` — bypass DVDisaster augmentation (forced on for tape; see
   per-media section) (`src/lcsas/cli/main.py:138`).
6. `--dry-run` / `-n` — compute the bin-pack plan and report it, then exit
   without touching staging, the catalog, or the disc
   (`src/lcsas/burn/orchestrator.py:551-566`).
7. Internally: `orch.stage(...)` → `_gather_packs_for_staging` →
   `_multi_bin_pack` → per-volume `_stage_single_volume`
   (`src/lcsas/cli/main.py:929-935`,
   `src/lcsas/burn/orchestrator.py:503`).

**Expected outcome:** A new burn session in `STAGED` state, with one volume
per disc in `STAGING` state, an ISO file per volume in the session staging
directory, ECC applied (for optical media), a `session.json` manifest, and
log output listing each ISO path and size. Volumes are reserved with unique
labels (`<prefix>_<media_label>_<seq>`) generated via
`generate_volume_label` (`src/lcsas/burn/orchestrator.py:601-606`).

**Variant axes that apply:**

- **Media type** — all production and TEST_* media supported; LTO forces
  `skip_ecc=True` inside `_stage_single_volume`
  (`src/lcsas/burn/orchestrator.py:452-460`).
- **Multi-tenant** — packs from multiple repos co-mingle on one volume;
  metadata is injected per-repo under `metadata/<repo_id>/`.
- **Optical drive count** — not relevant (no burn here).
- **Multi-copy** — irrelevant for `stage`; the resulting session can later
  be burned to multiple locations.
- **ECC** — `--skip-ecc` honoured; tape always skips.
- **Recovery tier** — staging only writes to Tier 1 (WARM staging SSD/HDD).

**Test coverage:**

- `tests/unit/test_session_pipeline.py::TestStage*` — happy path,
  multi-volume, skip-ecc, repo filter, `for_location`, pack hash filter,
  per-repo metadata injection.
- `tests/unit/test_binpack.py` — FFD algorithm correctness and oversize
  detection.
- `tests/unit/test_staging.py` — `StagingBuilder` hardlink + missing-pack
  paths.
- `tests/unit/test_burn_orchestrator.py::TestStage*` —
  `orch.stage()` direct API.
- Gaps: no test exercises a real `xorriso`/`dvdisaster` binary failure
  during `stage` (only the burn-time integration tests use real binaries).

**Source refs:** `src/lcsas/cli/main.py:131-145`,
`src/lcsas/cli/main.py:876-951`, `src/lcsas/burn/orchestrator.py:503-654`,
`src/lcsas/burn/orchestrator.py:332-485`,
`src/lcsas/binpack/algorithm.py:10-70`,
`src/lcsas/staging/builder.py:28-194`,
`src/lcsas/staging/metadata.py:28-64`,
`src/lcsas/iso/xorriso.py:98-194`,
`src/lcsas/ecc/dvdisaster.py:46-97`.

---

## `lcsas burn` — full pipeline (legacy stage + burn)

**Purpose:** One-shot "do everything": stage all unarchived packs, build ISOs
+ ECC, and burn to optical media in a single command. This is the legacy
path (`cmd_burn_legacy`, `src/lcsas/cli/main.py:1015`) invoked when `burn` is
called **without** `--session`. Routing: `cmd` dispatch sends
`burn --session ...` to `cmd_burn_session` and bare `burn` to
`cmd_burn_legacy` (`src/lcsas/cli/main.py:2685-2689`).

**Prerequisites:**

- Everything `stage` needs, plus:
- A writable optical disc inserted in `optical_device` (default
  `/dev/sr0`); the device path is checked in `cmd_burn_session` but
  `cmd_burn_legacy` relies on `xorriso burn_iso` to surface device errors
  (`src/lcsas/burn/orchestrator.py:704`).
- `--location <name>` (or `default_location` from config) — used to record
  the volume copy in the catalog.

**Steps:**

1. `lcsas burn --config <path>` — entry point
   (`src/lcsas/cli/main.py:114`,
   dispatched at `src/lcsas/cli/main.py:2685-2689`).
2. `--media <type>` — optional media override (`src/lcsas/cli/main.py:115`).
3. `--repo <id>...` — optional repository restriction
   (`src/lcsas/cli/main.py:117`).
4. `--skip-ecc` — skip ECC augmentation (`src/lcsas/cli/main.py:119`).
5. `--location <name>` — physical location tag for the burn
   (`src/lcsas/cli/main.py:123`).
6. `--device <path>` — override `optical_device`
   (`src/lcsas/cli/main.py:125`).
7. Internally: `orch.stage(...)` → `orch.burn_session(...)`
   (`src/lcsas/cli/main.py:1058-1077`).

**Expected outcome:** All unarchived packs are partitioned across one or
more volumes, every ISO is mastered + ECC-augmented + burned + verified,
each volume reaches `VERIFIED` state, a `BurnReceipt` is emitted per
volume, and the session is `COMPLETE`. The staging ISOs are deleted after a
successful verify (`src/lcsas/burn/orchestrator.py:791-802`).

**Variant axes that apply:**

- **Media type** — all supported; tape forces `skip_ecc=True`.
- **Multi-tenant** — yes, via `--repo` and per-repo metadata injection.
- **OS** — Linux only (xorriso + dvdisaster + `/dev/sr*` semantics).
- **Optical drive count** — single drive at a time; multi-drive needs
  parallel `lcsas burn-iso` invocations on staged ISOs (see split-machine
  workflow doc).
- **Multi-copy** — single location per `burn` invocation; for second copies
  use `lcsas burn --session <id> --location <other>` against the same
  session.
- **ECC** — `--skip-ecc` honoured; tape always skips
  (`src/lcsas/burn/orchestrator.py:452-460`).
- **Recovery tier** — produces Tier 2 (COLD) media.

**Test coverage:**

- `tests/unit/test_session_pipeline.py::TestBurnSession*` and
  `TestStageAndBurnEndToEnd` — full pipeline against in-process xorriso /
  dvdisaster fakes; covers happy path, verify-fail behaviour, receipts,
  multi-copy, and session status transitions.
- `tests/unit/test_burn_orchestrator.py` — direct
  `orch.prepare()`/`orch.execute()` legacy single-volume API, plus
  `skip_ecc` and `skip_burn` matrices.
- `tests/integration/test_disc_only_restore.py` — uses real `xorriso`,
  `dvdisaster`, and SquashFS pipelines to validate end-to-end burn + restore.
- Gaps: no automated test covers a real `--device` lockout or removable
  media insertion races; no test of `burn` with a hot-swapped device path.

**Source refs:** `src/lcsas/cli/main.py:114-128`,
`src/lcsas/cli/main.py:1015-1083`, `src/lcsas/burn/orchestrator.py:503-813`,
`src/lcsas/iso/xorriso.py:272-325`, `src/lcsas/ecc/dvdisaster.py:46-97`.

---

## `lcsas burn --session <id>` — burn a previously staged session

**Purpose:** Burn the ISOs from an existing `STAGED` (or `PARTIAL`) session
to a disc. The split staging/burning model is essential for the
recommended workflow: stage on the catalog host, then burn on a faster
machine with a different drive. Also the path used to add a second copy of
the same volumes to a different location.

**Prerequisites:**

- `--config <path>` (`src/lcsas/cli/main.py:963-968`).
- An existing session — either pass a UUID or `latest`
  (`resolve_session_id`, `src/lcsas/burn/orchestrator.py:674`).
- Staged ISO files must still exist on disk; deleted ISOs raise
  `FileNotFoundError` (`src/lcsas/burn/orchestrator.py:685-689`).
- A writable disc in the device; `cmd_burn_session` validates the device
  exists before calling the orchestrator
  (`src/lcsas/cli/main.py:993-1001`).

**Steps:**

1. `lcsas burn --session <id|latest> --location <name> --config <path>`
   — dispatched to `cmd_burn_session` (`src/lcsas/cli/main.py:954`).
2. `--device <path>` — override the optical device
   (`src/lcsas/cli/main.py:125`).
3. Internally: `orch.burn_session(session_ref=..., location=..., device=...)`
   (`src/lcsas/cli/main.py:1003-1007`,
   `src/lcsas/burn/orchestrator.py:656`).

**Expected outcome:** Each volume in the session is burned + verified +
recorded as a copy at the requested location. Re-burns (a volume already
`VERIFIED`) skip the status transitions and simply add another
`volume_copies` row (`src/lcsas/burn/orchestrator.py:692-695`). Verify
failures on re-burns emit `VERIFY_FAIL_REBURN` events without rolling the
volume backward (`src/lcsas/burn/orchestrator.py:733-742`).

**Variant axes that apply:**

- **Media type** — inherited from the session; no override.
- **Multi-copy** — primary use case. Invoke once per location with the
  same `--session`.
- **Optical drive count** — `--device` lets multiple physical drives
  share work; one process per drive.
- **ECC** — already baked into the staged ISOs; nothing to do at burn time.
- **Recovery tier** — produces Tier 2 (COLD) media.

**Test coverage:**

- `tests/unit/test_session_pipeline.py::TestBurnSession::test_burn_session_multi_location`
  — explicit multi-copy coverage.
- `tests/unit/test_session_pipeline.py::TestBurnSession::test_burn_session_latest`
  — `latest` resolution.
- `tests/unit/test_session_pipeline.py` covers verify-pass, verify-fail,
  receipt generation, session status updates, auto-location-creation.
- Gaps: no automated test of `--device` selection across multiple physical
  drives in parallel (would need fixtures with two fake `XorrisoRunner`
  instances).

**Source refs:** `src/lcsas/cli/main.py:121-128`,
`src/lcsas/cli/main.py:954-1012`, `src/lcsas/burn/orchestrator.py:656-813`,
`src/lcsas/db/sessions.py:30-110`,
`src/lcsas/db/volume_copies.py`.

---

## `lcsas burn --dry-run` — plan + ISO build without burning

**Purpose:** Validate the plan and current device state without writing to
physical media. Two distinct dry-run modes depending on whether `--session`
is set.

**Mode A — `lcsas burn --session <id> --dry-run` (`cmd_burn_session` dry-run):**
Resolves the session and prints each volume label + status; no I/O is
performed. The optical device existence check is skipped
(`src/lcsas/cli/main.py:981-991`).

**Mode B — `lcsas burn --dry-run` (legacy, no `--session`):** Falls through
to `cmd_burn_legacy`, which calls `orch.stage(dry_run=True)` and returns
before invoking `burn_session`. The stage `dry_run` branch prints the
volume plan (count, pack count per volume, byte fill, total bytes) and
returns a sentinel `StageResult(session_id="dry-run", ...)` with no
side effects (`src/lcsas/burn/orchestrator.py:551-566`,
`src/lcsas/cli/main.py:1063-1069`).

**Prerequisites:**

- For Mode A: an existing session id.
- For Mode B: the same as `stage`'s prerequisites (config, catalog, mirror
  scan).

**Steps:**

1. Mode A — `lcsas burn --session <id> --dry-run --config <path>`
   (`src/lcsas/cli/main.py:127`).
2. Mode B — `lcsas burn --dry-run --media <type> --config <path>`
   (`src/lcsas/cli/main.py:127`).

**Expected outcome:**

- Mode A: log lines like `[DRY RUN] Session <sid>: N volume(s)` followed by
  per-volume status. No catalog mutation, no device I/O.
- Mode B: log lines like `[DRY RUN] N volume(s) planned on <media>`
  followed by per-volume pack counts and percent fill. No catalog mutation,
  no staging directory created, no session row inserted, no ISOs written.

**Variant axes that apply:**

- **Media type** — all (Mode B exercises the bin-pack plan for the chosen
  media; Mode A inherits from the staged session).
- **ECC** — not exercised in dry-run (no ISOs are mastered).
- **Recovery tier** — none; planning only.

**Test coverage:**

- Argparse: `tests/unit/test_cli.py` covers `--dry-run` parsing.
- Mode B `dry_run` branch is covered indirectly by orchestrator tests; the
  CLI-level `cmd_burn_legacy --dry-run` path is partially covered through
  CLI-comprehensive tests.
- Gaps: no end-to-end CLI test asserts the exact dry-run log lines for
  Mode A.

**Source refs:** `src/lcsas/cli/main.py:127`,
`src/lcsas/cli/main.py:981-991`, `src/lcsas/cli/main.py:1063-1069`,
`src/lcsas/burn/orchestrator.py:551-566`.

---

## `lcsas stage --dry-run` — plan-only (no side effects)

**Purpose:** Identical to Mode B of `burn --dry-run` but never tries to
burn. Use this on the catalog host to estimate volume counts before
committing to a full stage.

**Prerequisites:** Same as `lcsas stage`.

**Steps:**

1. `lcsas stage --dry-run --media <type> --config <path>` — handler
   `cmd_stage` skips the result-logging block when `dry_run=True`
   (`src/lcsas/cli/main.py:937-938`).

**Expected outcome:** Per-volume plan printed, no session created, no
staging directories, no catalog mutation.

**Variant axes that apply:** Media type (all), repo filter (`--repo`),
location filter (`--for-location`). Tape vs optical does not matter — no
ISO is produced.

**Test coverage:** `tests/unit/test_session_pipeline.py` exercises the
`stage(dry_run=True)` branch; argparse tested in `tests/unit/test_cli.py`.

**Source refs:** `src/lcsas/cli/main.py:131-145`,
`src/lcsas/cli/main.py:937-938`,
`src/lcsas/burn/orchestrator.py:551-566`.

---

## `lcsas burn --for-location <name>` — delta burn for a specific location

**Purpose:** Stage only the packs that are **not yet present at a specific
physical location**, then burn them. The classic "Offsite_Safe is six
months out of date — catch it up" workflow. Note: in the CLI the flag is
`--for-location` on `stage` and `--location` on `burn`; `cmd_burn_legacy`
internally passes `args.location` as `for_location` to `orch.stage`
(`src/lcsas/cli/main.py:1060`).

**Prerequisites:**

- The target location must be registered (`lcsas location add <name>`); if
  missing, `ensure_location` will create it during burn
  (`src/lcsas/burn/orchestrator.py:679`).
- Catalog must reflect which packs already live at each location — this is
  populated by previous burns via `add_volume_copy` and by
  `lcsas catalog import-receipts` for split-machine burns.
- All other `stage`/`burn` prerequisites.

**Steps:**

1. `lcsas stage --for-location <name> --config <path>`
   (`src/lcsas/cli/main.py:134`) or
   `lcsas burn --location <name> --config <path>`
   (`src/lcsas/cli/main.py:123`).
2. Internally: `_gather_packs_for_staging(for_location=<name>)` calls
   `get_unarchived_or_missing_at_location` which returns the union of
   `unarchived` and `archived-but-not-at-this-location` packs
   (`src/lcsas/burn/orchestrator.py:848-851`).
3. Bin-pack, stage, ISO, ECC, burn — same as the normal pipeline.

**Expected outcome:** New volumes containing only the packs that needed to
land at the target location. Packs already on disc elsewhere become
candidates for **re-burns** on identical volumes if the planner ends up
including them on a fresh volume — the orchestrator handles this case
transparently (re-burning a `VERIFIED` volume only adds a new
`volume_copies` row; see "Re-burn" semantics in
`src/lcsas/burn/orchestrator.py:692-742`).

**Variant axes that apply:**

- **Multi-copy** — primary use case.
- **Media type** — all; tape still skips ECC.
- **Multi-tenant** — combine `--for-location` with `--repo` to restrict
  further (`src/lcsas/burn/orchestrator.py:862-863`).

**Test coverage:**

- `tests/unit/test_session_pipeline.py` —
  `test_stage_for_location_*`, `test_for_location_combined_with_repo`.
- `tests/unit/test_location_queries.py` —
  `get_unarchived_or_missing_at_location` logic.
- Gaps: no automated test exercises a multi-location plan where the same
  volume appears as a re-burn on one location and a fresh burn on another
  in the same session.

**Source refs:** `src/lcsas/cli/main.py:123`,
`src/lcsas/cli/main.py:134`, `src/lcsas/burn/orchestrator.py:503-547`,
`src/lcsas/burn/orchestrator.py:838-865`, `src/lcsas/db/queries.py`.

---

## Per-media-type variants

Media is selected by `--media <NAME>` (or `default_media_type` in config).
The CLI maps the flag string to `MediaType[name]` and rejects unknown values
with a list of valid types (`src/lcsas/cli/main.py:913-921`,
`src/lcsas/cli/main.py:1034-1042`). All values come from
`src/lcsas/config/media.py:8-79`.

The orchestrator's media handling rules:

- **ECC skip for tape** — `MediaType.is_tape` (`LTO8`, `LTO9`) forces
  `skip_ecc=True` regardless of `--skip-ecc`. In `execute()` this is logged
  and applied at `src/lcsas/burn/orchestrator.py:234-240`. In
  `_stage_single_volume()` the equivalent guard sits at
  `src/lcsas/burn/orchestrator.py:452-460`.
- **Source bundle skip for test media** —
  `if not media_type.is_test: injector.write_lcsas_source()`
  (`src/lcsas/burn/orchestrator.py:427-428`). Test discs stay small.
- **Label suffix** — `MediaType.label_name` (`src/lcsas/config/media.py:66`)
  is what appears in the disc label. It defaults to the enum member name.
- **Bin-pack capacity** — `usable_bytes` is `capacity_bytes × (100 −
  ecc_overhead_pct) / 100`. For LTO (`ecc_overhead_pct=0`) this is the full
  raw capacity (`src/lcsas/config/media.py:46-49`).
- **Hard reject on oversize packs** — A pack larger than `usable_bytes −
  metadata_reserve_bytes` raises `ValueError` from `_multi_bin_pack`
  before any side effects (`src/lcsas/burn/orchestrator.py:888-919`).
- **Hard reject on oversized ISO** — Post-ECC ISO larger than
  `capacity_bytes` aborts the burn with a clear error
  (`src/lcsas/burn/orchestrator.py:280-289`,
  `src/lcsas/burn/orchestrator.py:468-474`).

### Production media

| Media   | `capacity_bytes` | `ecc_overhead_pct` | `usable_bytes` | ECC step | Notes |
|---------|------------------|--------------------|----------------|----------|-------|
| `BD25`     | 25,025,314,816    | 15 | ~21.27 GB | RS03 augment | Single-layer BD-R. (`src/lcsas/config/media.py:17`) |
| `BD50`     | 50,050,629,632    | 15 | ~42.54 GB | RS03 augment | Dual-layer BD-R. (`src/lcsas/config/media.py:18`) |
| `BDXL100`  | 100,103,356,416   | 15 | ~85.09 GB | RS03 augment | Triple-layer BDXL. (`src/lcsas/config/media.py:19`) |
| `MDISC25`  | 25,025,314,816    | 15 | ~21.27 GB | RS03 augment | Same geometry as `BD25`; longevity-rated. (`src/lcsas/config/media.py:20`) |
| `MDISC100` | 100,103,356,416   | 15 | ~85.09 GB | RS03 augment | Same geometry as `BDXL100`; longevity-rated. (`src/lcsas/config/media.py:21`) |
| `LTO8`     | 12,000,000,000,000 | 0 | 12 TB    | **Skipped** (tape) | LTO has built-in error correction. (`src/lcsas/config/media.py:22`) |
| `LTO9`     | 18,000,000,000,000 | 0 | 18 TB    | **Skipped** (tape) | LTO has built-in error correction. (`src/lcsas/config/media.py:23`) |

### Test media

| Media        | `capacity_bytes` | `ecc_overhead_pct` | ECC step | Source bundle | Notes |
|--------------|------------------|--------------------|----------|---------------|-------|
| `TEST_TINY`  | 1,048,576        | 0  | Skipped (per `is_test` defaults in tests) | **Skipped** (`is_test`) | 1 MB; canonical test media — fastest unit tests, multi-volume pipeline smoke tests, blind-restore acceptance. (`src/lcsas/config/media.py:26`) |

### ECC-skip behaviour, explicitly

The DVDisaster step is **skipped** when **any** of these are true:

1. `--skip-ecc` was passed on the CLI (passed to `orch.stage(skip_ecc=...)`
   and `orch.execute(skip_ecc=...)`).
2. The media's `is_tape` property is `True` (i.e. `LTO8`, `LTO9`). This
   override is unconditional and logged
   (`src/lcsas/burn/orchestrator.py:234-240`,
   `src/lcsas/burn/orchestrator.py:456-460`).

Otherwise, `dvdisaster -mRS03 -n <default_ecc_redundancy_pct> -c` is run on
the ISO via a temp copy + atomic rename
(`src/lcsas/ecc/dvdisaster.py:71-93`).

Tests **do not** invoke real `dvdisaster` for `is_test` media; in practice
the test runners stub out the runner. There is no in-code rule that forces
test media to skip ECC — that's a test-harness convention. If a user runs
`lcsas stage --media TEST_TINY` with a real `dvdisaster` on PATH, the ECC
step will execute (and may fail because 1 MB is below DVDisaster's RS03
minimum).

### Per-media test coverage gaps

| Media       | Has dedicated test? | Notes |
|-------------|---------------------|-------|
| `BD25`      | No automated unit test exercises this path with media-specific assertions. Indirectly covered via shared orchestrator tests that use generic capacity. |
| `BD50`      | No automated coverage. |
| `BDXL100`   | No automated coverage. |
| `MDISC25`   | No automated coverage. |
| `MDISC100`  | No automated coverage. |
| `LTO8`      | **No automated coverage** — critically, the `is_tape` ECC-skip branch is not covered by any test. |
| `LTO9`      | **No automated coverage** — same as `LTO8`. |
| `TEST_TINY` | Heavy coverage in `test_session_pipeline.py` (including multi-volume, multi-tenant), `test_burn_orchestrator.py`, `test_staging.py`, `test_binpack.py`, `test_config.py`; end-to-end coverage via `tests/integration/test_disc_only_restore.py`. |

---

## Session lifecycle and resuming an interrupted burn

Session statuses (set by `update_session_status` and
`create_session`):

- **`STAGED`** — created by `stage()`; all volumes are in `STAGING` with
  ISOs ready on disk (`src/lcsas/burn/orchestrator.py:590-595`).
- **`COMPLETE`** — `burn_session` finished all volumes successfully
  (`src/lcsas/burn/orchestrator.py:805`).
- **`PARTIAL`** — `burn_session` succeeded for at least one volume but
  hit an exception on a later one; the failed volume is rolled back to
  `STAGING`, others remain `VERIFIED`
  (`src/lcsas/burn/orchestrator.py:774-789`).
- **`CLEANED`** — `clean_session` removed ISOs and the staging directory
  (`src/lcsas/burn/orchestrator.py:815-832`).

Volume statuses (set by `update_status` / `mark_closed`):

- **`STAGING`** — set by `create_volume` during `_stage_single_volume`.
  Also where a volume falls back if `execute` or `burn_session` raises
  (`src/lcsas/burn/orchestrator.py:304-314`,
  `src/lcsas/burn/orchestrator.py:774-784`).
- **`BURNING`** — set immediately before `xorriso burn_iso`.
- **`BURNED`** — set when a burn completes but post-burn `verify_disc`
  fails. The volume holds at `BURNED` so the operator can investigate
  (`src/lcsas/burn/orchestrator.py:731-732`).
- **`VERIFIED`** — burn + verify passed; volume is closed via `mark_closed`
  (`src/lcsas/burn/orchestrator.py:725-729`).
- **`DEPRECATED`** / **`DESTROYED`** — not reached by the burn pipeline;
  set by retention/consolidate workflows.

### Resuming an interrupted burn

The pipeline is interrupt-safe in three places:

1. **Inside `stage()`** — if the process dies after some volumes are staged
   but before all are written, the partially-built session remains in
   `STAGED` with some volumes in `STAGING`. Re-running `lcsas stage` will
   create a **new** session for the still-unarchived packs (the
   partial volumes' packs are linked but the volumes are still `STAGING`
   so `get_unarchived_packs` excludes them via `volume_packs`). Today
   there is no "resume this STAGED session" command; the recommended
   recovery is `lcsas stage --clean --session <id>` to discard the
   partial session (`src/lcsas/cli/main.py:907-911`,
   `src/lcsas/burn/orchestrator.py:815-832`) and then re-stage.
2. **Inside `burn_session()` between volumes** — if volume 3 of 5 fails,
   volumes 1-2 are `VERIFIED`, volume 3 is back to `STAGING`, volumes 4-5
   are still `STAGING`, session is `PARTIAL`. **Resume** by re-running
   `lcsas burn --session <id> --location <name>`. `burn_session` iterates
   all `session_volumes` and the orchestrator's re-burn logic
   (`src/lcsas/burn/orchestrator.py:692-695`) treats `VERIFIED` volumes as
   "already done, just add another copy" — so the second invocation will
   re-burn volumes 1-2 to the same location (recording a second copy,
   which is harmless) and complete 3-5. To avoid re-burning 1-2, the
   operator currently has to manually identify the failed volume and
   re-stage just that volume; this is a documented sharp edge.
3. **Inside a single volume's burn** — if `xorriso burn_iso` or
   `verify_disc` raises, the volume transitions back to `STAGING` and the
   exception propagates (`src/lcsas/burn/orchestrator.py:774-784`). The
   ISO file is **not** deleted unless verify passed
   (`src/lcsas/burn/orchestrator.py:791-802`), so re-running
   `lcsas burn --session <id>` will retry that volume with the same ISO.

### Listing and inspecting sessions

`lcsas session list [--status <STAGED|COMPLETE|PARTIAL|ABORTED>]`
(`src/lcsas/cli/main.py:363-369`, `src/lcsas/cli/main.py:2741`) prints the
session table — useful to find a session id to resume against.

### Cleaning a session

`lcsas stage --clean --session <id|latest>` deletes the staged ISOs and
the staging directory and marks the session `CLEANED`
(`src/lcsas/cli/main.py:907-911`, `src/lcsas/burn/orchestrator.py:815-832`).
Volumes that already reached `VERIFIED` keep their catalog rows; the
disc remains the source of truth.

---

## Variant-axis matrix

| Axis | `stage` | `burn` (legacy) | `burn --session` | `burn --dry-run` | `burn --for-location` |
|------|---------|-----------------|------------------|------------------|-----------------------|
| Media type | All 10 types; tape forces `skip_ecc` | All 10; tape forces `skip_ecc` | Inherited from session | All (Mode B); inherited (Mode A) | All; tape forces `skip_ecc` |
| Multi-tenant | `--repo` filter; per-repo metadata injection | `--repo` filter | n/a (session already includes repo selection) | Same as parent mode | `--repo` + `--for-location` combined |
| OS | Linux | Linux | Linux | Linux | Linux |
| Optical drive count | n/a (no burn) | 1 (`--device`) | 1 (`--device`) | n/a | 1 (`--device`) |
| Multi-copy | n/a | Single location per call | **Primary mechanism** — call once per location with same `--session` | n/a | Single location per call |
| ECC | `--skip-ecc` honoured; tape always skips | `--skip-ecc` honoured; tape always skips | Already baked into staged ISOs | n/a (no ISOs created in Mode B; Mode A no-op) | `--skip-ecc` honoured; tape always skips |
| Recovery tier | Tier 1 (WARM) only | Tier 1 → Tier 2 | Tier 1 → Tier 2 | None | Tier 1 → Tier 2 |

---

## Test coverage summary

Primary unit tests for this pipeline:

- `tests/unit/test_binpack.py` — FFD correctness, oversize-item handling,
  capacity edge cases, multi-volume layout (1 reference to `TEST_TINY`).
- `tests/unit/test_burn_orchestrator.py` — `prepare()` / `execute()` legacy
  API, `skip_ecc` matrix, `skip_burn` matrix, custom ISO output paths,
  oversize-pack rejection, manifest rollback (4 references to
  `TEST_TINY`).
- `tests/unit/test_session_pipeline.py` — the broadest coverage: stage
  (single & multi-volume, multi-tenant, skip-ecc, `for_location`,
  pack-sha256 filter), burn_session (happy path, latest resolution,
  multi-location, verify-pass/fail event recording, receipt JSON shape,
  session status transitions, ISO cleanup), clean_session, repeated
  re-burn semantics (12+ references to `TEST_TINY`).
- `tests/unit/test_staging.py` — `StagingBuilder.stage_packs` hardlink +
  copy fallback paths, missing-pack detection, partial-stage retry, hash
  verification of staged packs.
- `tests/unit/test_xorriso.py` — `SubprocessXorrisoRunner` command
  construction and error translation.
- `tests/unit/test_dvdisaster.py` — `SubprocessDVDisasterRunner` command
  construction and atomic-replace semantics.
- `tests/unit/test_db_sessions.py` — `sessions` and `session_volumes` CRUD.
- `tests/unit/test_db_volume_copies.py` — multi-location copy tracking.
- `tests/unit/test_db_volume_events.py` — `VERIFY_PASS` / `VERIFY_FAIL`
  audit trail.
- `tests/unit/test_parser_staging_labels.py` — disc label generation.
- `tests/integration/test_disc_only_restore.py` — real `xorriso` +
  `dvdisaster` + restore round-trip on `TEST_TINY`.

### Coverage gaps

1. **LTO8 / LTO9** — no test exercises the `media_type.is_tape` branches.
   The unconditional ECC-skip in
   `src/lcsas/burn/orchestrator.py:234-240` and `:456-460` is currently
   covered only by manual review.
2. **BD25 / BD50 / BDXL100 / MDISC25 / MDISC100** — no test asserts the
   media-specific capacity is honoured; coverage is implicit via the
   generic `usable_bytes` math.
3. **`stage --dry-run` exact log lines** — the dry-run branch returns the
   sentinel `StageResult` but no CLI-level test captures the human-facing
   output.
4. **Cross-drive parallel burn** — no test fixture instantiates two
   `XorrisoRunner` fakes binding to different `/dev/srN` paths.
5. **Re-stage of a `PARTIAL` session** — no test asserts the "discard then
   re-stage" recovery path is correct when the partial session contains
   already-`VERIFIED` volumes.
6. **Test media + real DVDisaster** — `is_test` media types don't carry an
   in-code rule forcing `skip_ecc`; if an operator runs `lcsas stage
   --media TEST_TINY` against a real `dvdisaster` binary, behaviour is
   undefined (RS03 typically fails on sub-MB ISOs).

### Consolidated source refs

| Concern | File | Lines |
|---------|------|-------|
| Argparse: `stage` | `src/lcsas/cli/main.py` | 131-145 |
| Argparse: `burn` | `src/lcsas/cli/main.py` | 114-128 |
| Argparse: `session list` | `src/lcsas/cli/main.py` | 363-369 |
| Handler: `cmd_stage` | `src/lcsas/cli/main.py` | 876-951 |
| Handler: `cmd_burn_session` | `src/lcsas/cli/main.py` | 954-1012 |
| Handler: `cmd_burn_legacy` | `src/lcsas/cli/main.py` | 1015-1083 |
| Handler: `cmd_burn_iso` | `src/lcsas/cli/main.py` | 1086-1148 |
| Dispatch: `burn` ↔ `--session` | `src/lcsas/cli/main.py` | 2685-2689 |
| `BurnOrchestrator.prepare` | `src/lcsas/burn/orchestrator.py` | 121-207 |
| `BurnOrchestrator.execute` | `src/lcsas/burn/orchestrator.py` | 209-316 |
| `BurnOrchestrator.abort` | `src/lcsas/burn/orchestrator.py` | 318-331 |
| `BurnOrchestrator._stage_single_volume` | `src/lcsas/burn/orchestrator.py` | 332-485 |
| `BurnOrchestrator.stage` | `src/lcsas/burn/orchestrator.py` | 503-654 |
| `BurnOrchestrator.burn_session` | `src/lcsas/burn/orchestrator.py` | 656-813 |
| `BurnOrchestrator.clean_session` | `src/lcsas/burn/orchestrator.py` | 815-832 |
| `BurnOrchestrator._gather_packs_for_staging` | `src/lcsas/burn/orchestrator.py` | 838-865 |
| `BurnOrchestrator._multi_bin_pack` | `src/lcsas/burn/orchestrator.py` | 867-932 |
| `first_fit_decreasing` | `src/lcsas/binpack/algorithm.py` | 10-70 |
| `estimate_volumes_needed` | `src/lcsas/binpack/algorithm.py` | 73-101 |
| `StagingBuilder` | `src/lcsas/staging/builder.py` | 28-194 |
| `HolographicInjector.inject_metadata` | `src/lcsas/staging/metadata.py` | 35-59 |
| `HolographicInjector.inject_catalog` | `src/lcsas/staging/metadata.py` | 61-64 |
| `SubprocessXorrisoRunner.create_iso` | `src/lcsas/iso/xorriso.py` | 98-194 |
| `SubprocessXorrisoRunner.burn_iso` | `src/lcsas/iso/xorriso.py` | 272-305 |
| `SubprocessXorrisoRunner.verify_disc` | `src/lcsas/iso/xorriso.py` | 307-325 |
| `SubprocessDVDisasterRunner.augment_iso` | `src/lcsas/ecc/dvdisaster.py` | 46-97 |
| `MediaType` enum | `src/lcsas/config/media.py` | 8-79 |
| `MediaType.is_tape` (ECC-skip predicate) | `src/lcsas/config/media.py` | 56-62 |
| `MediaType.usable_bytes` | `src/lcsas/config/media.py` | 46-49 |
| ECC-skip enforcement (execute) | `src/lcsas/burn/orchestrator.py` | 234-240 |
| ECC-skip enforcement (stage) | `src/lcsas/burn/orchestrator.py` | 452-460 |
| Re-burn (already-`VERIFIED`) semantics | `src/lcsas/burn/orchestrator.py` | 692-742 |
| Session status: PARTIAL | `src/lcsas/burn/orchestrator.py` | 786-789 |
| Session status: COMPLETE | `src/lcsas/burn/orchestrator.py` | 805 |
| ISO cleanup after verified burn | `src/lcsas/burn/orchestrator.py` | 791-802 |
