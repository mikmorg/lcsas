# RST-01: Multi-disc `lcsas restore standalone` falsely fails after a complete restore cache

> **STATUS: RESOLVED** — landed in `04b68a3` (cli: prune recovered packs against cache before failing restore [RST-01]); guarded by `tests/unit/test_restore_from_disc.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix false PackCorruptionError on multi-disc restore standalone

## Problem

`lcsas restore standalone` (cmd_restore_from_disc — the disc-only restore path that needs no
central server, only a mounted disc and its holographic catalog) deterministically aborts with
"N packs could not be recovered from any disc" for **any snapshot that spans more than one
disc**, even when every pack was successfully ingested and the restore cache is complete and
hash-verified. The data is fine; the bookkeeping lies.

Mechanism: the initial-disc ingest is handed ALL required pack hashes with
`collect_failures=True`. `RestoreExecutor.ingest_volume` records every pack that lives on a
*later* disc as a failure ("not found on this volume"), so `all_failed` is seeded with every
off-initial-disc pack. The per-volume loop then ingests those packs successfully, but nothing
ever prunes `all_failed`. The alternates retry only removes packs that have catalog alternates;
in the common single-copy layout (each pack on exactly one volume, `alternates_map` empty) every
off-initial-disc pack survives to `still_failed`, and `PackCorruptionError` is raised — *after*
the cache is complete. `main()` catches it and prints "Unexpected error: N packs could not be
recovered from any disc", exit 1. Both `--volume-dir` batch mode and interactive mode are
affected. An operator (or a technically-assisted heir) restoring via this documented Workflow-C
path will conclude the discs are damaged and the data is gone. The audit reproduced this
end-to-end with a real `RestoreExecutor` and a real 2-pack/2-volume catalog.

This was downgraded from critical only because the heir's primary START_HERE journey is
`recovery/scripts/restore.sh` (tiers 1/2/3), which is unaffected; this path requires installed
lcsas + rustic.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/cli/main.py:2782-2788` — batch mode seeds the poison:
  ```python
  result = executor.ingest_volume(
      cache_dir, disc_path, plan.required_pack_hashes,
      verify=not args.skip_verify, collect_failures=True,
  )
  ...
  all_failed.extend(result.failed)
  ```
- `src/lcsas/cli/main.py:2810-2820` — `if all_failed:` → `_retry_from_alternates_batch` →
  `raise PackCorruptionError(f"{len(still_failed)} packs could not be recovered from any disc...")`.
- `src/lcsas/cli/main.py:2834-2839` and `2886-2896` — interactive path, identical pattern
  (initial ingest with all hashes at 2834, `all_failed.extend` at 2839, raise at 2893-2896).
- `src/lcsas/restore/executor.py:307-315` — "Pack not found on this volume" →
  `failed.append(sha256)` whenever `collect_failures=True`.
- `src/lcsas/cli/main.py:2944-2999` — `_retry_from_alternates_batch` starts from
  `remaining = list(failed_packs)` and only removes packs recovered from alternates; it never
  consults the cache. Same for `_retry_from_alternates_interactive` (3002+).
- Tests mask the bug by mocking: `tests/unit/test_restore_from_disc.py:279` returns
  `IngestionResult(len(pack_hashes), [])` and `:328` returns `IngestionResult(0, [])`, so
  `result.failed` is never realistically populated.
- The completeness check that would tell the truth already exists and runs *after* the raise:
  `RestoreExecutor.verify_cache_completeness` at `cli/main.py:2900`.

## Fix design

The cache is the source of truth: a pack that is present and hash-valid in the cache is
recovered, no matter which volume supplied it. Prune `all_failed` against the cache before any
retry/raise. Chosen over "don't seed all_failed from the initial ingest" because pruning also
heals the case where a pack fails verification on volume A but is later ingested fine from
volume B inside the same loop.

In `src/lcsas/cli/main.py`, add one helper near `_retry_from_alternates_batch`:

```python
def _prune_recovered(cache_dir: Path, failed: list[str]) -> list[str]:
    """Drop packs already present + valid in the cache (deduped)."""
    if not failed:
        return []
    unique = list(dict.fromkeys(failed))
    return RestoreExecutor.verify_cache_completeness(cache_dir, unique)
```

Apply at four points in `cmd_restore_from_disc`:

1. Batch (`:2810`): `all_failed = _prune_recovered(cache_dir, all_failed)` before the
   `if all_failed:` retry, and `still_failed = _prune_recovered(cache_dir, still_failed)` after
   `_retry_from_alternates_batch` before raising.
2. Interactive (`:2886`): same two prunes around `_retry_from_alternates_interactive`.

When raising, name discs not hashes: build a `sha → volume label(s)` map from
`pick_list.volumes` and print, e.g.:
`"3 pack(s) could not be recovered. They live on disc(s): VOL_002, VOL_007. Re-mount those discs (check for damage) and retry."`
(The full label-mapping UX is RST-07's scope; the minimal label list here must land with this fix
so the raise is actionable.)

Apply the same `_prune_recovered` guards to the parallel raises in `cmd_restore_exec`
(`cli/main.py:2372-2377` and `2434-2439`) — its failures are genuine today, but pruning makes the
invariant uniform: *never fail a restore whose cache is complete*.

No catalog/schema change; purely CLI control-flow. Old burned discs are unaffected (this code
runs on the operator's machine, not from the disc).

## Tests & gates

All always-on unit tests (`make test-unit`, already in `.github/workflows/test.yml`):

- `tests/unit/test_restore_from_disc.py::test_batch_multidisc_single_copy_returns_0` — real
  `RestoreExecutor` (no `MagicMock` for the executor), catalog with pack A on VOL_001 (the
  initial `--disc`) and pack B only on VOL_002 under `--volume-dir`, no alternates. Assert exit
  code 0 and that the (mocked) rustic `execute_restore`/runner is invoked. This is the audit's
  ready-made repro (`/var/tmp/audit_repro/repro_from_disc.py`) turned into a test.
- `tests/unit/test_restore_from_disc.py::test_interactive_multidisc_no_spurious_failure` —
  monkeypatched `input()` supplying the second disc's mount path; assert exit 0 and that
  `_retry_from_alternates_interactive` produces no prompt when the cache is complete.
- `tests/unit/test_restore_from_disc.py::test_genuinely_missing_pack_error_names_disc_labels` —
  pack C on no mounted volume; assert the error output contains the catalog volume label, not
  only the SHA-256.
- Refactor the two masking mocks (`:279`, `:328`) to return realistic
  `IngestionResult(n, failed=[...])` shapes so future regressions surface.
- Opt-in e2e: extend `tests/e2e/` with a non-agent test driving
  `lcsas restore standalone --volume-dir` against a real 2-volume staged archive (the existing
  cdemu blind-restore gates exercise `recovery/scripts/restore.sh`, not this CLI path). Wire as
  `make test-e2e` (local-only, like the rest of e2e).

## Acceptance criteria

- [ ] The audit repro scenario (2 packs, 2 volumes, no alternates, `--volume-dir`) exits 0 and
      runs the restore.
- [ ] Interactive mode with all discs supplied exits 0 with no alternates prompt.
- [ ] A genuinely missing pack still fails (exit 1 / PackCorruptionError) and the message names
      the volume label(s) holding it.
- [ ] `pytest tests/unit/test_restore_from_disc.py -v` passes; no test mocks
      `ingest_volume` to return an empty `failed` list for packs absent from the given volume.
- [ ] `make test-unit && make lint && make typecheck` pass.

## Dependencies & related plans

- RST-07 (interactive 'skip' discards alternates; hash-only errors) — shares the label-mapping
  helper; land RST-01 first, RST-07 extends the same error-reporting code.
- Independent of the GATE-prefix CI wiring plans; tests here ride the always-on unit lane.

## Effort

1.5 days (0.5 impl, 1 test — converting the repro into non-mocked unit tests and de-masking the
existing mocks is most of the work). No special environment.

---
**Implemented:** 2026-06-13. As planned: `_prune_recovered` cache-truth guard + `_discs_for_packs` label helper applied at all four `cmd_restore_from_disc` raise points and both `cmd_restore_exec` raises; genuine-failure messages now name volume labels. De-masked the empty-`failed` mocks; added real-executor multidisc (batch+interactive) + label-naming tests (proved red without the prune). Deviation: opt-in `make test-e2e` driver deferred (needs a real 2-volume staged-archive harness); unit-lane acceptance gate met.
