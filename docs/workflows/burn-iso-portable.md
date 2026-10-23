# Portable Burn-ISO Workflow

The canonical LCSAS burn pipeline (`lcsas stage` → `lcsas burn`) holds an open
write transaction against the SQLite catalog while it stages, masters, burns,
and verifies each volume. That works well when the burner machine has direct
access to the master `catalog.db`, but it is unsuitable for two important
deployment shapes:

1. **Remote / airgapped burn sites.** Discs are physically produced at a
   location that cannot reach the catalog (offsite vault, customer site,
   classified network, a slow-link branch office). The catalog lives back at
   HQ and must not be exposed to the remote host.
2. **Split-machine workflows.** The machine that has fast access to the
   Rustic mirrors (NAS-attached) is not the machine with the fastest /
   spare optical drive. Staging happens on machine **A**, ISOs are shipped
   to machine **B**, and machine **B** drives the optical stack.

The portable workflow decouples the *physical burn* from the *catalog
update*. `lcsas burn-iso` writes a single, already-mastered ISO to optical
media without touching any database, then optionally emits a small JSON
**receipt** describing what happened (label, location, device, burn date,
ISO SHA-256, verify result). Receipts travel back to the canonical host
where `lcsas catalog import-receipts` reconciles them with the master
catalog: status is advanced, locations are ensured, copies and audit
events are recorded. The receipt is the only authoritative record of a
remote burn, so its handling — generation, transport, idempotent
ingestion — is the central concern of this document.

## Table of contents

