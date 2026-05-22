# Tier-1 Coverage Exemptions

This document is **the authoritative list** of every uncovered line
in `recovery/src/lcsas-restore/*.c`.  Every uncov line must appear
here; every line listed here must actually be uncovered.

The `make coverage-c` target enforces both invariants via
`recovery/scripts/exemptions_check.py` — see "Enforcement" below.

## Categories

- `INTRACTABLE` — cannot be tested without infrastructure beyond the
  current harness (signal injection, cryptographic break, etc.).
- `DEFENSIVE` — defensive code path provably unreachable given upstream
  invariants, kept for safety/readability.
- `DEFERRED` — TRACTABLE but cost > value (1M-file fixtures, etc.) and
  documented for a future contributor.

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
catalog.c:156   INTRACTABLE   sqlite3_prepare_v2 on a hardcoded well-formed SELECT cannot fail without malloc inside SQLite (which fault-inject cannot reach)
catalog.c:157   INTRACTABLE   "                                                                                                                            "

# disc_locator.c
disc_locator.c:135   INTRACTABLE   mkdir_p race: another process must create-the-target between our mkdir() and stat(); not unit-testable
disc_locator.c:138   INTRACTABLE   same as 135
disc_locator.c:178   DEFERRED      path_under prefix-child branch ("path begins with meta + '/'"); reachable via a deeper search-path layout but cheaper to document
disc_locator.c:198   DEFERRED      push_discovered dedup against existing search_paths; reachable with a mount_parent==search_paths fixture
disc_locator.c:243   DEFERRED      consider_catalog cache_dir branch; needs cache_dir + discovered mount with catalog.db
disc_locator.c:245   DEFERRED      "                                                                                                                            "
disc_locator.c:247   DEFERRED      "                                                                                                                            "
disc_locator.c:248   DEFERRED      "                                                                                                                            "
disc_locator.c:251   DEFERRED      "                                                                                                                            "
disc_locator.c:253   DEFERRED      "                                                                                                                            "
disc_locator.c:329   DEFERRED      refresh_discovered "path too long" warn — needs mount_parent name approaching PATH_MAX
disc_locator.c:331   DEFERRED      "                                                                                                                            "
disc_locator.c:336   DEFERRED      "                                                                                                                            "
disc_locator.c:418   DEFERRED      copy_file fwrite error path; fault-inject ENOSPC or full-fs needed
disc_locator.c:419   DEFERRED      "                                                                                                                            "
disc_locator.c:528   DEFERRED      drain_disc fs_critically_full warn; needs tmpfs with <10% free
disc_locator.c:529   DEFERRED      "                                                                                                                            "
disc_locator.c:535   DEFERRED      "                                                                                                                            "
disc_locator.c:537   DEFERRED      "                                                                                                                            "
disc_locator.c:546   DEFENSIVE     drain_disc "path too long" defensive continue (4 KiB+ prefix_dir overflow)
disc_locator.c:551   DEFENSIVE     "                                                                                                                            "
disc_locator.c:552   DEFENSIVE     "                                                                                                                            "
disc_locator.c:574   DEFENSIVE     drain_disc "path too long" defensive continue (cache_prefix overflow)
disc_locator.c:576   DEFENSIVE     "                                                                                                                            "
disc_locator.c:583   DEFENSIVE     drain_disc "path too long" defensive continue (src path overflow)
disc_locator.c:585   DEFENSIVE     "                                                                                                                            "
disc_locator.c:596   DEFENSIVE     drain_disc "path too long" defensive continue (dst path overflow)
disc_locator.c:598   DEFENSIVE     "                                                                                                                            "
disc_locator.c:603   DEFENSIVE     drain_disc skip-if-already-cached branch (stat success on dst)
disc_locator.c:605   DEFENSIVE     drain_disc skip-non-regular-source branch
disc_locator.c:632   DEFENSIVE     drain_disc chunk_limit_reached early-exit (limited by LCSAS_DRAIN_CHUNK_PACKS=1 in tests, already covered)
disc_locator.c:697   DEFERRED      print_prompt "catalog has the pack but no current volume mapping" — needs populated catalog
disc_locator.c:700   DEFERRED      print_prompt "catalog has no record of this pack hash" — needs populated catalog
disc_locator.c:790   DEFERRED      lcsas_disc_locate_pack interactive prompt-loop "still not found" branch — needs 2+ prompt iterations

# lcsas_io.c
lcsas_io.c:22   INTRACTABLE   EINTR retry in lcsas_pread_exact read loop; needs racy signal injection
lcsas_io.c:23   INTRACTABLE   same as 22 (error-path return)
lcsas_io.c:40   INTRACTABLE   EINTR retry in lcsas_write_exact write loop
lcsas_io.c:41   INTRACTABLE   same as 40
lcsas_io.c:75   INTRACTABLE   EINTR retry in lcsas_read_file read loop
lcsas_io.c:76   INTRACTABLE   same as 75

