# FMA-08: No blast-radius reporting — "what is lost if all copies of disc X fail?"

**Priority:** P2 · **Severity:** medium · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: adjacent only — READINESS_CHECKLIST.txt "VOLUME COUNT CHECK" (inventory gap), DEFERRED_WORK.txt item 4 (snapshot-aware pick list, restore-side inverse)
**Suggested GH issue title:** Surface redundancy report and per-volume impact in the CLI

## Problem

The pre-mortem question — "all copies of ONE disc fail: which data is lost?" — has no
answer in the tooling. `get_redundancy_report()` (packs below N volume copies) exists in
`db/queries.py` but is called only by tests; no CLI command exposes it. Nothing maps a
volume to the repos/packs/snapshots that become unrestorable if it is lost. `lcsas status`
prints aggregate counts plus per-volume status lines; `lcsas location list/status` reports
location completeness but nothing per-disc. An owner cannot find which discs are
single-points-of-failure for which memories, and an heir triaging a box with one damaged
disc cannot tell whether that disc matters.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/db/queries.py:397-417` — `get_redundancy_report()` defined (counts volumes
  with status NOT IN DEPRECATED/DESTROYED per pack); `grep` over `src/lcsas/cli/main.py`:
  zero references (only `tests/unit/test_edge_cases.py`, `test_cross_location_restore.py`,
  `test_multi_copy_restore.py` use it).
- `src/lcsas/cli/main.py:909-933` — `cmd_status` output is counts + volume lines only.
- `src/lcsas/cli/main.py:1226-1269` — `location list/status`: completeness ("N packs
  behind"), nothing per-volume; no impact command among the subcommands.
- No pack↔snapshot mapping exists in the catalog (only `repo_id` on both); snapshot-level
  impact requires the rustic index from per-repo mirror metadata (same machinery the
  restore planner uses to derive required packs).

## Fix design

Two CLI surfaces, catalog-only first, snapshot-aware as an explicit second step.

1. **`lcsas status --redundancy [--min-copies N]`** (default N=2) — surface
   `get_redundancy_report` grouped by the volume(s) holding each under-replicated pack:
   per volume: label, status, locations of ACTIVE copies, count + bytes of packs for which
   it is the *only* durable holder. After FMA-04/FMA-01 land, count redundancy by
   **ACTIVE `volume_copies`** rather than volume rows — update
   `get_redundancy_report` accordingly (a volume row with zero ACTIVE copies is not a
   copy). Default `lcsas status` prints one line when any pack has < 2 copies:
   `"WARNING: N packs (X GB) exist on only one disc — run 'lcsas status --redundancy'"`.
2. **`lcsas volume impact <LABEL>`** — for the named volume:
   - packs whose only durable home is this volume (reuse the
     `check_deprecation_safe` query shape, `db/volumes.py:241+`), grouped by repo with
     counts/bytes;
   - locations holding copies, with `last_verified_at` ages (ties into FMA-05);
   - `--snapshots` flag (phase 2): for each affected repo, load the rustic index from the
     mirror metadata (planner machinery), compute which snapshots reference any at-risk
     pack, and list snapshot id/time/paths. Requires the mirror mounted; degrade with a
     clear message when it isn't (`"snapshot impact needs the live mirror — pack-level
     report above is catalog-only"`).
3. **READINESS_CHECKLIST**: add a "BLAST-RADIUS REVIEW" item referencing both commands
   (monthly, alongside the volume count check at `READINESS_CHECKLIST.txt:76-84`).

No schema change (v6 untouched); pure query + CLI work. Old on-disc catalogs work with the
pack-level report as-is.

## Tests & gates

Always-on in `make test-unit`:

- `tests/unit/test_cli_handlers.py::test_status_redundancy_lists_single_copy_packs` —
  catalog with one single-copy volume ⇒ flagged with pack/byte counts; volumes with ≥2
  ACTIVE copies absent.
- `tests/unit/test_cli_handlers.py::test_status_warns_on_under_replication` — default
  status output contains the one-line warning iff under-replicated packs exist.
- `tests/unit/test_cli_handlers.py::test_volume_impact_lists_at_risk_packs` — volume
  holding the only copy of packs P1,P2 plus a pack also on another VERIFIED volume ⇒ only
  P1,P2 reported, grouped by repo, with bytes.
- `tests/unit/test_cli_handlers.py::test_volume_impact_snapshots_at_risk` — fixture mirror
  metadata mapping snapshot S → P1 ⇒ S appears under `--snapshots`; missing mirror ⇒
  degrade message, exit 0.
- `tests/unit/test_db_queries.py::test_redundancy_report_counts_active_copies` — pins the
  copy-based (not volume-row-based) counting after the FMA-01/04 semantics.
- Static doc test (recovery_hardening pattern) pinning the READINESS_CHECKLIST item.

## Acceptance criteria

- [ ] `lcsas status --redundancy` on the live catalog lists every single-copy pack grouped
  by holding volume.
- [ ] `lcsas volume impact <LABEL>` answers the pre-mortem in one command, including
  snapshot listing when the mirror is mounted.
- [ ] Default `lcsas status` warns when redundancy < 2 anywhere.
- [ ] All new tests green in `make test-unit`.

## Dependencies & related plans

- **FMA-01 / FMA-04**: define what counts as a real copy — land first so the redundancy
  math is honest from day one.
- **FMA-05**: provides `last_verified_at` ages shown in the impact report.
- **BURN — "Volume status is never reconciled with its physical copies"**: shares the
  copy-based counting change; coordinate the `get_redundancy_report` edit.
- **DEFERRED_WORK item 4** (snapshot-aware pick list): phase-2 `--snapshots` shares its
  index-walk helper; build it once, in `restore/planner.py` or a small shared module.

## Effort

1.5 days for phase 1 (CLI + queries + tests); +1 day for `--snapshots` phase 2 (index-walk
reuse). No special environment.

---
**Implemented:** 2026-06-12. As planned, with notes: `get_redundancy_report` now counts
ACTIVE `volume_copies` rows (two ACTIVE copies of one volume = two discs), and
`check_deprecation_safe` delegates to the new shared `get_at_risk_packs_for_volume`
query so guard and impact report cannot disagree. Phase-2 `--snapshots` maps
snapshot→packs via per-snapshot `restore_dry_run` (the planner machinery) over
catalog-registered snapshots, degrading per-repo. The pre-existing BURN-10 test
`test_redundancy_report_counts_active_copies` was extended (not duplicated) to pin
copy-based counting.
