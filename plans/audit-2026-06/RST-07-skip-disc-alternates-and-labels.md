# RST-07: Interactive 'skip' bypasses alternates; missing-pack errors print hashes not labels

**Priority:** P2 · **Severity:** medium · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: adjacent only — recovery/docs/UX_CONCERNS.txt ID 005 (CLOSED, tier-1 prompt only); CODE_REVIEW_CLEANUP.md:71 unchecked. The CLI-path gap itself is untracked.
**Suggested GH issue title:** Route skipped discs into alternates retry; name volume labels in errors

## Problem

The planner builds `PickListV2` with per-pack alternate volumes precisely for the
lost/damaged-disc case — but the interactive restore loop throws that machinery away. In
`cmd_restore_exec`, answering `skip` to "Mount volume X" just breaks out of the prompt: the
skipped volume's packs are never added to `all_failed`, so `_retry_from_alternates_interactive`
never offers the alternate discs the catalog knows about. The restore then dies at the
completeness check printing up to 10 bare SHA-256 hashes and "mount the missing volumes and
retry" — no volume label, no mention that DISC_012 holds redundant copies of what was on the
lost DISC_007. A user who skipped a disc *because it is lost* is shown 64-char hex strings and
no actionable next step, even when redundancy exists.

The same hash-only final error appears in `cmd_restore_from_disc`, and `lcsas restore plan`
still uses the v1 pick list, so the printed plan never shows alternates either. UX_CONCERNS ID
005 fixed this exact class for the tier-1 C prompt; the LCSAS-CLI journey was never covered.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/cli/main.py:2395-2397` — `if mount_path.lower() == "skip": logger.info(f"  Skipping
  {label}"); break` — packs not enqueued anywhere.
- `src/lcsas/cli/main.py:2426-2433` — alternates retry fires only `if all_failed:`.
- `src/lcsas/cli/main.py:2448-2460` — completeness failure prints `f"  missing: {sha}"` ×10 and
  "mount the missing volumes and retry" with no labels.
- `src/lcsas/cli/main.py:2863-2865` (restore standalone interactive skip) and `:2901-2913`
  (hash-only completeness error) — same patterns in `cmd_restore_from_disc`.
- Alternates exist and are populated: `src/lcsas/restore/planner.py:95-136`
  (`generate_pick_list_v2`), `src/lcsas/db/queries.py:198-261`
  (`get_pick_list_with_alternates`, VERIFIED-before-BURNED ordering).
- `src/lcsas/cli/main.py:2121-2122` — `cmd_restore_plan` calls `planner.generate_pick_list(...)`
  (v1, no alternates column).

## Fix design

Three coordinated changes in `src/lcsas/cli/main.py`:

1. **Skip → alternates.** In both interactive loops (`:2395` and `:2863`): on `skip`, do
   `all_failed.extend(pack_hashes)` before `break` (pack_hashes is already in scope). The
   existing `_retry_from_alternates_interactive` then prompts for each alternate label. Improve
   its prompt to say *why*: "Disc 'DISC_007' was skipped/failed; 14 of its packs are also on
   'DISC_012'. Mount 'DISC_012' and enter its path (or 'skip'):". RST-01's cache-pruning
   guarantees skipped-then-recovered packs don't poison the final raise.

2. **Labels in final errors.** Add a helper used by both commands:
   ```python
   def _labels_for_packs(pick_list, hashes: list[str]) -> dict[str, list[str]]:
       """sha → [primary_label, *alternates] from PickListV2 sources."""
   ```
   Rewrite the completeness-failure block (`:2448-2460`, `:2901-2913`) to group missing packs by
   label: `"still need disc DISC_007 (14 packs) — alternates: DISC_012"`, listing hashes only at
   debug level. Mention deprecated labels via the existing `pick_list.deprecated_disc_labels`.

3. **Plan shows alternates.** Switch `cmd_restore_plan` (`:2121`) to `generate_pick_list_v2`
   and add an "also on:" column when any source has alternates. PickListV2 is structurally
   compatible (volumes dict of PackSource vs Pack — adjust the size summation accordingly).

No catalog/schema change; reads existing `volume_packs` rows, works against old holographic
catalogs unchanged (alternates simply come out empty for single-copy archives).

## Tests & gates

Always-on unit (`make test-unit`):

- `tests/unit/test_cli_restore.py::test_skip_primary_volume_triggers_alternate_prompt` — real
  executor, catalog with pack on VOL_A (alternate VOL_B); monkeypatched `input()` answers
  `skip` then VOL_B's path; assert restore completes from the alternate (exit 0).
- `tests/unit/test_cli_restore.py::test_final_missing_error_names_volume_labels` — skip
  everything; assert error output contains the volume label(s) and alternate label(s), and bare
  hashes are not the only identifier.
- `tests/unit/test_restore_from_disc.py` — mirror both tests for `cmd_restore_from_disc`'s
  interactive path.
- `tests/unit/test_restore.py::test_plan_output_shows_alternates` — `lcsas restore plan` output
  includes "also on:" when packs have multiple volumes; unchanged output when they don't.

## Acceptance criteria

- [ ] Skipping a disc whose packs have catalog alternates leads to an alternate prompt and a
      successful restore without re-running the command.
- [ ] Final missing-pack errors name discs (and alternates/deprecated labels), with hashes
      demoted to debug output.
- [ ] `lcsas restore plan` shows alternates for multi-copy packs.
- [ ] `make test-unit && make lint && make typecheck` pass.

## Dependencies & related plans

- **RST-01 first** — it establishes the cache-pruning invariant and the minimal label mapping in
  the raise path; this plan builds the full UX on top (shared `_labels_for_packs` helper).
- Complements UX-prefix journey plans (heir-facing docs of the lost-disc flow) and the
  FUP-03 disc-confidentiality follow-up (no overlap, just adjacent text).

## Effort

1.5 days: 0.5 impl, 1 tests (interactive-input fixtures for two commands). No special
environment.
