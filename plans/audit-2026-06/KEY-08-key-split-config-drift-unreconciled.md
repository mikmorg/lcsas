# KEY-08: key_split/K/N on-disc instructions are self-reported config, never reconciled

**Priority:** P2 · **Severity:** medium · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Record split state in catalog; fail burn on key_split drift

## Problem

Whether discs print share instructions — and the K/N they claim — comes
solely from hand-edited `lcsas.toml` fields (`key_split`, `key_threshold`,
`key_shares`). `lcsas key split` accepts `--threshold/--shares` overrides but
persists nothing and prints no reminder to set `key_split = true`; the burn
pipeline never checks consistency. Three concrete heir-facing failure modes:
owner splits but forgets `key_split=true` → every disc tells the heir to find
a single key that may no longer exist anywhere; owner splits 3-of-5 via flags
while config says 2-of-5 → discs say "any 2 cards" and the combine fails after
the heir gathered exactly 2 (the card shows the true K, but the heir must
notice the contradiction unaided); `key_split=true` with no split ever
performed → the heir hunts for nonexistent cards. The discs are burned once
and read decades later — wrong instructions are permanent. Root cause: the
split event has no durable record; disc text is rendered from free-floating
config at stage time.

## Evidence

Re-checked 2026-06-10 against master:

- `src/lcsas/config/settings.py:44-52` — free defaults (`key_threshold=2`,
  `key_shares=5`, `key_split=False`), no linkage; populated at `:246-248`.
- `src/lcsas/cli/main.py:3219-3230` — flags override config, nothing written
  back; `:3281-3293` — success output has no `key_split=true` reminder.
- `src/lcsas/staging/metadata.py:48-49` and `:349-351` — KEY_INFO/START_HERE
  K/N rendered from config values only. `grep key_split src/lcsas/burn/
  src/lcsas/db/` → nothing: no consistency check, no recorded state.
- `docs/ESTATE_PLANNING.md:73-79` — `key_split` is a separate manual step.
- `src/lcsas/db/schema.py:7` — `CURRENT_SCHEMA_VERSION = 6` (root CLAUDE.md
  says v5; code is at 6).

## Fix design

Record split state durably at split time; derive disc text from the record;
fail the burn on drift.

1. **Schema v7 (additive table — avoids the table-recreate atomicity problem
   flagged by the failure-modes dimension):**
   ```sql
   CREATE TABLE IF NOT EXISTS key_escrow (
       repo_id    TEXT PRIMARY KEY,
       threshold  INTEGER NOT NULL,
       shares     INTEGER NOT NULL,
       slip39_id  INTEGER NOT NULL,   -- SLIP-0039 identifier of the split
       split_at   DATETIME NOT NULL
   );
   ```
   New `src/lcsas/db/key_escrow.py` with `record_split`, `get_split`,
   `clear_split`. Bump `CURRENT_SCHEMA_VERSION` to 7; migration is a bare
   CREATE TABLE (no data movement).
2. **`cmd_key_split`** — when `--config` is given, after verification
   (KEY-03) record `(repo, K, N, slip39_id, now)` into the catalog at
   `config.db_path` (replace-on-conflict = rotation). Always print the
   next-step reminder:
   `"NEXT STEP: set key_split = true (and key_threshold = K, key_shares = N)
   under [defaults] in lcsas.toml so burned discs print share instructions."`
   Without `--config`: loud warning that the split was NOT recorded and burn
   drift-checking is unavailable.
