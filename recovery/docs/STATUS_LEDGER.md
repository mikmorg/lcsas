# LCSAS Recovery — Unified Status Ledger

This is the single tracker for tier-1 recovery audit findings and the
deferred-work parking lot.  It folds together the content that used to
live in `AUDIT_FINDINGS.md` and `DEFERRED_WORK.txt` (both now thin
redirect stubs), reconciled against what has actually landed in `master`
as of the 2026-06 audit-remediation pass.

Two sibling documents are **not** folded in here because they are live
contracts, not status ledgers:

- **`AUDIT.md`** — operator-facing description of the `make audit-gate`
  mechanism and threshold rationale.  Guarded by
  `tests/recovery_hardening/test_audit_gate_threshold_parity.py`.
- **`EXEMPTIONS.md`** — the line-by-line coverage contract for
  `src/lcsas-restore/*.c`, enforced live by
  `recovery/scripts/exemptions_check.py` at the end of `make coverage-c`.

Sections below: **Resolved**, **Open / deferred**, **Coverage posture**.

---

## Resolved

### Audit bugs (Phase 0 manual audit + follow-ups)

| id     | file:line                            | issue                                                     | status                                                       |
|--------|--------------------------------------|-----------------------------------------------------------|-------------------------------------------------------------|
| BUG-1  | json_q.c:290                         | stack overflow via unbounded buffer write (critical)      | fixed in PR #168                                            |
| BUG-2  | repo.c:179 (load_keys_dir)           | silent key cap at 256                                     | fixed in PR #169                                            |
| BUG-3  | repo.c:429 (load_index)              | silent index cap at 2048                                  | fixed in PR #169                                            |
| BUG-4  | repo.c:480 (load_index)              | silent supersedes cap at 8192 (now fail-loud)             | fixed in PR #169                                            |
| BUG-5  | disc_locator.c (5 sites)             | silent path-too-long drops                                | fixed in PR #169                                            |
| T1C-03 | tree.c (name + linktarget decode)    | embedded-NUL path collision (` ` escape truncates C str)  | fixed (`decode_path_component` length-checks the decode)   |

### Audit-gate infrastructure (landed since the original audit)

- **Fault injection (#165)** — `recovery/scripts/malloc_inject.c` (LD_PRELOAD
  Nth-allocation-failure shim) + `recovery/scripts/run_fault_inject.py` driver
  + `make -C recovery fault-inject` target.  Pinned as a zero-crash regression
  gate by `tests/recovery_hardening/test_tier1_fault_inject.py` (opt-in
  `LCSAS_FAULT_INJECT=1`).
- **`fuzz_tree_restore` (T1C-04)** — `recovery/fuzz/fuzz_tree_restore.c`, a
  link-seam LibFuzzer harness over `lcsas_tree_restore`; in the `fuzz-smoke`
  aggregate.  Surfaced + fixed an OOB token read on a malformed `nodes` array,
  depth-caps the recursion, and fails loud on a >4095-byte restored path.
- **FMA-09 fs-full drain seam** — `LCSAS_TEST_FULL_FS_DIR` env seam +
  `tests/recovery_hardening/test_restore_space_preflight.py` exercise the
  `disc_locator.c` drain-guard / `<10%-free` branches that were previously
  unreachable in `coverage-c`.
- **lcsas-keyshare under the tier-1 gates (KEY-06)** — the SLIP-0039 combiner
  is in the `coverage-c` gcovr report, the `fuzz-smoke` aggregate
  (`fuzz_slip39_mnemonic`), the `sanitize` ASan/UBSan/LSan run, and the
  audit-gate CI path filter.  Coverage posture documented narratively in
  `EXEMPTIONS.md`.

### Scalability (Phase 11)

The blob index was migrated from an O(n) linear scan to a sorted array +
`bsearch` (O(log n)).  `find_ns_mean` dropped ~28,000× at N=1M (≈61 ms →
≈2 µs).  At ~900M-entry (petabyte) extrapolation a find is ~3 µs, so
**petabyte restore is no longer find-bound**.  Validated by
`scaling_bench.py` and the opt-in `LCSAS_PETABYTE=1` end-to-end test
(`test_tier1_petabyte_fixture.py`: 1M entries + 1k files, RSS ~110 MiB,
wall ~7.8 s).

