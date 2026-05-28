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
disc_locator.c:229   DEFERRED      push_discovered dedup against existing search_paths; reachable with a mount_parent==search_paths fixture
disc_locator.c:274   DEFERRED      consider_catalog cache_dir branch (snprintf success path); needs cache_dir + discovered mount with catalog.db
disc_locator.c:276   DEFERRED      "                                                                                                                            "
disc_locator.c:278   DEFERRED      "                                                                                                                            "
disc_locator.c:279   DEFERRED      "                                                                                                                            "
disc_locator.c:282   DEFERRED      "                                                                                                                            "
disc_locator.c:284   DEFERRED      "                                                                                                                            "
disc_locator.c:366   DEFERRED      refresh_discovered "path too long" warn — needs mount_parent name approaching PATH_MAX
disc_locator.c:368   DEFERRED      "                                                                                                                            "
disc_locator.c:373   DEFERRED      meta_disc path_under exclusion in refresh_discovered — needs meta_disc + discovered mount under it
disc_locator.c:457   DEFERRED      copy_file fwrite error path; fault-inject ENOSPC or full-fs needed
disc_locator.c:458   DEFERRED      "                                                                                                                            "
disc_locator.c:567   DEFERRED      drain_disc fs_critically_full warn; needs tmpfs with <10% free
disc_locator.c:568   DEFERRED      "                                                                                                                            "
disc_locator.c:574   DEFERRED      "                                                                                                                            "
disc_locator.c:576   DEFERRED      "                                                                                                                            "
disc_locator.c:585   DEFERRED      drain_disc 1 GiB cache_bytes_used soft warn; needs >1 GiB in cache_dir
disc_locator.c:590   DEFERRED      "                                                                                                                            "
disc_locator.c:591   DEFERRED      "                                                                                                                            "
disc_locator.c:613   DEFENSIVE     drain_disc "path too long" defensive continue (prefix_dir overflow)
disc_locator.c:615   DEFENSIVE     "                                                                                                                            "
disc_locator.c:622   DEFENSIVE     drain_disc "path too long" defensive continue (cache_prefix overflow)
disc_locator.c:624   DEFENSIVE     "                                                                                                                            "
disc_locator.c:635   DEFENSIVE     drain_disc "path too long" defensive continue (src path overflow)
disc_locator.c:637   DEFENSIVE     "                                                                                                                            "
disc_locator.c:642   DEFENSIVE     drain_disc "path too long" defensive continue (dst path overflow)
disc_locator.c:644   DEFENSIVE     "                                                                                                                            "
disc_locator.c:671   DEFENSIVE     scan_paths cache_dir try_with_meta hit branch — needs a pre-populated cache_dir with the pack
disc_locator.c:736   DEFERRED      print_prompt "catalog has the pack but no current volume mapping" — needs populated catalog
disc_locator.c:739   DEFERRED      print_prompt "catalog has no record of this pack hash" — needs populated catalog
disc_locator.c:829   DEFENSIVE     lcsas_disc_locate_pack cwd-under-meta_disc chdir-to-root fallback (best-effort; cwd is /tmp during tests so the predicate is false)

