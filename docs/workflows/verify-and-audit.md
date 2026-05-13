# Verify & Audit Workflows

Cold storage is only as trustworthy as the last time someone looked. Once a disc leaves the burner, slides into a sleeve, and gets locked in a safe-deposit box, the laws of media physics start working against it: organic dyes oxidize, M-Disc inorganic layers flake at microscopic scale, sleeves get bent, and humidity creeps in. The **Verify & Audit** layer of LCSAS is how operators (and their heirs) re-establish trust over time — pulling discs out of their boxes on a periodic cadence, reading them back, comparing the bits against the holographic catalog burned alongside the data, and recording the result in a tamper-evident audit trail.

This document covers four observability/integrity commands and the policies that bind them:

- `lcsas verify <volume>` — verify one disc/ISO and update its status
- `lcsas verify --all` — batch-verify every `BURNED`/`VERIFIED` volume
- `lcsas status` — at-a-glance archive inventory
- `lcsas session list` — list staging/burn sessions
- `lcsas session show <id>` — drill into a specific session *(gap: not implemented)*
- Periodic re-verification cadence — how often to pull discs out of safes
- Reading the `volume_events` audit trail — the immutable lifecycle log

For the meaning of status strings (`STAGING`, `BURNING`, `BURNED`, `VERIFIED`, `DEPRECATED`, `DESTROYED`) and event types (`VERIFY_PASS`, `VERIFY_FAIL`, `ECC_REPAIR`, `LOCATION_MOVE`, `CONDITION_CHECK`, `NOTE`, `VERIFY_FAIL_REBURN`), see `docs/WORKFLOWS.md` (status legend, when it lands).

## Table of contents

