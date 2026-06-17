# BURN-01: Fail loud when a repo mirror is missing at stage time

> **STATUS: RESOLVED** — landed in `9e2811b` (burn: fail loud when a repo mirror is missing at stage time [BURN-01]); guarded by `tests/unit/test_burn_orchestrator.py`.

**Priority:** P0 · **Severity:** critical · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Stage must fail loud when a repo mirror/data dir is unavailable

## Problem

When `lcsas stage` runs while a repo's NAS mirror is unmounted (or its `mirror_path`
has moved since registration), the orchestrator silently stages **zero packs** for
that repo and proceeds anyway: the volume is created, ALL selected packs are linked
in `volume_packs`, the catalog is injected, the ISO is mastered, ECC is applied, and
the disc is burned and marked VERIFIED. The disc physically lacks the packs, but the
catalog reports them archived forever — `get_unarchived_packs` only checks for a
`volume_packs` row, so no future stage will ever pick them up again.

The trigger is mundane: an NFS automount drops between `lcsas scan` and
`lcsas stage`, or one repo of several has a stale path. The owner sees green at
every step for years. The non-technical heir, decades later — when the hot mirror
no longer exists — inserts the disc the catalog points to and gets "pack not found"
for data that was never on any disc. This is the purest form of the catalog lying
about data being safe, and it must be fixed before the next real burn.

The size pre-flight does not catch it either: `_stage_single_volume` only rejects a
staging tree that is too **large** for the medium, never one that is too small
relative to the packs the catalog is about to claim.

## Evidence

All re-checked against current code (2026-06-10, master @ 7e65a38):

- `src/lcsas/burn/orchestrator.py:374-380` — the silent skip:
  ```python
  for repo_id, repo_packs in packs_by_repo.items():
      mirror_path = mirror_paths.get(repo_id)
      if mirror_path is None:
          continue
      data_dir = mirror_path / "data"
      if data_dir.is_dir():
          builder.stage_packs(repo_packs, data_dir)
  ```
  No `else` branch on either condition. `stage_packs` (the only place
  `MissingPacksError` is raised — `staging/builder.py:180-181`) is never called.
- `src/lcsas/burn/orchestrator.py:398-401` — `bulk_link_packs(self._conn,
  volume.volume_id, pack_ids, ...)` then `self._conn.commit()` for **all**
  `selected_packs`, regardless of how many were actually staged.
- `src/lcsas/db/queries.py:30-47` — `get_unarchived_packs`: archived == any
  `volume_packs` row exists; volume status irrelevant.
- `src/lcsas/staging/metadata.py:106-115` — `inject_metadata` also silently skips
  missing metadata dirs/files (`if src.is_dir(): copy_tree(...)`), so the same
  unmounted mirror also yields a disc with no rustic index/snapshots/keys for that
  repo, breaking the holographic self-describing property.
- `src/lcsas/config/settings.py:188-198` — only mitigation anywhere: a non-fatal
  config-load WARNING when a configured `mirror_path` doesn't exist. The pipeline
  proceeds. (Note `_get_mirror_paths` at orchestrator.py:482-492 reads mirror paths
  from the **DB** `repositories` table, so the config warning can miss the actual
  path used.)
- Test gap: `tests/unit/test_staging.py` and `tests/unit/test_filesystem_failures.py`
  exercise `MissingPacksError` only via direct `StagingBuilder.stage_packs` calls;
  no test drives the orchestrator with an absent repo `data/` dir.

## Fix design

All changes in `src/lcsas/burn/orchestrator.py::_stage_single_volume` (and one new
exception). Everything below happens **before** `create_volume` (line 387), so no DB
write occurs on the failure path — no compensation or rollback logic is needed.

1. **New exception** in `src/lcsas/staging/builder.py`:
   ```python
   class MirrorUnavailableError(MissingPacksError):
       """A repo's mirror (or its data/ dir) is absent while packs from it
       were selected for staging."""
       def __init__(self, repo_id: str, mirror_path: Path | None, packs: list[str]) -> None:
   ```
   Subclassing `MissingPacksError` keeps existing `except MissingPacksError`
   handlers working. Message wording (user-facing, shown by the CLI):
   `"Mirror for repo '<repo_id>' is unavailable (<path or 'no mirror path
   registered'>): <N> selected pack(s) cannot be staged. Is the NAS mounted?
   Nothing was written to the catalog; fix the mirror and re-run 'lcsas stage'."`

2. **Replace the silent-skip loop** (orchestrator.py:374-380):
   ```python
   for repo_id, repo_packs in packs_by_repo.items():
       mirror_path = mirror_paths.get(repo_id)
       data_dir = mirror_path / "data" if mirror_path else None
       if mirror_path is None or data_dir is None or not data_dir.is_dir():
           raise MirrorUnavailableError(
               repo_id, mirror_path, [p.sha256[:12] for p in repo_packs])
       builder.stage_packs(repo_packs, data_dir)
   ```