- [Portable burn — `lcsas burn-iso`](#portable-burn--lcsas-burn-iso)
- [Remote burner — assemble on A, burn on B, reconcile on A](#remote-burner--assemble-on-a-burn-on-b-reconcile-on-a)
- [Receipt ingestion — `lcsas catalog import-receipts`](#receipt-ingestion--lcsas-catalog-import-receipts)
- [Failure modes](#failure-modes)
- [Gaps](#gaps)

---

## Portable burn — `lcsas burn-iso`

**Purpose:** Burn a single, pre-mastered `.iso` file to optical media from
a host that does not (and should not) hold the master catalog. The command
optionally emits a JSON receipt that can be transported back to the
canonical host for catalog reconciliation. Useful for offsite burners,
airgapped sites, and split-machine "stage here, burn there" pipelines.

**Prerequisites:**

- An ISO previously produced by `lcsas stage` (typically containing the
  holographic catalog snapshot and DVDisaster RS03 ECC already
  applied — see `src/lcsas/burn/orchestrator.py`).
- `xorriso` ≥ 1.4.0 on the burner machine (used both for burning and
  for read-back verification — `src/lcsas/iso/xorriso.py`).
- A target optical drive (`/dev/sr0` by default).
- For receipt emission: a known **volume label** (defaults to the ISO's
  own PVD volume ID, i.e. the label it was mastered with, then to the
  parent directory name for a non-ISO file — #514) and a
  **location tag** that names the physical
  destination of the disc (required when `--emit-receipt` is given —
  `cmd_burn_iso`).
- **No** config file, **no** database, **no** mirror access is required.

**Steps:**

1. Verify the ISO exists; bail out early if it does not
   (`cmd_burn_iso`).
2. If `--emit-receipt` is requested without `--location`, refuse the run
   so that downstream import has a destination to write
   (`cmd_burn_iso`).
3. Hash the ISO with SHA-256 **before** the burn, so the receipt records
   exactly the bits that went to disc even if the file is later
   replaced (`cmd_burn_iso`, `src/lcsas/utils/hashing.py`).
4. Unless `--no-verify` is passed, a blank-media pre-check refuses a disc
   that already carries data before the burn begins (`cmd_burn_iso`).
5. Stream the ISO to the optical device with xorriso in `-as cdrecord
   -dao` mode (`cmd_burn_iso`, `src/lcsas/iso/xorriso.py`).
6. Unless `--no-verify` is passed, read-back-verify the disc with
   `xorriso -check_media`, then read the disc's PVD Volume ID back and
   confirm it matches the label; record `verify_passed` for the receipt
   (`cmd_burn_iso`, `src/lcsas/iso/xorriso.py`).
7. If `--emit-receipt` was given, build the receipt dict with
   `volume_label` (from `--label` or `iso.parent.name`), `session_id`,
   `location`, `device`, `burn_date` (UTC ISO 8601), `iso_sha256`,
   `iso_size_bytes`, and `verify_passed` (`cmd_burn_iso`).
8. Persist the receipt JSON. If `--emit-receipt` points at a directory,
   auto-name the file `<label>_<location>.json`; otherwise treat the
   path as the receipt filename and create parent directories as
   needed. The write is `fsync()`-ed before the process returns so the
   receipt survives a power loss on the burner
   (`cmd_burn_iso`).
9. Exit non-zero if `--verify` was on and verification failed — but the
   receipt is still written so the failure is recorded
   (`cmd_burn_iso`).

**Expected outcome:**

- One physical disc burned at `--device`.
- Optionally one receipt JSON on disk, containing every datum needed for
  later catalog import (label, location, device, burn date, ISO hash,
  verify result, session id).
- Exit code `0` on success, `1` on missing ISO, missing `--location`
  with `--emit-receipt`, or verify failure.
- No changes to any SQLite catalog; the local filesystem is the only
  state touched.

**Variant axes that apply:**

- **Media type:** Mostly transparent — `xorriso -as cdrecord -dao`
  drives BD-R, BD-RE, M-DISC, and DVD identically. The ECC layer is
  already baked into the ISO at staging time
  (`src/lcsas/burn/orchestrator.py`) so no media-specific behaviour
  is needed here.
- **Optical drive count:** The command is single-drive; multi-drive
  burning is achieved by running multiple `burn-iso` invocations in
  parallel against distinct `--device` paths, each emitting its own
  receipt file (auto-naming uses `<label>_<location>.json` so
  collisions are avoided when locations differ).
- **Multi-copy:** Re-burns of the same volume to additional locations
  are handled by running `burn-iso` again with a different `--location`
  (each produces a distinct receipt; on import the catalog detects the
  already-VERIFIED volume and only appends a copy — see "Receipt
  ingestion" below).
- **ECC:** Not toggled here; the ISO is consumed as-is.
- **Recovery tier:** N/A — this is a pure write-path tool.

The **Multi-tenant** and **OS** axes do not change behaviour for this
command.

**Test coverage:**

- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_missing_file` — missing-ISO bail-out.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_file_exists` — happy path, runner
  invoked, exit 0.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emit_receipt_requires_location` — guard for
  `--emit-receipt` without `--location`.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emits_receipt_with_label_inferred` — parent-dir label
  inference; receipt structure asserted (label, location,
  `verify_passed`, 64-char SHA-256).
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emits_receipt_with_explicit_label` — `--label` overrides
  inference; receipt path is honoured as a file (not a directory).
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_receipt_records_verify_failure` — failed verify still
  emits a receipt with `verify_passed: false` and exits 1.
- `tests/unit/test_xorriso.py::test_burn_iso_args` — underlying xorriso command
  shape.
- `tests/unit/test_subprocess_timeouts.py::test_burn_iso_timeout_raises_runtime_error` — timeout handling.
- `tests/unit/test_burn_iso_receipt_io.py::TestEmitReceiptCreatesParents` —
  `--emit-receipt` into a directory tree that does not exist yet creates
  every missing parent (`mkdir(parents=True)`) and writes the receipt at the
  exact path given (`::test_nested_missing_dirs_are_created`), alongside a
  single missing parent, an existing parent, overwriting an existing receipt
  file, and a failed burn leaving no receipt and creating no directories.
- `tests/unit/test_burn_iso_receipt_io.py::TestReceiptIsFsynced` — the receipt
  file is `fsync()`-ed exactly once, after every byte has been flushed
  (`::test_receipt_file_is_fsynced_with_all_data_already_flushed`); the same
  holds for the receipt of a failed verify and for a receipt written into a
  directory target, and no receipt requested means no `fsync`.

**Gaps in coverage:**

- (Resolved, #514) `--no-verify` records `verify_passed: null` (not
  verified), and import keeps such a volume `BURNED`, never `VERIFIED`
  (`TestCmdBurnIso::test_burn_iso_no_verify_records_null_not_true`,
  `TestCmdCatalogImport::test_import_unverified_burn_is_burned_not_verified`).
- (Resolved, #514) The default label is the ISO's PVD volume ID, so a
  staged `<session>/<volume_label>.iso` gets the right label:
  `TestCmdBurnIso::test_burn_iso_label_defaults_to_the_iso_volume_id`.
  The older label-inference tests
  (`test_burn_iso_emits_receipt_with_label_inferred` and
  `_receipt_records_verify_failure`) still use a label-named parent
  directory (`ARCHIVE_TEST_0001/vol.iso`), which is now only the
  fallback for an ISO without a readable PVD.

**Source refs:**

- `build_parser()` in `src/lcsas/cli/main.py` — argparse wiring for `burn-iso`.
- `cmd_burn_iso` in `src/lcsas/cli/main.py` — handler and dispatch.
- `src/lcsas/iso/xorriso.py` — `burn_iso` and `verify_disc`.
- `src/lcsas/utils/hashing.py` — `sha256_file`.
- `tests/unit/test_cli_comprehensive.py` — `TestCmdBurnIso` test class.

---

## Remote burner — assemble on A, burn on B, reconcile on A

**Purpose:** Run the burn pipeline across two machines so that the
catalog stays on the trusted "master" host (A) while physical burns
happen on a host with better optical hardware or physical proximity to
its long-term-storage location (B).

**Prerequisites:**

- Master host **A** has the canonical `catalog.db`, the LCSAS config,
  Rustic mirror access, and a writable staging area.
- Burner host **B** has `xorriso` ≥ 1.4.0, an optical drive, and a
  filesystem location where ISOs and receipts can land.
- A one-way (or round-trip) transport channel between A and B —
  scp/rsync over SSH, a courier USB stick, a write-once jump host, etc.
  ISOs go A → B; receipts go B → A.

**Steps:**

1. **On A — stage.** Run `lcsas stage [--media ...]` to bin-pack
   unarchived packs into one or more volumes; each volume is staged
   into `<staging>/<session>/<volume_label>/`, an ISO is created next
   to it as `<staging>/<session>/<volume_label>.iso`, and ECC is
   applied. Volumes are written to the catalog in `STAGING` status
   (`src/lcsas/burn/orchestrator.py`).
2. **On A — verify staging.** The session manifest `session.json` is
   written into the session directory and lists every volume label,
   ISO path, UUID, and pack ID set
   (`src/lcsas/burn/orchestrator.py`). This is the manifest you
   ship alongside the ISOs.
3. **Transport.** Copy each `<volume_label>.iso` from A to B. Optional
   but recommended: also copy `session.json` and the per-volume
   directory's `volume_info.json` so the burner operator can sanity-check
   what they are about to write. The ISO already embeds the holographic
   catalog and standalone restorer
   (`src/lcsas/staging/metadata.py`), so the disc is self-describing
   regardless of which host burns it.
4. **On B — burn.** For each ISO, run `lcsas burn-iso <iso> --device
   /dev/srN --emit-receipt <receipts-dir> --location <site-name>
   [--label <volume_label>] [--session <session-id>]`. `--label` defaults
   to the ISO's PVD volume ID, the label it was mastered with, so it can
   be left out for ISOs from `lcsas stage` (#514; before that the default
   was the session directory's name and the identity check failed).
   `--session` is free-form metadata that ends up in the receipt's
   `session_id` field; note that `cmd_catalog_import` does **not**
   consult it (it reads only `volume_label`, `location`,
   `verify_passed`, `burn_date`, `iso_sha256`, and `iso_size_bytes`),
   though the value is preserved verbatim in the
   `BURN_RECEIPT_IMPORTED` audit event (`build_parser()`, `cmd_burn_iso`).
5. **On B — collect receipts.** Each burn writes one JSON receipt to
   the directory passed to `--emit-receipt`, auto-named
   `<label>_<location>.json`
   (`cmd_burn_iso`). After all burns finish, B has
   one receipt per (volume, location) pair.
6. **Transport back.** Copy the receipt directory back to A. Receipts
   are small JSON files; bandwidth and trust requirements are minimal.
   A receipt does not contain Rustic secrets or pack contents.
7. **On A — reconcile.** Run `lcsas catalog import-receipts
   <receipts-dir>/*.json` against the master catalog. Each receipt
   transitions its volume's status (or appends a copy if the volume is
   already VERIFIED — see next section).
8. **Optional — physical handoff.** If the burner site is also the
   long-term storage location, the discs stay where they are. If they
   are being couriered to a third site, follow up with `lcsas location
   move <label> --from <burner-site> --to <vault>` after the disc
   physically arrives (`build_parser()`, `cmd_location`).

**Expected outcome:**

- Catalog on A reflects every successful burn on B (`STAGING →
  BURNING → VERIFIED` for first burns, plus a `volume_copies` row per
  location).
- Failed verifies on B become `BURNED` (not `VERIFIED`) on A and emit
  a `VERIFY_FAIL` audit event so the operator can investigate
  (`cmd_catalog_import`).
- A complete audit trail exists on A: every receipt-imported event
  includes the receipt filename in its detail field
  (`cmd_catalog_import`).

**Variant axes that apply:**

- **Multi-copy:** This is the single most common reason to use the
  remote workflow — site A stages once, site B burns one copy for its
  vault, ships a second ISO to site C which burns its own copy, both
  receipts come back to A. The catalog merges them via two
  `volume_copies` rows for the same volume_id (re-burn case handled by
  `cmd_catalog_import`).
- **Multi-tenant:** Each volume can contain packs from multiple Rustic
  repos; the receipt is repo-agnostic — it carries only the volume
  label — so multi-tenant deployments need no special handling at
  burn-iso time.
- **Optical drive count:** B can run multiple `burn-iso` invocations in
  parallel against distinct devices and locations; each gets its own
  receipt.
- **ECC:** Applied on A at staging time; B is unaware of it.
- **Media type:** Optical only.
- **Recovery tier:** N/A.

**OS** does not vary behaviour here as long as xorriso is available on
B.

**Test coverage:**

- The two halves of the workflow have unit-level coverage individually
  (`test_burn_iso_*` and `test_import_*` test classes — see the per-
  command sections of this document). The receipt JSON format is
  pinned by the burn-iso tests and consumed by the import tests using
  the same field names, providing implicit end-to-end coverage at the
  format level.
- The session-staging side is covered by
  `tests/unit/test_session_pipeline.py` and
  `tests/unit/test_burn_orchestrator.py`.

**Gaps in coverage:**

- No test chains the two halves end-to-end through the real handlers
  (stage on a temp catalog, run `burn-iso --emit-receipt` against the
  staged ISO, then `catalog import-receipts` against that catalog).
  `tests/unit/test_cli_handlers.py::test_import_receipts_from_durable_dir`
  replays receipts written by the *session* burn pipeline
  (`BurnOrchestrator.burn_session`), not by `burn-iso`, so the
  `burn-iso` receipt format is only pinned by the two halves' separate
  unit tests, and the staged-ISO label mismatch described under Gaps went
  unnoticed.
- (Resolved) The `session_id` carried by a remote-burn receipt is
  preserved verbatim in the `BURN_RECEIPT_IMPORTED` audit event and
  asserted by `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_persists_receipt_provenance`.
  `cmd_catalog_import` does **not** otherwise act on it — for status
  transitions it consults only `volume_label`, `location`,
  `verify_passed`, `burn_date`, `iso_sha256`, and `iso_size_bytes`
  (intentional: a session id is free-form metadata on the portable path).

**Source refs:**

- `build_parser()` in `src/lcsas/cli/main.py` — `burn-iso` and
  `catalog import-receipts` CLI wiring.
- `cmd_burn_iso` in `src/lcsas/cli/main.py`.
- `cmd_catalog_import` in `src/lcsas/cli/main.py`.
- `src/lcsas/burn/orchestrator.py` — `stage()` (on host A) and the
  session manifest + receipt writers.
- `src/lcsas/staging/metadata.py` — holographic catalog injection.

---

## Receipt ingestion — `lcsas catalog import-receipts`

**Purpose:** Pull one or more burn receipts produced by remote (or
local) `burn-iso` runs into the canonical master catalog, advancing
volume status, ensuring locations exist, and recording copies and
audit events. This is the only "write" path that mutates the catalog
based on out-of-band burn evidence; everything else in the catalog is
produced by a live burn pipeline.

**Prerequisites:**

- An LCSAS config file (`--config` is required —
  `cmd_catalog_import`).
- Read/write access to the master `catalog.db` (defaults from config;
  overridable via `--db`).
- One or more receipt JSON files with at minimum the keys
  `volume_label` and `location`
  (`cmd_catalog_import`).
- The referenced volume must already exist in the master catalog —
  i.e. `lcsas stage` must have run for it. Receipts whose volume label
  is unknown are skipped with a warning
  (`cmd_catalog_import`); they are **not** an error and
  do not abort the rest of the batch.

**Steps:**

1. Load the LCSAS config; refuse if `--config` was not provided
   (`cmd_catalog_import`).
2. Open `catalog.db` under a locked connection and ensure the schema
   exists (`cmd_catalog_import`,
   `src/lcsas/db/connection.py`, `src/lcsas/db/schema.py`).
3. For each receipt file:
   1. Parse JSON; on `JSONDecodeError` or `OSError`, log a warning and
      continue with the next file
      (`cmd_catalog_import`).
   2. Validate required keys (`volume_label`, `location`); skip with a
      warning if any are missing
      (`cmd_catalog_import`).
   3. Look up the volume by label; skip with a warning if absent
      (`cmd_catalog_import`).
   4. Issue #18 hash guard: if any prior `BURN_RECEIPT_IMPORTED` event
      for this volume recorded a different `iso_sha256`, **reject** the
      receipt (no rows written) and count it toward the non-zero exit
      (`cmd_catalog_import`).
   5. Ensure the destination location exists (creates the row if not —
      `cmd_catalog_import`,
      `src/lcsas/db/locations.py`).
   6. Read `verify_passed` (default `False`)
      (`cmd_catalog_import`).
   7. If the volume is in `STAGING`, advance it:
      - `STAGING → BURNING` (always)
      - then `BURNING → VERIFIED` + `mark_closed` + `VERIFY_PASS`
        event, if `verify_passed`
        (`cmd_catalog_import`).
      - or `BURNING → BURNED` + `VERIFY_FAIL` event, if not
        (`cmd_catalog_import`).
      Status transitions are validated against `VALID_TRANSITIONS`
      (`src/lcsas/db/volumes.py`), so an already-`VERIFIED`
      volume cannot be reverted from a receipt.
   8. Add a `volume_copies` row for this `(volume_id, location)` pair,
      stamping the burn date, `iso_sha256`, and `iso_size_bytes` from
      the receipt (`cmd_catalog_import`). The underlying upsert
      collapses re-imports of the same receipt into the existing copy
      (`src/lcsas/db/volume_copies.py`).
   9. Record a `BURN_RECEIPT_IMPORTED` audit event whose `detail` is a
      JSON blob of the receipt provenance (`iso_sha256`, `session_id`,
      `device`, `pack_ids`, receipt filename)
      (`cmd_catalog_import`).
   10. Commit per receipt; bump the `imported` counter
      (`cmd_catalog_import`).
4. Log the total imported count; if any receipt was rejected for an
   `iso_sha256` mismatch, return a non-zero exit code
   (`cmd_catalog_import`).

**Expected outcome:**

- One `volume_copies` row per `(volume_id, location)` in the receipts.
- One status transition per first-time-burned volume (STAGING → BURNED
  or STAGING → VERIFIED) with an accompanying `volume_events` row
  citing the receipt filename in `detail`.
- For volumes that were already `VERIFIED` at import time, **no**
  status change — only a copy row is added (the "re-burn to a new
  location" path)
  (`cmd_catalog_import`;
  asserted by `TestCmdCatalogImport::test_import_reburn_only_adds_copy`).
- Bad / malformed / orphan receipts are skipped with warnings; the
  command still returns `0` in that case (the successful ones are
  committed). It returns a **non-zero** exit code only when a receipt
  is rejected for an `iso_sha256` mismatch against a prior
  `BURN_RECEIPT_IMPORTED` event (`cmd_catalog_import`).

**Variant axes that apply:**

- **Multi-copy:** First-class — a single volume can have many receipts
  (one per location); each adds a `volume_copies` row, and the volume
  remains `VERIFIED` after the first one passes.
- **Multi-tenant:** Volumes carry packs from multiple repos but the
  receipt and import path are repo-agnostic.
- **Recovery tier:** Indirectly affected — without imported receipts,
  the catalog cannot answer "where is this pack physically?" and
  restore planning degrades. This is why receipt-import discipline is
  critical to the recovery contract.

**Media type, Optical drive count, OS, ECC** do not vary import
behaviour.

**Test coverage:**

- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_receipts_from_json` — basic import path.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_transitions_staging_to_verified` — STAGING →
  VERIFIED with `VERIFY_PASS` event and `closed_at` set.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_keeps_staging_on_verify_fail` — the volume
  stays STAGING (#483) with a `VERIFY_FAIL` event, `closed_at` left null.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_reburn_only_adds_copy` — already-VERIFIED
  volume keeps its status; just a copy row is added.
- `tests/unit/test_import_receipts_errors.py::TestMalformedReceiptFile` — a
  truncated, empty, non-JSON or trailing-comma file, a nonexistent path, a
  directory in place of a receipt, and JSON that is not a receipt object
  (`[]`, a bare string, `{}`) are each skipped with a warning, exit 0, and
  leave the catalog tables unchanged.
- `tests/unit/test_import_receipts_errors.py::TestMissingKeys` — a receipt
  missing `volume_label`, `location` or both is skipped and the missing keys
  are named (and only those); a receipt with just those two keys imports
  (the other keys are optional).
- `tests/unit/test_import_receipts_errors.py::TestOrphanReceipt` — a receipt
  whose `volume_label` is not in the catalog is skipped, writes nothing
  (not even its `location` row, and no volume is invented), and the label
  match is exact, not a prefix match.
- `tests/unit/test_import_receipts_errors.py::TestMixedBatch` — a batch
  mixing good and bad receipts (malformed, missing keys, orphan,
  nonexistent file), bad first or bad last, still imports the good receipt
  and leaves no trace of the bad one; bad receipts sandwiching two good ones
  import both; an all-bad batch exits 0 and changes nothing; and an
  `iso_sha256` mismatch is the one non-zero exit (`rc == 1`) yet does not
  stop the rest of the batch (`::test_hash_mismatch_exits_nonzero_but_does_not_stop_the_batch`).
- `tests/unit/test_import_receipts_errors.py::TestMalformedReceiptFile` also
  pins the shapes that used to abort the whole batch (#522): a file that
  is not valid UTF-8 (UTF-16 from a PowerShell `>`, binary garbage), JSON
  that is `null`, a number or `true`, an `iso_size_bytes` that is not a
  non-negative integer (which also must not leave a stray `locations`
  row), and a non-string or blank `volume_label` / `location` are each
  skipped and the later receipts still import
  (`::test_undecodable_file_is_skipped_and_later_receipts_still_import`,
  `::test_scalar_json_is_skipped_and_later_receipts_still_import`,
  `::test_invalid_size_is_skipped_and_writes_nothing`,
  `::test_non_string_or_blank_label_or_location_is_skipped`).

**Gaps in coverage:**

- (Resolved) `iso_sha256` and `iso_size_bytes` are persisted onto the
  `volume_copies` row, and `iso_sha256`, `session_id`, `device`, and
  `pack_ids` are persisted as a `BURN_RECEIPT_IMPORTED` audit event
  (issue #18 — `cmd_catalog_import`), pinned by
  `TestCmdCatalogImport::test_import_persists_receipt_provenance` and
  `::test_import_rejects_hash_mismatch`. (Resolved, #514) A
  `verify_passed: false` receipt records no copy:
  `TestCmdCatalogImport::test_import_failed_first_burn_records_no_copy`
  and `::test_import_failed_reburn_records_event_not_copy`.

**Source refs:**

- `build_parser()` in `src/lcsas/cli/main.py` — argparse for `catalog
  import-receipts`.
- `cmd_catalog_import` in `src/lcsas/cli/main.py` — handler and dispatch.
- `src/lcsas/db/volumes.py` — `update_status`, `mark_closed`,
  `VALID_TRANSITIONS`.
- `src/lcsas/db/volume_copies.py` — `add_volume_copy` upsert.
- `src/lcsas/db/volume_events.py` — `add_event`, `get_events_for_volume`.
- `src/lcsas/db/locations.py` — `ensure_location`.
- `tests/unit/test_cli_comprehensive.py` — `TestCmdCatalogImport` test class.

---

## Failure modes

This section catalogues realistic ways the portable workflow can break
and how the code responds today.

### Partial burn (xorriso dies mid-burn)

- **Symptom:** xorriso exits non-zero before the ISO is fully written
  (cable yank, drive eject, write error, timeout).
- **Behaviour:** `SubprocessXorrisoRunner.burn_iso` raises (see
  `src/lcsas/iso/xorriso.py` —
  `CalledProcessError` is logged and re-raised after
  `_translate_burn_error`; `TimeoutExpired` becomes a `RuntimeError`
  via `_handle_timeout`).
- **Catalog impact:** None on the burner — no DB is touched. No
  receipt is emitted because the receipt-write step
  (`cmd_burn_iso`) is reached only after the burn
  and (optionally) the verify both complete.
- **Recovery:** Discard the bad media; re-run `burn-iso` with a fresh
  disc. No reconciliation needed on host A.

### Partial burn that passes write but fails verify

- **Symptom:** xorriso reports a successful write, but `verify_disc`
  (`-check_media`) returns non-zero.
- **Behaviour:** `cmd_burn_iso` sets `verify_passed = False`. The
  receipt is **still written** with `verify_passed: false`; the
  process exits 1 (`cmd_burn_iso`). The disc is physically present
  but suspect.
- **Catalog impact on import:** the volume stays `STAGING` (#483, as
  `burn_session` does) and a `VERIFY_FAIL` audit event is appended.
  No `volume_copies` row is added (BURN-05, #514): a disc that failed
  verification is not a copy, so its packs stay in the unarchived pool
  and the next `lcsas stage` offers them again
  (`cmd_catalog_import`;
  `TestCmdCatalogImport::test_import_keeps_staging_on_verify_fail`,
  `::test_import_failed_first_burn_records_no_copy`).
- **Recovery:** Investigate the disc (mount and run `lcsas catalog
  validate` — `cmd_catalog_validate`; or use `lcsas verify
  --disc`); if irrecoverable, re-burn a replacement and re-import its
  receipt.

### Missing receipt (burn happened, JSON never arrived)

- **Symptom:** Host A never receives a receipt for a known burn on B.
- **Behaviour:** The volume remains in `STAGING` (or whatever state
  it had before staging). There is no way for the canonical catalog
  to learn about the burn until the receipt is recovered.
- **Recovery options:**
  1. Re-fetch the receipt from B (still on disk unless deleted).
  2. Manually reconstruct a receipt JSON — the schema is small
     (`volume_label`, `location`, `verify_passed`, `burn_date`) —
     and feed it to `catalog import-receipts`.
  3. Use the manual recovery path: `lcsas verify <label>
     --mark-verified --detail "remote burn, receipt lost"`
     (`cmd_verify`). This skips the
     receipt entirely but only works for already-staged volumes.

### Duplicate import (same receipt fed twice)

- **Symptom:** Operator imports the same receipt directory twice, or
  ships overlapping batches.
- **Behaviour:**
  - **Volume status:** Already `VERIFIED` after the first import.
    The second import sees `vol.status == "VERIFIED"` and skips the
    transition block entirely
    (`cmd_catalog_import`). No `VERIFY_PASS` event is
    re-fired.
  - **Volume copy:** `add_volume_copy` is an UPSERT on
    `(volume_id, location)` — second import refreshes `burn_date`
    and `notes` on the existing row rather than creating a duplicate
    (`src/lcsas/db/volume_copies.py`).
- **Net effect:** Idempotent except for the `burn_date` and `notes`
  fields on the copy, which take the latest receipt's values.
- **Caveat (resolved, #514):** If the second receipt has
  `verify_passed: false` and the volume is already `VERIFIED`, no copy is
  recorded or refreshed for that location; a `VERIFY_FAIL_REBURN` event
  is added instead, as `BurnOrchestrator` does, so the location still
  shows as needing the volume
  (`TestCmdCatalogImport::test_import_failed_reburn_records_event_not_copy`).

### Receipt arrives before the volume is in the catalog

- **Symptom:** Operator races and imports a receipt before A has
  staged the volume (e.g. the staging-side `catalog.db` was rolled
  back, or the receipt is for a volume that never existed).
- **Behaviour:** `get_volume_by_label` returns `None`; the receipt is
  skipped with a warning
  (`cmd_catalog_import`). No error, no partial update.
- **Recovery:** Stage the volume on A (or restore the missing volume
  row), then re-import the receipt.

### Receipt JSON is corrupt or unreadable

- **Symptom:** Truncated file, encoding error (e.g. UTF-16 from a
  PowerShell `>` redirect), permission denied, JSON that is not an
  object, a non-string or blank `volume_label` / `location`, or an
  `iso_size_bytes` that is not a non-negative integer.
- **Behaviour:** each of these is caught per receipt (`ValueError`,
  which includes `JSONDecodeError` and `UnicodeDecodeError`, plus
  `OSError` and the field checks). The file is skipped with a warning
  and writes nothing, not even its location row. The rest of the batch
  continues (`cmd_catalog_import`, #522; pinned by
  `tests/unit/test_import_receipts_errors.py::TestMalformedReceiptFile`).
- **Recovery:** Repair the file, or re-export it on B if the
  filesystem there is intact.

### Standalone `burn-iso` receipts cannot register pack membership

- **Symptom:** Importing a `burn-iso` receipt on host A does not, by
  itself, populate the `volume_packs` rows for the volume. The
  session-based burn path on A produces receipts that carry `pack_ids`
  (see `BurnReceipt` in
  `src/lcsas/burn/orchestrator.py`), but the standalone
  `cmd_burn_iso` path emits a receipt **without** `pack_ids` —
  `cmd_burn_iso` does not have catalog access on the burner host and
  therefore cannot resolve the staged ISO's contents to integer pack
  ids.
- **Behaviour on import:** `catalog import-receipts` persists the
  receipt's `pack_ids` list verbatim into the
  `BURN_RECEIPT_IMPORTED` audit event's `detail` (an empty list for
  standalone `burn-iso` receipts). The catalog does **not** insert
  `volume_packs` rows from this list — pack membership is owned by the
  session-stage path and by `lcsas scan`.
- **Recovery:** Pack membership for portable-burn volumes must already
  be present in the catalog at the time `lcsas stage` ran on A. If you
  imported a `burn-iso` receipt for a volume that was never staged
  through the session pipeline (e.g. an ISO produced out-of-band), run
  `lcsas scan` against the mirror or backfill `volume_packs` rows
  manually. This is the explicitly deferred half of issue #18 — the
  fix in that issue persists provenance (`iso_sha256`, `session_id`,
  `device`, `pack_ids` verbatim) but does **not** expand `burn-iso` to
  track pack membership, which would require giving the burner host
  catalog access and was judged the higher-risk option.

### Burner host loses power immediately after write

- **Behaviour:** The receipt write uses `flush()` + `fsync()`
  (`cmd_burn_iso`), so a successful return from
  `cmd_burn_iso` implies the receipt is durable on B's filesystem
  (pinned by `tests/unit/test_burn_iso_receipt_io.py::TestReceiptIsFsynced`).
- **Caveat:** If power is lost **before** the write completes, no
  receipt exists; treat as "Missing receipt" above.

---

## Gaps

The following are intentional or accidental gaps in the portable burn
workflow as currently implemented. None of the open ones block the
workflow (the three that could leave the catalog wrong were resolved in
#514):

- **(Resolved, issue #18) Receipt provenance is persisted on import.**
  The importer passes `iso_sha256` (and `iso_size_bytes`) through to
  `add_volume_copy` and emits a `BURN_RECEIPT_IMPORTED` `volume_events`
  row whose `detail` JSON carries `iso_sha256`, `session_id`, `device`
  and `pack_ids`. A receipt whose `iso_sha256` differs from a prior
  `BURN_RECEIPT_IMPORTED` event for the same volume is **rejected** with
  a non-zero exit code and no event row (pinned by
  `TestCmdCatalogImport::test_import_persists_receipt_provenance` and
  `::test_import_rejects_hash_mismatch`). `pack_ids` is still **not
  emitted by standalone `burn-iso`** (intentional: the burner host has
  no catalog) — see "Standalone `burn-iso` receipts cannot register pack
  membership" under Failure modes for the rationale.
- **No "dry-run" / preview for `import-receipts`.** Operators cannot
  preview which receipts would be skipped, which volumes would
  transition, or which copies would be refreshed before committing.
  (`import-receipts` takes only `receipt_files`; no `--dry-run` flag.)
- **No CLI cross-check between receipt's `iso_sha256` and the
  staging-side ISO hash.** A receipt could be silently swapped for one
  from a different ISO and the import would not notice it differs from
  the session-time hash (the master catalog has the session-time hash
  in `session_volumes.iso_sha256`, written by `update_session_volume_iso`
  in `BurnOrchestrator.stage` — but `cmd_catalog_import` does not
  consult it; it only cross-checks against prior
  `BURN_RECEIPT_IMPORTED` events for the same volume, so the *first*
  receipt for a volume is trusted).
- **(Resolved, #514) Failed and unverified receipts.** A receipt with
  `verify_passed: false` no longer records a copy (BURN-05: a failed disc
  is not a copy, so the location still needs the volume); a failed
  re-burn of a `VERIFIED` volume records `VERIFY_FAIL_REBURN`, as
  `BurnOrchestrator` does.  `verify_passed: null` (`--no-verify`) records
  the copy but keeps the volume `BURNED`, not `VERIFIED`.  The
  `BURN_RECEIPT_IMPORTED` detail carries `verify_passed`.
- **(Resolved, #514) The default `--label`** is the ISO's PVD volume ID,
  not the parent directory (which is the session directory for a staged
  ISO).
- **No batching guarantee (intentional: per-receipt commits keep a bad
  receipt from poisoning the rest, and re-running is idempotent).** Each
  receipt is committed individually (`cmd_catalog_import`), so a
  `KeyboardInterrupt` mid-batch leaves a partial import; this is benign
  because every operation is idempotent on re-run (a re-import adds
  another `BURN_RECEIPT_IMPORTED` event), but worth documenting for
  operators.

---

## See also

- [Architecture overview](../architecture.md) — the burn pipeline,
  storage tiers, and holographic-catalog design that this workflow
  decouples.

---

## Source refs (consolidated)

- CLI: `build_parser()` in `src/lcsas/cli/main.py` (argparse for
  `burn-iso` and `catalog import-receipts`), `cmd_burn_iso` and
  `cmd_catalog_import` in `src/lcsas/cli/main.py` (handlers + dispatch).
- Burn pipeline: `src/lcsas/burn/orchestrator.py` — `BurnReceipt`,
  `stage`, `burn_session`, and the manifest + receipt writers.
- ISO tooling: `src/lcsas/iso/xorriso.py` — `burn_iso`, `verify_disc`.
- DB: `src/lcsas/db/volumes.py` (`VALID_TRANSITIONS`, `update_status`,
  `mark_closed`), `src/lcsas/db/volume_copies.py` (`add_volume_copy`
  UPSERT), `src/lcsas/db/volume_events.py` (`add_event`,
  `get_events_for_volume`), `src/lcsas/db/locations.py`
  (`ensure_location`).
- Hashing: `src/lcsas/utils/hashing.py` (`sha256_file`).
- Tests: `tests/unit/test_cli_comprehensive.py` (`TestCmdBurnIso`,
  `TestCmdCatalogImport`), `tests/unit/test_burn_iso_receipt_io.py`
  (receipt parent directories and `fsync`),
  `tests/unit/test_import_receipts_errors.py` (bad-receipt skipping and
  mixed batches), `tests/unit/test_xorriso.py` (xorriso
  burn/verify), `tests/unit/test_subprocess_timeouts.py` (burn timeout).