1. [`lcsas verify <volume>` — single-volume verification](#lcsas-verify-volume--single-volume-verification)
2. [`lcsas verify --all` — batch re-verification](#lcsas-verify---all--batch-re-verification)
3. [`lcsas catalog validate` — disc/catalog cross-check (companion command)](#lcsas-catalog-validate--disccatalog-cross-check-companion-command)
4. [`lcsas status` — inventory dashboard](#lcsas-status--inventory-dashboard)
5. [`lcsas session list` — list sessions](#lcsas-session-list--list-sessions)
6. [`lcsas session show <id>` — session drill-down (gap)](#lcsas-session-show-id--session-drill-down-gap)
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
1. Argparse registers `verify` with positional `volume_label` plus `--iso`, `--disc`, `--device`, `--mark-verified`, `--mark-failed`, `--detail`, `--all`, `--location` (`src/lcsas/cli/main.py:336-355`).
2. Dispatch routes `args.command == "verify"` to `cmd_verify` (`src/lcsas/cli/main.py:2732-2733`).
3. `cmd_verify` loads config and acquires a `locked_connection` against the resolved db path, then runs `create_all` to guarantee schema v5 (`src/lcsas/cli/main.py:1521-1530`).
4. If `--all` is set, control hands off to `_verify_all` (`src/lcsas/cli/main.py:1532-1534`) — see the next workflow.
5. Otherwise, look up the volume by label; bail if missing (`src/lcsas/cli/main.py:1537-1544`).
6. **Manual-marking branch (`--mark-verified`):** transition `BURNED → VERIFIED` (or `STAGING → VERIFIED` with `force=True` for the split-machine workflow) and append a `VERIFY_PASS` event (`src/lcsas/cli/main.py:1547-1566`).
7. **Manual-marking branch (`--mark-failed`):** append a `VERIFY_FAIL` event with `detail` text; do not change status (`src/lcsas/cli/main.py:1568-1574`).
8. **Physical/ISO branch:** if `--iso` was not passed and `--disc` was not requested, look up the most-recent `session_volumes.iso_path` for the volume; bail if neither is available (`src/lcsas/cli/main.py:1577-1590`).
9. If `--disc`: instantiate `SubprocessXorrisoRunner` and call `verify_disc(device=args.device)` (`src/lcsas/cli/main.py:1594-1601`).
10. Else: confirm the ISO exists, instantiate `SubprocessDVDisasterRunner`, call `verify_iso(iso_path)` to read the RS03 ECC layer (`src/lcsas/cli/main.py:1602-1613`).
11. Append a `VERIFY_PASS` or `VERIFY_FAIL` event with detail describing the source; the valid event types come from `VALID_EVENT_TYPES` in `src/lcsas/db/volume_events.py:23-31` and the underlying insert is `add_event` (`src/lcsas/db/volume_events.py:34-75`).
12. If the verification passed *and* the volume was `BURNED`, promote it to `VERIFIED` via `update_status` (`src/lcsas/cli/main.py:1620-1623`).
13. Return `0` on pass, `1` on fail (`src/lcsas/cli/main.py:1625`).

**Expected outcome:**
- Stdout shows `Verifying ISO: <path>` (or `Verifying disc on <device> ...`) and a `PASS`/`FAIL` line.
- For a previously-`BURNED` volume that passes, log line `Volume <label>: promoted BURNED → VERIFIED`.
- A new row appears in `volume_events` with `event_type` set to `VERIFY_PASS` or `VERIFY_FAIL`, today's UTC `event_date`, and the path or device in `detail`.
- Exit code `0` on pass, `1` on fail or argument error.

**Variant axes that apply:**
- **Media type:** ISO-mode verification is media-agnostic (it just reads the staged file). `--disc` mode reads physical media and is therefore sensitive to drive capability — Blu-ray media (BD25, MDISC100) needs a BD-capable drive at `--device`. DVDisaster RS03 ECC applies to any ISO regardless of target media, so the same verification machinery is exercised for TEST_TINY images during development.
- **Multi-tenant:** This command operates on a single physical volume; the underlying disc may carry packs from multiple Rustic repositories. Verification is at the *pack-hash* layer (and the ECC layer), not the repository layer, so it is multi-tenancy-blind by design — a single `VERIFY_PASS` covers every tenant's packs on the disc.
- **Optical drive count:** Single-drive sites must serialize `--disc` runs (one drive, one disc at a time). Multi-drive sites can run several `lcsas verify` processes in parallel as long as each names a distinct `--device`; the catalog write path uses `locked_connection` so concurrent event writes are serialized at the SQLite level (`src/lcsas/cli/main.py:1529`).
- **Multi-copy:** This command verifies *one* copy at a time. To re-verify every physical copy of one volume you must run it once per copy with the appropriate `--device` and `--detail` describing which copy was checked. Copies are tracked in `volume_copies`; `verify` does not iterate them automatically. (See "Gaps" below.)
- **ECC:** Default mode reads the DVDisaster RS03 ECC layer — that is the whole point of using ECC in the first place. `--disc` mode falls back to xorriso's `verify_disc` (a simpler read-test). Sites that bypass ECC (none in default builds) lose the ability to detect single-sector bit-rot at the cost of recovery margin.
- **Recovery tier:** This is a Tier-2 (COLD) integrity check; it does not touch the HOT/WARM Rustic mirrors. A pass here only guarantees the disc is readable — it does not guarantee `rustic restore` will succeed (that's covered by the restore workflows).

**Test coverage:**
- Existing: `tests/unit/test_db_verify.py` exhaustively covers `_collect_disc_packs` (`tests/unit/test_db_verify.py:16-100`) and `validate_disc` (`tests/unit/test_db_verify.py:103-238`) — the catalog/disc cross-check used by `lcsas catalog validate`. Event insertion paths used by `cmd_verify` are covered by `tests/unit/test_db_volume_events.py::TestAddEvent` (`tests/unit/test_db_volume_events.py:19-68`).
- Gaps:
  - No unit test exercises `cmd_verify` end-to-end (the CLI handler is uncovered) — the `BURNED → VERIFIED` promotion in `src/lcsas/cli/main.py:1620-1623` and the `STAGING → VERIFIED` split-machine path at `src/lcsas/cli/main.py:1551-1555` are tested only manually.
  - No test for the auto-resolution of `iso_path` from `session_volumes` (`src/lcsas/cli/main.py:1579-1590`).
  - No injection point: `SubprocessXorrisoRunner` / `SubprocessDVDisasterRunner` are instantiated inline rather than passed as a `Protocol` — verification of `--disc` and ISO-mode in tests requires monkey-patching.

**Source refs:**
- `src/lcsas/cli/main.py:336-355` (argparse), `:1513-1625` (`cmd_verify`), `:2732-2733` (dispatch)
- `src/lcsas/db/volume_events.py:23-75` (`add_event`, `VALID_EVENT_TYPES`)
- `src/lcsas/db/verify.py:63-181` (`validate_disc`)
- `tests/unit/test_db_verify.py`, `tests/unit/test_db_volume_events.py`

---

## `lcsas verify --all` — batch re-verification

**Purpose:** Sweep every volume in `BURNED` or `VERIFIED` status (optionally filtered to one storage location) and re-verify each via its ISO. This is the cron-friendly form: point it at the staging directory after a periodic ISO rebuild, run overnight, and get a summary plus a fresh `VERIFY_PASS` / `VERIFY_FAIL` event per volume.

**Prerequisites:**
- Same DB prerequisites as the single-volume form.
- A reachable ISO file recorded in `session_volumes.iso_path` for each volume to be checked. Volumes without an ISO (e.g. those whose staging was already cleaned) are skipped, not failed.
- `dvdisaster` on `$PATH` — batch mode runs ECC verification only; no `--disc` equivalent.

**Steps:**
1. `cmd_verify` checks `args.verify_all` and delegates to `_verify_all` (`src/lcsas/cli/main.py:1532-1534`).
2. `_verify_all` pulls all `BURNED` and `VERIFIED` volumes and concatenates them (`src/lcsas/cli/main.py:1634-1636`).
3. If `--location` is set, fetch `volume_copies` for each candidate and keep only those with a copy at the named location (`src/lcsas/cli/main.py:1638-1645`).
4. Short-circuit if no candidates remain (`src/lcsas/cli/main.py:1647-1649`).
5. Instantiate one `SubprocessDVDisasterRunner` for the whole sweep (`src/lcsas/cli/main.py:1655-1656`).
6. For each candidate, look up the most-recent `session_volumes.iso_path`; skip if missing or the file is gone (`src/lcsas/cli/main.py:1660-1673`).
7. Call `dvd_runner.verify_iso(iso_path)`, append a `VERIFY_PASS` / `VERIFY_FAIL` event with `"Batch ISO verify: <path>"` as detail (`src/lcsas/cli/main.py:1675-1677`).
8. On pass, promote `BURNED → VERIFIED` (idempotent for already-`VERIFIED` volumes) (`src/lcsas/cli/main.py:1681-1683`).
9. Print a summary line: passed / failed / skipped (`src/lcsas/cli/main.py:1688-1689`).
10. Exit `1` if any volume failed *or* if every candidate was skipped; `0` otherwise (`src/lcsas/cli/main.py:1690-1695`).

**Expected outcome:**
- One log line per volume (`<label>: PASS` / `FAIL`, or `<label>: ISO not found ... — skipped`).
- One summary line: `Verification complete: N passed, M failed, K skipped`.
- One `VERIFY_PASS` or `VERIFY_FAIL` row in `volume_events` per non-skipped volume.
- `BURNED` volumes that passed are now `VERIFIED`.

**Variant axes that apply:**
- **Media type:** ECC verification only, so this works equally well for BD25 / MDISC100 / TEST_TINY ISOs.
- **Multi-tenant:** Repository-blind, like the single form. A single sweep covers every tenant's data simultaneously.
- **Optical drive count:** No drive involvement — batch mode operates entirely on staged ISO files. The optical-drive count constraint is moved to `--disc` mode (single form). This means batch can run on any host that can mount the staging directory.
- **Multi-copy:** Operates on the volume, not the physical copies. If a volume has three copies on three different shelves, `--all` checks the *ISO* (one image) once — it does *not* iterate copies. Use `--location` to restrict the sweep to volumes that exist at one site.
- **ECC:** This workflow is the canonical way to exercise the DVDisaster RS03 layer in bulk. Without ECC, the value of batch verify drops sharply (you'd just be re-reading SHA-256s without any redundancy margin).
- **Recovery tier:** Tier-2 only; never touches HOT/WARM.

**Test coverage:**
- Existing: `tests/unit/test_db_volume_events.py::TestGetEventsByType` (`tests/unit/test_db_volume_events.py:165-190`) covers the cross-volume query that an operator would use to audit the result. List-volume queries are covered indirectly by the broader volumes test suite (not in scope for this doc).
- Gaps:
  - No test exercises the `--location` filter (`src/lcsas/cli/main.py:1638-1645`).
  - No test asserts the "all skipped → exit 1" branch (`src/lcsas/cli/main.py:1692-1694`).
  - No test for the `BURNED → VERIFIED` promotion in batch mode (`src/lcsas/cli/main.py:1681-1683`).

**Source refs:**
- `src/lcsas/cli/main.py:352-355` (argparse for `--all` / `--location`), `:1628-1695` (`_verify_all`)
- `src/lcsas/db/volume_events.py:34-75` (event recording)
- `tests/unit/test_db_volume_events.py`

---

## `lcsas catalog validate` — disc/catalog cross-check (companion command)

> Listed here because it shares the verify/audit conceptual scope, even though the user-facing command lives under `catalog`. Operators routinely combine it with `lcsas verify` when a disc fails ECC.

**Purpose:** Mount a disc, compare the pack files actually present in its `data/` directory against the SHA-256 list recorded in its on-disc `catalog.db` / `volume_info.json`, and report any packs that are missing or orphaned. Where `verify` answers "is the ECC layer still good?", `catalog validate` answers "did all the files we *thought* were on this disc actually land here?"

**Prerequisites:**
- A mounted LCSAS disc at `disc_path` containing a `catalog.db` and a `data/` directory.
- The disc was burned with the holographic injector (`staging/metadata.py`) so `volume_info.json` and `catalog.db` are present.

**Steps:**
1. Argparse exposes `lcsas catalog validate <disc_path>` (handler at `src/lcsas/cli/main.py:1314`).
2. `cmd_catalog_validate` calls `validate_disc(disc_path)` (`src/lcsas/cli/main.py:1325`).
3. `validate_disc` requires `catalog.db` and `data/` to exist; raises `FileNotFoundError` / `ValueError` otherwise (`src/lcsas/db/verify.py:81-93`).
4. Walk `data/` recursively, collecting any filename that is exactly 64 lowercase hex chars (`src/lcsas/db/verify.py:39-60`) — this handles both flat (`data/HASH`) and two-level (`data/ab/abcdef...`) layouts.
5. Open the on-disc catalog read-only (`mode=ro` URI; `src/lcsas/db/verify.py:103`).
6. Prefer `volume_info.json` `sha256_manifest` as the source of truth; fall back to a SQL query over `volumes`/`volume_packs`/`packs` filtered to volumes whose status is `VERIFIED`, `BURNED`, `STAGING`, or `BURNING` (`src/lcsas/db/verify.py:107-160`).
7. Compute set differences: `missing_from_disc = catalog - disc`, `orphaned_on_disc = disc - catalog` (`src/lcsas/db/verify.py:178-179`).
8. `cmd_catalog_validate` logs each missing/orphaned hash and returns `0` only if both sets are empty (`src/lcsas/cli/main.py:1330-1358`).

**Expected outcome:**
- `Catalog validation PASSED` (exit 0) or `Catalog validation FAILED — N missing, M orphaned.` (exit 1).
- No mutation of the master catalog (this command is read-only against the disc's *own* embedded DB; it does *not* write a `volume_events` row — see "Gaps").

**Variant axes that apply:**
- **Media type:** Reads files from a mount point; works for any media that presents a filesystem.
- **Multi-tenant:** A single disc can contain packs from multiple repositories — the SQL fallback query joins `volume_packs` so all tenants on the disc are validated in one pass.
- **Multi-copy:** Validate one copy at a time by re-mounting each.
- **ECC:** Independent of the ECC layer — this is a filesystem-level check, not a sector-level check. Useful when ECC reports OK but you suspect a write-side regression.
- **Recovery tier:** Tier-2; this is exactly what you run on a recovery host before trusting a disc's contents.

**Test coverage:**
- Existing: `tests/unit/test_db_verify.py` covers every branch of `validate_disc` — `volume_info`-driven (`test_single_disc_all_packs_present`, `tests/unit/test_db_verify.py:126-151`), missing packs (`tests/unit/test_db_verify.py:153-171`), orphaned packs (`tests/unit/test_db_verify.py:173-195`), missing catalog (`tests/unit/test_db_verify.py:197-204`), missing data dir (`tests/unit/test_db_verify.py:206-210`), empty manifest (`tests/unit/test_db_verify.py:212-222`), and the `ok` property (`tests/unit/test_db_verify.py:224-238`). Both layout shapes (flat / two-level) are tested.
- Gaps:
  - Mixed-case hex pack names are explicitly *not* matched (`tests/unit/test_db_verify.py:71-87` documents this). If a downstream tool writes uppercase pack files they will appear as missing — that is a real, currently-tested-as-known limitation.
  - The fallback SQL query (`src/lcsas/db/verify.py:127-160`) is not unit-tested in isolation; the `volume_info.json`-present path is the only one covered.
  - `cmd_catalog_validate` does **not** record a `volume_events` entry — a successful validate doesn't move a volume's status. If you want the cross-check to count toward the audit trail, currently you have to follow it with a separate `lcsas verify <label> --mark-verified --detail "catalog validate ok"`.

**Source refs:**
- `src/lcsas/cli/main.py:1314-1359` (`cmd_catalog_validate`)
- `src/lcsas/db/verify.py` (entire module)
- `tests/unit/test_db_verify.py` (entire module)

---

## `lcsas status` — inventory dashboard

**Purpose:** One-shot human-readable summary of the archive: how many packs total / archived / unarchived / pruned, plus a table of every volume with its label, media type, lifecycle status, and current storage location. The "did I lose any data this week?" command.

**Prerequisites:**
- Catalog database initialized.
- No external tools — pure SQL.

**Steps:**
1. Argparse: `subparsers.add_parser("status", ...)` (`src/lcsas/cli/main.py:110-111`).
2. Dispatch: `args.command == "status"` → `cmd_status` (`src/lcsas/cli/main.py:2673-2674`).
3. `cmd_status` opens an unlocked connection (read-only is fine here) and calls `create_all` to ensure schema exists (`src/lcsas/cli/main.py:761-769`).
4. Query `get_archive_status_summary(conn)` for pack counts (`src/lcsas/cli/main.py:771`).
5. Query `list_volumes(conn)` for the volume table (`src/lcsas/cli/main.py:772`).
6. Print one line of pack stats and one line per volume in a fixed-width table (`src/lcsas/cli/main.py:776-782`).
7. Return `0`.

**Expected outcome:**
- A `Packs:` line and a `Volumes: N total` line, followed by one fixed-width row per volume: `<label:25> <media_type:10> <status:10> <location>`.
- Exit `0` unconditionally (this is read-only; no failure modes apart from a missing DB).

**Variant axes that apply:**
- **Media type:** Listed per-volume in the `media_type` column. No media-specific behaviour beyond column width.
- **Multi-tenant:** The output is volume-centric, not repository-centric — multi-tenant deployments will see every tenant's volumes interleaved. To filter by tenant, use SQL directly against `volume_packs JOIN packs JOIN repositories`.
- **Multi-copy:** The `location` column shows a single location per volume row. Volumes with multiple copies (`volume_copies` table) are still listed once; the displayed location comes from the volume row itself, not from `volume_copies` — so this command **underreports multi-copy state**. (See "Gaps".)
- **ECC:** N/A.
- **Recovery tier:** Catalog-only; no media is touched.

**Test coverage:**
- Existing: `get_archive_status_summary` and `list_volumes` are covered by the broader queries/volumes test suites (not in scope here).
- Gaps:
  - No CLI-level smoke test for `cmd_status`.
  - The single-location display ignores `volume_copies` entirely — a volume with copies at Home_Shelf *and* SafeDeposit_NYC shows only the primary `volume.location`. Sites that depend on multi-copy invariants should not use `status` as a coverage check.

**Source refs:**
- `src/lcsas/cli/main.py:110-111` (argparse), `:759-783` (`cmd_status`), `:2673-2674` (dispatch)

---

## `lcsas session list` — list sessions

**Purpose:** Enumerate every burn session in the catalog (each session is a single staging+burn run that may produce multiple volumes). Used for picking the session ID to feed into `lcsas burn --session <id>`, or for forensically tracing "which run of LCSAS produced this disc?"

**Prerequisites:**
- Catalog database initialized.
- `--config` is **required** — `cmd_session_list` errors out without it (this is inconsistent with `status`/`verify`, which fall back to `archive.db`). See "Gaps".

**Steps:**
1. Argparse: `session` subcommand with `list` sub-sub-command, optional `--status` filter (`src/lcsas/cli/main.py:362-369`).
2. Dispatch: `args.command == "session"` and `args.session_command == "list"` → `cmd_session_list` (`src/lcsas/cli/main.py:2710-2715`).
3. Refuse to run without `--config` (`src/lcsas/cli/main.py:2747-2749`).
4. Load config, open a connection on `config.db_path` (`src/lcsas/cli/main.py:2750-2751`).
5. Call `list_sessions(conn, status_filter=args.status)` (`src/lcsas/cli/main.py:2753`) which translates to a `SELECT * FROM burn_sessions [WHERE status = ?] ORDER BY created_at` (`src/lcsas/db/sessions.py:94-108`).
6. For each session, also fetch `get_session_volumes` (`src/lcsas/db/sessions.py:136-145`) and render a one-line header plus an indented `volumes(N): ...` line (`src/lcsas/cli/main.py:2760-2772`).
7. Return `0`.

**Expected outcome:**
- A fixed-width header (`SESSION ID  STATUS  MEDIA  CREATED`), then one line per session, optionally followed by an indented list of `volume_id`s.
- Exit `0` even if there are zero sessions (just prints `No sessions found.`).

**Variant axes that apply:**
- **Media type:** `media_type` is displayed per session (one column). Sessions are media-typed at creation in `create_session` (`src/lcsas/db/sessions.py:30-48`).
- **Multi-tenant:** Sessions are tenant-blind — a single staging run can pack volumes from multiple repos.
- **Multi-copy:** Sessions are not copy-aware. A session lists its volumes once, regardless of how many physical copies were burned later (copies live in `volume_copies`, written by the burn orchestrator).
- **ECC:** N/A.
- **Recovery tier:** Catalog-only.

**Test coverage:**
- Existing: `tests/unit/test_db_sessions.py::TestSessionCRUD` covers `list_sessions` (`tests/unit/test_db_sessions.py:80-94`), including the status filter, plus all neighbouring CRUD ops (`tests/unit/test_db_sessions.py:23-100`). Session-volume linkage is covered by `TestSessionVolumes` (`tests/unit/test_db_sessions.py:103-140`).
- Gaps:
  - No CLI-level test for `cmd_session_list`.
  - No test for the `--config required` error path.
  - The volume label rendering at `src/lcsas/cli/main.py:2765` stringifies `volume_id` rather than looking up the human-readable `label` — operators looking for "which discs are in session X?" must cross-reference. Worth a bug-fix PR; flagged here as a UX gap.

**Source refs:**
- `src/lcsas/cli/main.py:362-369` (argparse), `:2741-2776` (`cmd_session_list`)
- `src/lcsas/db/sessions.py:94-108` (`list_sessions`), `:136-145` (`get_session_volumes`)
- `tests/unit/test_db_sessions.py`

---

## `lcsas session show <id>` — session drill-down (gap)

**Purpose (intended):** Show every volume in one session, with their ISO paths, ISO SHA-256s, current statuses, and the most recent `volume_events`. Effectively the per-session deep-dive companion to `session list`.

**Prerequisites:** N/A — **this subcommand is not implemented.**

**Steps:** N/A. The parser registers only `session list` (`src/lcsas/cli/main.py:364-369`), and the dispatcher rejects anything else with `Usage: lcsas session {list}` (`src/lcsas/cli/main.py:2713-2715`).

**Expected outcome:** N/A.

**Variant axes that apply:** N/A (gap).

**Test coverage:**
- The *building blocks* are already in place and tested:
  - `get_session(conn, session_id)` — `tests/unit/test_db_sessions.py:24-33`
  - `resolve_session_id(conn, "latest" | id)` — `tests/unit/test_db_sessions.py:60-72`
  - `get_session_volumes(conn, session_id)` — `tests/unit/test_db_sessions.py:114-122`
  - `get_events_for_volume(conn, vol_id)` — `tests/unit/test_db_volume_events.py:86-124`
- **Gap (feature):** No CLI handler `cmd_session_show`, no parser entry, no dispatch case. A working implementation would compose the four functions above and print a per-session report. This is a small, well-scoped addition.

**Source refs:**
- `src/lcsas/cli/main.py:364-369` (parser; note absence of `show`), `:2710-2715` (dispatch; note absence of `show` branch)
- `src/lcsas/db/sessions.py:51-145` (every function a `show` impl would need)

---

## Periodic re-verification cadence

**Purpose:** Cold storage that is never read is no different from cold storage that has rotted — you only learn about bit-rot when you try to read the disc. A scheduled cadence pulls discs out of long-term storage on a known rhythm so failures are caught with maximum recovery margin (i.e. before the second copy also rots).

**Prerequisites:** A running LCSAS install with at least one `BURNED` or `VERIFIED` volume.

**Recommended cadence — sourced from the codebase:**

The repository specifies a cadence in two places. Both apply equally to BD-R, M-Disc, and other optical media, and are written into every disc's on-disc README via the holographic injector:

| Cadence | Action | Source |
|---------|--------|--------|
| Every **2-5 years** | Spot-check a few discs | `src/lcsas/staging/metadata.py:506` |
| Every **5-10 years** | Full verify of all discs (`lcsas verify --all`) | `src/lcsas/staging/metadata.py:507` |
| Every **5-10 years** | Re-burn discs to fresh media (even M-Disc degrades) | `docs/ESTATE_PLANNING.md:116` |
| On **any** read error | Re-burn ALL data — same-batch media may be co-degrading | `src/lcsas/staging/metadata.py:508-510` |

The codebase does **not** distinguish a separate cadence for BD-R vs. M-Disc — the same 2-5y / 5-10y window covers both. Media-vendor literature suggests M-Disc could safely use the longer end of that window and BD-R the shorter, but LCSAS itself does not encode that policy. **Gap:** there is no automated reminder, no `last_verified_at` derived metric, and no `lcsas verify --stale` flag that would flag overdue volumes — operators must consult `volume_events` manually (see next section).

**Steps:**
1. On the cadence above, plan which discs to pull. The audit trail in `volume_events` (queried via `get_events_for_volume`, `src/lcsas/db/volume_events.py:88-111`) tells you the date of the last `VERIFY_PASS` per volume.
2. Pull the discs from storage and mount them on a verification host.
3. Run `lcsas verify <label> --disc --device <dev>` per disc, or stage their ISOs and run `lcsas verify --all` (`src/lcsas/cli/main.py:1513-1695`).
4. Cross-check with `lcsas catalog validate <mount>` for each disc (`src/lcsas/cli/main.py:1314-1359`).
5. If any disc fails: append a manual `VERIFY_FAIL_REBURN` event (one of the `VALID_EVENT_TYPES`, `src/lcsas/db/volume_events.py:23-31`) and trigger the re-burn workflow (out of scope here).
6. Optionally record a `CONDITION_CHECK` `NOTE` event for spot-checks that passed without doing a full ECC pass (`src/lcsas/db/volume_events.py:23-31`).

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
  - No `lcsas verify --stale <duration>` command to surface overdue volumes.
  - No scheduled-task / hook integration; cadence runs out-of-band.
  - The cadence written to disc (`staging/metadata.py:506-510`) and the cadence in `ESTATE_PLANNING.md:116` are *slightly* divergent (2-5y vs. 5-10y vs. 5-10y reburn). Worth reconciling in a follow-up doc PR.

**Source refs:**
- `src/lcsas/staging/metadata.py:501-510` (on-disc periodic-verification text)
- `docs/ESTATE_PLANNING.md:114-124` (operator-facing periodic maintenance checklist)
- `src/lcsas/db/volume_events.py:88-149` (queries for "when was this last verified")

---

## Reading the `volume_events` audit trail

**Purpose:** `volume_events` is the immutable lifecycle log for every volume — every verification, ECC repair, location move, and free-form note lands here with a UTC timestamp. It is the single source of truth for "what happened to this disc, and when?" and is what an heir, auditor, or future-you will use to reconstruct the history of any volume.

**Prerequisites:**
- Catalog database initialized.
- Events have been recorded (every burn, verify, and location move appends rows automatically).

**Steps (current state — there is no `lcsas events` CLI; querying is via Python or sqlite3):**
1. The valid event vocabulary is fixed at module load: `VERIFY_PASS`, `VERIFY_FAIL`, `VERIFY_FAIL_REBURN`, `ECC_REPAIR`, `LOCATION_MOVE`, `CONDITION_CHECK`, `NOTE` (`src/lcsas/db/volume_events.py:23-31`). The schema enforces this with a `CHECK` constraint (referenced in the module docstring, `src/lcsas/db/volume_events.py:22`).
2. Append events via `add_event(conn, volume_id, event_type, location=None, detail="", event_date=None)` — invalid event types raise `ValueError` (`src/lcsas/db/volume_events.py:34-75`).
3. Read all events for one volume, newest-first: `get_events_for_volume(conn, volume_id, event_type=None)` (`src/lcsas/db/volume_events.py:88-111`).
4. Read just the most-recent event (overall, or filtered by type) for a volume: `get_latest_event(conn, volume_id, event_type=None)` (`src/lcsas/db/volume_events.py:114-134`). This is the building block of "when was this volume last verified?"
5. Pull a global feed of one event type across all volumes, limited: `get_events_by_type(conn, event_type, limit=100)` (`src/lcsas/db/volume_events.py:137-149`). This is what you would build a "recent failures across the archive" dashboard on top of.
6. Single-event lookup by primary key: `get_event(conn, event_id)` (`src/lcsas/db/volume_events.py:78-85`).

**Expected outcome:**
- Calling `add_event` returns a fully-populated `VolumeEvent` (dataclass; see `src/lcsas/db/volume_events.py:11-19`) with `event_id`, `event_date` (UTC ISO), and `detail` set.
- Read functions return `list[VolumeEvent]` newest-first; `get_latest_event` returns `VolumeEvent | None`.

**Variant axes that apply:**
- **Media type:** Events are media-agnostic.
- **Multi-tenant:** Events are tied to a `volume_id`, not a repository. A single event "covers" every tenant whose packs are on that volume.
- **Multi-copy:** `volume_events.location` lets you tag *which* copy an event applies to (e.g. "VERIFY_PASS at SafeDeposit_NYC"). This is the canonical way to distinguish per-copy verification state. (See `tests/unit/test_db_volume_events.py::TestAddEvent::test_with_location`, `tests/unit/test_db_volume_events.py:32-42`.)
- **ECC:** `ECC_REPAIR` is its own event type, intended for the future workflow where DVDisaster actually repairs sectors rather than just reporting them — at present there is no CLI command that emits this event automatically.
- **Recovery tier:** Tier-2 / catalog-only.

**Test coverage:**
- Existing: `tests/unit/test_db_volume_events.py` is the most-complete test module in this area — every function in `volume_events.py` is covered, including invalid-type rejection (`tests/unit/test_db_volume_events.py:44-50`), every valid type (`tests/unit/test_db_volume_events.py:52-59`), custom event dates (`tests/unit/test_db_volume_events.py:61-68`), and the global cross-volume query with a `limit` (`tests/unit/test_db_volume_events.py:181-190`).
- Gaps:
  - No public CLI surface — there is no `lcsas events <label>` or `lcsas events --recent` command, so operators have to drop to sqlite3 or a Python REPL. Worth adding.
  - `VERIFY_FAIL_REBURN` and `CONDITION_CHECK` are *valid* event types (`src/lcsas/db/volume_events.py:23-31`) but no current command writes them automatically; they exist only for manual / future use.
  - There is no "delete-event" or "amend" function — by design, events are append-only — but neither is this constraint documented anywhere visible to the user.

**Source refs:**
- `src/lcsas/db/volume_events.py` (entire module)
- `tests/unit/test_db_volume_events.py` (entire module)