3. **Stage/burn drift check** — at staging-metadata time (where the text is
   rendered: `HolographicInjector.write_key_info` / `write_start_here`), and
   surfaced through the burn orchestrator's preflight: compare
   `config.key_split/K/N` against `get_split(repo)`:
   - config says split, no record (or K/N mismatch) → **abort** with:
     `"key escrow drift: lcsas.toml says key_split=true 2-of-5 but the
     catalog records 3-of-5 split on 2026-06-01 (or: no split). Discs would
     print wrong instructions. Re-run 'lcsas key split' or fix [defaults]."`
   - record exists, config says `key_split=false` → abort symmetrically
     (heir would be told to find a single key that was superseded).
   - K/N rendered into KEY_INFO/START_HERE come from the **record** when
     present (`test_kn_comes_from_recorded_split_not_config`).
   Escape hatch `--allow-escrow-drift` on the stage/burn commands for
   multi-config edge cases, logged as a volume event.
4. **Migration/compat note (schema v6 → v7).** Additive only: old catalogs
   gain an empty table via `migrate()`. Burned discs carry v≤6 holographic
   catalogs forever — restore-side code (tier-1 C reader, tier-3
   `standalone_restorer`, `db/rebuild.py`) must not require `key_escrow`:
   tier-1/tier-3 never read it (they use volumes/packs/volume_packs);
   `rebuild.py` must treat the table as optional when merging disc catalogs
   (guard with `sqlite_master` existence check). Note the failure-modes
   finding that `migrate()` is not invoked on production paths — this
   plan's check must call it (or tolerate the missing table with a clear
   "run lcsas migrate" error) rather than crash.

## Tests & gates

Always-on, `make test-unit` → `gate`:

- `tests/unit/test_cli_key.py::test_split_records_state` — split with
  `--threshold 3 --shares 4`; catalog row says 3/4; reminder printed.
- `tests/unit/test_staging_metadata.py::test_kn_comes_from_recorded_split_not_config`
  — record 3-of-5, config 2-of-5 + drift override → rendered text says
  "any 3".
- `tests/unit/test_burn_orchestrator.py::test_burn_fails_on_escrow_drift` —
  both directions (config-true/no-record; record/config-false) abort with the
  actionable message; `--allow-escrow-drift` proceeds + logs event.
- `tests/unit/test_db_key_escrow.py` — CRUD + v6→v7 migration on a v6
  fixture db; rebuild.py merge of a disc catalog lacking the table.

## Acceptance criteria

- [ ] Split with `--config` writes a key_escrow row; success output names the key_split next step.
- [ ] Burn/stage aborts on all three drift modes with messages naming both sides of the disagreement.
- [ ] Disc text K/N comes from the record when one exists.
- [ ] v6 catalog migrates additively; rebuild from old disc catalogs unaffected.

## Dependencies & related plans

- **KEY-03** — same command surgery (`cmd_key_split`); land KEY-03 first;
  store `slip39_id` via its `share_identifier` helper.
- **FMA** "Schema migrations are never executed by any production code path"
  — the v7 migration depends on that wiring; coordinate version-bump order.
  **FMA** "Catalog rebuild resurrects…" — rebuild.py merge of the new table.
- **BURN** preflight plans — drift check slots into the same preflight block
  (`orchestrator.py:233-292`).

## Effort

2 days: 0.75 schema/db module + migration, 0.75 split/stage/burn wiring,
0.5 tests.

---
**Implemented:** 2026-06-13. As planned, adapted to drift since the plan was
written: schema was already at v8, so the additive `key_escrow` table landed
as **v9** (v8→v9 migration). New `db/key_escrow.py` (record/get/clear + the
`detect_escrow_drift` helper + `EscrowDriftError`). `cmd_key_split` records the
split at `config.db_path` and prints the next-step reminder (loud warning
without `--config`). Drift preflight runs in `_stage_single_volume` (covers
both `prepare()` and `stage()`), surfaced through `cmd_stage`; disc K/N comes
from the record via `escrow_override` on `write_start_here`/`write_key_info`;
`--allow-escrow-drift` escape hatch logs a NOTE volume event. rebuild.py
already probes `sqlite_master` so v≤8 disc catalogs lacking the table merge
fine (test added). All plan tests present and passing; full `make test-unit`
green (1607 passed).
