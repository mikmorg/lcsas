# FMT-01: Bring RS03 ECC repair onto the bare recovery path (decision + implementation)

**Priority:** P1 · **Severity:** critical · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** tracked (misleadingly): docs/SURVIVABILITY.md §2.5 marks "dvdisaster is abandoned" ✅ Resolved, but the resolution was documentation-only; the runtime gap is untracked in UX_CONCERNS.txt / DEFERRED_WORK.txt / READINESS_CHECKLIST.txt
**Suggested GH issue title:** Decide and ship in-house RS03 repair on the bare recovery path

## Problem

Every burned disc pays ~15% of its capacity for DVDisaster RS03 parity, but no tooling that
ships on the meta-volume can *spend* that parity at restore time. The tier-1 C binary contains
zero RS03 code; `restore.sh` and `restore.bat` never mention ECC or repair; and the documented
damaged-disc procedure requires the heir to run `ddrescue` (never bundled anywhere) and to
"have dvdisaster installed". dvdisaster itself is bundled only opportunistically — only if it
happens to be on the build host's PATH, only as a build-host-arch, glibc-dynamic binary — and
is absent from `recovery/UPSTREAM.sha256` pinning and from the 6-target cross-build matrix
that tiers 1/2/3 all received. On Windows, the burned manual tells the heir to download
dvdisaster from a fan-maintained mirror URL. Upstream dvdisaster is abandoned (last release
2020).

The consequence for the non-technical-heir goal: in 2050, a scratched disc gets the fail-loud
half of the disc-integrity layer (tier-1's Poly1305/SHA-256 checks correctly *reject* corrupt
blobs) but not the repair half. The parity bytes are physically present and unreachable.
Because `consolidate/` collapses redundant packs across discs, an affected pack may exist on
exactly one disc — so this is plausible permanent data loss in precisely the scenario ECC was
designed for. This is the only critical in the format dimension; it is P1 rather than P0 only
because it does not corrupt the catalog or block the next burn — but it must land before the
system is declared heir-ready.

## Evidence

All re-checked against current code (2026-06):

- `src/lcsas/meta/builder.py:31` — `_OPTIONAL_TOOLS = ("dvdisaster",)`; `:1792-1795` — bundled
  only `if _shutil.which(tool)` (silently skipped otherwise).
- `src/lcsas/meta/bundler.py:26-40` — `_SYSTEM_LIB_PREFIXES` deliberately excludes glibc-family
  libs, so a bundled dvdisaster is glibc-dynamic and build-host-arch only.
- `recovery/UPSTREAM.sha256` — pins only rustic v0.11.2 and CPython 3.12.13; no dvdisaster.
- `recovery/scripts/restore.sh`, `recovery/scripts/restore.bat` — `grep -iE 'dvdisaster|ecc|repair'`
  returns zero matches.
- `recovery/src/` (all four C tools) — zero matches for `rs03|dvdisaster`.
- `recovery/docs/RECOVER.txt:154-161` — "Use ddrescue…", "If you have dvdisaster installed:
  `dvdisaster -i /tmp/disc.iso -f`".
- `recovery/docs/RECOVER_WINDOWS.txt:371-375` — "install dvdisaster from https://dvdisaster.jcea.es".
- `recovery/docs/TIERS.txt:92-102` — claims RS03 "repairs bit-rotted sectors back to their
  original bytes" as one of the two disc-integrity guards.
- `tests/integration/test_ecc_repair.py:41-45` — the only repair validation; opt-in
  (`LCSAS_ECC_REPAIR=1`) and requires the real dvdisaster binary on PATH.
- Near-refutation checked: `src/lcsas/restore/executor.py:73-83` and `lcsas verify` *do* wire
  `verify_iso`/`repair_iso` — but via `SubprocessDVDisasterRunner`
  (`src/lcsas/ecc/dvdisaster.py:121`), i.e. shelling out to an externally installed dvdisaster.
  The dependency claim stands.

## Fix design

This is a decision plan. Two viable designs; pick one, then the "regardless" items apply to both.

### Option A — vendor + pin static dvdisaster builds for all 6 targets

