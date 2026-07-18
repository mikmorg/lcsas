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
  parent directory name) and a **location tag** that names the physical
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

- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_missing_file`
  (`tests/unit/test_cli_comprehensive.py:303-306`) — missing-ISO bail-out.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_file_exists`
  (`tests/unit/test_cli_comprehensive.py:308-321`) — happy path, runner
  invoked, exit 0.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emit_receipt_requires_location`
  (`tests/unit/test_cli_comprehensive.py:323-333`) — guard for
  `--emit-receipt` without `--location`.
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emits_receipt_with_label_inferred`
  (`tests/unit/test_cli_comprehensive.py:335-359`) — parent-dir label
  inference; receipt structure asserted (label, location,
  `verify_passed`, 64-char SHA-256).
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_emits_receipt_with_explicit_label`
  (`tests/unit/test_cli_comprehensive.py:361-379`) — `--label` overrides
  inference; receipt path is honoured as a file (not a directory).
- `tests/unit/test_cli_comprehensive.py::TestCmdBurnIso::test_burn_iso_receipt_records_verify_failure`
  (`tests/unit/test_cli_comprehensive.py:381-403`) — failed verify still
  emits a receipt with `verify_passed: false` and exits 1.
- `tests/unit/test_xorriso.py::test_burn_iso_args`
  (`tests/unit/test_xorriso.py:50-61`) — underlying xorriso command
  shape.
- `tests/unit/test_subprocess_timeouts.py::test_burn_iso_timeout_raises_runtime_error`
  (`tests/unit/test_subprocess_timeouts.py:44-52`) — timeout handling.

**Gaps in coverage:**

- No test exercises `--no-verify`; the receipt should then record
  `verify_passed: true` purely on the basis that no verify was attempted
  (this is the current code path — `verify_passed` is initialised `True`
  and only flipped by an actual failed verify run, see
  `cmd_burn_iso`). Reasonable people could disagree
  on whether that semantics is correct.
- No test asserts that the receipt file is `fsync()`-ed.
- No test covers `--emit-receipt` pointing at a non-existent nested
  directory (the code calls `mkdir(parents=True, exist_ok=True)` —
  `cmd_burn_iso`).

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
   [--label <override>] [--session <session-id>]`. The `--label`
   defaults to the ISO's parent directory name, which matches the
   label that A used when staging
   (`cmd_burn_iso`), so it is usually safe to omit.
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

- No integration test wires the full A → B → A loop end-to-end (stage
  on a temp catalog, run `burn-iso` against the staged ISO, then
  `catalog import-receipts` against a different temp catalog).
- No test asserts that a remote-burn receipt carrying `session_id`
  produces a session-correlatable audit trail; the session ID is
  written into the receipt by `cmd_burn_iso` and is preserved verbatim
  in the `BURN_RECEIPT_IMPORTED` audit event, but `cmd_catalog_import`
  does **not** otherwise act on it — for status transitions it consults
  only `volume_label`, `location`, `verify_passed`, `burn_date`,
  `iso_sha256`, and `iso_size_bytes`.
- No documented procedure for what to ship alongside ISOs (the
  session.json manifest is a strong candidate but is not required for
  burning).

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

- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_receipts_from_json`
  (`tests/unit/test_cli_comprehensive.py:481-500`) — basic import path.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_transitions_staging_to_verified`
  (`tests/unit/test_cli_comprehensive.py:502-531`) — STAGING →
  VERIFIED with `VERIFY_PASS` event and `closed_at` set.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_transitions_staging_to_burned_on_verify_fail`
  (`tests/unit/test_cli_comprehensive.py:533-561`) — STAGING → BURNED
  with `VERIFY_FAIL` event, `closed_at` left null.
- `tests/unit/test_cli_comprehensive.py::TestCmdCatalogImport::test_import_reburn_only_adds_copy`
  (`tests/unit/test_cli_comprehensive.py:563-590`) — already-VERIFIED
  volume keeps its status; just a copy row is added.

**Gaps in coverage:**

- No test exercises a malformed JSON file or a missing-keys receipt
  (the warning-and-skip path in `cmd_catalog_import`).
- No test exercises an orphan receipt (`volume_label` unknown) —
  `cmd_catalog_import`.
- No test exercises a batch with **mixed** good/bad receipts to confirm
  the bad ones do not poison the transaction.
- `iso_sha256` and `iso_size_bytes` are now persisted onto the
  `volume_copies` row, and `iso_sha256`, `session_id`, `device`, and
  `pack_ids` are persisted as a `BURN_RECEIPT_IMPORTED` audit event
  (issue #18 — `cmd_catalog_import`). See "Gaps" below for the
  remaining `pack_ids` limitation.

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
- **Catalog impact on import:** `STAGING → BURNED` (not VERIFIED), a
  `VERIFY_FAIL` audit event is appended, and a `volume_copies` row is
  still added — so the operator can see the disc exists at the
  location but is known-bad
  (`cmd_catalog_import`;
  `TestCmdCatalogImport::test_import_transitions_staging_to_burned_on_verify_fail`).
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
- **Caveat:** If the second receipt has `verify_passed: false` and the
  volume is already `VERIFIED`, the copy is **still** refreshed but no
  `VERIFY_FAIL` event is added — a real verify failure on a re-burn
  to a new location would normally be recorded only when the import
  enters the `STAGING` branch, which this no longer is. See "Gaps".

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

- **Symptom:** Truncated file, encoding error, permission denied.
- **Behaviour:** `JSONDecodeError` / `OSError` are caught
  per-receipt; the file is skipped with a warning and the rest of the
  batch continues (`cmd_catalog_import`).
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
  `cmd_burn_iso` implies the receipt is durable on B's filesystem.
- **Caveat:** If power is lost **before** the write completes, no
  receipt exists; treat as "Missing receipt" above.

---

## Gaps

The following are intentional or accidental gaps in the portable burn
workflow as currently implemented. None of them block the workflow,
but operators should be aware:

- **ISO SHA-256, `session_id`, `device`, and `pack_ids` are persisted on
  import via a `BURN_RECEIPT_IMPORTED` audit event (issue #18).** The
  importer now both passes `iso_sha256` (and `iso_size_bytes`) through
  to `add_volume_copy` and emits a `volume_events` row of type
  `BURN_RECEIPT_IMPORTED` whose `detail` is a JSON blob containing all
  four provenance fields. If a prior `BURN_RECEIPT_IMPORTED` event for
  the same volume recorded a different `iso_sha256`, the incoming
  receipt is **rejected** with a non-zero exit code and no spurious
  event row is written. `pack_ids` is still **not emitted by standalone
  `burn-iso`** — see "Standalone `burn-iso` receipts cannot register
  pack membership" under Failure modes for the rationale.
- **No "dry-run" / preview for `import-receipts`.** Operators cannot
  preview which receipts would be skipped, which volumes would
  transition, or which copies would be refreshed before committing.
- **No CLI cross-check between receipt's `iso_sha256` and the
  staging-side ISO hash.** A receipt could be silently swapped for one
  from a different ISO and the import would not notice it differs from
  the session-time hash (the master catalog has the session-time hash
  in `session_volumes` via `add_session_volume` —
  `src/lcsas/burn/orchestrator.py` — but the import path does not
  consult it; it only cross-checks against prior receipts for the same
  volume).
- **Re-burn verify failures aren't recorded.** A receipt with
  `verify_passed: false` against an already-`VERIFIED` volume updates
  the copy row but emits no `VERIFY_FAIL` event (the failure-event
  branch lives inside the `vol.status == "STAGING"` block —
  `cmd_catalog_import`). The interactive burn path *does*
  log a `VERIFY_FAIL_REBURN` event for the equivalent case
  (`src/lcsas/burn/orchestrator.py`); the receipt-import path
  does not have parity.
- **No batching guarantee.** Each receipt is committed individually
  (`cmd_catalog_import`), so a `KeyboardInterrupt` mid-batch
  leaves a partial import; this is benign because every operation is
  idempotent on re-run, but worth documenting for operators.

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
  `TestCmdCatalogImport`), `tests/unit/test_xorriso.py` (xorriso
  burn/verify), `tests/unit/test_subprocess_timeouts.py` (burn timeout).