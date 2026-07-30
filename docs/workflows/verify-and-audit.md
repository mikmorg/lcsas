# Verify & Audit Workflows

Cold storage is only as trustworthy as the last time someone looked. Once a disc leaves the burner, slides into a sleeve, and gets locked in a safe-deposit box, the laws of media physics start working against it: organic dyes oxidize, M-Disc inorganic layers flake at microscopic scale, sleeves get bent, and humidity creeps in. The **Verify & Audit** layer of LCSAS is how operators (and their heirs) re-establish trust over time — pulling discs out of their boxes on a periodic cadence, reading them back, comparing the bits against the holographic catalog burned alongside the data, and recording the result in a tamper-evident audit trail.

This document covers the observability/integrity commands and the policies that bind them:

- `lcsas verify <volume>` — verify one disc/ISO and update its status
- `lcsas verify --all` — batch-verify every `BURNED`/`VERIFIED` volume (`--disc` for physical re-verify)
- `lcsas catalog validate <disc>` — disc/catalog filesystem cross-check
- `lcsas status` — at-a-glance archive inventory (`--stale-copies`, `--redundancy`)
- `lcsas session list` — list staging/burn sessions
- `lcsas session abort` — abort a never-burned session and reclaim its packs
- Periodic re-verification cadence — how often to pull discs out of safes
- Reading the `volume_events` audit trail — the immutable lifecycle log

The integrity model has three layers that this doc threads together: the
**RS03 ECC** layer (repair bit-rotted sectors — `verify` tries dvdisaster
first, then the in-house `lcsas-ecc`, FMT-01), the **SHA-256** content
layer (authenticate the bytes when no ECC tool is present, or device
read-back for `--disc`), and the **`volume_events`** audit trail (record
the result immutably). See [`docs/architecture.md`](../architecture.md) and
[`docs/workflows/recovery-toolchain.md`](recovery-toolchain.md) for the full
disc-integrity / tier story.

For the meaning of status strings (`STAGING`, `BURNING`, `BURNED`,
`VERIFIED`, `DEPRECATED`, `DESTROYED`) and event types (`VERIFY_PASS`,
`VERIFY_FAIL`, `VERIFY_FAIL_REBURN`, `ECC_REPAIR`, `LOCATION_MOVE`,
`CONDITION_CHECK`, `BURN_RECEIPT_IMPORTED`, `NOTE`), see
`src/lcsas/db/volume_events.py` (`VALID_EVENT_TYPES`) and the schema in
`src/lcsas/db/schema.py` (schema v10).

## Table of contents

