# ADR 0003 — Lost volumes are not a schema state

- Status: Accepted
- Date: 2026-10-30
- Issue: none (owner decisions Q33 and Q38 of the 2026-10-30 review;
  glossary in [`CONTEXT.md`](../../CONTEXT.md))

## Context

The glossary separates two kinds of volume that the catalog records the
same way:

- A **deprecated volume** has been superseded: another volume holds every
  pack on it, so its discs are no longer needed. `lcsas consolidate
  --deprecate` makes one through `update_status()`, which writes the
  `NOTE` event `Status changed: <prev> → DEPRECATED`.
- A **lost volume** is a burned volume with no disc left in service. Its
  packs must be burned again unless another volume already holds them.
  Since BURN-10, `lcsas copy deprecate` / `lcsas copy destroy` on a
  volume's last in-service disc calls `_demote_volume_if_last_copy_lost()`
  (`src/lcsas/db/volume_copies.py`), which writes `DEPRECATED` straight
  into `volumes.status` and adds a `NOTE` event starting
  `auto-deprecated: last in-service disc …`.

The *behaviour* for a lost volume is already right. `DEPRECATED` is not
in `DURABLE_VOLUME_STATUSES`, so `get_unarchived_packs()` (through
`_HAS_DURABLE_LINK`, `src/lcsas/db/queries.py`) puts its packs back into
the next `lcsas stage`; the redundancy report and the deprecation guard
stop counting it; restore pick lists never choose it. What was wrong is the
*word*: every owner display said `DEPRECATED`, which reads as
"superseded, nothing to do" for a volume whose discs are gone.

A related gap (Q38): `lcsas copy destroy` meant "destroyed or lost", and
the catalog could not tell the two apart. A destroyed disc is gone; a lost
disc may be in a stranger's hands, carrying the disc catalog in plain text
and every repository's locked key files for an offline password-guessing
attack (`docs/DISC_CONFIDENTIALITY.md`).

## Options considered

1. **A `LOST` volume status (schema v11).** `volumes.status` has a
   `CHECK (status IN (…))` constraint (`src/lcsas/db/schema.py`), and
   SQLite cannot alter a CHECK in place, so v11 would rebuild the
   `volumes` table: rename it, recreate it with the wider CHECK, copy the
   rows, recreate the indexes, and keep the foreign keys from
   `volume_packs`, `volume_copies`, `volume_events` and `session_volumes`
   intact. Every query that enumerates statuses would then need `LOST`
   added: the ten `IN` / `NOT IN ('DEPRECATED', 'DESTROYED')` filters
   in `src/lcsas/db/queries.py` alone, plus `VALID_TRANSITIONS`, the
   status rank that `catalog rebuild` uses to merge disc catalogs, and
   the verify filters. Every disc burned afterwards would carry a v11
   disc catalog.

   The C reader (tier 1) would **not** need a change:
   `LCSAS_TIER1_SCHEMA_MAX` is 5 (`recovery/src/lcsas-restore/catalog.c`),
   a newer schema only prints a warning, and only when a lookup fails the
   way schema skew does (`warn_schema_skew_once()`); tier 1 reads only
   the frozen v5 surface (`TIER1_FROZEN_SURFACE`) and filters volumes
   on `status != 'DESTROYED'`, which a `LOST` value would pass through
   exactly as `DEPRECATED` does today.

   Rejected: it changes the burned catalog format, and touches every
   status-enumerating query, to fix a display word for behaviour that is
   already correct.

2. **Keep `DEPRECATED` plus the event, and display `LOST`.** Chosen.

## Decision

1. **No schema change.** A lost volume stays recorded as `DEPRECATED`
   plus the `auto-deprecated:` `NOTE` event. Stored status values, the
   CHECK constraints and the tier-1 frozen surface are untouched.
2. **One predicate decides "lost"**: `_LOST_VOLUME_SQL` behind
   `get_lost_volume_ids()` in `src/lcsas/db/queries.py`. A volume is lost
   when it has disc records, none of them `ACTIVE`, and either
   - its recorded status still claims a disc (`BURNED`, `VERIFIED`,
     `CONSOLIDATING`: a catalog from before BURN-10, or status/disc drift),
     or
   - it is `DEPRECATED` and carries a `NOTE` whose detail starts with
     `auto-deprecated:` (`AUTO_DEPRECATED_NOTE_PREFIX` in
     `src/lcsas/constants.py`; only the prefix is matched, because older
     catalogs say "last physical copy").

   A volume deprecated by consolidation carries `Status changed: … →
   DEPRECATED` instead, so it stays a deprecated volume even after its
   discs are recorded deprecated or destroyed. A volume with no disc
   records at all counts by its status alone (legacy) and is never lost;
   nor is a `DESTROYED` or never-burned `STAGING`/`BURNING` volume.
3. **Owner displays show `LOST`** through `volume_display_status()`:
   `lcsas status` (with `N total (M lost)`), `volume impact`, the
   `catalog reconcile` drift list (`status=LOST (recorded as …)`), the
   `burn --dry-run` volume list and the `verify --mark-verified` refusal.
   `status --redundancy` uses the same
   helper, but a lost volume is never a live holder there, so it never
   appears in that report. The estate
   card's disc count leaves lost volumes out, because the heir will not
   hold their discs. In `lcsas status` a lost volume's packs count as
   unarchived, not as staged on a never-burned volume.
4. **Recording the last in-service disc gone warns the owner.**
   `lcsas copy deprecate` / `copy destroy` say the volume is now lost and
   list, by repository, the packs now on no disc in service, only in the
   mirror (`get_lost_volume_packs()`, the same rule as the stage pool), and
   say the next `lcsas stage` burns them again. Separately, under `DATA
   LOSS`, they list any such pack that was also pruned from the mirror:
   nothing in service holds it, so an older snapshot that still needs it
   can no longer be fully restored. `lcsas volume impact` on a lost volume
   prints the same lists in full.
5. **A lost disc is recorded with `lcsas copy destroy --lost` (Q38).** The
   disc status is `DESTROYED`, as for a destroyed disc. Every `copy
   deprecate` / `copy destroy` now writes a per-disc `NOTE` (`disc at
   <location> recorded as deprecated (out of service)` / `… as destroyed`
   / `… as lost (whereabouts unknown)`), and the auto-deprecation note
   says `lost` too, so the volume's events tell the two apart. `--lost`
   also warns that the disc may be in a stranger's hands and that only a
   strong password protects it.

## Consequences

- Disc catalogs, the restore path and tier 1 see no change: they still
  read `DEPRECATED`. An heir browsing a disc catalog with sqlite sees
  `DEPRECATED`, not `LOST`.
- "Lost" depends on the text of a `NOTE` event. The prefix is a named
  constant shared by the writer and the predicate, and
  `tests/unit/test_lost_volumes.py` pins both the old and the new wording.
  `catalog rebuild` merges `volume_events` from the disc catalogs, so the
  marker survives a rebuild from discs burned after the volume was lost;
  a rebuild from older discs only cannot know the volume was lost.
- A drifted volume (durable status, no disc in service) shows `LOST`, but
  its packs still count as archived, so `lcsas stage` does not burn them
  again until the drift is fixed. `catalog reconcile` reports it; it is
  not auto-fixed.
- If the `volumes` table is ever rebuilt for another reason, folding
  `LOST` into the stored statuses becomes cheap and this decision should
  be revisited.
