# UX-06: restore.sh --help QUICK START points at "ANY data disc", which never carries restore.sh

**Priority:** P2 · **Severity:** medium · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix restore.sh --help QUICK START to start from the META disc

## Problem

The first help text a confused operator or phone-a-friend helper reads
(`restore.sh --help`) says: "1. Insert ANY data disc into your drive. 2. Mount it.
3. Run: sh /mnt/restore.sh ~/restored/ latest". Data discs do not carry restore.sh —
the staging injector writes only standalone_restorer.py, the txt docs, catalog.db,
metadata/ and data/ — and RECOVER.txt explicitly states the META disc "is the only
disc that contains the recovery binaries ... and restore.sh". Following the QUICK
START verbatim yields `sh: can't open /mnt/restore.sh`.

The error is recoverable (clear message; RECOVER.txt corrects the routing), so this
is friction rather than a dead end — but it is a contradiction inside the primary
driver's own documentation, likely a leftover from the legacy bash driver's
bootstrap-from-data-disc flow, and it costs the heir confidence at the moment they
asked for help.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/scripts/restore.sh:273-277` — the `--help` heredoc:
  ```
  QUICK START:
    1. Insert ANY data disc into your drive.
    2. Mount it (typically: sudo mount /dev/sr0 /mnt).
    3. Run: sh /mnt/restore.sh ~/restored/ latest
  ```
- `recovery/docs/RECOVER.txt:252-254` — "Always start with the META disc — the one
  labelled LCSAS_META.  It is the only disc that contains the recovery binaries
  (bin/<arch>/), the catalog, and restore.sh."
- `src/lcsas/staging/metadata.py` (data-disc payload writers, ~189-310) and
  `src/lcsas/staging/builder.py` — no restore.sh staged onto data discs.
- `tests/recovery_hardening/test_restore_sh_ux.py` asserts only that a QUICK START
  heading exists, not that its content is correct (and the hardening tier is not
  CI-run).

## Fix design

Edit the heredoc at `recovery/scripts/restore.sh:273-277`:

```
QUICK START:
  1. Insert the disc labelled LCSAS_META into your drive.
  2. Mount it (typically: sudo mount /dev/sr0 /mnt).
  3. Run: sh /mnt/restore.sh ~/restored/ latest
  4. Answer the prompts (repository, password).
  5. When asked to swap discs, do so and press Enter.
```

Steps 3-5 stay; only step 1 changes. Alternative considered — staging restore.sh
onto every data disc to make the current text true — rejected: it adds a second
copy of a 40 KB script that drifts independently of the meta volume, and RECOVER.txt
already canonicalizes "start with META". One source of truth wins.

No catalog/schema impact; discs already burned carry the old help text but
restore.sh's repo discovery tolerates being started from any layout regardless.

## Tests & gates

- `tests/unit/test_doc_command_contract.py::test_restore_sh_help_starts_from_meta` —
  read `recovery/scripts/restore.sh`, extract the `--help` heredoc, assert it
  contains `LCSAS_META` and does NOT contain `ANY data disc`. Always-on
  (`make test-unit` / CI test.yml) — per the verifier's note, mirroring this in
  `tests/recovery_hardening/test_disc_swap_docs.py` alone would never gate merges.
- Optionally extend `tests/recovery_hardening/test_restore_sh_ux.py` with the same
  assertion for the local hardening run (cheap, keeps that suite self-consistent).

## Acceptance criteria

- [ ] `sh recovery/scripts/restore.sh --help | grep -c 'ANY data disc'` → 0;
      `... | grep -c 'LCSAS_META'` → ≥1.
- [ ] `pytest tests/unit/test_doc_command_contract.py -v` passes and fails if the old
      phrasing returns.
- [ ] RECOVER.txt and --help now agree on the starting disc.

## Dependencies & related plans

- **UX-02** — same contract-test file; land after its skeleton (or carry the one
  assertion independently — no hard ordering).
- **UX-05** — START_HERE commands must match this help text's mount idiom.

## Effort

0.25 days (one heredoc line + one test). No special environment.

---
**Implemented:** 2026-06-11. As planned: heredoc step 1 → LCSAS_META; always-on unit gate + hardening-suite assertion. Also refreshed the stale restore.sh line in recovery/MANIFEST.sha256 (left stale by KEY-02).
