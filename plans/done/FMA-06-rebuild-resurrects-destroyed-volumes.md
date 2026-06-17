# FMA-06: Catalog rebuild resurrects DEPRECATED/DESTROYED volumes to VERIFIED

> **STATUS: RESOLVED** — landed in `412910a` (db+docs: recency-aware catalog rebuild; never resurrect destroyed volumes [FMA-06]); guarded by `tests/unit/test_db_rebuild.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Make catalog rebuild recency-aware; stop resurrecting destroyed volumes

## Problem

Mixed-generation disc box: discs burned *before* a volume was deprecated or destroyed carry
holographic catalogs recording it as `VERIFIED`. `_merge_one_disc` resolves volume-status
conflicts by "most alive wins" (`VERIFIED`=6 outranks `DEPRECATED`=1/`DESTROYED`=0), so an
heir rebuilding a master catalog from a pile of mixed-age discs silently upgrades destroyed
volumes back to `VERIFIED` — in **both** feed orders, since the rank rule converges to the
max. `get_missing_packs` then reports packs on those volumes as restorable, the pick list
sends the heir hunting for a shredded disc, and the planner's `deprecated_disc_labels`
warning channel never fires because the status didn't survive the merge.

Separately, `packs` merge with `INSERT OR IGNORE` keyed on sha256, so `is_pruned`/
`size_bytes` come from whichever disc was fed **first** — rebuild output differs with disc
insertion order, with no warning. There is no per-row timestamp to prefer the newest
catalog's view.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/db/rebuild.py:109-150` — rank CASE table (`VERIFIED`=6 … `DESTROYED`=0) with
  `WHERE (target rank) < (source rank)` → upgrade-only; comment at `:52-53`
  ("preferring the less-destroyed state").
- `src/lcsas/db/rebuild.py:84-93` — `INSERT OR IGNORE INTO packs ... keyed on sha256` —
  first disc wins `is_pruned`/`size_bytes`.
- `src/lcsas/db/queries.py:300-314` — `get_missing_packs` trusts any non-DEPRECATED/
  DESTROYED status; `src/lcsas/restore/planner.py:80,92` — `deprecated_disc_labels`
  populated only from surviving DEPRECATED/DESTROYED status.
- Verifier correction (incorporated): volume-status resolution is order-**independent**
  (max-rank convergence) — resurrection happens regardless of order; the order dependence
  applies to **pack fields** only.
- `tests/unit/test_db_rebuild.py:50,88` —
  `test_merge_status_conflict_prefers_higher_quality` / `_keeps_better_status` **pin the
  hazard as intended behavior**; the fix must invert them.

## Fix design

Make the merge **recency-aware** with a per-source freshness ordinal, falling back to loud
warnings when freshness is unknowable.

1. **Freshness signal.** In `rebuild_catalog()` (`rebuild.py:205`), before merging compute
   `source_freshness` per disc catalog as the max of: `catalog.db` file mtime (preserved by
   Rock Ridge on the ISO) and `MAX(volumes.created_at)` inside the source DB (guards
   against mtime-mangling copies). Sort `disc_paths` by freshness descending and merge
   newest-first — this alone makes pack-field merges deterministic and newest-wins.
2. **Status resolution** (`rebuild.py:109-150`): replace pure rank-upgrade with:
   - If the source catalog is **fresher** than the catalog that last set this volume's
     status (track per-volume `status_source_freshness` in a temp dict during the rebuild
     run): take the source's status — including downgrades to DEPRECATED/DESTROYED.
   - If the source is staler: keep target, but when the stale source ranks *higher*
     (the resurrection case), append a warning to `RebuildResult.errors`-adjacent new field
     `warnings: list[str]`:
     `"volume <label>: an older disc catalog records VERIFIED but a newer one records
     DESTROYED — keeping DESTROYED. If you physically hold this disc, verify it with
     'lcsas verify <label> --disc'."`
3. **Pack fields**: keep `INSERT OR IGNORE` (newest-first ordering makes it newest-wins);
   additionally, when a staler source disagrees on `is_pruned`, count it and emit one
   summary warning.
4. **CLI + docs**: `cmd_catalog_rebuild` (`cli/main.py:1481`) prints `warnings`; add a
   "rebuilding from mixed-age discs" caveat to `recovery/docs/RECOVER.txt`'s rebuild
   section (newest disc first is automatic, warnings explained).
5. Invert/replace the two pinning tests (see Tests).

Why this design over per-row timestamps: a `status_changed_at` column would need schema v7+
**and** would still be absent from every already-burned catalog forever — catalog-file
freshness works for the entire existing disc fleet, which is the population that matters.

**Migration/compat (schema v6 — no DDL change).** Rebuild only writes the *output* DB;
source disc catalogs are read-only. Old catalogs lacking nothing — freshness derives from
file metadata + existing columns.

## Tests & gates

Always-on in `make test-unit`:

- `tests/unit/test_db_rebuild.py::test_destroyed_volume_not_resurrected_either_order` —
  stale catalog says VERIFIED, fresh says DESTROYED; merge in both orders ⇒ DESTROYED
  survives, resurrection warning emitted exactly when the stale disc is processed.
- `tests/unit/test_db_rebuild.py::test_pack_fields_prefer_freshest_catalog` — sources
  disagree on `is_pruned`/`size_bytes`; both orders ⇒ identical output equal to the
  freshest source (per verdict refinement: target pack fields, not volume status).
- Update/invert `test_merge_status_conflict_prefers_higher_quality` and
  `test_merge_status_conflict_keeps_better_status` (`test_db_rebuild.py:50,88`) — they must
  now assert recency-wins (delete or rewrite; do not leave them pinning the hazard).
- `tests/unit/test_db_rebuild.py::test_freshness_falls_back_to_max_created_at` — equal
  mtimes ⇒ row-derived freshness decides.
- Static doc test alongside `tests/recovery_hardening/test_disc_swap_docs.py` pinning the
  new RECOVER.txt rebuild-caveats section.

## Acceptance criteria

- [ ] Rebuild from {stale-VERIFIED, fresh-DESTROYED} catalogs yields DESTROYED + a warning,
  in both feed orders.
- [ ] Rebuild output (all 7 merged tables) is byte-identical regardless of disc order in
  the new tests.
- [ ] `lcsas catalog rebuild` prints warnings; `lcsas restore plan` on the rebuilt catalog
  routes the destroyed volume's packs through `deprecated_disc_labels` as designed.
- [ ] RECOVER.txt rebuild caveat present (static test green).

## Dependencies & related plans

- **FMA-10 — burn provenance**: explains the inherent staleness of on-disc catalogs that
  makes this merge problem permanent; doc sections should cross-reference.
- **BURN — "holographic catalog predates its own burn"**: same root cause, pipeline side.
- **FMA-01**: pick-list "unconfirmed volume" warning complements the resurrection warning.

## Effort

2 days (1 impl, 1 test incl. inverting the pinned tests). No special environment.

---
**Implemented:** 2026-06-12. As planned, plus: pre-existing volumes in a non-empty
output DB are seeded with a row-derived freshness baseline (file mtime is useless
post-ensure_schema); RECOVER.txt manifests regenerated (also fixing pre-existing
README.txt/RECOVER_WINDOWS.txt hash drift from BOOT-02/BOOT-08).