Vendor the dvdisaster 0.79.x source tarball (pin in `recovery/MANIFEST.sha256`), build static
CLI-only binaries for the six approved targets (Linux x86_64/aarch64/armv7 musl, Windows-gnu,
macOS Intel/ARM via `zig cc`), pin artifacts in `recovery/UPSTREAM.sha256`, promote dvdisaster
from `_OPTIONAL_TOOLS` to a required per-target meta-volume item, and wire `restore.sh`/`restore.bat`
to invoke it on read failure.

- **Effort:** 5–10 days of build engineering, *if it works at all*.
- **Risk: high.** dvdisaster's core depends on glib (GTK additionally for the GUI); statically
  cross-compiling glib-dependent autotools code with zig cc for macOS (no SDK) and Windows-gnu
  is the hardest build target in the project, against an abandoned codebase nobody here has
  audited (~50k LOC). Failure on even one target leaves the matrix incomplete. Ongoing cost:
  we own patching an abandoned C codebase for decades.
- **Upside:** byte-exact behavioral compatibility with the tool that wrote the parity; the
  augment (encode) side comes for free.

### Option B — implement an RS03 decoder in the tier-1 C codebase (RECOMMENDED)

New C89 tool `recovery/src/lcsas-ecc/` built and cross-built exactly like `lcsas-restore`
(same recovery `Makefile` zig-cc matrix already proven for `lcsas-keyshare`):

```
lcsas-ecc verify <image>      # parse RS03 header (cookie "*dvdisaster*"), CRC-check sectors
lcsas-ecc fix    <image>      # erasure-decode damaged sectors in place (or --out <copy>)
lcsas-ecc info   <image>      # print geometry: nroots, dataSectors, eccSectors, layout
```

Components (~800–1200 LOC total, in-repo style, no new vendored deps):
- GF(2^8) arithmetic over primitive polynomial 0x11D (already documented in
  `docs/DVDISASTER_RS03_FORMAT.md` §4).
- RS erasure/error decoder (syndromes + Berlekamp–Massey + Forney; erasure-only fast path for
  CRC-located bad sectors).
- RS03 header parse + CRC32 sector layer + the interleaving layout — the layout is the one
  genuinely undocumented piece, extracted once from the pinned dvdisaster source and frozen
  into the completed spec (**that extraction is plan FMT-02; do it first**).
- Must accept *small/unpadded* images in addition to dvdisaster's full-medium-padded augmented
  images, so fast always-on tests are possible (dvdisaster pads every augmented image to a full
  medium, ~700 MB minimum — see test_ecc_repair.py docstring).
- Phase 2 (optional, separate PR): `lcsas-ecc augment` encode mode — encoding is simpler than
  decoding and removes dvdisaster from the *burn* pipeline too, but is not required to close
  this finding.

Why B: it rides the proven zig-cc 6-target matrix instead of fighting glib autotools; the
result is ~1k LOC of our own audited C89 under our existing coverage/fuzz gates, not 50k LOC
of abandoned upstream; and because the *format* is unchanged it repairs the already-burned
back-catalog. PAR2 sidecars were considered and rejected: a second parity system that does
nothing for existing discs.

- **Effort:** ~10–13 days (see Effort). **Risk: medium** — layout reverse-engineering errors,
  fully mitigated by differential conformance against the real dvdisaster binary (CI already
  installs dvdisaster, `.github/workflows/test.yml:61-66`).

### Wiring (Option B)

1. `recovery/Makefile`: add `lcsas-ecc` to build, test, coverage, and the cross-arch targets;
   commit per-target bins under `recovery/bin/<target>/` like keyshare.
2. `recovery/scripts/restore.sh`: before the tier cascade, when an image/mount read fails or
   `--check-disc` is passed, run `lcsas-ecc verify`; on CRC failures print and offer
   `lcsas-ecc fix`. Mirror in `restore.bat` (depends on INFRA-01 for testability).
3. `src/lcsas/ecc/`: add an `LcsasEccRunner` implementing the existing `DVDisasterRunner`
   protocol so operator-side `lcsas verify` / `restore exec` can use the in-house tool when
   dvdisaster is absent (augment still requires dvdisaster until phase 2).