# main.c
main.c:437   INTRACTABLE   "ERROR: snapshot load failed" — load_snapshots returns 0 even on per-file decrypt failures; only -1 on early calloc fail (fault-inject blocked)
main.c:438   INTRACTABLE   goto out after 437

# poly1305.c
poly1305.c:146   INTRACTABLE   Final-clamp non-underflow branch: fires when h ≥ 2^130 − 5 after accumulation; for random messages probability ≈ 5/2^130; requires chosen-message attack on MAC accumulator
poly1305.c:147   INTRACTABLE   same as 146
poly1305.c:148   INTRACTABLE   same as 146
poly1305.c:149   INTRACTABLE   same as 146
poly1305.c:150   INTRACTABLE   same as 146

# repo.c
repo.c:193    DEFERRED      "key count exceeded sanity limit" warn; needs >1M key files
repo.c:194    DEFERRED      closedir + goto out after 193
repo.c:195    DEFERRED      "                                                                                                                            "
repo.c:216    DEFENSIVE     keys-name sort-swap; the committed fixture's 3 keys are in readdir order on ext4; forcing a swap couples to filesystem hash-ordering
repo.c:217    DEFENSIVE     "                                                                                                                            "
repo.c:218    DEFENSIVE     "                                                                                                                            "
repo.c:369    INTRACTABLE   decrypt_repo_file decrypt-MAC fail; requires a ciphertext that decrypts but fails MAC (AEAD prevents crafting without breaking the primitive)
repo.c:370    INTRACTABLE   return NULL after 369
repo.c:377    INTRACTABLE   strip_v2_prefix returns -1 (decrypted to 0 bytes); 32 bytes of AEAD overhead means a 32-byte ciphertext produces 0-byte plaintext — would have to craft
repo.c:378    INTRACTABLE   return NULL after 377
repo.c:395    INTRACTABLE   zstd decompress fail AFTER probe succeeded; needs a frame whose header parses but body is corrupt
repo.c:396    INTRACTABLE   "                                                                                                                            "
repo.c:397    INTRACTABLE   "                                                                                                                            "
repo.c:494    DEFERRED      "index count exceeded sanity limit" warn; needs >1M index files
repo.c:495    DEFERRED      closedir + goto out after 494
repo.c:496    DEFERRED      "                                                                                                                            "
repo.c:499    DEFERRED      index names realloc growth past 2048 entries; needs 2049+ index files (the petabyte fixture exercises at integration time)
repo.c:500    DEFERRED      "                                                                                                                            "
repo.c:501    DEFERRED      "                                                                                                                            "
repo.c:502    DEFERRED      "                                                                                                                            "
repo.c:503    DEFERRED      "                                                                                                                            "
repo.c:609    INTRACTABLE   blob_index_push realloc fail; fault-inject blocked by gcov-runtime malloc-intolerance
repo.c:610    INTRACTABLE   "                                                                                                                            "
repo.c:842    INTRACTABLE   read_blob malloc fail before pread; gcov-fault-inject blocked
repo.c:849    INTRACTABLE   read_blob plaintext malloc fail; gcov-fault-inject blocked
repo.c:867    INTRACTABLE   read_blob zstd probe returned <=0 or > 256 MB; needs crafted blob (AEAD-protected)
repo.c:868    INTRACTABLE   "                                                                                                                            "
repo.c:869    INTRACTABLE   "                                                                                                                            "
repo.c:875    INTRACTABLE   read_blob zstd decode fail after probe; needs corrupt-mid-frame zstd (AEAD-protected)
repo.c:876    INTRACTABLE   "                                                                                                                            "
repo.c:877    INTRACTABLE   "                                                                                                                            "
repo.c:886    INTRACTABLE   read_blob hash mismatch; needs crafted ciphertext that decrypts but verify-mismatches
repo.c:887    INTRACTABLE   "                                                                                                                            "
repo.c:888    INTRACTABLE   "                                                                                                                            "

# scrypt.c — 100% covered by fault-tolerant gcov fault-inject sweep (Phase 13d)

# tree.c
tree.c:160   INTRACTABLE   write_exact fail mid-file-write; needs RO mount or syscall injection
tree.c:299   INTRACTABLE   symlink() syscall fail; needs RO fs or pre-existing target with EXDEV
```
<!-- EXEMPTIONS-FENCE-END -->

## Path forward

Reducing this list further requires:

1. **Fault-tolerant gcov runtime patch** — unlocks all `INTRACTABLE` malloc-failure entries (scrypt.c, repo.c:609-610, 842, 849).  Phase 13d delivers a SIGSEGV-handler shim that calls `__gcov_dump()` before exit.
2. **EINTR-injection wrapper** — covers lcsas_io.c 6 lines.  Risky to wire into the normal test path.
3. **AEAD-corruption fixtures** — would require breaking the cryptographic primitive to craft inputs that decrypt-but-verify-fail or corrupt-mid-zstd.  Genuinely not testable.
4. **1M+ file fixtures** — the petabyte-scale stress test (`LCSAS_PETABYTE=1`) exercises some at integration time.  For coverage-c we'd need the same scale during the standard build (~10s per million-file readdir).
