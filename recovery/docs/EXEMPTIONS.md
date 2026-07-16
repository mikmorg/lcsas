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
> (added in KEY-06) and under the fuzz + sanitize gates, but it is not yet
> line-pinned in the FENCE block — its coverage posture is documented
> narratively in the "lcsas-keyshare (SLIP-0039 combiner)" section at the
> end of this file.

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
catalog.c:157   INTRACTABLE   sqlite3_prepare_v2 on a hardcoded well-formed SELECT cannot fail without malloc inside SQLite (fault-inject cannot reach); warn_schema_skew_once hedges a corrupt/old catalog
catalog.c:158   INTRACTABLE   "
catalog.c:213   INTRACTABLE   print_pending_packs sqlite3_prepare_v2 on a hardcoded well-formed SELECT; same SQLite-internal-malloc constraint as 157
catalog.c:214   INTRACTABLE   "

# disc_locator.c
disc_locator.c:282   DEFERRED   consider_catalog copy_file-failure fallback (open the original); needs the copy to fail (e.g. a dir collision at cache/.locator-catalog.db) [#401]
disc_locator.c:366   DEFERRED   refresh_discovered path-too-long warn; needs a real openable mount_parent name approaching PATH_MAX
disc_locator.c:368   DEFERRED   "
disc_locator.c:457   VOLATILE   copy_file fwrite error path; covered by test_disc_locator RLIMIT_FSIZE fs-full drain when a >=11%-free cache base exists (TMPDIR / /dev/shm / /tmp), uncovered otherwise [FMA-09]
disc_locator.c:458   VOLATILE   "
disc_locator.c:567   VOLATILE   drain_disc fs_critically_full warn; covered via the gated tmpfs harness (LCSAS_TEST_FULL_FS_DIR, test_restore_space_preflight.py) or on hosts whose cache base fs is <10% free [FMA-09]
disc_locator.c:568   VOLATILE   "
disc_locator.c:574   VOLATILE   "
disc_locator.c:576   VOLATILE   "
disc_locator.c:613   DEFENSIVE   drain_disc path-too-long defensive continue (prefix_dir overflow)
disc_locator.c:615   DEFENSIVE   "
disc_locator.c:622   DEFENSIVE   drain_disc path-too-long defensive continue (cache_prefix overflow)
disc_locator.c:624   DEFENSIVE   "
disc_locator.c:635   DEFENSIVE   drain_disc path-too-long defensive continue (src path overflow)
disc_locator.c:637   DEFENSIVE   "
disc_locator.c:642   DEFENSIVE   drain_disc path-too-long defensive continue (dst path overflow)
disc_locator.c:644   DEFENSIVE   "
disc_locator.c:737   DEFERRED   print_prompt catalog-has-pack-but-no-current-volume-mapping; needs a populated catalog fixture
disc_locator.c:739   DEFERRED   print_prompt schema-skew branch (find_pack prepare failed, catalog written by a newer LCSAS); needs a catalog that opens but whose packs query fails [#401]
disc_locator.c:743   DEFERRED   "
disc_locator.c:744   DEFERRED   "
disc_locator.c:745   DEFERRED   "
disc_locator.c:746   DEFERRED   "
disc_locator.c:748   DEFERRED   print_prompt catalog-has-no-record-of-this-pack-hash (fr>0); needs a valid catalog lacking the pack row
disc_locator.c:838   DEFERRED   lcsas_disc_locate_pack cwd-under-meta chdir-to-root fallback; reachable by chdir into a meta subdir before the interactive path (mutates process cwd) [#401]

# lcsas_io.c
lcsas_io.c:22   INTRACTABLE   EINTR retry in lcsas_pread_exact read loop; needs racy signal injection
lcsas_io.c:23   INTRACTABLE   "
lcsas_io.c:30   INTRACTABLE   lcsas_pread_exact unexpected-EOF EIO branch (#222 disc-disconnect classifier); needs a source that returns 0 mid-read - integration-only
lcsas_io.c:31   INTRACTABLE   "
lcsas_io.c:47   INTRACTABLE   EINTR retry in lcsas_write_exact write loop; needs racy signal injection
lcsas_io.c:48   INTRACTABLE   "
lcsas_io.c:82   INTRACTABLE   EINTR retry in lcsas_read_file read loop; needs racy signal injection
lcsas_io.c:83   INTRACTABLE   "

# main.c
main.c:561   INTRACTABLE   main lcsas_mkdir_p ENOSPC/EDQUOT classifier on the --target path; needs filesystem-full tmpfs (integration-only)

# poly1305.c
poly1305.c:146   INTRACTABLE   Final-clamp non-underflow branch: fires when h >= 2^130-5 after accumulation; ~5/2^130 for random messages; requires a chosen-message attack on the MAC accumulator
poly1305.c:147   INTRACTABLE   "
poly1305.c:148   INTRACTABLE   "
poly1305.c:149   INTRACTABLE   "
poly1305.c:150   INTRACTABLE   "

# repo.c
repo.c:216   VOLATILE   keys-dir sort/init blocks; fixture-key readdir order decides which run is cold vs warm (inverse of the 254-256 swap body) - env-dependent, allowed either way
repo.c:217   VOLATILE   "
repo.c:218   VOLATILE   "
repo.c:231   DEFERRED   key count exceeded sanity limit guard; needs >1M key files
repo.c:232   DEFERRED   "
repo.c:233   DEFERRED   "
repo.c:461   DEFENSIVE   strip_v2_prefix 0-byte-plaintext return; decrypt rejects <33B input so pt_len>=1 always -> provably unreachable
repo.c:462   DEFENSIVE   "
repo.c:463   DEFENSIVE   "
repo.c:598   DEFERRED   index count exceeded sanity limit guard; needs >1M index files
repo.c:599   DEFERRED   "
repo.c:600   DEFERRED   "
repo.c:725   DEFENSIVE   load_index pass-2 TOCTOU guard for TOOBIG/ZSTD; pass-1 already fatals on the same static file set, unreachable single-threaded (kept as a guard)
repo.c:726   DEFENSIVE   "
repo.c:729   DEFENSIVE   "
repo.c:731   DEFENSIVE   "
repo.c:733   DEFENSIVE   "
repo.c:735   DEFENSIVE   "
repo.c:744   DEFENSIVE   load_index pass-2 index-too-large (-2); pass-1 parses the same file first with the same cap and fatals -> unreachable
repo.c:747   DEFENSIVE   "
repo.c:750   DEFENSIVE   load_index pass-2 invalid-JSON (<=0); pass-1 parses the same file first and fatals -> unreachable
repo.c:752   DEFENSIVE   "
repo.c:786   INTRACTABLE   blob_index_push realloc fail; malloc fault-inject blocked by the gcov runtime
repo.c:787   INTRACTABLE   "
repo.c:788   INTRACTABLE   "
repo.c:1109   INTRACTABLE   read_blob pread() disc-disconnect EIO/ENXIO classifier; the fstat guard passed, so pread fails only on a real media error / shrink race - hardware/race only
repo.c:1110   INTRACTABLE   "
repo.c:1111   INTRACTABLE   "
repo.c:1112   INTRACTABLE   "
repo.c:1116   INTRACTABLE   "
repo.c:1117   INTRACTABLE   "
repo.c:1120   INTRACTABLE   read_blob pread generic-errno else-branch + shared exit; needs a non-classified errno from pread (hardware/race)
repo.c:1123   INTRACTABLE   "
repo.c:1125   INTRACTABLE   "

# tree.c
tree.c:252   DEFENSIVE   decode_node_mtime decode-string fail (lcsas_json_decode_string<0); fixture mtime fields are always well-formed
tree.c:281   INTRACTABLE   write_blob_sparse write_exact fail on the non-zero prefix; needs a RO mount or write-syscall injection
tree.c:291   INTRACTABLE   write_blob_sparse lseek fail on the sparse-hole seek; SEEK_CUR on a writable regular fd cannot fail without syscall injection
tree.c:295   INTRACTABLE   write_blob_sparse write_exact fail on a short (<4 KiB) zero run; same syscall-injection need as 281 (the branch itself is now covered)
tree.c:323   INTRACTABLE   apply_node_ownership body; guarded by geteuid()!=0 early-return - only reachable when the test process runs as root, which coverage-c never does
tree.c:324   INTRACTABLE   "
tree.c:325   INTRACTABLE   "
tree.c:326   INTRACTABLE   "
tree.c:328   INTRACTABLE   "
tree.c:329   INTRACTABLE   "
tree.c:331   INTRACTABLE   "
tree.c:336   INTRACTABLE   apply_node_ownership lchown wrapper; only reachable when running as root with valid uid/gid fields
tree.c:626   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on lcsas_create_file fail (#221); needs a filesystem-full target - integration-only (test_tier1_target_full.py)
tree.c:627   INTRACTABLE   "
tree.c:628   INTRACTABLE   "
tree.c:633   INTRACTABLE   "
tree.c:689   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on write fail mid-content (#221); same constraint as 626
tree.c:690   INTRACTABLE   "
tree.c:691   INTRACTABLE   "
tree.c:692   INTRACTABLE   "
tree.c:700   INTRACTABLE   "
tree.c:932   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT classifier on mkdir_p fail (#221); a non-ENOSPC mkdir failure is tested, the fs-full branch is integration-only
tree.c:936   INTRACTABLE   "
tree.c:1013   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT/EPERM/EOPNOTSUPP/ENOSYS classifier on symlink() fail (#221/#224); needs a filesystem-full or non-POSIX (FAT32/exFAT/SMB) target - integration-only
tree.c:1014   INTRACTABLE   "
tree.c:1015   INTRACTABLE   "
tree.c:1017   INTRACTABLE   "
tree.c:1021   INTRACTABLE   "
tree.c:1022   INTRACTABLE   "
tree.c:1023   INTRACTABLE   "
tree.c:1032   INTRACTABLE   "
tree.c:1037   INTRACTABLE   tree_restore_recurse generic symlink()-fail catch-all; any symlink() failure needs an environmental condition (RO mount, EACCES) the coverage-c harness does not provide
```<!-- EXEMPTIONS-FENCE-END -->

## Path forward

The categories above are honest about *why* each line is uncovered:

- **`DEFERRED`** entries are TRACTABLE — a test could reach them cheaply — but
  the tests are not yet written.  The bulk of this work is tracked in
  **#401** (convert testable-but-exempt lines to real tests): notably the
  `repo.c` read-blob / decrypt / snapshot error paths.  The C unit harness
  controls the master key (`test_repo.c enc_write`) and the blob metadata
  (`lcsas_blob_loc`), so a decrypt-MAC-fail / hash-mismatch / corrupt-zstd
  input is crafted by corrupting a valid blob — **no cryptographic primitive is
  broken** (the earlier "AEAD prevents crafting" framing was wrong; #383).
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

These files are deliberately not (yet) line-pinned in the FENCE block:
`exemptions_check.py` is `lcsas-restore`-scoped, and widening the
line-by-line contract to a second directory is tracked separately.  The
gates above (fuzz + sanitize + CI trigger + the baseline presence check)
are the active guards for the combiner.
