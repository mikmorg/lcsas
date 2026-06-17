# FMA-10: Burn provenance for the newest session is never holographic; rebuild drops the audit trail

> **STATUS: RESOLVED** — landed in `249f8f8` (db+docs: rebuild keeps burn provenance; document newest-session STAGING gap [FMA-10]); guarded by `tests/unit/test_burn_orchestrator.py`.

**Priority:** P2 · **Severity:** low · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** untracked (adjacent: DEFERRED_WORK.txt item 1, disc_index sidecar — catalog redundancy, not provenance)
**Suggested GH issue title:** Persist burn provenance: copy hashes, rebuild events, doc the STAGING gap

## Problem

By construction the catalog burned onto each disc is copied at staging time, before any
burn: every volume of the final session appears as `STAGING` with zero `volume_copies` in
every surviving on-disc catalog. Burn receipts (location, verify result, iso_sha256) live
only in the staging SSD's session directory, which `clean_session` deletes; and
`rebuild_catalog` merges 7 tables but not `volume_events`, `burn_sessions`, or
`session_volumes`. After NAS loss, a disc-rebuilt catalog therefore has no locations, no
verification history, and no ISO hashes for the newest discs (`volume_copies.iso_sha256`
is NULL anyway — the orchestrator never passes it). Restore still works; resuming
*operations* from the holographic copy loses where copies live, what was verified, and the
portable hash-verify capability — and the limitation is documented nowhere.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/burn/orchestrator.py:407-409` — `inject_catalog` runs inside
  `_stage_single_volume`, before `add_session_volume` (`:629-637`) and any burn.
- `orchestrator.py:742-748` — `add_volume_copy(...)` without `iso_sha256`/`media_serial`;
  `:965-1001` — receipts JSON written under `session_dir/receipts`; `:813-830` —
  `clean_session` deletes that directory tree.
- `src/lcsas/db/rebuild.py:62-196` — merges exactly repositories, locations, packs,
  volumes, snapshots, volume_packs, volume_copies; events/sessions excluded.
- `recovery/scripts/restore.sh:826-831` — meta-disc deliberately carries no catalog;
  freshest data-disc catalog wins.

## Fix design

Three cheap moves; the structural gap (a disc cannot record its own burn) is accepted and
documented instead of engineered away.

1. **Stop nulling provenance at the source** — pass `iso_sha256=sv.iso_sha256` (and
   `media_serial` when known) in `burn_session`'s `add_volume_copy` call. This is the same
   edit as FMA-04 item 1; implement once there, assert here.
2. **Rebuild keeps history** — extend `_merge_one_disc` to merge `volume_events` (keyed on
   `(volume_id-via-uuid, event_type, event_date)`, `INSERT OR IGNORE`) and
   `burn_sessions`/`session_volumes` (natural keys `session_id`, `(session_id, volume_id)`
   with uuid translation). Older disc catalogs that predate some columns merge what they
   have (tolerant SELECT column lists, same pattern as `volume_copies` merge).
3. **Document the known limitation** — add to `recovery/docs/RECOVER.txt` (rebuild
   section, alongside FMA-06's caveat): a catalog rebuilt from discs shows the newest
   session's volumes as `STAGING` with no locations or verify history; this is expected —
   run `lcsas verify --disc` to re-establish copies. Doc-only operator habit: keep the
   latest `receipts/` JSONs with the printed estate papers; `lcsas catalog import-receipts`
   (`cli/main.py:1288+`) already re-ingests them.

**Migration/compat (schema v6 — no DDL change).** Rebuild-side only; old disc catalogs
lacking the event/session tables are skipped table-by-table (probe `sqlite_master` first)
without failing the merge.

## Tests & gates

Always-on in `make test-unit`:

- `tests/unit/test_burn_orchestrator.py::test_burn_records_iso_sha256_on_copy` —
  `volume_copies.iso_sha256 == session_volumes.iso_sha256` after burn (shared with FMA-04).
- `tests/unit/test_db_rebuild.py::test_rebuild_merges_events_and_sessions` — source
  catalog with events + session rows ⇒ present in output with translated volume ids.
- `tests/unit/test_db_rebuild.py::test_rebuild_tolerates_catalog_without_event_tables` —
  v3-era-shaped source ⇒ merge succeeds, other tables intact.
- Static doc test (recovery_hardening pattern) pinning the RECOVER.txt limitations section.

## Acceptance criteria

- [ ] Verified burns produce copy rows with non-NULL `iso_sha256`.
- [ ] `lcsas catalog rebuild` output contains the source discs' `volume_events` and
  session tables.
- [ ] RECOVER.txt documents the newest-session STAGING gap (static test green).

## Dependencies & related plans

- **FMA-04** — owns the `add_volume_copy` provenance pass-through; land first.
- **FMA-06** — same rebuild function and same RECOVER.txt section; coordinate edits.
- **BURN — "holographic catalog predates its own burn"** — pipeline-side statement of the
  same structural fact; the doc text should be written once.

## Effort

1 day (0.5 impl, 0.5 test). No special environment.

---
**Implemented:** 2026-06-13. Items 2+3 as planned; item 1 (iso_sha256 pass-through) was already landed by BURN-04/05+FMA-03 — covered here by the new `test_burn_records_iso_sha256_on_copy` assertion. Rebuild now merges volume_events/burn_sessions/session_volumes (uuid-translated, sqlite_master-probed for old catalogs); RECOVER.txt documents the newest-session STAGING gap + import-receipts remedy.