4. `src/lcsas/meta/builder.py`: bundle `lcsas-ecc` per target next to `lcsas-restore`; build
   fails loud (not silent skip) if a target binary is missing — same gate as RST's
   meta-completeness plan.

### Regardless of option (ship in the same PR series)

- `recovery/docs/RECOVER.txt` physical-recovery section: add the no-tools imaging fallback
  `dd if=/dev/sr0 of=disc.iso conv=noerror,sync bs=2048` for when ddrescue is absent; replace
  "If you have dvdisaster installed" with the bundled-tool invocation.
- `recovery/docs/RECOVER_WINDOWS.txt:371-375`: replace the fan-mirror download instruction with
  the bundled tool path.
- `recovery/docs/TIERS.txt` DISC-INTEGRITY LAYER: update to name the shipped repair tool.

No catalog/schema impact. Already-burned discs carry the old docs forever; the repair tool on
any *newer* meta-volume can fix *older* data discs (format unchanged), so the heir guidance is
"use the newest META disc in the set" — already the convention.

## Tests & gates

1. `tests/e2e/test_ecc_selfrepair_no_dvdisaster.py` — **the headline gate**: master a small
   ISO, augment, corrupt 5% of data sectors, put a shim dir on PATH whose `dvdisaster` exits
   127, repair using ONLY shipped LCSAS tooling, assert byte-identical extraction.
   Two variants per the verifier's caveat (dvdisaster-compatible augmented images are padded
   to a full medium, multi-minute):
   - *always-on* (in `make gate` via test-e2e): tiny image augmented by `lcsas-ecc`'s own test
     encoder (or a checked-in small unpadded fixture) — runs in seconds;
   - *CI job* (dvdisaster is already installed in CI): image augmented by the real dvdisaster,
     repaired by `lcsas-ecc` — the cross-implementation proof. Opt-in locally
     (`LCSAS_ECC_REPAIR=1`, consistent with test_ecc_repair.py).
2. `tests/recovery_hardening/test_ecc_tooling_on_meta.py` — build a meta tree and assert an
   ECC repair binary exists for every approved target and is pinned in
   `recovery/MANIFEST.sha256`; the build must fail loudly, not skip, when missing.
3. `tests/recovery_hardening/test_restore_sh_ecc_dispatch.py` — static check (pattern of
   `test_disc_swap_docs.py`) that restore.sh contains the verify/repair step referencing the
   bundled tool, and that RECOVER.txt / RECOVER_WINDOWS.txt reference it (no fan-mirror URL).
4. C-side: `recovery/tests/test_ecc.c` unit tests (GF tables, syndrome/Forney vectors, header
   parse) under `make -C recovery test`; add `lcsas-ecc` to the C coverage + fuzz gates
   (cross-ref GATE: audit-gate path-filter holes).
5. Keep `tests/integration/test_ecc_repair.py` as the dvdisaster-behavior oracle; promote it
   to the scheduled CI job per GATE "RS03 repair validation never runs in CI".

## Acceptance criteria

- [ ] Decision recorded in this plan's PR (Option B unless a spike disproves it in ≤2 days).
- [ ] `make -C recovery all` produces `lcsas-ecc` for all 6 targets; bins committed and pinned.
- [ ] `tests/e2e/test_ecc_selfrepair_no_dvdisaster.py` (always-on variant) passes in `make gate`
      with dvdisaster shimmed to exit 127.
- [ ] CI conformance variant: dvdisaster-augmented image with 5% sector corruption is repaired
      byte-identically by `lcsas-ecc fix`.
- [ ] `restore.sh --check-disc <image>` reports CRC damage and `lcsas-ecc fix` repairs it on a
      bare Alpine container (no dvdisaster, no ddrescue installed).
- [ ] RECOVER.txt / RECOVER_WINDOWS.txt / TIERS.txt name only bundled tools for repair;
      `grep -i 'jcea.es'` returns nothing.
- [ ] Meta build fails (non-zero) when any target's ECC binary is missing.

## Dependencies & related plans

- **FMT-02** (RS03 spec completion + dvdisaster source on disc) — *do first*; it produces the
  pinned source + definitive layout documentation Option B implements against.
