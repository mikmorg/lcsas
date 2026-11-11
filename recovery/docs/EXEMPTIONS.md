# Tier-1 Coverage Exemptions

This document is **the authoritative list** of every uncovered line
in `recovery/src/lcsas-restore/*.c`.  Every uncov line must appear
here; every line listed here must actually be uncovered.

The `make coverage-c` target enforces both invariants via
`recovery/scripts/exemptions_check.py` — see "Enforcement" below.

> This file is a **live contract**, not a status ledger: it is parsed
> and enforced on every coverage run, so it stays in place.  For the
> consolidated open-vs-resolved audit tracker see
> [`STATUS_LEDGER.md`](STATUS_LEDGER.md); for the audit-gate mechanism
> see [`AUDIT.md`](AUDIT.md).

> **Scope note (KEY-06).** The line-by-line FENCE contract below and its
> enforcement script cover `src/lcsas-restore/*.c` only.  The
> `src/lcsas-keyshare/` combiner is *also* in the coverage-c gcovr report
> (added in KEY-06) and under the fuzz + sanitize gates.  It is not
> line-pinned, by decision (#488): its contract is the narrative posture in
> the "lcsas-keyshare (SLIP-0039 combiner)" section at the end of this file.

## Categories

- `INTRACTABLE` — cannot be tested without infrastructure beyond the
  current harness (signal injection, cryptographic break, etc.).
- `DEFENSIVE` — defensive code path provably unreachable given upstream
  invariants, kept for safety/readability.
- `DEFERRED` — TRACTABLE but cost > value (1M-file fixtures, etc.) and
  documented for a future contributor.
- `VOLATILE` — environment/order-dependent: legitimately covered on some
  hosts and uncovered on others (e.g. a branch whose coverage depends on
  filesystem `readdir` ordering). Stays documented so an uncovered one still
  satisfies the "every uncov line must be listed" rule, but is excluded from
  the "exempted-but-covered → remove it" drift rule (which would otherwise
  fail on whichever host happens to cover it).

## Enforcement

`recovery/scripts/exemptions_check.py` is invoked at the end of
`make coverage-c`.  It parses the `## Exemptions table` section below
and the live `build/coverage.json` and **fails** if:

1. An uncov line is NOT listed in the table (someone added uncovered
   code without updating the doc → either add a test or document why).
2. An entry IN the table is now covered (someone closed a gap and
   forgot to remove the entry → bring the doc back into sync).

This makes the doc a real contract.  Without it, the doc drifts the
moment anyone touches the tier-1 binary.

## Exemptions table

The block between the FENCE markers is parsed by the enforcement
script.  Each row is `file:line  CATEGORY  short-rationale`.
Order: by file, then by line number.  Comments and blank lines are
permitted (ignored by the parser).

<!-- EXEMPTIONS-FENCE-BEGIN -->
```
# catalog.c
catalog.c:317   INTRACTABLE   print_pending_packs sqlite3_prepare_v2 on a hardcoded well-formed SELECT cannot fail without malloc inside SQLite (fault-inject cannot reach) or a catalog lacking the tables, which no --list-pending-packs test supplies
catalog.c:318   INTRACTABLE   "

# disc_locator.c
disc_locator.c:984   DEFENSIVE   drain_walk path-too-long defensive continue (prefix_dir overflow)
disc_locator.c:986   DEFENSIVE   "
disc_locator.c:993   DEFENSIVE   drain_walk path-too-long defensive continue (cache_prefix overflow)
disc_locator.c:995   DEFENSIVE   "
disc_locator.c:1006  DEFENSIVE   drain_walk path-too-long defensive continue (src path overflow)
disc_locator.c:1008  DEFENSIVE   "
disc_locator.c:1013  DEFENSIVE   drain_walk path-too-long defensive continue (dst path overflow)
disc_locator.c:1015  DEFENSIVE   "
disc_locator.c:1101   VOLATILE   drain_disc no-room warn (lcsas_drain_fits, #634); covered via the gated tmpfs harness (LCSAS_TEST_FULL_FS_DIR, test_restore_space_preflight.py) or on hosts whose cache fs lacks room for a test disc plus the margin [FMA-09]
disc_locator.c:1102   VOLATILE   "
disc_locator.c:1110   VOLATILE   "
disc_locator.c:1111   VOLATILE   "
disc_locator.c:1113   VOLATILE   "

# lcsas_io.c
lcsas_io.c:22   INTRACTABLE   EINTR retry in lcsas_pread_exact read loop; needs racy signal injection
lcsas_io.c:23   INTRACTABLE   "
lcsas_io.c:30   INTRACTABLE   lcsas_pread_exact unexpected-EOF EIO branch (#222 disc-disconnect classifier); needs a source that returns 0 mid-read - integration-only
lcsas_io.c:31   INTRACTABLE   "
lcsas_io.c:47   INTRACTABLE   EINTR retry in lcsas_write_exact write loop; needs racy signal injection
lcsas_io.c:48   INTRACTABLE   "

# main.c
main.c:668   INTRACTABLE   main lcsas_mkdir_p ENOSPC/EDQUOT classifier on the --target path; needs filesystem-full tmpfs (integration-only)

# poly1305.c
poly1305.c:146   INTRACTABLE   Final-clamp non-underflow branch: fires when h >= 2^130-5 after accumulation; ~5/2^130 for random messages; requires a chosen-message attack on the MAC accumulator
poly1305.c:147   INTRACTABLE   "
poly1305.c:148   INTRACTABLE   "
poly1305.c:149   INTRACTABLE   "
poly1305.c:150   INTRACTABLE   "

# repo.c
repo.c:259   DEFERRED   key count exceeded sanity limit guard; needs >1M key files
repo.c:260   DEFERRED   "
repo.c:261   DEFERRED   "
repo.c:282   VOLATILE   keys-dir name-sort swap body; runs only when readdir returns the fixture's key files out of order, which varies with the filesystem's directory-hash seed (one CI runner covers it, another doesn't) - env-dependent, allowed either way
repo.c:283   VOLATILE   "
repo.c:284   VOLATILE   "
repo.c:489   DEFENSIVE   strip_v2_prefix 0-byte-plaintext return; decrypt rejects <33B input so pt_len>=1 always -> provably unreachable
repo.c:490   DEFENSIVE   "
repo.c:491   DEFENSIVE   "
repo.c:626   DEFERRED   index count exceeded sanity limit guard; needs >1M index files
repo.c:627   DEFERRED   "
repo.c:628   DEFERRED   "
repo.c:761   DEFENSIVE   load_index pass-2 TOCTOU guard for TOOBIG/ZSTD; pass-1 already fatals on the same static file set, unreachable single-threaded (kept as a guard)
repo.c:762   DEFENSIVE   "
repo.c:765   DEFENSIVE   "
repo.c:767   DEFENSIVE   "
repo.c:769   DEFENSIVE   "
repo.c:771   DEFENSIVE   "
repo.c:780   DEFENSIVE   load_index pass-2 index-too-large (-2); pass-1 parses the same file first with the same cap and fatals -> unreachable
repo.c:783   DEFENSIVE   "
repo.c:786   DEFENSIVE   load_index pass-2 invalid-JSON (<=0); pass-1 parses the same file first and fatals -> unreachable
repo.c:788   DEFENSIVE   "
repo.c:822   INTRACTABLE   blob_index_push realloc fail; malloc fault-inject blocked by the gcov runtime
repo.c:823   INTRACTABLE   "
repo.c:824   INTRACTABLE   "
repo.c:1181   INTRACTABLE   read_blob pread() disc-disconnect EIO/ENXIO classifier; the fstat guard passed, so pread fails only on a real media error / shrink race - hardware/race only
repo.c:1182   INTRACTABLE   "
repo.c:1183   INTRACTABLE   "
repo.c:1184   INTRACTABLE   "
repo.c:1188   INTRACTABLE   "
repo.c:1189   INTRACTABLE   "
repo.c:1192   INTRACTABLE   read_blob pread generic-errno else-branch + shared exit; needs a non-classified errno from pread (hardware/race)
repo.c:1195   INTRACTABLE   "
repo.c:1197   INTRACTABLE   "

# tree.c
tree.c:292   DEFENSIVE   decode_node_mtime decode-string fail (lcsas_json_decode_string<0); fixture mtime fields are always well-formed
tree.c:321   INTRACTABLE   write_blob_sparse write_exact fail on the non-zero prefix; needs a RO mount or write-syscall injection
tree.c:331   INTRACTABLE   write_blob_sparse lseek fail on the sparse-hole seek; SEEK_CUR on a writable regular fd cannot fail without syscall injection
tree.c:335   INTRACTABLE   write_blob_sparse write_exact fail on a short (<4 KiB) zero run; same syscall-injection need as 281 (the branch itself is now covered)
tree.c:363   INTRACTABLE   apply_node_ownership body; guarded by geteuid()!=0 early-return - only reachable when the test process runs as root, which coverage-c never does
tree.c:364   INTRACTABLE   "
tree.c:365   INTRACTABLE   "
tree.c:366   INTRACTABLE   "
tree.c:368   INTRACTABLE   "
tree.c:369   INTRACTABLE   "
tree.c:371   INTRACTABLE   "
tree.c:376   INTRACTABLE   apply_node_ownership lchown wrapper; only reachable when running as root with valid uid/gid fields
tree.c:698   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on lcsas_create_file fail (#221); needs a filesystem-full target - integration-only (test_tier1_target_full.py)
tree.c:699   INTRACTABLE   "
tree.c:700   INTRACTABLE   "
tree.c:705   INTRACTABLE   "
tree.c:761   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on write fail mid-content (#221); same constraint as 626
tree.c:762   INTRACTABLE   "
tree.c:763   INTRACTABLE   "
tree.c:764   INTRACTABLE   "
tree.c:772   INTRACTABLE   "
tree.c:1016   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT classifier on mkdir_p fail (#221); a non-ENOSPC mkdir failure is tested, the fs-full branch is integration-only
tree.c:1020   INTRACTABLE   "
tree.c:1112   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT/EPERM/EOPNOTSUPP/ENOSYS classifier on symlink() fail (#221/#224); needs a filesystem-full or non-POSIX (FAT32/exFAT/SMB) target - integration-only
tree.c:1113   INTRACTABLE   "
tree.c:1114   INTRACTABLE   "
tree.c:1116   INTRACTABLE   "
tree.c:1120   INTRACTABLE   "
tree.c:1121   INTRACTABLE   "
tree.c:1122   INTRACTABLE   "
tree.c:1131   INTRACTABLE   "
tree.c:1136   INTRACTABLE   tree_restore_recurse generic symlink()-fail catch-all; any symlink() failure needs an environmental condition (RO mount, EACCES) the coverage-c harness does not provide
```<!-- EXEMPTIONS-FENCE-END -->

## Path forward

The categories above are honest about *why* each line is uncovered:

- **`DEFERRED`** entries are TRACTABLE but cost more than they are worth.
  Since #488 the only ones left are the two `repo.c` sanity limits (more
  than 1M key files, more than 1M index files): reaching them needs a
  million-entry fixture, and that stays deferred by design.  Every
  cheaply-testable row has been converted: **#401** turned 50 of them into
  real tests (ba3a93d), and #488 the last eleven `disc_locator.c` rows
  (`test_disc_locator.c`: the catalog-copy fallback, the path-too-long warn,
  the three print_prompt catalog branches, and the cwd-under-meta chdir).
  The C unit harness controls the master key (`test_repo.c enc_write`) and
  the blob metadata (`lcsas_blob_loc`), so a decrypt-MAC-fail /
  hash-mismatch / corrupt-zstd input is crafted by corrupting a valid blob:
  **no cryptographic primitive is broken** (the earlier "AEAD prevents
  crafting" framing was wrong; #383).
- **`INTRACTABLE`** entries genuinely need infrastructure beyond the unit
  harness: the fault-tolerant gcov runtime cannot reach some `malloc`-failure
  branches (`repo.c blob_index_push`); `EINTR` retries in `lcsas_io.c` need
  racy signal injection; the `poly1305` final-clamp needs a chosen-message
  attack; the ENOSPC/EDQUOT and disc-disconnect classifiers need a
  filesystem-full or hardware-eject target (exercised only by the
  integration-tier tests, not `coverage-c`).
- **`DEFENSIVE`** entries are provably unreachable given upstream invariants
  (kept as guards); **`VOLATILE`** entries are environment/order-dependent
  (`readdir` order, free-space) and covered on some hosts, uncovered on others.

Reducing the `INTRACTABLE` set further would need a fault-tolerant gcov runtime
patch (malloc-failure branches), an EINTR-injection wrapper (`lcsas_io.c`), or
running the integration-tier filesystem/disc-fault fixtures inside `coverage-c`.

## lcsas-keyshare (SLIP-0039 combiner)

The combiner (`recovery/src/lcsas-keyshare/`) recovers an LCSAS repository
password from SLIP-0039 mnemonic shares with no python3 dependency.  It
parses untrusted, heir-typed text (mnemonics and whole printed share-card
files) on the 50-year critical path, so KEY-06 brought it under the same
tier-1 gates as `lcsas-restore`:

- **Coverage** — `make coverage-c` now passes a second
  `--filter 'src/lcsas-keyshare/.*'` to gcovr, so `slip39.c` and `main.c`
  appear in `build/coverage.json` / the HTML report.
  `tests/recovery_hardening/test_tier1_coverage_baseline.py` asserts both
  files stay in the report, guarding against the filter silently
  regressing.
- **Fuzzing** — `fuzz/fuzz_slip39_mnemonic.c` exercises
  `lcsas_keyshare_extract`, `lcsas_keyshare_check_share`, and
  `lcsas_keyshare_recover_password` (passphrase matrix).  Run via
  `make -C recovery fuzz-keyshare-smoke` and included in the `fuzz-smoke`
  aggregate, so `make audit-gate` (and audit-gate CI) covers it.  Seed
  corpus: the 45 official SLIP-0039 vectors + real printed card files.
- **Sanitize** — `test_keyshare` is in `TEST_BINS`, so the `make
  -C recovery sanitize` ASan/UBSan/LSan run already builds and runs it
  (it formalises the one-off ASan check noted in PLAN.md C5.3).
- **CI trigger** — `.github/workflows/audit-gate.yml` lists
  `recovery/src/lcsas-keyshare/**` in both the push and pull_request path
  filters, so a PR touching only combiner source runs the gate.

Coverage notes (NOT FENCE-enforced — narrative only):

- `wordlist.c` is pure data (the 1024-word const table); gcov reports it
  as zero executable lines, so it neither needs an exclusion nor drags the
  line percentage.
- `slip39.c` carries the algorithm; its uncovered lines are the same class
  of cryptographic-failure / malloc-failure branches that the
  `lcsas-restore` table marks INTRACTABLE (a share set that decrypts but
  fails the integrity digest cannot be crafted without breaking the
  primitive).
- `main.c` is the CLI shell.  coverage-c drives it (Step 3d: real cards
  via `lcsas key split` for the success path, plus usage/error
  invocations); the binding correctness proof is the integration test
  `tests/integration/test_keyshare_binary_cards.py`, which subprocesses
  the *committed* binary against real card artifacts.  A source-only
  checkout without python3 skips Step 3d cleanly (non-fatal), so main.c
  may read low there — that is environment-dependent, not a regression.

These files are deliberately not line-pinned in the FENCE block, and that
is a decision, not a backlog item (#488).  The combiner is small and
self-contained, fuzzed (`fuzz_slip39_mnemonic`) and sanitized, and its
correctness proof is the byte-exact integration test against real cards
(`tests/integration/test_keyshare_binary_cards.py`).  Widening
`exemptions_check.py` to a second directory would add upkeep without
guarding anything those don't.  The gates above (fuzz + sanitize + CI
trigger + the baseline presence check) are the contract for the combiner.
