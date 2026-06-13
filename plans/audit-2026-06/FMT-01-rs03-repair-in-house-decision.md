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