- FMT-06 (ECC capacity claims) — touches the same RECOVER.txt lines; land FMT-06 first (tiny).
- GATE: "RS03 ECC repair validation is opt-in and never runs in CI" — that plan provides the CI
  job this plan's conformance tests ride on.
- GATE: audit-gate path-filter holes — ensure `recovery/src/lcsas-ecc/` is inside the C
  coverage/fuzz gate from day one.
- RST: meta-volume completeness gate — same fail-loud bundling mechanism; share implementation.
- BURN: "default_ecc_redundancy_pct silently ignored by RS03 augmented mode" — `lcsas-ecc info`
  should print the *actual* geometry, which that plan's fix needs for honest reporting.
- INFRA-01 — restore.bat ECC wiring is only testable once the Windows e2e scaffolding exists.

## Effort

**Option B (recommended): 10–13 focused days** — spec-driven decoder impl 5–7d, conformance +
C unit/fuzz tests 3d, restore.sh/operator wiring + docs 2d, cross-build/bin regeneration 1d
(qemu + wine verify loop already exists per `make keyshare-arches` precedent).
Option A for comparison: 5–10d with high abort risk on the macOS/Windows glib cross-builds,
plus permanent maintenance of an abandoned 50k-LOC codebase.
A 2-day timeboxed spike on Option B's layout extraction (FMT-02) settles the decision cheaply.

