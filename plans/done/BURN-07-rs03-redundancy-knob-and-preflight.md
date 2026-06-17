# BURN-07: RS03 ignores the redundancy knob; pre-flight must budget full-medium padding

> **STATUS: RESOLVED** — landed in `67235bf` (ecc+burn: drop placebo RS03 -n; pre-flight budgets padded-medium size [BURN-07]); guarded by `tests/integration/test_ecc_repair.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Drop placebo -n for RS03 augment; budget padded-medium size in pre-flight

## Problem

`augment_iso` passes `-mRS03 -n <pct> -c`, but per the dvdisaster manual (verified
on this host): for RS03 augmented images "Setting the redundancy is not possible
due to constraints in the format. The codec will automatically choose the size of
the smallest fitting medium." Two consequences: (1) `default_ecc_redundancy_pct`
(documented, range-validated, default 15) is a placebo — a user configuring 30%
for extra protection gets whatever padding the medium leaves, with no warning.
(The wrapper also omits the `%` suffix, so even in ECC-file mode dvdisaster would
read `-n 15` as 15 *roots*, not 15%.) (2) RS03 pads every image up to the smallest
fitting medium (a ~1 GB ISO grows to ~4.7 GB DVD size; the project's own ECC test
notes a small ISO padding to ≈700 MB CD size), so the `stage()` disk-space
pre-flight — `1.05 * (1 + ecc/100) + 1` — underestimates real staging needs several-fold
for small sessions, producing mid-pipeline ENOSPC in `augment_iso` *after* volumes
are committed, feeding BURN-03's orphaned-volume path.

This also props up a false durability belief (the "configured 15%" appears in docs
as ~30% capacity claims — see the FMT counterpart finding).

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/ecc/dvdisaster.py:74-80` — `cmd = [self._binary, "-i", str(tmp),
  "-mRS03", "-n", str(redundancy_pct), "-c"]`.
- `man dvdisaster` (this host): RS03 images — redundancy not settable; smallest
  fitting medium chosen automatically.
- `tests/integration/test_ecc_repair.py:15-17` — "RS03 augmented-image mode pads a
  small image up to a full optical medium (≈700 MB here)".
- `src/lcsas/burn/orchestrator.py:567-582` — pre-flight `overhead_factor = 1.05 *
  (1 + ecc_pct / 100) + 1`; no medium-padding term.
- `src/lcsas/config/settings.py:32` (knob, default 15) and `:315-318` (0–100
  range validation as if effective).
- Verifier correction (adopted): padding targets the smallest fitting **dvdisaster
  medium** for the image size (CD→DVD→DVD9→BD25→BD50→BDXL100 ladder), not the
  configured target medium — the audit's "~25 GB per volume" overstated; the
  underestimate is still real (~2.2x budgeted vs ~5-6x actual for small sessions).

## Fix design