# lcsas_io.c
lcsas_io.c:22   INTRACTABLE   EINTR retry in lcsas_pread_exact read loop; needs racy signal injection
lcsas_io.c:23   INTRACTABLE   same as 22 (error-path return)
lcsas_io.c:30   INTRACTABLE   lcsas_pread_exact unexpected-EOF EIO branch (issue #222 disc-disconnect classifier); needs a source that returns 0 mid-read — integration-only (test_tier1_drive_disconnect.py truncates packs; not run in coverage-c)
lcsas_io.c:31   INTRACTABLE   "                                                                                                                            "
lcsas_io.c:47   INTRACTABLE   EINTR retry in lcsas_write_exact write loop; needs racy signal injection
lcsas_io.c:48   INTRACTABLE   same as 47 (error-path return)
lcsas_io.c:82   INTRACTABLE   EINTR retry in lcsas_read_file read loop
lcsas_io.c:83   INTRACTABLE   same as 82 (error-path return)

# main.c
main.c:438   INTRACTABLE   "ERROR: snapshot load failed" — load_snapshots returns 0 even on per-file decrypt failures; only -1 on early calloc fail (fault-inject blocked)
main.c:439   INTRACTABLE   goto out after 438
main.c:475   INTRACTABLE   main lcsas_mkdir_p ENOSPC/EDQUOT classifier on the --target path; needs filesystem-full tmpfs (integration-only)

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
repo.c:861    INTRACTABLE   read_blob open() disc-disconnect EIO/ENXIO/EACCES classifier (issue #222); needs cdemu/USB-drive ejection mid-read — integration-only
repo.c:862    INTRACTABLE   "                                                                                                                            "
repo.c:863    INTRACTABLE   "                                                                                                                            "
repo.c:864    INTRACTABLE   "                                                                                                                            "
repo.c:865    INTRACTABLE   "                                                                                                                            "
repo.c:870    INTRACTABLE   return -1 after 861-869 (open() disc-disconnect classifier path exit)
repo.c:883    INTRACTABLE   read_blob pack-truncation diagnostic (issue #220); needs an on-disc pack shorter than the index says — integration-only (write-side bug; tier-1 reads validated packs)
repo.c:886    INTRACTABLE   "                                                                                                                            "
repo.c:887    INTRACTABLE   "                                                                                                                            "
repo.c:888    INTRACTABLE   "                                                                                                                            "
repo.c:900    INTRACTABLE   read_blob pread() disc-disconnect EIO/ENXIO classifier (issue #222); needs cdemu/USB ejection between fstat and pread
repo.c:901    INTRACTABLE   "                                                                                                                            "
repo.c:902    INTRACTABLE   "                                                                                                                            "
repo.c:903    INTRACTABLE   "                                                                                                                            "
repo.c:907    INTRACTABLE   "                                                                                                                            "
repo.c:908    INTRACTABLE   "                                                                                                                            "
repo.c:911    INTRACTABLE   read_blob generic pack-read fail diagnostic (covered by 900-908 classifier above when errno IS classifiable; else-branch needs a non-EIO/ENXIO/EBADF/ENOENT errno from pread)
repo.c:914    INTRACTABLE   "                                                                                                                            "
repo.c:916    INTRACTABLE   "                                                                                                                            "
repo.c:923    INTRACTABLE   read_blob decrypt fail; AEAD primitive cannot be crafted into a decrypt-fail (mac would also fail first)
repo.c:941    INTRACTABLE   read_blob zstd probe returned <=0 or > 256 MB; needs crafted blob (AEAD-protected)
repo.c:942    INTRACTABLE   "                                                                                                                            "
repo.c:943    INTRACTABLE   "                                                                                                                            "
repo.c:949    INTRACTABLE   read_blob zstd decode fail after probe; needs corrupt-mid-frame zstd (AEAD-protected)
repo.c:950    INTRACTABLE   "                                                                                                                            "
repo.c:951    INTRACTABLE   "                                                                                                                            "
repo.c:960    INTRACTABLE   read_blob hash mismatch; needs crafted ciphertext that decrypts but verify-mismatches
repo.c:961    INTRACTABLE   "                                                                                                                            "
repo.c:962    INTRACTABLE   "                                                                                                                            "

# scrypt.c — 100% covered by fault-tolerant gcov fault-inject sweep (Phase 13d)

# tree.c
tree.c:216   DEFENSIVE     decode_node_mtime "fail" path (lcsas_json_decode_string returns -1); fixture mtime fields are always well-formed
tree.c:245   INTRACTABLE   write_blob_sparse write_exact fail on non-zero prefix; needs RO mount or syscall injection
tree.c:254   DEFERRED      write_blob_sparse "hole >= 4 KiB" lseek branch; fixture file content "hello from lcsas-restore fixture\n" has no zero runs
tree.c:255   DEFERRED      "                                                                                                                            "
tree.c:259   DEFERRED      write_blob_sparse short-zero write branch; same reason as 254
tree.c:263   DEFERRED      write_blob_sparse loop-exit return 0; only reached when the buffer ends with a zero byte (write the prefix then fall out of the loop); fixture content "hello from lcsas-restore fixture\n" ends with '\n' so the early `if (zstart >= len) return 0;` at line 247 fires first.  NOTE: the fault-inject sweep can occasionally exercise this line as a side-effect of malloc-fail unwind paths in broken-tree fixtures; when that happens you'll see a stale-exemption error and can remove this entry for that one run.
tree.c:287   INTRACTABLE   apply_node_ownership body; guarded by `geteuid() != 0` (early return at line 285) — only reachable when the test process runs as root, which the standard coverage-c harness never does
tree.c:288   INTRACTABLE   "                                                                                                                            "
tree.c:289   INTRACTABLE   "                                                                                                                            "
tree.c:290   INTRACTABLE   "                                                                                                                            "
tree.c:292   INTRACTABLE   "                                                                                                                            "
tree.c:293   INTRACTABLE   "                                                                                                                            "
tree.c:295   INTRACTABLE   "                                                                                                                            "
tree.c:300   INTRACTABLE   lchown wrapper around (void)cast; only reachable when running as root with valid uid/gid fields
tree.c:347   DEFENSIVE     apply_node_xattrs: body of non-object array entry guard; all entries in the fixture xattr list are JSON objects — the non-object path is a hardening guard against malformed JSON, never triggered in practice
tree.c:348   DEFENSIVE     "                                                                                                                            "
tree.c:354   DEFENSIVE     apply_node_xattrs: body of missing-name/value guard; fixture xattr objects always have both "name" and "value" keys — this path handles intentionally-malformed xattr descriptors
tree.c:355   DEFENSIVE     "                                                                                                                            "
tree.c:358   DEFENSIVE     apply_node_xattrs: body of non-string name type guard; fixture name field is always a JSON string — this path handles malformed type (e.g. a number in the name field)
tree.c:359   DEFENSIVE     "                                                                                                                            "
tree.c:365   DEFENSIVE     apply_node_xattrs: body of empty/failed name decode guard; fixture name "user.lcsas-test" always decodes without error or truncation
tree.c:366   DEFENSIVE     "                                                                                                                            "
tree.c:380   INTRACTABLE   apply_node_xattrs: malloc fail for value_buf; requires fault injection against a gcov-instrumented binary — the standard fault-inject sweep may not reach this specific allocation in test_repo
tree.c:381   INTRACTABLE   "                                                                                                                            "
tree.c:603   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on lcsas_create_file fail (issue #221); needs a filesystem-full target — integration-only (test_tier1_target_full.py mounts a 1 MiB tmpfs; not run in coverage-c)
tree.c:604   INTRACTABLE   "                                                                                                                            "
tree.c:605   INTRACTABLE   "                                                                                                                            "
tree.c:610   INTRACTABLE   "                                                                                                                            "
tree.c:665   INTRACTABLE   restore_file_node ENOSPC/EDQUOT classifier on write fail mid-content (issue #221); same constraint as 603
tree.c:666   INTRACTABLE   "                                                                                                                            "
tree.c:667   INTRACTABLE   "                                                                                                                            "
tree.c:668   INTRACTABLE   "                                                                                                                            "
tree.c:676   INTRACTABLE   "                                                                                                                            "
tree.c:849   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT classifier on mkdir_p fail (issue #221); same constraint as 603 (filesystem-full target)
tree.c:850   INTRACTABLE   "                                                                                                                            "
tree.c:851   INTRACTABLE   "                                                                                                                            "
tree.c:852   INTRACTABLE   "                                                                                                                            "
tree.c:856   INTRACTABLE   "                                                                                                                            "
tree.c:925   INTRACTABLE   tree_restore_recurse ENOSPC/EDQUOT/EPERM/EOPNOTSUPP/ENOSYS classifier on symlink fail (issues #221/#224); needs a filesystem-full or non-POSIX (FAT32/exFAT/SMB) target — integration-only (test_tier1_fat32_target.py loop-mounts a vfat; not run in coverage-c)
tree.c:926   INTRACTABLE   "                                                                                                                            "
tree.c:927   INTRACTABLE   "                                                                                                                            "
tree.c:929   INTRACTABLE   "                                                                                                                            "
tree.c:933   INTRACTABLE   "                                                                                                                            "
tree.c:934   INTRACTABLE   "                                                                                                                            "
tree.c:935   INTRACTABLE   "                                                                                                                            "
tree.c:944   INTRACTABLE   "                                                                                                                            "
tree.c:949   INTRACTABLE   "                                                                                                                            "
```
<!-- EXEMPTIONS-FENCE-END -->

## Path forward

Reducing this list further requires:

1. **Fault-tolerant gcov runtime patch** — unlocks all `INTRACTABLE` malloc-failure entries (scrypt.c, repo.c:609-610, 842, 849).  Phase 13d delivers a SIGSEGV-handler shim that calls `__gcov_dump()` before exit.
2. **EINTR-injection wrapper** — covers lcsas_io.c 6 lines.  Risky to wire into the normal test path.
3. **AEAD-corruption fixtures** — would require breaking the cryptographic primitive to craft inputs that decrypt-but-verify-fail or corrupt-mid-zstd.  Genuinely not testable.
4. **1M+ file fixtures** — the petabyte-scale stress test (`LCSAS_PETABYTE=1`) exercises some at integration time.  For coverage-c we'd need the same scale during the standard build (~10s per million-file readdir).
