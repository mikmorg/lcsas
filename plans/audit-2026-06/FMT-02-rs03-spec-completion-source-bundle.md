# FMT-02: Complete the RS03 spec; bundle pinned dvdisaster source on the meta-volume

**Priority:** P1 · **Severity:** high · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** tracked (misleadingly): docs/SURVIVABILITY.md §2.5 "dvdisaster RS03 format docs" marked ✅ Done (lines 168, 318); the incompleteness of that resolution is untracked
**Suggested GH issue title:** Make DVDISASTER_RS03_FORMAT.md re-implementable; bundle dvdisaster source

## Problem

`docs/DVDISASTER_RS03_FORMAT.md` is the designated survivability artifact for the day the
dvdisaster binary no longer runs: a future engineer is supposed to re-implement RS03 repair
from it. The doc explicitly punts on the two things a re-implementer cannot guess: the exact
ECC header struct layout ("may vary between dvdisaster versions; consult the source code's
`rs03-common.h`") and the interleaving order ("requires reading the RS03 source code"). That
source exists only at a dormant GitHub fork URL and is NOT bundled on the meta-volume —
`_SOURCE_ITEMS` ships only LCSAS `src`, `_DOC_ITEMS` ships `docs/README/pyproject`. The doc
also recommends pip-installable libraries (`reedsolo`), exactly the dependency class the
recovery design forbids.

Contrast `docs/RESTIC_FORMAT_SPEC.md`: it was complete enough that the tier-1 C reader and the
tier-3 Python reader were independently implemented from it. The RS03 doc fails that same bar,
yet SURVIVABILITY.md §2.5 claims the abandonment risk is ✅ Resolved on the strength of it. A
2050 engineer holding a damaged disc and this doc still cannot decode the parity — the
load-bearing hedge is broken while tracked as fixed.

## Evidence

Re-checked against current files:

- `docs/DVDISASTER_RS03_FORMAT.md:109-111` — "The actual struct layout and byte order may vary
  between dvdisaster versions; consult the source code's `rs03-common.h` for the definitive
  field layout."
- `docs/DVDISASTER_RS03_FORMAT.md:207` — "Python: `reedsolo` library (pure Python,
  pip-installable)".
- `docs/DVDISASTER_RS03_FORMAT.md:212-213` — "The main challenge is matching dvdisaster's
  specific interleaving layout, which requires reading the RS03 source code."
- `src/lcsas/meta/builder.py:34-35` — `_SOURCE_ITEMS = ("src",)`,
  `_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")` — no dvdisaster source anywhere;
  `recovery/vendored/` holds only sqlite + zstd.
- `docs/SURVIVABILITY.md:168-182` and `:318` — §2.5 marked ✅ Done, claiming the doc "covers
  RS03 binary layout … and re-implementation guidance", contradicted by the doc's own caveats.

## Fix design

Two parts, one PR each.

### Part 1 — pin and bundle the dvdisaster source

1. Add the exact dvdisaster source tarball for the version used at burn time (0.79.x, the
   version `lcsas burn` preflight accepts ≥0.79.0 per `burn/orchestrator.py:240`) to
   `recovery/UPSTREAM.sha256` with its SHA-256, fetched by `recovery/scripts/fetch_upstream.sh`
   like the rustic/CPython artifacts.
2. `src/lcsas/meta/builder.py`: bundle the tarball onto every meta-volume (e.g.
   `tools/src/dvdisaster-<ver>.tar.gz`), as a *required* item — build fails loud if missing.
   Size is a few MB; budget note: the TEST_TINY media-size comment at `metadata.py:29-38`
   concerns the holographic payload on data discs, not the meta-volume, so no conflict.
3. License note: dvdisaster is GPLv3. If FMT-01 ships dvdisaster *binaries* (Option A), source
   distribution is arguably required; bundling the tarball satisfies it either way.

### Part 2 — complete the spec for the pinned version

Rewrite the two punted sections of `docs/DVDISASTER_RS03_FORMAT.md`, scoped explicitly to the
pinned version ("definitive for dvdisaster 0.79.x as pinned in recovery/UPSTREAM.sha256"):