3. **Post-stage invariant** (defense in depth — catches any future partial-stage
   regression, e.g. a `stage_packs` variant that miscounts). New method
   `StagingBuilder.assert_staged_complete(packs: list[Pack]) -> None` that walks
   `self._data_dir`, counts files matching 64-hex names and sums `st_size`, and
   raises `MissingPacksError` if count != `len(packs)` or sum != `sum(p.size_bytes)`.
   Call it in `_stage_single_volume` immediately after the staging loop, before
   `inject_metadata`/`create_volume`. Cost: one `os.walk` of hardlinks, no reads.

4. **Make `inject_metadata` fail loud for repos with packs on this volume.** Change
   `_stage_single_volume` to pass only the mirrors of repos present in
   `packs_by_repo` plus a `required=True` semantic: in
   `staging/metadata.py::inject_metadata`, if a repo's `mirror_root` lacks **all**
   of `config`/`keys` (the minimum for the disc to be self-describing), raise
   `MirrorUnavailableError`. Missing `index/` or `snapshots/` stays a WARNING
   (legitimately absent in some repo states is not — but keep the strict check to
   config+keys to avoid false positives; tighten later if needed).

5. **Edge cases:**
   - Repos in the DB with packs selected but no `repositories` row → already
     impossible (`packs.repo_id` FK), but `mirror_paths.get` returning `None` is
     handled by the same raise.
   - The legacy `"default"` fallback mirror (orchestrator.py:489-490) gets the same
     treatment — no special-casing.
   - Multi-volume sessions: a failure on volume *i* leaves volumes 1..i-1 committed
     with their packs genuinely staged — that is correct and burnable; the raised
     error aborts the session loop in `stage()` (see BURN-03 for the compensation
     path of the *failing* volume — here nothing was committed for it yet).

No schema or catalog-semantics change; no migration needed.

## Tests & gates

All always-on pure-unit tests (no external binaries), run by `make test-unit` /
`.github/workflows/test.yml`:

- `tests/unit/test_burn_orchestrator.py::test_stage_raises_when_repo_data_dir_missing`
  — register 2 repos, seed packs for both, `shutil.rmtree` one repo's `data/` after
  seeding the catalog, call `orch.stage()`; assert `MirrorUnavailableError` is
  raised, **and** that no volume row exists and `get_unarchived_packs()` still
  returns every pack (nothing committed).
- `tests/unit/test_burn_orchestrator.py::test_stage_raises_when_mirror_path_unregistered`
  — repo whose `mirror_paths` entry is absent (DB repo row with a path that does
  not exist); same assertions.
- `tests/unit/test_burn_orchestrator.py::test_stage_asserts_staged_count_equals_selected`
  — monkeypatch `StagingBuilder.stage_packs` to silently stage N-1 packs without
  raising; assert the post-stage invariant fails loud before `create_volume`
  (no volume row committed).
- `tests/unit/test_staging.py::test_inject_metadata_raises_when_repo_config_missing`
  — mirror root without `config`/`keys`; assert raise.
- Update any existing test that relied on the silent skip (grep
  `test_session_pipeline.py` / `test_multi_tenant.py` for multi-repo fixtures whose
  mirrors are partially built).

The verifier confirmed no `already_covered_by` — these are all new.

## Acceptance criteria

- [ ] `lcsas stage` with one repo's mirror unmounted exits non-zero with the
      `MirrorUnavailableError` message naming the repo and path; `lcsas status`
      afterwards shows the same unarchived pack count as before.
- [ ] A volume row is never created when any selected pack could not be staged
      (verified by the three new orchestrator tests).
- [ ] `StagingBuilder.assert_staged_complete` exists and is called before
      `create_volume`; mutating it out (`continue` restored) fails
      `test_stage_asserts_staged_count_equals_selected`.
- [ ] `make test-unit` green; new tests run in CI on every PR.

## Dependencies & related plans

- **BURN-02** (hash-verify staged pack content) — touches the same
  `stage_packs` loop; land BURN-01 first (smaller, unblocks the next burn), then
  BURN-02 extends the same code path.
- **BURN-03** (reclaim packs from failed staging) — owns compensation for failures
  *after* the commit point; BURN-01 deliberately keeps its failures before it.
- **FMA** "packs linked to never-burned STAGING volume counted archived" — the
  state-machine counterpart; this plan does not change the meaning of "archived".

## Effort

1.5 days: 0.5 impl (exception + loop + invariant + metadata strictness),
1.0 tests + fixing any multi-repo fixtures that depended on the silent skip.
No special environment.

---
**Implemented:** 2026-06-11. As planned, with two small extensions: `MirrorUnavailableError` gained an optional `detail` kwarg so the metadata-injection raise has an accurate message, and `inject_metadata` strictness landed as a `required_repos` param (all mirrors still injected; strict only for repos with packs on the volume, preserving holographic breadth). One pre-existing fixture (`test_prepare_with_repo_filter`) relied on the silent skip and was given a minimal config+keys mirror, as the plan anticipated.