---
**Implemented (increment 1 of N):** 2026-06-13. Decision: **Option B** (in-house C89 RS03 decoder), per plan recommendation. This increment landed the foundational core only: `recovery/src/lcsas-ecc/` (gf256 over 0x187, rs03 layout/CRC/erasure-decode, CLI info/verify/fix), C unit tests (`recovery/tests/test_ecc.c` — GF/CRC/layout/interleaving/encode-corrupt-repair byte-identical round trip/uncorrectable/reject), and host Makefile wiring (`all`, object/test rules, `TEST_BINS`). `make -C recovery test` green (`test_ecc: OK`, `ALL OK`); manual CLI smoke (info/verify/fix on a synthetic augmented image) byte-identical repair confirmed. **Remaining (future increments):** 6-target cross-build + committed/pinned bins (recovery/bin/*, UPSTREAM/MANIFEST); restore.sh/restore.bat --check-disc wiring; `LcsasEccRunner` Python runner in src/lcsas/ecc/; meta/builder.py per-target bundling + fail-loud gate (promote out of `_OPTIONAL_TOOLS`); docs (RECOVER.txt/RECOVER_WINDOWS.txt/TIERS.txt) repair-tool naming + remove jcea.es; e2e `test_ecc_selfrepair_no_dvdisaster.py`, `test_ecc_tooling_on_meta.py`, `test_restore_sh_ecc_dispatch.py`; add lcsas-ecc to C coverage/fuzz/audit-gate path filters; cross-conformance against real dvdisaster (opt-in CI variant).

---
**Implemented (increment 2 of N):** 2026-06-14. 6-target cross-build + committed bins. Added `bin/<arch>/lcsas-ecc` zig-cc recipes for all six approved tier-1 targets and an `ecc-arches` aggregator (mirrors `keyshare-arches`); committed the six bins under `recovery/bin/*`; wired all six into `scripts/bin_parity.py` TARGETS and added the macOS/Windows ecc entries to `BIN_PARITY_EXEMPT`. `make ecc-arches` green; `bin-parity` byte-identical on the three Linux musl ecc bins and clean-rebuild-OK (exempt) on the macOS/Windows ecc bins. As planned. **Remaining (future increments):** restore.sh/restore.bat --check-disc wiring; `LcsasEccRunner` Python runner in src/lcsas/ecc/; meta/builder.py per-target bundling + fail-loud gate; docs (RECOVER.txt/RECOVER_WINDOWS.txt/TIERS.txt) repair-tool naming + remove jcea.es; e2e + recovery_hardening tests; add lcsas-ecc to C coverage/fuzz/audit-gate path filters; UPSTREAM/MANIFEST pinning of the bundled tool; cross-conformance against real dvdisaster.

---
**Implemented (increment 9 of N):** 2026-06-15. e2e + recovery_hardening + cross-conformance tests (Tests & gates §1-3, §5). (1) **Always-on headline gate** `tests/e2e/test_ecc_selfrepair_no_dvdisaster.py`: a new in-repo fixture generator `recovery/tests/ecc_make_fixture.c` (reuses the shipped rs03 encoder/decoder + the build_image "fill parity via rs03_fix" trick; calls `gf_init()`) writes a TINY unpadded RS03 image (runs in <1 s, vs dvdisaster's ~700 MB padded medium), then the test corrupts a data sector and repairs it with ONLY the built `lcsas-ecc` while a `dvdisaster` shim on PATH exits 127 — asserting byte-identical recovery; a second test covers `fix --out F` (source untouched, out is the repair). 2 tests pass. (2) `tests/recovery_hardening/test_ecc_tooling_on_meta.py`: per-target lcsas-ecc source-present + git-tracked, required-contents enforcement, and a real `lcsas meta build` bundles lcsas-ecc for every present target with a clean completeness gate. 8 tests pass. (3) restore.sh `--check-disc` dispatch + docs-vs-reality checks already landed in increments 4/7 (`test_restore_sh_ecc_dispatch.py`). (4) **Opt-in cross-conformance** added to `tests/integration/test_ecc_repair.py` (`test_lcsas_ecc_repairs_dvdisaster_augmented_image`, gated by the existing `LCSAS_ECC_REPAIR=1` mark + a runnable lcsas-ecc): augments with the REAL dvdisaster, then verifies/repairs with the in-house `LcsasEccRunner` and asserts byte-identical extraction — the differential proof that lcsas-ecc reads dvdisaster-written parity. `make lint`/`make typecheck` clean. **FMT-01 remaining (not in this push):** UPSTREAM/MANIFEST pinning of the bundled lcsas-ecc binary itself (the dvdisaster *source* tarball is already pinned per FMT-02; pinning the per-target ecc bins is a small follow-up); promoting the opt-in cross-conformance to a scheduled CI job (depends on the GATE plan's CI job); restore.bat ECC e2e under wine (INFRA-01 dependency). Optional phase-2 `lcsas-ecc augment` encoder remains out of scope per the plan.

**Implemented (increment 10 of N):** 2026-06-15. lcsas-ecc binary pinning — clarified + gated. Investigation showed the per-target lcsas-ecc bins are ALREADY pinned by the project's real mechanisms, not the source-tree files the earlier footers loosely named: (a) `recovery/UPSTREAM.sha256` is for opaque UPSTREAM artifacts only (rustic/CPython/dvdisaster-source) — our own cross-built bins do not belong there; (b) the source-tree `recovery/MANIFEST.sha256` generator prunes `./bin`, so NO committed bin (restore/keyshare/ecc alike) is listed there — in-repo bin integrity is the `recovery/scripts/bin_parity.py` rebuild-and-diff gate (lcsas-ecc wired in increment 2) + git object hashing; (c) the **on-disc** pin an heir verifies is the meta volume's regenerated `recovery/MANIFEST.sha256` — `_regenerate_recovery_manifest` walks all of `bin/` after `_bundle_tier1_binaries`, so once increment 6 made lcsas-ecc a required bundled bin it is auto-pinned. Verified empirically: a built meta tree's MANIFEST.sha256 carries all 12 `./bin/<target>/lcsas-ecc[.exe]` rows and `sha256sum -c` passes for every one. Added an always-on gate `tests/recovery_hardening/test_meta_bundling_completeness.py::test_meta_manifest_pins_lcsas_ecc_per_target` that builds a meta volume and asserts every bundled lcsas-ecc has a matching MANIFEST row + hash (guards against a bundling / manifest-regen regression silently un-pinning the repair tool). `make lint` clean; new test passes. **This closes the "UPSTREAM/MANIFEST pinning" follow-up** — correctly: the bins are pinned on-disc (regenerated meta MANIFEST) + in-repo (bin_parity), and that is now gated. Genuinely remaining (non-blocking): promote opt-in dvd cross-conformance to scheduled CI (GATE-plan dependency); restore.bat ECC e2e under wine (INFRA-01); optional phase-2 `lcsas-ecc augment` encoder.

---
**Implemented (increment 8 of N):** 2026-06-15. lcsas-ecc into the C coverage + fuzz + audit-gate (Tests & gates §4; GATE audit-gate path-filter). Coverage: added `--filter '$(ECC_DIR)/.*'` to the `coverage-c` gcovr invocation so `recovery/src/lcsas-ecc/*.c` is in the C coverage report (mirrors how `KEYSHARE_DIR` is filtered in; `coverage_check.py`/`exemptions_check.py` remain scoped to `src/lcsas-restore/`, so no threshold/exemptions-contract change). Fuzz: new `recovery/fuzz/fuzz_rs03_parse.c` (LibFuzzer harness driving `rs03_parse → rs03_verify → rs03_fix` over untrusted/corruption-controlled image bytes) + 3 corpus seeds under `recovery/fuzz/corpus/rs03/` + `fuzz-rs03-smoke`/`fuzz-rs03` Makefile rules, added to the `fuzz-smoke` aggregator and `.PHONY` (so `make audit-gate` and the CI audit-gate job both exercise it; `.github/workflows/audit-gate.yml` already globs `recovery/src/**` + `recovery/fuzz/**`). **BUG FOUND + FIXED by the new fuzzer:** `rs03_verify` had a heap-buffer-overflow (rs03.c:186 `read_stored_crc`/`rd_u32le`) — it bounds-checked the *data* sector against `img_len` but read the *CRC* sector (a later image position) unconditionally, so a truncated image over-read the heap. Fixed by treating a CRC slot past the readable image as an erasure (matching rs03.h's documented "truncated image → damage, not crash" contract). After the fix the seeded crash replays clean and a 30 s smoke finds no crashes. Because rs03.c changed, all 6 committed `recovery/bin/<arch>/lcsas-ecc[.exe]` were rebuilt (`make ecc-arches`, zig 0.16.0) and re-committed; the Linux musl ecc bins are reproducible on this host. C unit tests green (`test_ecc: OK`, all 17 test_* OK). **Known pre-existing (NOT this change):** `make bin-parity` reports the Linux `lcsas-restore`/`lcsas-iso9660`/`lcsas-keyshare` bins STALE (committed != clean rebuild) on baseline 876f53d too — an environment/zig-version reproducibility gap in those previously-committed bins, independent of lcsas-ecc; the macOS/Windows ecc bins are BIN_PARITY_EXEMPT (rebuilt-OK) as wired in increment 2. **Remaining (future increments):** e2e `test_ecc_selfrepair_no_dvdisaster.py` + recovery_hardening `test_ecc_tooling_on_meta.py`; UPSTREAM/MANIFEST pinning of the bundled tool; opt-in cross-conformance against real dvdisaster.

---
**Implemented (increment 7 of N):** 2026-06-15. Docs: damaged-disc path now names the bundled in-house tool (Regardless-of-option items). `recovery/docs/RECOVER.txt` PHYSICAL RECOVERY rewritten — the no-tools `dd if=/dev/sr0 ... conv=noerror,sync bs=2048` imaging fallback (ddrescue noted as optional, not required/bundled), then repair via `sh /mnt/restore.sh --check-disc /tmp/disc.iso` (or `lcsas-ecc verify/fix` directly); the old "if you have dvdisaster installed: dvdisaster -i -f" is gone. `recovery/docs/RECOVER_WINDOWS.txt` PHYSICAL DISC PROBLEMS rewritten to `restore.bat --check-disc` / `lcsas-ecc.exe`; the `https://dvdisaster.jcea.es` fan-mirror download instruction REMOVED and replaced with the in-house-tool + pinned-source story. `recovery/docs/TIERS.txt` DISC-INTEGRITY LAYER updated to name `lcsas-ecc` + `restore.sh --check-disc` as the repair tool/entry point and to reference the always-on no-dvdisaster self-repair gate. `docs/DVDISASTER_RS03_FORMAT.md` header reframed around lcsas-ecc + the pinned/audited source (jcea.es URL dropped; stable GitHub source mirror kept as provenance). `grep -i jcea.es` across docs/ + recovery/docs/ now returns nothing. 5 docs-vs-reality static checks added to `test_restore_sh_ecc_dispatch.py` (restore.sh wires --check-disc→verify/fix; RECOVER*/TIERS name lcsas-ecc + --check-disc; no jcea.es in any recovery/docs/*.txt). doc-command-contract + ecc-capacity + disc-swap-docs gates green. **Remaining (future increments):** e2e `test_ecc_selfrepair_no_dvdisaster.py` + `test_ecc_tooling_on_meta.py`; lcsas-ecc into C coverage/fuzz/audit-gate path filters; UPSTREAM/MANIFEST pinning of the bundled tool; opt-in cross-conformance against real dvdisaster.

---
**Implemented (increment 6 of N):** 2026-06-15. lcsas-ecc promoted to a REQUIRED per-target meta artifact + fail-loud gate (Wiring §4). `src/lcsas/meta/required_contents.py`: `required_target_paths()` now includes `recovery/bin/<triple>/lcsas-ecc[.exe]` (new `_ecc_name()`), so the post-build completeness gate (`missing_required_contents()` / `cmd_meta_verify`) FAILS LOUD when any approved target's lcsas-ecc is absent — the same mechanism that enforces lcsas-restore/keyshare (RST-05), not a silent skip. `src/lcsas/meta/builder.py`: `_bundle_tier1_binaries` now copies `lcsas-ecc{suffix}` next to lcsas-restore/lcsas-keyshare for every target. (Note: `_OPTIONAL_TOOLS=("dvdisaster",)` stays — that governs the host-PATH dvdisaster *encoder* bundling, a separate concern from the per-target tier-1 ecc binary family; the in-house lcsas-ecc is now required, dvdisaster remains opportunistic.) 6 tests added/extended in `tests/recovery_hardening/test_meta_bundling_completeness.py`: per-target lcsas-ecc source-present + git-tracked, required-contents includes ecc, build bundles ecc per target, and a fail-loud test that deletes a bundled lcsas-ecc and asserts `missing_required_contents()` reports it. All 6 lcsas-ecc bins already committed (increment 2) so the gate is satisfiable. `make lint`/`make typecheck` clean; new meta tests green (15 passed); the only failing test in that file is the PRE-EXISTING `test_meta_build_bundles_every_present_target` (host lacks the fetched dvdisaster-source cache; fails identically on baseline — my build-based tests pass `allow_no_dvdisaster_source=True` to avoid that host limitation). **Remaining (future increments):** docs (RECOVER*/TIERS + jcea.es) [done as incr 7]; e2e + recovery_hardening meta-tooling tests; lcsas-ecc into C coverage/fuzz/audit-gate; UPSTREAM/MANIFEST pinning; cross-conformance.

