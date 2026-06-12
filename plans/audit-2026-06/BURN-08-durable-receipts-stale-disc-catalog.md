# BURN-08: Durable burn receipts; document the self-stale on-disc catalog

**Priority:** P2 · **Severity:** medium · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** partial — MULTI_DISC_DESIGN.txt:327-329 acknowledges meta-volume catalog staleness; the burn-time write-ordering gap itself is untracked
**Suggested GH issue title:** Persist burn receipts durably; document final-session catalog staleness

## Problem

The holographic catalog is copied into staging *before* the ISO is mastered;
volume_copies rows, VERIFY events, VERIFIED statuses, and receipts are written to
the hot DB *after*. So every disc of session N carries a catalog in which session
N's own volumes are STAGING with zero copies at zero locations. A later session's
discs supersede it — but the **final** session is always self-stale. For an heir
working from discs alone, the on-disc catalog can never answer "where are the
other copies of the newest discs kept?": that information exists only on the hot
DB the disaster destroyed.

The burn receipts that capture exactly this (location, verify result, ISO hash per
volume) are written into the session staging directory — which `clean_session`
deletes — and `catalog import-receipts` repairs only the hot DB. The write
ordering is inherent (the disc must contain its own volume row, so the catalog
must be injected pre-master, and burn results cannot exist pre-burn); the fix is
to make the burn-time provenance durable *outside* the staging tree and to tell
the heir, on the disc, that this gap exists and where to look.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/burn/orchestrator.py:403-420` — `inject_catalog` at stage time;
  `:742-749` — volume_copies written at burn time, after every catalog of the
  session was already mastered.
- `orchestrator.py:805-809` — `_write_receipts(receipts, session_dir, location)`
  into `session_dir/receipts/` (965-999); `clean_session` (813-830) removes
  `staging_dir` — the receipts die with it.
- `src/lcsas/cli/main.py:3116` — `catalog import-receipts` repairs the hot DB
  only.
- `recovery/docs/MULTI_DISC_DESIGN.txt:327-329` — freshest-catalog selection,
  which cannot help when ALL discs of the final session predate their own burns.
- Restore pick-list location preference depends on catalog locations
  (`db/queries.py:160-173`).

## Fix design

Scope deliberately small (medium severity); the full "refresh disc / USB append"
idea stays a follow-up.

1. **Durable receipts** (`orchestrator.py::burn_session`): after
   `_write_receipts` into the session dir, write the same JSON files to a
   permanent location beside the catalog:
   `self._config.db_path.parent / "receipts" / "<session>_<label>_<location>.json"`.
   This survives `clean_session`, lives on the (backed-up) catalog volume, and is
   what `catalog import-receipts` globs after a hot-DB rebuild. Log the path in
   the burn summary: `"Receipt: <path> — print it and file it with the disc."`
2. **clean_session keeps receipts**: with (1) the durable copy exists, but also
   make `clean_session` skip `session_dir/receipts/` deletion explicitly is
   unnecessary — instead just document that staging receipts are duplicates.
   (No code change beyond (1); avoids special-casing tree removal.)
3. **Tell the heir on-disc**: extend `HolographicInjector.write_start_here` /
   `write_volume_info` (`staging/metadata.py`) with a generated line in
   START_HERE.txt: `"NOTE: this disc's catalog was written BEFORE this disc was
   burned. It cannot list where THIS batch's copies are stored. Check the printed
   receipt filed with the discs, or the catalog on any NEWER disc."` Coordinate
   wording with the UX heir-orientation plans so the docs-vs-reality gate covers
   it.
4. **Pre-write intended location**: `create_volume` already records
   `location=self._config.default_location` on the volume row
   (orchestrator.py:393), so the *first* intended location does reach the disc.
   Document this field's meaning ("intended first location, not a burn
   confirmation") in volume_info.json's writer rather than adding new fields.

No schema change; no migration. On-disc catalogs remain self-stale by
construction — the contract is now stated on the disc and the provenance is
durable on the operator side.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_session_pipeline.py::test_receipts_survive_clean_session` —
  stage + burn (skip_burn, mocked verify) + `clean_session(force as needed)`;
  assert receipt JSONs exist under `db_path.parent/receipts/` with correct
  location + verify fields.
- `tests/unit/test_holographic_catalog_freshness.py` (new) — stage a session,
  open `catalog.db` inside the staged tree, assert-and-document the staleness
  contract (own volume STAGING, volume_copies empty) **and** assert
  START_HERE.txt in the same tree contains the staleness NOTE — pinning that the
  gap is disclosed wherever it exists.
- `tests/unit/test_cli_handlers.py::test_import_receipts_from_durable_dir` —
  `catalog import-receipts` over `db_path.parent/receipts/*.json` repopulates
  copies on a fresh DB.
- E2e (with the GATE weekly job): extend `tests/e2e/cdemu_blind_restore/verify.sh`
  to assert the agent can state the disc inventory from the newest disc's catalog
  AND correctly reports that copy locations require the receipt/newer catalog.

## Acceptance criteria

- [ ] Burn receipts exist under `<db dir>/receipts/` after every burn and survive
      `stage --clean`.
- [ ] START_HERE.txt on every staged volume discloses the catalog-staleness
      contract.
- [ ] `catalog import-receipts <db dir>/receipts/*.json` restores copy/location
      rows onto a rebuilt hot DB.
- [ ] The freshness test documents (and will catch changes to) what the on-disc
      catalog claims about its own session.

## Dependencies & related plans

- **FMA** "burn provenance for the newest session exists only on the NAS catalog"
  (low) — same root; this plan is the implementation, FMA's rebuild-drops-audit
  finding consumes the durable receipts.
- **UX** heir-orientation / START_HERE plans — wording + docs-vs-reality gate.
- **BURN-03** — `clean_session` changes land there first; rebase.

## Effort

1 day: 0.4 impl, 0.6 tests. No special environment.

---
**Implemented:** 2026-06-12. As planned, with one e2e deviation: verify.sh gained a
deterministic fixture check (#16 — newest data disc's START_HERE.txt discloses the
staleness contract) instead of the agent-statement assertion, which would require
recalibrating the blind-drill prompt and cannot be validated outside the weekly gate.