- §3.2 header: replace "may vary… consult rs03-common.h" with the definitive field table —
  every offset, width, byte order (little-endian), and the `"*dvdisaster*"` cookie semantics —
  transcribed from the pinned `rs03-common.h`.
- New section: the layer/interleaving formula — exact mapping from (codeword index, root index)
  to image sector number, the CRC32 sector layout, and the padding rule (augmented images are
  padded to full medium size), with one fully worked example small enough to verify by hand.
- §"Reference Implementations": drop the pip-installable framing; point to the bundled source
  tarball and (once FMT-01 lands) to `recovery/src/lcsas-ecc/` as the in-tree reference.
- Fix `docs/SURVIVABILITY.md` §2.5: re-state the resolution accurately (source pinned + spec
  definitive + in-house decoder per FMT-01), not "docs written".

This work *is* the layout-extraction spike that decides FMT-01's Option B; do it first.

## Tests & gates

1. `tests/integration/test_rs03_doc_conformance.py` — opt-in alongside `test_ecc_repair.py`
   (`LCSAS_ECC_REPAIR=1`, requires dvdisaster; runs in the same scheduled CI job per the GATE
   plan since CI installs dvdisaster): augment a fixture ISO with the real binary, then parse
   the ECC header using ONLY the offsets/cookie documented in the spec, and assert
   nroots/dataSectors/eccSectors agree with `dvdisaster -t` output. Fails whenever the doc and
   the binary truth diverge — the doc can never silently rot again.
2. `tests/recovery_hardening/test_meta_bundles_dvdisaster_source.py` — always-on (static +
   build-tree check, pattern of `test_meta_bundling_completeness.py`): assert the dvdisaster
   source archive appears in `recovery/UPSTREAM.sha256` and lands on a built meta tree; assert
   the spec contains the definitive-layout marker text and no "consult the source code" /
   "pip-installable" punts. Runs under `make test-recovery-hardening` (and CI once the GATE
   plan wires that suite in).

## Acceptance criteria

- [ ] `recovery/UPSTREAM.sha256` contains a dvdisaster source tarball entry;
      `fetch_upstream.sh` fetches and verifies it.
- [ ] A built meta tree contains the tarball; removing it makes `lcsas meta build` fail loud.
- [ ] `DVDISASTER_RS03_FORMAT.md` contains zero instances of "may vary", "consult the source
      code", or "pip-installable"; header table and interleaving formula present with a worked
      example.
- [ ] `test_rs03_doc_conformance.py` passes against the real dvdisaster (header parsed from
      doc offsets alone matches `dvdisaster -t`).
- [ ] SURVIVABILITY.md §2.5 resolution text matches reality.

## Dependencies & related plans

- Feeds **FMT-01** (RS03 repair in-housing) — the completed spec is Option B's input; do
  FMT-02 first (its Part 2 doubles as FMT-01's decision spike).
- GATE: "RS03 ECC repair validation never runs in CI" — provides the scheduled job that runs
  the conformance test.
- GATE: "recovery-hardening suite never runs in CI" — needed for test #2 to gate merges.
- RST: meta-volume completeness gate — same fail-loud bundling mechanism.

## Effort

**3 focused days**: 1.5d source-reading + spec rewrite with worked example, 0.5d
pin/fetch/bundle plumbing, 1d conformance + hardening tests. Needs dvdisaster installed
locally (multi-minute augment passes; see ECC test-environment memory note — don't run
concurrently with other pytest runs).

---
**Implemented:** 2026-06-13. As planned. Pinned dvdisaster 0.79.10-pl6 source in recovery/UPSTREAM.sha256 (new `dvdisaster/src/` category) + fetch_upstream.sh handler; meta builder bundles it to `tools/src/` fail-loud (`--allow-no-dvdisaster-source` escape hatch). Rewrote DVDISASTER_RS03_FORMAT.md §3.2 with byte-exact EccHeader table (offsets confirmed via offsetof + real-image parse), new §4 layout/interleaving (RS03SectorIndex) + worked example, corrected GF poly 0x187, dropped pip-installable framing → bundled source. SURVIVABILITY.md §2.5 restated. Conformance test passes vs real dvdisaster (4m14s); 5 always-on hardening tests pass.