---
**Implemented (increment 5 of N):** 2026-06-15. Operator-side fallback wiring (Wiring §3). Added `select_ecc_runner()` to `src/lcsas/ecc/dvdisaster.py`: prefers the real `dvdisaster` on PATH (encode+decode), else falls back to the in-house `LcsasEccRunner` (decode-only verify/repair) so a host without dvdisaster still spends the burned RS03 parity, else returns `None` (caller degrades to SHA-256). A `require_augment=True` mode restricts the choice to encoders (dvdisaster only) for the burn path. Wired into `src/lcsas/cli/main.py`: single-ISO `lcsas verify`, batch `verify --all`, and both `RestoreExecutor` construction sites (restore exec + standalone-env restore) now select via the factory instead of hard-coding `SubprocessDVDisasterRunner` — so verify/repair on a dvdisaster-less host uses lcsas-ecc rather than skipping ECC. Burn/stage/consolidate/meta augment paths keep `SubprocessDVDisasterRunner` (encode still needs dvdisaster). 5 `select_ecc_runner` unit tests added (`TestSelectEccRunner`); the two `verify --all` SHA-fallback tests updated to mock BOTH ECC tools absent. `make lint`/`make typecheck` clean; `make test-unit` green except two PRE-EXISTING failures unrelated to FMT-01 (`test_blind_prompt_hygiene.py::test_docs_prompt_has_no_spoonfeed_tokens` and `[restore.sh]` — `agent_prompt_split_docs.txt` already contains `restore.sh` tokens on master 445d912, fails identically pre-FMT-01). **Remaining (future increments):** meta/builder.py per-target bundling + fail-loud gate; docs; e2e + recovery_hardening tests; lcsas-ecc into C coverage/fuzz/audit-gate; UPSTREAM/MANIFEST pinning; cross-conformance.

