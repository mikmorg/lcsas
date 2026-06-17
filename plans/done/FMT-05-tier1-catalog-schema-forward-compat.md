# FMT-05: Tier-1 catalog reader: schema forward-compat contract

> **STATUS: RESOLVED** — landed in `601c687` (recovery+db: tier-1 catalog schema-skew contract; freeze v5 query surface [FMT-05]); guarded by `tests/unit/test_schema_v5_columns_frozen.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** untracked (recovery/docs/DEFERRED_WORK.txt item 1 covers catalog *corruption*, not version skew)
**Suggested GH issue title:** Define tier-1 catalog schema-skew contract; freeze v5 query surface

## Problem

The tier-1 C catalog reader (`catalog.c`) hard-codes schema-v5 SQL against `packs`,
`volume_packs`, and `volumes`. It reads `schema_version` but only logs it; nothing gates on
it. Worse, `lcsas_catalog_find_pack` returns `-1` both when the SQL prepare fails (schema
mismatch) and when the pack genuinely isn't cataloged. Archives accumulate discs across
years; an heir will routinely pair an *older* meta-volume binary with *newer* data discs. If
a future schema bump renames or drops any column tier-1 queries, the disc-swap hint system
fails indistinguishably from a missing pack: the heir gets bare "(catalog has no record of
this pack hash)" prompts with no volume labels and no explanation — on multi-disc archives
where tier 1 is the primary and only practical pre-Python path (TIERS.txt).

SQLite-the-file-format is a fine 2050 bet (vendored amalgamation, statically linked); the gap
is the version-skew contract on top of it. No test exercises a v>5 catalog. The heir can still
brute-force disc insertion via the prompt loop, which is why this is medium (degraded journey,
not a dead end).

## Evidence

Re-checked against current code:

- `recovery/src/lcsas-restore/catalog.c:49-63` — `lcsas_catalog_schema_version` reads the
  version; the only consumer is `lcsas_catalog_describe` (`:186-192`) which `fprintf`s
  `"[catalog] schema v%d"`. Nothing gates.
- `catalog.c:78-99` — `lcsas_catalog_find_pack`: `if (rc != SQLITE_OK) return -1;` …
  `return found ? 0 : -1;` — prepare failure and miss are the same `-1`.
- `recovery/src/lcsas-restore/disc_locator.c:723-740` — the sole caller: on non-zero it prints
  "(catalog has no record of this pack hash)", so schema skew masquerades as a genuine miss.
- `recovery/src/lcsas-restore/catalog.h:8` — "Schema version 5".
- `recovery/tests/test_catalog.c:46-48` — fixture hard-codes `INSERT INTO schema_version
  VALUES (5, …)`; grep `v6|future|forward` → no forward-compat case.

## Fix design

1. **Distinguish error from miss** — `catalog.c`: change `lcsas_catalog_find_pack` (and
   `lcsas_catalog_volumes_for_pack`) return convention to `0` = found, `1` = not found,
   `-1` = query error; update `catalog.h` docs. Sole caller `disc_locator.c:723` becomes a
   three-way branch:
   - `0` → unchanged (print volume labels);
   - `1` → existing "(catalog has no record of this pack hash)";
   - `-1` → "(catalog could not be queried — likely written by a newer LCSAS; disc-swap hints
     unavailable. Insert discs one at a time when prompted, or use the restore tools from the
     NEWEST meta disc in this set.)"
2. **Loud one-time skew diagnostic** — cache `schema_version` in the `lcsas_catalog` struct at
   open; if `> 5`, print once to stderr: *"catalog schema v%d is newer than this recovery
   binary (supports v5). Disc-swap hints may be missing — prefer the restore tooling from the
   same-generation META disc, or proceed and insert discs when prompted."* Restore itself
   continues — the locator's search-path scanning is independent of the catalog.
3. **Freeze the v5 query surface** — `src/lcsas/db/schema.py`: add a `TIER-1 FROZEN SURFACE`
   comment block listing exactly what `catalog.c` queries (`schema_version.version`;
   `packs.pack_id/sha256/size_bytes/repo_id`; `volume_packs.volume_id/pack_id`;
   `volumes.volume_id/label/status` + the `status != 'DESTROYED'` filter, per
   `catalog.c:146-154`), with the policy: future schema bumps must be **additive** — never
   rename, drop, or re-type these; burned tier-1 binaries query them forever.

**Migration/compat note (schema v5):** old burned discs carry v≤5 catalogs — the new binary's
behavior on those is unchanged (version check is `> 5` only). The additive-only policy exists
because *already-burned* tier-1 binaries can never be fixed; only the writer side can be
disciplined. No schema change in this plan.

## Tests & gates

1. `recovery/tests/test_catalog.c` — new case (runs under existing `make -C recovery test`):
   build a synthetic `schema_version = 6` catalog with `packs.sha256` renamed to
   `content_hash`; assert open succeeds, the newer-schema warning is emitted exactly once on
   stderr, and `lcsas_catalog_find_pack` returns `-1` (error) — distinguishable from `1`
   (miss) on a healthy v5 fixture. Per the verifier's refinement: do NOT assert
   "falls back to directory scanning" (the locator scans regardless); assert instead that the
   misleading "(catalog has no record …)" message is replaced by the explicit newer-schema
   text in the prompt output (extend the disc_locator prompt test or capture via the existing
   test harness's stderr).
2. `tests/unit/test_schema_v5_columns_frozen.py` — always-on (`make test-unit`, CI): create an
   in-memory catalog from `db/schema.py`, then for each (table, column) in the frozen-surface
   list assert presence via `PRAGMA table_info`, and assert `volumes.status` CHECK still
   includes `'DESTROYED'`. Fails any future migration that would break tier-1 SQL.
3. Optional cross-language pin: the frozen list in test #2 lives in one place and a comment in
   `catalog.c` points at it, so drift is caught on the Python side where CI always runs.

## Acceptance criteria

- [ ] On a v6/renamed-column catalog: restore proceeds, one newer-schema warning on stderr,
      disc prompt shows the "newer LCSAS / use newest META disc" text, not "no record".
- [ ] On a v5 catalog: behavior byte-identical to today (existing test_catalog cases green).
- [ ] `make -C recovery test` includes the v6 case; C coverage gate unaffected.
- [ ] Deleting `packs.sha256` from schema.py makes `test_schema_v5_columns_frozen.py` fail.

## Dependencies & related plans

- FMA: "Schema migrations never executed by production code" — that plan owns the writer-side
  migration machinery; this plan's additive-only policy must be referenced there before any
  v6 migration is authored.
- FMA: "Catalog rebuild resurrects DEPRECATED/DESTROYED volumes" — touches the same
  status-filter semantics tier-1 relies on.
- RST: "interactive restore prints SHA-256 hashes instead of disc labels" — same heir prompt
  in disc_locator.c; coordinate the message edits to avoid merge churn.

## Effort

**2 focused days**: 1d C changes + C test (incl. cross-arch bin regen via `make
keyshare-arches`-style flow + qemu spot-check), 0.5d Python frozen-surface test, 0.5d
schema.py policy text + prompt-message coordination.

---
**Implemented:** 2026-06-13. As planned: catalog.c tri-states find_pack (0/1/-1) + caches schema_version + one-time skew warning (>v5); disc_locator.c three-way prompt branch; volumes_for_pack warns on prepare error. schema.py gains TIER1_FROZEN_SURFACE block + test_schema_v5_columns_frozen.py (always-on). test_catalog.c v6/renamed-column case (stderr-capture, warn-once, returns -1). All 5 committed lcsas-restore bins rebuilt via zig cc; qemu/wine hardening green.