1. **Stop passing `-n` in augmented mode** (`ecc/dvdisaster.py::augment_iso`):
   drop the two argv elements; keep the `redundancy_pct` parameter for signature
   stability but mark it deprecated in the docstring ("ignored: RS03 augmented
   images pad to the smallest fitting medium"). Log at INFO after augment:
   `"RS03 ECC: image padded to <size> (~<pct>% effective redundancy)"` computed
   from before/after `st_size`.
2. **Deprecate the config knob honestly** (`config/settings.py`): keep
   `default_ecc_redundancy_pct` parsing (existing TOMLs must not break) but
   `validate_config` emits a WARNING when it is set to a non-default value:
   `"default_ecc_redundancy_pct has no effect on RS03 augmented images; effective
   redundancy is the padding of the smallest fitting medium"`. Update
   `docs/architecture.md` / config docs accordingly (coordinates with the FMT
   ECC-capacity-claims plan).
3. **Fix the pre-flight** (`burn/orchestrator.py:567-582`): new helper
   `src/lcsas/ecc/dvdisaster.py::smallest_fitting_medium_bytes(image_bytes) -> int`
   with the RS03 ladder constants (CD 737 280 000; DVD 4 700 372 992; DVD9
   8 543 666 176; BD25 25 025 314 816; BD50 50 050 629 632; BDXL100
   100 103 356 416 — verify against dvdisaster source/manual when implementing).
   Required staging bytes per volume ≈ `data_bytes (staging tree) +
   padded_iso_bytes`; plus one extra `max(padded_iso)` for `augment_iso`'s temp
   copy (it duplicates the ISO during augmentation, dvdisaster.py:71-73). Replace
   the flat `overhead_factor` with this sum over `volume_plans` (per-volume
   estimated ISO size ≈ `vol_bytes * 1.05 + metadata_reserve_bytes`). Note ISOs
   now persist across the whole session (BURN-06), so sum — don't max — the
   padded sizes.
4. Edge case: `TEST_TINY` (0% ECC overhead) skips augmentation entirely
   (orchestrator.py:452) — the pre-flight must not add padding for media with
   `ecc_overhead_pct == 0`.

No schema change; no migration.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_dvdisaster.py` — update `test_augment_*` (line ~25 already
  pins `-n` IN the argv; flip it to assert `-n` is **absent**, with a comment
  documenting the RS03 manual semantics so re-adding it is a deliberate act).
  Per the verifier's refined gates, do not duplicate the command-pinning test —
  amend the existing one.
- `tests/unit/test_dvdisaster.py::test_smallest_fitting_medium_ladder` — boundary
  cases: 1 byte→CD, CD+1→DVD, DVD9+1→BD25, >BDXL100 raises ValueError.
- `tests/unit/test_session_pipeline.py::test_stage_preflight_budgets_padded_medium`
  — small session on BD25 config with `shutil.disk_usage` monkeypatched to report
  free space above the old formula but below `data + padded_DVD + temp copy`;
  assert the pre-flight `OSError` fires (per refined gate: budget is the
  smallest-fitting-medium for the ISO size, not BD25 capacity).
- Opt-in integration (`LCSAS_ECC_REPAIR=1`, per project memory these are slow):
  extend `tests/integration/test_ecc_repair.py` to assert the augmented size
  equals a ladder value and to log the effective redundancy.

## Acceptance criteria

- [ ] `dvdisaster` argv contains no `-n` for augmented images; effective
      redundancy is logged per volume.
- [ ] Setting `default_ecc_redundancy_pct = 30` produces a load-time warning, not
      silent placebo behavior.
- [ ] A 1 GB session on BD25 with 5 GB free staging fails the pre-flight before
      any catalog write, instead of ENOSPC inside dvdisaster.
- [ ] Opt-in ECC integration test still passes with the `-n`-less command.

## Dependencies & related plans

- **FMT** "ECC capacity claims internally inconsistent (~30% vs 15%)" — owns the
  doc-side truth; share the effective-redundancy wording.
- **FMT** "RS03 repair in-housing" (P1) — if repair moves in-house, the ladder
  constants live next to that decoder; keep `smallest_fitting_medium_bytes` in
  the ecc package so both use it.
- **BURN-03** — ENOSPC mid-augment currently orphans volumes; its compensation
  makes this failure recoverable, this plan makes it rare.

## Effort

1.5 days: 0.5 impl, 0.5 unit tests, 0.5 opt-in integration run + doc updates
(ECC integration passes are minutes-long; run serially per project memory).

---
**Implemented:** 2026-06-12. As planned, with one empirical correction:
the opt-in `LCSAS_ECC_REPAIR=1` run showed dvdisaster rounds the augmented
image down to whole RS03 layers (255-sector multiples — a CD-sized pad is
735,836,160 bytes, not the nominal 737,280,000), so the integration gate
asserts `≤ ladder value and within 2%` rather than exact equality;
`smallest_fitting_medium_bytes` is documented as the safe upper bound the
pre-flight budgets with. Ladder constants verified against `man dvdisaster`
/ manual.pdf on this host (DVD = 2,295,104 sectors; BD trio matches
`MediaType` capacities). Adjacent (not fixed here, FMT owns doc truth):
`staging/metadata.py:584` still writes "ECC redundancy: <pct>%" into the
on-disc CONFIG_SUMMARY.