---
**Implemented (increment 4 of N):** 2026-06-15. Heir-facing `--check-disc` repair path (Fix design / Wiring §2). `recovery/scripts/restore.sh` gained a self-contained `--check-disc IMAGE` mode: resolves the recovery root (positional or auto-detected) + per-target rust-triple (uname-derived, `$LCSAS_TARGET`-overridable), locates the bundled `bin/<triple>/lcsas-ecc` (with `.exe`/flat fallbacks), runs `lcsas-ecc verify`, and on damage (exit 1) offers — or with `LCSAS_CHECK_DISC_AUTOFIX=1` performs — `lcsas-ecc fix` in place. Script exit codes mirror the C contract (0 clean/repaired · 1 uncorrectable/declined · 2 no RS03 header · 3 usage/I-O/missing-bin); requires NO externally installed dvdisaster/ddrescue. Documented in `--help` (SCRATCHED / DAMAGED DISC? section, names lcsas-ecc + the dd imaging fallback). Mirrored in `recovery/scripts/restore.bat` (`--check-disc IMAGE` → `lcsas-ecc.exe`). 13 dispatch tests in `tests/recovery_hardening/test_restore_sh_ecc_dispatch.py` (verify/fix exit-code mapping via env-controlled stub; autodetect + AUTO_RECOVERY + missing-image/missing-bin/no-tree branches). `sh -n`/`dash -n` clean; restore-sh hardening suite green (98 passed); `tools/cov_shell.py` reports 89.9% shell-coverage (above the 89% floor). NOTE: `make shell-coverage` also runs `test_restore_bat_wine_smoke.py::test_missing_repo_reports_error`, which fails on this VM **pre-existing and unrelated** to this change — a real LCSAS archive disc is mounted at `/media` (CDEmu `/dev/sr0`, iso9660) and wine's default drive map lets the `.bat` discover its alpha/bravo repos; the same test fails identically on baseline (`.bat` stashed). **Remaining (future increments):** operator-side `LcsasEccRunner` fallback wiring into `lcsas verify`/restore when dvdisaster absent; meta/builder.py per-target bundling + fail-loud gate; docs (RECOVER.txt/RECOVER_WINDOWS.txt/TIERS.txt) + remove jcea.es; e2e + recovery_hardening (meta-tooling) tests; lcsas-ecc into C coverage/fuzz/audit-gate; UPSTREAM/MANIFEST pinning; cross-conformance against real dvdisaster.