| N (entries) | linear find_ns | bsearch find_ns |
|------------:|---------------:|----------------:|
|         102 |         11,333 |             860 |
|       1,002 |         76,894 |           1,131 |
|      10,002 |        572,238 |           5,794 |
|     100,002 |      6,022,275 |           1,821 |
|   1,000,002 |     61,156,104 |           2,139 |

---

## Open / deferred

Parking lot for future iterations — the catalog-driven prompt-hint path
(catalog.db → disc_locator → volume label in the swap prompt) is
implemented and verified end-to-end
(`tests/test_multidisc.py::case_catalog_prompt_label`).  These follow-ups
were deliberately deferred from that work.

### 1. disc_index.txt sidecar (resilience-only redundancy) — NOT BUILDING (decided #457, 2026-10)

The proposal was a plain-text `<sha256>  <label>  <uuid>` pack→volume map
written at burn time, read by a C-side `disc_index.{c,h}` as a *tertiary*
fallback when SQLite cannot open `catalog.db`.  Decision (#457): don't
build it until one of the triggers below turns up.  Nothing for it exists
in-tree.

Why the gap it would fill is small:

- **Every copy has to fail first.**  `catalog.db` is copied to every data
  disc, and tier-1 statically links the SQLite amalgamation, so the
  sidecar only matters when every catalog copy to hand fails to open
  while the packs on those same discs still read.
- **Losing the catalog costs the hint, not the restore.**  Tier-1 finds
  packs through the repository's own index.  The catalog only supplies
  the swap prompt's "insert disc X" line.  Without it the prompt names
  the pack hash and the operator inserts discs one at a time.  The
  blind-restore `no-catalog` variant (every data disc without
  `catalog.db`, so the hash-only prompt) scored the full 15/15 with an
  agent that had never seen LCSAS when it was promoted out of xfail on
  2026-05-27 (`tests/e2e/cdemu_blind_restore/VARIANT_FLAKE_NOTES.md`).
- **Discs are named anyway.**  Since #456 tier-1 names each disc on
  insertion from its own `volume_info.json`, so even with no catalog the
  operator can see which disc is in the drive.
- **A second map can drift.**  A sidecar would be another pack→volume map
  to keep in step with the catalog through consolidation and extra
  copies.

Reopen and build it if any of these turns up:

- a real restore or physical drill in which every available `catalog.db`
  failed to open but the discs' packs still read;
- the blind `no-catalog` variant losing points at the swap step;
- a field report that swapping discs by hash alone was impractical for
  the size of the archive.

*Effort if built:* Python writer ~30 LOC; C reader + locator wire-in
~150 LOC; test ~50 LOC.

### 2. mounted-disc identification via volume_info.json — LANDED (#456, 2026-10)

`volume_info.json` is written to every staging root
(`staging/metadata.py::write_volume_info`) and was read only by the *Python*
restore path (`meta/restore_single_drive.py::verify_disc`).  Now each
`refresh_discovered()` in `disc_locator.c` also reads it, through the
existing `json_q`, for every disc root it visits, and prints
`[lcsas-restore] mounted volume <label> at <path>` once per insertion.
While the prompt polls (#455), a disc that isn't the one asked for is
followed by `(still not found -- check the disc label and try again)`.
The meta disc's file has no label and is named as the recovery disc.  An
absent file is silent.  A bad one gets a one-line note and the disc is
still searched.  Bad means unreadable, past 16 MiB, not a JSON object, or
without a usable label.  The label is shown as printable ASCII only.  No
new on-disc file, and the UUID is not compared against the catalog.
Tests: `recovery/tests/test_disc_locator.c` and
`tests/recovery_hardening/test_tier1_volume_label.py` (labels written by
the real `write_volume_info`).

### 3. Auto-mount detection (eliminate the Enter press) — LANDED (#455, 2026-10)

Notice a new mount and skip the manual Enter.  Done by polling rather
than inotify/kevent/udisks: while the swap prompt waits, `disc_locator.c`
`select()`s the prompt's stdin with a 2 s timeout and, on each timeout,
re-runs the same `refresh_discovered()` + `scan_paths()` an Enter would.
So a disc the desktop auto-mounts resumes the restore by itself, with
no new mechanism per platform and nothing new to trust.  ENTER and `q`
keep working throughout; the answer is read with `read(2)`, because
`select()` can't see bytes already in a stdio buffer.
`--auto-mount off` restores the Enter-only prompt.

Limits: Linux/macOS only (a Windows console handle can't be
`select()`ed, so the prompt there always waits for Enter), and it does
not help the bare-metal initramfs flow, where nothing mounts a disc
unasked.  Tier-3's Python prompt is unchanged.
Tests: `recovery/tests/test_disc_locator.c` and
`tests/recovery_hardening/test_tier1_auto_mount.py`.

### 4. Snapshot-aware pick list — CLOSED WON'T-FIX (issue #350, 2026-07-16)

Print upfront "this restore needs vols 001, 003, 007 — have them ready"
instead of resolving missing packs reactively.

Resolved as **won't-fix by design** (#350, commit 985c118): the catalog
structurally lacks a snapshot→pack mapping, so the tier-1 pick list
(`catalog.c::lcsas_catalog_print_pending_packs`) is whole-archive **by
design**.  Per-snapshot pick lists remain a tier-3 capability
(`meta/restore_single_drive.py::_build_pick_list` groups packs by volume
per snapshot).  See the #350 discussion for the full rationale.

### Priority order for resuming

- Item 1 (disc_index.txt sidecar) is not being built (#457); item 1 lists
  the evidence that would reopen it.
- Item 2 (volume_info.json mounted-disc id) landed in #456.
- Item 3 (auto-mount detection) landed in #455.
- Item 4 (snapshot-aware pick list) closed won't-fix per #350 — no longer
  in the queue.

---

## Coverage posture

Overall tier-1 (`src/lcsas-restore/*.c`) coverage is **~93.9%** measured
(`make coverage-c`), against a regression floor of **88%**
(`recovery/Makefile` `THRESHOLD ?= 88`; CI runs the same gate at
`THRESHOLD=83`, the floor minus the measured ~5 pt CI-environment delta).

Every uncovered line is line-pinned and categorised in `EXEMPTIONS.md`
(INTRACTABLE / DEFENSIVE / DEFERRED / VOLATILE), enforced as a contract by
`exemptions_check.py`.  Per-file numbers and the threshold rationale live
in `AUDIT.md`; this ledger does not duplicate them.

Aspirational target: 95%.  The path forward (fault-tolerant gcov runtime
patch, EINTR-injection wrapper, AEAD-corruption fixtures, 1M+ file
fixtures) is documented under "Path forward" in `EXEMPTIONS.md`.

---

## On-disc spec drift (durable note for old-disc + new-source pairings)

`recovery/docs/FORMAT.txt` is copied onto every burned disc.  Copies on
discs burned **before 2026-06-13** carry an outdated PATH SAFETY claim.  A
future reader pairing an old disc with newer source should apply these
corrections (T1C-03):

- **Absolute symlink targets.**  Old FORMAT.txt said symlinks with an
  absolute `linktarget` are REJECTED.  The binary has allowed absolute
  targets since issue #187 (rustic/tier-2 parity); the spec was reconciled
  on 2026-06-13 to say absolute targets are restored as-is with NO
  restore-tree containment guarantee.  Only RELATIVE linktargets that
  lexically escape the restore root are rejected.  The code
  (`path.c:lcsas_path_safe_symlink`) never changed — only the doc.
- **NUL in names.**  The NUL-rejection claim was always in the spec but was
  not enforced for names until 2026-06-13
  (`tree.c:decode_path_component`).  Old discs' binaries do not reject
  embedded-NUL names; rebuilt binaries do.
