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

### 1. disc_index.txt sidecar (resilience-only redundancy) — DEFERRED (P4)

A plain-text `<sha256>  <label>  <uuid>` pack→volume map written at burn
time, read by a C-side `disc_index.{c,h}` as a *tertiary* fallback when
SQLite cannot open `catalog.db`.  catalog.db is bundled on every data disc
and the C reader queries it directly; the binary statically links the
SQLite amalgamation, so the "sqlite refuses to open" surface is small.
**Not started** (no `disc_index` symbol anywhere in-tree as of 2026-06).
Prioritise only if field evidence shows catalog.db corruption events.

*Effort:* Python writer ~30 LOC; C reader + locator wire-in ~150 LOC;
test ~50 LOC.

### 2. mounted-disc identification via volume_info.json — DEFERRED (P3)

`volume_info.json` is already written to every staging root
(`staging/metadata.py::write_volume_info`) and is consumed by the *Python*
restore path (`meta/restore_single_drive.py::verify_disc`).  The **C**
locator does not yet read it on a new mount to print
`[lcsas-restore] mounted vol-XXX (…)`.  Low effort, low payoff; most useful
once auto-mount (item 3) lands.  Uses the existing JSON — no new on-disc
sidecar needed.

*Effort:* C parser via existing `json_q` ~80 LOC; locator wire-in ~30 LOC;
test ~60 LOC.

### 3. Auto-mount detection (eliminate the Enter press) — DEFERRED (P2)

Notice a new mount and skip the manual Enter.  Requires inotify on
`/media/$USER` + `/Volumes` (Linux/macOS), or heavier dbus/udisks2/
DiskArbitration, or `/proc/mounts` polling.  **Not started** (no `inotify`
or `--auto-mount` handling in `recovery/src/`).  The Enter press costs ~1 s
per swap (<1 min across a 50-disc archive), and it does not help the
bare-metal initramfs flow (no auto-mount daemon there), so it is not urgent.

*Effort:* Linux inotify ~120 LOC + ~80 LOC test; macOS kevent ~100 LOC;
Windows polling ~30 LOC.

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

- **P2:** Auto-mount detection (item 3) — quality-of-life; Linux/macOS only.
- **P3:** volume_info.json wire-up (item 2) — low effort, low payoff;
  useful once auto-mount lands.
- **P4:** disc_index.txt sidecar (item 1) — pure defence in depth; defer
  indefinitely unless field evidence demands it.
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