---
**Implemented (increment 3 of N):** 2026-06-14. Operator-side Python runner (Fix design §3). Added `LcsasEccRunner` in `src/lcsas/ecc/dvdisaster.py` implementing the existing `DVDisasterRunner` Protocol via the bundled `lcsas-ecc` binary: `verify_iso`/`repair_iso` map the C exit-code contract (0 ok / 1 damage·uncorrectable / 2 no-header / 3 I-O), raising loudly on exit 2/3 so an unreadable image is never silently reported intact; `repair_iso` trusts lcsas-ecc's atomic exit code (no re-verify round trip — fix refuses partial writes); `augment_iso` raises `NotImplementedError` (encode stays dvdisaster's job until phase 2). 11 unit tests added to `tests/unit/test_dvdisaster.py`. `make lint`/`make typecheck` clean; `make test-unit` green (1783 passed). As planned. **Remaining (future increments):** restore.sh/restore.bat --check-disc wiring; operator-side wiring of `LcsasEccRunner` into `lcsas verify`/`restore exec` fallback when dvdisaster absent; meta/builder.py per-target bundling + fail-loud gate; docs (RECOVER.txt/RECOVER_WINDOWS.txt/TIERS.txt) + remove jcea.es; e2e + recovery_hardening tests; lcsas-ecc into C coverage/fuzz/audit-gate path filters; UPSTREAM/MANIFEST pinning; cross-conformance against real dvdisaster.