1. [`lcsas verify <volume>` — single-volume verification](#lcsas-verify-volume--single-volume-verification)
2. [`lcsas verify --all` — batch re-verification](#lcsas-verify---all--batch-re-verification)
3. [`lcsas catalog validate` — disc/catalog cross-check (companion command)](#lcsas-catalog-validate--disccatalog-cross-check-companion-command)
4. [`lcsas status` — inventory dashboard](#lcsas-status--inventory-dashboard)
5. [`lcsas session list` — list sessions](#lcsas-session-list--list-sessions)
6. [`lcsas session abort` — abort a never-burned session](#lcsas-session-abort--abort-a-never-burned-session)
7. [Periodic re-verification cadence](#periodic-re-verification-cadence)
8. [Reading the `volume_events` audit trail](#reading-the-volume_events-audit-trail)

---

## `lcsas verify <volume>` — single-volume verification

**Purpose:** Re-confirm that one specific volume (identified by its catalog label) still reads back cleanly from media, then record the result on the volume's permanent event log. This is the per-disc workhorse: it is what you run when you pull a single disc out of the safe and want to know "is this still good?"

**Prerequisites:**
- Catalog database initialized (`lcsas init`); `archive.db` (or `--db` / `config.db_path`) exists.
- Volume label is registered in the catalog (`volumes.label`).
- Either:
  - A reachable ISO file (via `--iso` or auto-detected from the latest `session_volumes` row), **or**
  - A burned disc mounted in an optical drive at `--device` (default `/dev/sr0`) when `--disc` is set, **or**
  - A `--mark-verified` / `--mark-failed` flag for the split-machine / remote workflow where the verification was performed on a different host.
- `dvdisaster` binary on `$PATH` if performing ECC verification of an ISO; `xorriso` binary on `$PATH` if `--disc` mode is used.

**Steps:**
1. Argparse registers `verify` with optional positional `volume_label` plus `--iso`, `--disc`, `--device`, `--mark-verified`, `--mark-failed`, `--detail`, `--all`, `--location` (`build_parser()`, `src/lcsas/cli/main.py`).
2. Dispatch routes `args.command == "verify"` to `cmd_verify` (`src/lcsas/cli/main.py`).
3. `cmd_verify` loads config and acquires a `locked_connection` against the resolved db path, then runs `ensure_schema` (schema v10) (`cmd_verify`, `src/lcsas/cli/main.py`).
4. If `--all` is set, control hands off to `_verify_all_disc` (when `--disc`) or `_verify_all` — see the next workflow.
5. Otherwise, look up the volume by label; bail if missing.
6. **Manual-marking branch (`--mark-verified`):** transition `BURNED → VERIFIED` (or `STAGING → VERIFIED` with `force=True` for the split-machine workflow) and append a `VERIFY_PASS` event.
7. **Manual-marking branch (`--mark-failed`):** append a `VERIFY_FAIL` event with `detail` text; do not change status.
8. **Physical/ISO branch:** if `--iso` was not passed and `--disc` was not requested, look up the most-recent `session_volumes.iso_path` for the volume; bail if neither is available.
9. If `--disc`: resolve which copy is in the drive (`--location`, or the sole ACTIVE copy), then run `_check_disc_for_volume` (`src/lcsas/cli/main.py`): an **identity gate** first (read the disc's ISO 9660 Volume ID; a mismatch / unreadable ID is `WRONG_DISC` and records *nothing* — FMA-03), then `verify_disc` (`-check_media` readability), then a **device read-back SHA-256** compared against the hash recorded at stage time (BURN-04). A PASS stamps `last_verified_at` on the copy (FMA-05).
10. Else (ISO mode): confirm the ISO exists, then **FMT-01 ECC chain** via `select_ecc_runner()` — prefer real `dvdisaster` (RS03 verify, can detect *and* repair); else fall back to the bundled in-house `lcsas-ecc` (decode-only); if *neither* ECC tool is present, fall back to a portable **SHA-256** compare against the catalog-recorded hash (detect-only).
11. Append a `VERIFY_PASS` or `VERIFY_FAIL` event with detail describing the source; the valid event types come from `VALID_EVENT_TYPES` (`src/lcsas/db/volume_events.py`) and the underlying insert is `add_event`.
12. If the verification passed *and* the volume was `BURNED`, promote it to `VERIFIED` via `update_status`.
13. Return `0` on pass, `1` on fail.

**Expected outcome:**
- Stdout shows `Verifying ISO: <path>` (or `Verifying disc on <device> ...`) and a `PASS`/`FAIL` line.
- For a previously-`BURNED` volume that passes, log line `Volume <label>: promoted BURNED → VERIFIED`.
- A new row appears in `volume_events` with `event_type` set to `VERIFY_PASS` or `VERIFY_FAIL`, today's UTC `event_date`, and the path or device in `detail`.
- Exit code `0` on pass, `1` on fail or argument error.

**Variant axes that apply:**
- **Media type:** ISO-mode verification is media-agnostic (it just reads the staged file). `--disc` mode reads physical media and is therefore sensitive to drive capability — Blu-ray media (BD25, MDISC100) needs a BD-capable drive at `--device`. DVDisaster RS03 ECC applies to any ISO regardless of target media, so the same verification machinery is exercised for TEST_TINY images during development.
- **Multi-tenant:** This command operates on a single physical volume; the underlying disc may carry packs from multiple Rustic repositories. Verification is at the *pack-hash* layer (and the ECC layer), not the repository layer, so it is multi-tenancy-blind by design — a single `VERIFY_PASS` covers every tenant's packs on the disc.
- **Optical drive count:** Single-drive sites must serialize `--disc` runs (one drive, one disc at a time). Multi-drive sites can run several `lcsas verify` processes in parallel as long as each names a distinct `--device`; the catalog write path uses `locked_connection` so concurrent event writes are serialized at the SQLite level (`cmd_verify`, `src/lcsas/cli/main.py`).
- **Multi-copy:** This command verifies *one* copy at a time. To re-verify every physical copy of one volume you must run it once per copy with the appropriate `--device` and `--detail` describing which copy was checked. Copies are tracked in `volume_copies`; `verify` does not iterate them automatically. (See "Gaps" below.)
- **ECC:** ISO mode runs the FMT-01 chain — real `dvdisaster` RS03 (detect + repair) if installed, else the bundled in-house `lcsas-ecc` (decode-only), else a portable SHA-256 compare against the recorded hash. `--disc` mode does the identity gate + `-check_media` readability test + device read-back SHA-256. Either way the recovery margin comes from the RS03 parity wrapped around the burned image at stage time.
- **Recovery tier:** This is a Tier-2 (COLD) integrity check; it does not touch the HOT/WARM Rustic mirrors. A pass here only guarantees the disc is readable — it does not guarantee `rustic restore` will succeed (that's covered by the restore workflows).

**Test coverage:**
- Existing: `tests/unit/test_db_verify.py` exhaustively covers `_collect_disc_packs` (`tests/unit/test_db_verify.py:16-100`) and `validate_disc` (`tests/unit/test_db_verify.py:103-238`) — the catalog/disc cross-check used by `lcsas catalog validate`. Event insertion paths used by `cmd_verify` are covered by `tests/unit/test_db_volume_events.py::TestAddEvent` (`tests/unit/test_db_volume_events.py:19-68`).
- Gaps:
  - No unit test exercises `cmd_verify` end-to-end (the CLI handler is uncovered) — the `BURNED → VERIFIED` promotion and the `STAGING → VERIFIED` split-machine path (`cmd_verify`, `src/lcsas/cli/main.py`) are tested only manually.
  - No test for the auto-resolution of `iso_path` from `session_volumes` (`cmd_verify`, `src/lcsas/cli/main.py`).
  - No injection point: `SubprocessXorrisoRunner` / `SubprocessDVDisasterRunner` are instantiated inline rather than passed as a `Protocol` — verification of `--disc` and ISO-mode in tests requires monkey-patching.

**Source refs:**
- `build_parser()` (`verify` subparser) + `cmd_verify` + `_check_disc_for_volume` + `_verify_disc_against_recorded_hash` (`src/lcsas/cli/main.py`)
- `select_ecc_runner` (`src/lcsas/ecc/dvdisaster.py`) — FMT-01 dvdisaster → lcsas-ecc selection
- `add_event` / `VALID_EVENT_TYPES` (`src/lcsas/db/volume_events.py`)
- `touch_last_verified` (`src/lcsas/db/volume_copies.py`)
- `tests/unit/test_db_verify.py`, `tests/unit/test_db_volume_events.py`

---

## `lcsas verify --all` — batch re-verification

**Purpose:** Sweep every volume in `BURNED` or `VERIFIED` status (optionally filtered to one storage location) and re-verify each. Two modes:

- **ISO mode** (default, `_verify_all`): verify each volume's staged ISO. The cron-friendly form — run overnight, get a summary plus a fresh `VERIFY_PASS` / `VERIFY_FAIL` event per volume.
- **Disc mode** (`--all --disc`, `_verify_all_disc`): re-verify the *physical discs* copy-by-copy (FMA-05), prompting the operator to insert each. A PASS stamps `last_verified_at` on that copy. This is what `lcsas status --stale-copies` points you to.

**Prerequisites:**
- Same DB prerequisites as the single-volume form.
- ISO mode: a reachable ISO recorded in `session_volumes.iso_path`. After a verified burn the orchestrator deletes the ISO, so for burned volumes the *normal* state is "ISO gone" — those are reported and you're pointed at `verify --all --disc` (not silently failed).
- ISO mode ECC: an ECC tool is *preferred* but not required — `select_ecc_runner()` picks `dvdisaster` or the in-house `lcsas-ecc` (FMT-01); with neither, it falls back to SHA-256 against the catalog-recorded hash (volumes with no recorded hash are skipped, never silently passed).
- Disc mode: an optical drive at `--device`; `xorriso` on `$PATH`.

**Steps (ISO mode):**
1. `cmd_verify` checks `args.verify_all`; with `--disc` it calls `_verify_all_disc`, else `_verify_all` (`src/lcsas/cli/main.py`).
2. `_verify_all` pulls all `BURNED` + `VERIFIED` volumes (optionally filtered by `--location` via `volume_copies`).
3. Probe the ECC tool ONCE up front with `select_ecc_runner()` (FMT-01); if absent, log that the whole sweep falls back to SHA-256.
4. For each candidate, look up the most-recent `session_volumes.iso_path`; if missing/gone, report and point at `verify --all --disc`.
5. Run `ecc_runner.verify_iso(iso_path)` (or the SHA-256 fallback), append a `VERIFY_PASS` / `VERIFY_FAIL` event (`"Batch ISO verify"` / `"Batch SHA-256 verify"`).
6. On pass, promote `BURNED → VERIFIED`.
7. Print a summary line: passed / failed / skipped.
8. Exit `1` if any volume failed *or* if every candidate was skipped; `0` otherwise.

**Steps (disc mode, `--all --disc`):** iterate ACTIVE copies of BURNED/VERIFIED volumes (optionally `--location`-filtered); for each, prompt to insert the disc, run the same identity-gate + readability + device-hash check as single `--disc`; a PASS stamps `last_verified_at` on the copy and records `VERIFY_PASS` (location-tagged); a wrong disc records nothing.

**Expected outcome:**
- One log line per volume (`<label>: PASS` / `FAIL`, or `<label>: ISO not found ... — skipped`).
- One summary line: `Verification complete: N passed, M failed, K skipped`.
- One `VERIFY_PASS` or `VERIFY_FAIL` row in `volume_events` per non-skipped volume.
- `BURNED` volumes that passed are now `VERIFIED`.

**Variant axes that apply:**
- **Media type:** ECC verification only, so this works equally well for BD25 / MDISC100 / TEST_TINY ISOs.
- **Multi-tenant:** Repository-blind, like the single form. A single sweep covers every tenant's data simultaneously.
- **Optical drive count:** ISO mode has no drive involvement (it reads staged ISOs); it runs on any host that can mount the staging directory. Disc mode (`--all --disc`) uses the optical drive at `--device` and is serial on single-drive sites.
- **Multi-copy:** ISO mode operates on the volume, checking the *ISO* once regardless of copy count. **Disc mode (`--all --disc`) iterates the physical copies** (one `volume_copies` row at a time), stamping `last_verified_at` per copy — that is the multi-copy-aware path. Use `--location` to restrict either mode to one site.
- **ECC:** This workflow is the canonical way to exercise the DVDisaster RS03 layer in bulk. Without ECC, the value of batch verify drops sharply (you'd just be re-reading SHA-256s without any redundancy margin).
- **Recovery tier:** Tier-2 only; never touches HOT/WARM.

**Test coverage:**
- Existing: `tests/unit/test_db_volume_events.py::TestGetEventsByType` (`tests/unit/test_db_volume_events.py:165-190`) covers the cross-volume query that an operator would use to audit the result. List-volume queries are covered indirectly by the broader volumes test suite (not in scope for this doc).
- Gaps:
  - No test exercises the `--location` filter.
  - No test asserts the "all skipped → exit 1" branch.
  - No test for the `BURNED → VERIFIED` promotion in batch mode.

**Source refs:**
- `build_parser()` (`--all` / `--disc` / `--location`), `_verify_all`, `_verify_all_disc` (`src/lcsas/cli/main.py`)
- `select_ecc_runner` (`src/lcsas/ecc/dvdisaster.py`); `touch_last_verified` (`src/lcsas/db/volume_copies.py`)
- `add_event` (`src/lcsas/db/volume_events.py`)
- `tests/unit/test_db_volume_events.py`

---

## `lcsas catalog validate` — disc/catalog cross-check (companion command)

> Listed here because it shares the verify/audit conceptual scope, even though the user-facing command lives under `catalog`. Operators routinely combine it with `lcsas verify` when a disc fails ECC.

**Purpose:** Mount a disc, compare the pack files actually present in its `data/` directory against the SHA-256 list recorded in its on-disc `catalog.db` / `volume_info.json`, and report any packs that are missing or orphaned. Where `verify` answers "is the ECC layer still good?", `catalog validate` answers "did all the files we *thought* were on this disc actually land here?"

**Prerequisites:**
- A mounted LCSAS disc at `disc_path` containing a `catalog.db` and a `data/` directory.
- The disc was burned with the holographic injector (`staging/metadata.py`) so `volume_info.json` and `catalog.db` are present.

**Steps:**
1. Argparse exposes `lcsas catalog validate <disc>` with an optional `--content` flag (`build_parser()`, `src/lcsas/cli/main.py`).
2. `cmd_catalog_validate` calls `validate_disc(disc_path, content=args.content)` (`src/lcsas/cli/main.py`).
3. `validate_disc` requires `catalog.db` and `data/` to exist; raises `FileNotFoundError` / `ValueError` otherwise (`src/lcsas/db/verify.py`).
4. Walk `data/` recursively, collecting any filename that is exactly 64 lowercase hex chars — this handles both flat (`data/HASH`) and two-level (`data/ab/abcdef...`) layouts.
5. Open the on-disc catalog read-only (`mode=ro` URI).
6. Prefer `volume_info.json` `sha256_manifest` as the source of truth; fall back to a SQL query over `volumes`/`volume_packs`/`packs` filtered to volumes whose status is `VERIFIED`, `BURNED`, `STAGING`, or `BURNING`.
7. Compute set differences: `missing_from_disc = catalog - disc`, `orphaned_on_disc = disc - catalog`.
8. **With `--content`:** additionally read every pack file on the disc and verify its SHA-256 against the filename hash, collecting `corrupt_on_disc` (detects bit-rot; reads the full data payload).
9. `cmd_catalog_validate` logs each missing / orphaned / corrupt hash and returns `0` only if all sets are empty.

**Expected outcome:**
- `Catalog validation PASSED` (exit 0) or `Catalog validation FAILED — N missing, M orphaned, K corrupt.` (exit 1).
- No mutation of the master catalog (this command is read-only against the disc's *own* embedded DB; it does *not* write a `volume_events` row — see "Gaps").

**Variant axes that apply:**
- **Media type:** Reads files from a mount point; works for any media that presents a filesystem.
- **Multi-tenant:** A single disc can contain packs from multiple repositories — the SQL fallback query joins `volume_packs` so all tenants on the disc are validated in one pass.
- **Multi-copy:** Validate one copy at a time by re-mounting each.
- **ECC:** Independent of the ECC layer — without `--content` this is a filesystem-level presence check; with `--content` it's a content SHA-256 check (still distinct from the sector-level RS03 ECC `verify` runs). Useful when ECC reports OK but you suspect a write-side regression.
- **Recovery tier:** Tier-2; this is exactly what you run on a recovery host before trusting a disc's contents.

**Test coverage:**
- Existing: `tests/unit/test_db_verify.py` covers every branch of `validate_disc` — `volume_info`-driven (`test_single_disc_all_packs_present`, `tests/unit/test_db_verify.py:126-151`), missing packs (`tests/unit/test_db_verify.py:153-171`), orphaned packs (`tests/unit/test_db_verify.py:173-195`), missing catalog (`tests/unit/test_db_verify.py:197-204`), missing data dir (`tests/unit/test_db_verify.py:206-210`), empty manifest (`tests/unit/test_db_verify.py:212-222`), and the `ok` property (`tests/unit/test_db_verify.py:224-238`). Both layout shapes (flat / two-level) are tested.
- Gaps:
  - Mixed-case hex pack names are explicitly *not* matched (`tests/unit/test_db_verify.py:71-87` documents this). If a downstream tool writes uppercase pack files they will appear as missing — that is a real, currently-tested-as-known limitation.
  - The fallback SQL query (`validate_disc`, `src/lcsas/db/verify.py`) is not unit-tested in isolation; the `volume_info.json`-present path is the only one covered.
  - `cmd_catalog_validate` does **not** record a `volume_events` entry — a successful validate doesn't move a volume's status. If you want the cross-check to count toward the audit trail, currently you have to follow it with a separate `lcsas verify <label> --mark-verified --detail "catalog validate ok"`.

**Source refs:**
- `cmd_catalog_validate` (`src/lcsas/cli/main.py`)
- `src/lcsas/db/verify.py` (`validate_disc`, `_collect_disc_packs`)
- `tests/unit/test_db_verify.py` (entire module)

---

## `lcsas status` — inventory dashboard

**Purpose:** One-shot human-readable summary of the archive: how many packs total / archived / staged / unarchived / pruned, a table of every volume, and **redundancy / staleness warnings** that flag the data-loss risks worth acting on this week. Two focused report flags drill in further:

- `--stale-copies [--older-than-days N]` — list ACTIVE physical copies never verified or overdue (default 365 days; FMA-05).
- `--redundancy [--min-copies N]` — blast-radius report: under-replicated packs grouped by the disc that holds them (default threshold 2; FMA-08).

**Prerequisites:**
- Catalog database initialized (`lcsas init`); `cmd_status` opens it read-only and refuses to run against a non-existent catalog (it does **not** create one as a side effect).
- No external tools — pure SQL.

**Steps:**
1. Argparse: `status` subparser with `--stale-copies`, `--older-than-days` (default 365), `--redundancy`, `--min-copies` (default 2) (`build_parser()`, `src/lcsas/cli/main.py`).
2. Dispatch: `args.command == "status"` → `cmd_status` (`src/lcsas/cli/main.py`).
3. `cmd_status` opens an existing catalog via `_open_existing_catalog` (read-only; errors if uninitialized).
4. If `--stale-copies`, delegate to `_print_stale_copies` (FMA-05) and return. If `--redundancy`, delegate to `_print_redundancy_report` (FMA-08) and return.
5. Otherwise: query `get_archive_status_summary`, `list_volumes`, partial sessions, the FMA-08 single-disc set (`get_redundancy_report` with `min_copies=2`), per-volume "needs re-burn" events, and `find_stale_copies`.
6. Print the pack-stats line, the per-volume table, and any **WARNING** lines: staged-never-burned packs, PARTIAL sessions, volumes needing re-burn, single-disc packs (→ `status --redundancy`), and stale copies (→ `status --stale-copies` / `verify --all --disc`).
7. Return `0`.

**Expected outcome:**
- A `Packs:` line (total / archived / staged / unarchived / pruned), a `Volumes: N total` line, then one fixed-width row per volume: `<label:25> <media_type:10> <status:10> <location>`, followed by any risk warnings.
- Exit `0` (read-only; the only failure is a missing/uninitialized catalog).

**Variant axes that apply:**
- **Media type:** Listed per-volume in the `media_type` column.
- **Multi-tenant:** Output is volume-centric; multi-tenant deployments see every tenant's volumes interleaved. The redundancy/blast-radius detail is per-disc; `lcsas volume impact <LABEL>` breaks one disc down per-repo.
- **Multi-copy:** The volume table's `location` column shows the volume row's own location; per-copy ACTIVE state lives in `volume_copies` and is surfaced by `--stale-copies` and `--redundancy` (and `lcsas location list` / `volume impact`).
- **ECC:** N/A.
- **Recovery tier:** Catalog-only; no media is touched.

**Test coverage:**
- Existing: `get_archive_status_summary`, `list_volumes`, `find_stale_copies`, `get_redundancy_report` are covered by the queries/volumes/volume_copies suites.
- Gaps:
  - No CLI-level smoke test for `cmd_status` (including the `--stale-copies` / `--redundancy` branches).

**Source refs:**
- `build_parser()` (`status` subparser), `cmd_status`, `_print_stale_copies`, `_print_redundancy_report` (`src/lcsas/cli/main.py`)
- `find_stale_copies` (`src/lcsas/db/volume_copies.py`); `get_redundancy_report` / `get_live_volumes_for_packs` (`src/lcsas/db/queries.py`)

---

## `lcsas session list` — list sessions

**Purpose:** Enumerate every burn session in the catalog (each session is a single staging+burn run that may produce multiple volumes). Used for picking the session ID to feed into `lcsas burn --session <id>`, or for forensically tracing "which run of LCSAS produced this disc?"

**Prerequisites:**
- Catalog database initialized.
- `--config` is **required** — `cmd_session_list` errors out without it (this is inconsistent with `status`/`verify`, which fall back to `archive.db`). See "Gaps".

**Steps:**
1. Argparse: `session` subcommand with `list` (optional `--status` filter) and `abort` sub-commands (`build_parser()`, `src/lcsas/cli/main.py`).
2. Dispatch: `args.command == "session"` and `args.session_command == "list"` → `cmd_session_list` (`src/lcsas/cli/main.py`).
3. Refuse to run without `--config`.
4. Load config, open a connection on `config.db_path`.
5. Call `list_sessions(conn, status_filter=args.status)` which translates to a `SELECT * FROM burn_sessions [WHERE status = ?] ORDER BY created_at` (`src/lcsas/db/sessions.py`).
6. For each session, also fetch `get_session_volumes` (`src/lcsas/db/sessions.py`) and render a one-line header plus an indented `volumes(N): ...` line.
7. Return `0`.

**Expected outcome:**
- A fixed-width header (`SESSION ID  STATUS  MEDIA  CREATED`), then one line per session, optionally followed by an indented list of `volume_id`s.
- Exit `0` even if there are zero sessions (just prints `No sessions found.`).

**Variant axes that apply:**
- **Media type:** `media_type` is displayed per session (one column). Sessions are media-typed at creation in `create_session` (`src/lcsas/db/sessions.py`).
- **Multi-tenant:** Sessions are tenant-blind — a single staging run can pack volumes from multiple repos.
- **Multi-copy:** Sessions are not copy-aware. A session lists its volumes once, regardless of how many physical copies were burned later (copies live in `volume_copies`, written by the burn orchestrator).
- **ECC:** N/A.
- **Recovery tier:** Catalog-only.

**Test coverage:**
- Existing: `tests/unit/test_db_sessions.py::TestSessionCRUD` covers `list_sessions` (`tests/unit/test_db_sessions.py:80-94`), including the status filter, plus all neighbouring CRUD ops (`tests/unit/test_db_sessions.py:23-100`). Session-volume linkage is covered by `TestSessionVolumes` (`tests/unit/test_db_sessions.py:103-140`).
- Gaps:
  - No CLI-level test for `cmd_session_list`.
  - No test for the `--config required` error path.
  - The volume rendering stringifies `volume_id` rather than the human-readable `label` — operators looking for "which discs are in session X?" must cross-reference. Worth a bug-fix PR; flagged here as a UX gap.

**Source refs:**
- `build_parser()` (`session list` subparser), `cmd_session_list` (`src/lcsas/cli/main.py`)
- `list_sessions` / `get_session_volumes` (`src/lcsas/db/sessions.py`)
- `tests/unit/test_db_sessions.py`

---

## `lcsas session abort` — abort a never-burned session

**Purpose:** Cleanly back out a session that was staged but never burned: delete its volumes and return their packs to the unarchived pool. This is the recovery for an interrupted or abandoned stage (the staged volumes otherwise linger as STAGING ghosts that `lcsas status` and `catalog reconcile` will flag). A single stranded STAGING volume (with no session) can be aborted by label with `--volume`.

**Synopsis:**

```bash
lcsas --config lcsas.toml session abort [REF] [--volume LABEL]
```

- `REF` — session ID or `latest` (default `latest`).
- `--volume LABEL` — abort one stranded STAGING volume by label instead of a whole session.

**Prerequisites:**
- `--config` is **required** (the handler errors without it).
- The session/volume must be in a never-burned state (the orchestrator refuses to abort a session that produced burned copies).

**Steps:**
1. Argparse: `session abort` with positional `ref` (default `latest`) and `--volume` (`build_parser()`, `src/lcsas/cli/main.py`).
2. `cmd_session_abort` loads config, takes a `locked_connection`, and constructs a `BurnOrchestrator`.
3. With `--volume`, calls `orch.abort_volume(label)`; otherwise `orch.abort_session(ref)`. A `ValueError` (e.g. session already burned) is logged and returns 1.
4. Returns 0 on success.

**Expected outcome:**
- The session's STAGING volumes are deleted and their packs return to the unarchived pool (re-selectable by `lcsas stage`). Exit 0; exit 1 if the session is not abortable or `--config` is missing.

**Variant axes that apply:**
- **Multi-tenant:** session-blind — aborting a session reclaims packs from every repo it staged.
- **Multi-copy / ECC / Recovery tier:** N/A — this is a catalog/staging cleanup, not a media operation.

**Test coverage:**
- `orch.abort_session` / `orch.abort_volume` are covered in the burn-orchestrator suite; `resolve_session_id` and the session/volume CRUD in `tests/unit/test_db_sessions.py`.
- Gap: no CLI-level test for `cmd_session_abort`.

**Source refs:**
- `build_parser()` (`session abort` subparser), `cmd_session_abort` (`src/lcsas/cli/main.py`)
- `BurnOrchestrator.abort_session` / `abort_volume` (`src/lcsas/burn/orchestrator.py`)
- `tests/unit/test_db_sessions.py`

---

## Periodic re-verification cadence

**Purpose:** Cold storage that is never read is no different from cold storage that has rotted — you only learn about bit-rot when you try to read the disc. A scheduled cadence pulls discs out of long-term storage on a known rhythm so failures are caught with maximum recovery margin (i.e. before the second copy also rots).

**Prerequisites:** A running LCSAS install with at least one `BURNED` or `VERIFIED` volume.

**Recommended cadence — sourced from the codebase:**

The repository specifies a cadence in two places. Both apply equally to BD-R, M-Disc, and other optical media, and are written into every disc's on-disc README via the holographic injector:

| Cadence | Action | Source |
|---------|--------|--------|
| Every **2-5 years** | Spot-check a few discs | `src/lcsas/staging/metadata.py` |
| Every **5-10 years** | Full verify of all discs (`lcsas verify --all --disc`) | `src/lcsas/staging/metadata.py` |
| Every **5-10 years** | Re-burn discs to fresh media (even M-Disc degrades) | `docs/ESTATE_PLANNING.md` |
| On **any** read error | Re-burn ALL data — same-batch media may be co-degrading | `src/lcsas/staging/metadata.py` |

The codebase does **not** distinguish a separate cadence for BD-R vs. M-Disc — the same 2-5y / 5-10y window covers both. Media-vendor literature suggests M-Disc could safely use the longer end of that window and BD-R the shorter, but LCSAS itself does not encode that policy. The `volume_copies.last_verified_at` stamp **is** tracked: `lcsas status --stale-copies [--older-than-days N]` surfaces ACTIVE copies never verified or overdue (FMA-05), and `lcsas status` warns about them in its summary. There is still no scheduled-reminder integration — the cadence runs out-of-band.

**Steps:**
1. Run `lcsas status --stale-copies` (or read the warning in plain `lcsas status`) to plan which discs to pull. The stamp comes from `verify --disc` / `verify --all --disc`.
2. Pull the discs from storage and mount them on a verification host.
3. Run `lcsas verify <label> --disc --device <dev>` per disc (stamps `last_verified_at`), or `lcsas verify --all --disc` to sweep every ACTIVE copy copy-by-copy.
4. Cross-check with `lcsas catalog validate <mount> [--content]` for each disc.
5. If any disc fails: append a manual `VERIFY_FAIL_REBURN` event (one of the `VALID_EVENT_TYPES`, `src/lcsas/db/volume_events.py`) and trigger the re-burn workflow (out of scope here).
6. Optionally record a `CONDITION_CHECK` / `NOTE` event for spot-checks that passed without a full ECC pass.

**Expected outcome:**
- Every volume in the affected cohort has a fresh `VERIFY_PASS` (or `VERIFY_FAIL` / `VERIFY_FAIL_REBURN`) event whose `event_date` is recent.
- Failures trigger an out-of-band re-burn pipeline.

**Variant axes that apply:**
- **Media type:** BD-R LTH (organic dye) should be biased toward the *shorter* end of every window; M-Disc (inorganic) toward the longer end. The codebase does not enforce this differential; operators must encode it in their own schedule.
- **Multi-tenant:** Cadence is per-disc, not per-tenant. A single sweep covers every tenant living on a disc.
- **Optical drive count:** Drive count dictates the *throughput* of the cadence — a single-drive site doing a 5-yearly full sweep of 1000 discs at ~10 min/disc needs roughly a week of wall-clock time. Plan accordingly.
- **Multi-copy:** Multi-copy sites should stagger the cadence per-copy so the same calendar event doesn't pull both copies of a volume out of storage simultaneously (defeating the redundancy).
- **ECC:** The whole cadence assumes ECC verification is what `verify` runs by default. If ECC is disabled the cadence still makes sense but the failure margin shrinks.
- **Recovery tier:** This is a Tier-2-only cadence; Tier-0 (HOT, Rustic mirror) is verified continuously by Rustic itself.

**Test coverage:**
- The cadence is a policy, not code, so there is nothing to unit-test directly. The supporting query (`get_events_for_volume` for `last verified` derivation) is tested at `tests/unit/test_db_volume_events.py:86-124`.
- Gaps:
  - `lcsas status --stale-copies` surfaces overdue copies, but there is no scheduled-task / hook integration; the cadence runs out-of-band.
  - The cadence written to disc (`src/lcsas/staging/metadata.py`) and the cadence in `docs/ESTATE_PLANNING.md` are *slightly* divergent (2-5y vs. 5-10y vs. 5-10y reburn). Worth reconciling in a follow-up doc PR.

**Source refs:**
- `src/lcsas/staging/metadata.py` (on-disc periodic-verification text)
- `docs/ESTATE_PLANNING.md` (operator-facing periodic maintenance checklist)
- `_print_stale_copies` / `find_stale_copies` (`src/lcsas/cli/main.py`, `src/lcsas/db/volume_copies.py`)
- `src/lcsas/db/volume_events.py` (queries for "when was this last verified")

---

## Reading the `volume_events` audit trail

**Purpose:** `volume_events` is the immutable lifecycle log for every volume — every verification, ECC repair, location move, and free-form note lands here with a UTC timestamp. It is the single source of truth for "what happened to this disc, and when?" and is what an heir, auditor, or future-you will use to reconstruct the history of any volume.

**Prerequisites:**
- Catalog database initialized.
- Events have been recorded (every burn, verify, and location move appends rows automatically).

**Steps (current state — there is no `lcsas events` CLI; querying is via Python or sqlite3):**
1. The valid event vocabulary is fixed at module load in `VALID_EVENT_TYPES`: `VERIFY_PASS`, `VERIFY_FAIL`, `VERIFY_FAIL_REBURN`, `ECC_REPAIR`, `LOCATION_MOVE`, `CONDITION_CHECK`, `NOTE`, `BURN_RECEIPT_IMPORTED` (`src/lcsas/db/volume_events.py`). The schema enforces this with a `CHECK` constraint.
2. Append events via `add_event(conn, volume_id, event_type, location=None, detail="", event_date=None)` — invalid event types raise `ValueError`.
3. Read all events for one volume, newest-first: `get_events_for_volume(conn, volume_id, event_type=None)`.
4. Read just the most-recent event (overall, or filtered by type) for a volume: `get_latest_event(conn, volume_id, event_type=None)`. This is the building block of "when was this volume last verified?"
5. Pull a global feed of one event type across all volumes, limited: `get_events_by_type(conn, event_type, limit=100)`. This is what you would build a "recent failures across the archive" dashboard on top of.
6. Single-event lookup by primary key: `get_event(conn, event_id)`.

**Expected outcome:**
- Calling `add_event` returns a fully-populated `VolumeEvent` (dataclass) with `event_id`, `event_date` (UTC ISO), and `detail` set.
- Read functions return `list[VolumeEvent]` newest-first; `get_latest_event` returns `VolumeEvent | None`.

**Variant axes that apply:**
- **Media type:** Events are media-agnostic.
- **Multi-tenant:** Events are tied to a `volume_id`, not a repository. A single event "covers" every tenant whose packs are on that volume.
- **Multi-copy:** `volume_events.location` lets you tag *which* copy an event applies to (e.g. "VERIFY_PASS at SafeDeposit_NYC"). This is the canonical way to distinguish per-copy verification state; `verify --disc` / `verify --all --disc` write location-tagged events. (See `tests/unit/test_db_volume_events.py::TestAddEvent::test_with_location`.)
- **ECC:** `ECC_REPAIR` is its own event type, intended for the workflow where dvdisaster / `lcsas-ecc` actually repairs sectors rather than just reporting them — at present no CLI command emits this event automatically.
- **Recovery tier:** Tier-2 / catalog-only.

**Test coverage:**
- Existing: `tests/unit/test_db_volume_events.py` is the most-complete test module in this area — every function in `volume_events.py` is covered, including invalid-type rejection (`tests/unit/test_db_volume_events.py:44-50`), every valid type (`tests/unit/test_db_volume_events.py:52-59`), custom event dates (`tests/unit/test_db_volume_events.py:61-68`), and the global cross-volume query with a `limit` (`tests/unit/test_db_volume_events.py:181-190`).
- Gaps:
  - No public CLI surface — there is no `lcsas events <label>` or `lcsas events --recent` command, so operators have to drop to sqlite3 or a Python REPL. Worth adding.
  - `VERIFY_FAIL_REBURN` and `CONDITION_CHECK` are *valid* event types (`VALID_EVENT_TYPES`, `src/lcsas/db/volume_events.py`) but no current command writes them automatically; they exist only for manual / future use.
  - There is no "delete-event" or "amend" function — by design, events are append-only — but neither is this constraint documented anywhere visible to the user.

**Source refs:**
- `src/lcsas/db/volume_events.py` (entire module)
- `tests/unit/test_db_volume_events.py` (entire module)
