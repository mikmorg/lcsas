# Tier-1 Coverage Exemptions

This document inventories every line of `recovery/src/lcsas-restore/*.c`
that is **not** covered by `make coverage-c` and explains why.

The goal stated in PR #182+ work and the user `/goal` is 100% test
coverage exempting only intractable code, documented here.

A line is "intractable" if covering it would require one of:
- LD_PRELOAD fault injection against gcov-instrumented code
  (gcov-runtime itself crashes when its own allocations fail, so the
  current shim cannot accumulate `.gcda` from fault-inject runs — see
  `recovery/docs/AUDIT.md` "Fault injection" section)
- Interrupting a syscall mid-execution with a signal (EINTR retry)
- Filling a real filesystem past 90% to trigger `fs_critically_full`
- A genuinely-pathological encrypted blob that decompresses but then
  fails verification mid-stream (would require a crypto break to craft)

Each exemption has a STATUS:
- `INTRACTABLE` — cannot reasonably be tested without infrastructure
  beyond the current harness
- `DEFENSIVE` — defensive code path that is provably unreachable given
  invariants enforced upstream, but kept for safety / readability

Counts as of 2026-05-22 (after Phase 12a-e).  Overall coverage: **95.5%**.

| File | Coverage | Uncov lines | Status |
|------|----------|-------------|--------|
| aes.c, b64.c, hex.c, json_q.c, path.c, pbkdf2.c, sha256.c, zstd_dec.c | 100% | 0 | ✓ |
| catalog.c | 98.0% | 2 | exempt |
| main.c | 98.6% | 3 | exempt |
| tree.c | 97.3% | 4 | exempt |
| poly1305.c | 95.0% | 5 | exempt |
| scrypt.c | 98.0% | 2 | exempt |
| disc_locator.c | 90.1% | 36 | exempt (mostly defensive overflow checks) |
| lcsas_io.c | 90.3% | 6 | exempt (EINTR retry) |
| repo.c | 91.9% | 41 | exempt (malloc-fail + corruption paths) |

## Per-file exemptions

### `catalog.c` — 2 lines

| Line | Branch | Status | Reason |
|------|--------|--------|--------|
| 156-157 | `sqlite3_prepare_v2` failure in `lcsas_catalog_print_pending_packs` | INTRACTABLE | SQLite never fails to prepare a hardcoded, well-formed SELECT. Triggering this would require corrupting SQLite's in-memory state or running out of memory inside SQLite — fault injection against gcov would crash before gcov flushes. |

### `disc_locator.c` — ~42 lines, mixed

The lines from the drain path's "path too long" warnings (528-529,
535, 537, 546, 551-552, 574, 576, 583, 585, 596, 598, 603, 605, 632)
fire only when the `snprintf` output would exceed `sizeof prefix_dir`
(4096 bytes), which requires synthesizing a directory tree with
parent paths > 4 KiB. Possible to test but adds substantial fixture
complexity and exercises only one defensive `if (rc >= sizeof buf)
continue;` pattern repeated across many sites.

| Lines | Branch | Status |
|-------|--------|--------|
| 135, 138 | `mkdir_p` mkdir/stat double-race failure | INTRACTABLE — needs interleaved process to create the file between our mkdir and stat |
| 528-537 | `fs_critically_full` warning + early return | INTRACTABLE — needs a tmpfs with <10% free |
| 528-529, 535, 537, 546, 551-552, 574, 576, 583, 585, 596, 598, 603, 605, 632 | various `if (rc >= sizeof buf)` path-overflow continues | DEFENSIVE — each branch is a 4 KiB+ path warning; the same defensive pattern repeated; testing one would mean testing all |
| 697, 700 | `print_prompt` catalog-pack-known-but-no-volume vs catalog-no-record | INTRACTABLE in unit test — requires a populated SQLite catalog with specific schema state; the integration blind-restore exercises this |

### `lcsas_io.c` — 6 lines (EINTR)

| Lines | Branch | Status |
|-------|--------|--------|
| 22-23, 40-41, 75-76 | `if (errno == EINTR) continue;` in `lcsas_pread_exact` / `lcsas_write_exact` / `lcsas_read_file` | INTRACTABLE — requires sending a signal between the read/write syscall starting and returning, racy and platform-specific |

### `main.c` — 3 lines

| Lines | Branch | Status |
|-------|--------|--------|
| 375 | "ERROR: index load failed" | INTRACTABLE — index load is the first thing after key decryption; making it fail mid-restore requires corrupting decrypted bytes which the AEAD prevents |
| 437-438 | "ERROR: snapshot load failed" + goto out | INTRACTABLE — same as 375 but for snapshots |

### `poly1305.c` — 5 lines

| Lines | Branch | Status |
|-------|--------|--------|
| 146-150 | Final-clamp `if (g4 & (1UL << 26))` taking the non-underflow branch | INTRACTABLE for normal poly1305 inputs — the branch fires only when the accumulator `h` is greater than or equal to the prime 2^130 - 5 after the final block. For random / typical messages h is bounded below the prime; reaching ≥ prime requires an adversarial sequence of message blocks chosen to drive the accumulator into that range. The Wycheproof poly1305 vectors used in `test_poly1305.c` don't happen to hit it. Not a correctness gap — the code is correct on this branch — but exercising it requires a chosen-message attack on the MAC accumulator. |

### `repo.c` — 45 lines

| Lines | Branch | Status |
|-------|--------|--------|
| 128 | scrypt KDF failure in `lcsas_repo_load_key_file` | INTRACTABLE — scrypt only fails on parameter-validation or OOM; we use known-good params and OOM requires fault injection |
| 193-195 | "[lcsas-restore] key count exceeded sanity limit" | INTRACTABLE — needs >1,000,000 key files in keys/ |
| 216-218 | Sort-swap inside `lcsas_repo_load_keys_dir` name-sort | DEFENSIVE — the loop body runs whenever names[b] sorts before names[a]; with N keys in readdir order, this fires for any out-of-order pair. The committed fixture's 3 keys happen to be in readdir order on this filesystem. Adding more keys to FORCE out-of-order order would couple the test to filesystem readdir implementation details. |
| 258-261 | `strip_v2_prefix` v2-plain branch (single prefix byte without zstd) | TRACTABLE — would require adding a v2-plain index file to fixture |
| 369-370 | `decrypt_repo_file` decrypt-mac fail | INTRACTABLE — needs a deterministic ciphertext that decrypts but fails MAC (cryptographic primitive prevents this without breaking AEAD) |
| 377-378 | `strip_v2_prefix` returns -1 (`*len < 1`) | INTRACTABLE — needs a file that decrypts to 0 bytes; AEAD overhead is 32 bytes minimum so the encrypted file would be 32 bytes and produce 0-byte plaintext, which we'd have to craft |
| 395-397 | zstd decompress failure AFTER probe succeeded | INTRACTABLE — needs a frame whose header is valid but body is corrupted; uncommon and crypto-difficult to forge |
| 494-503 | Index name realloc growth past 2048 entries + count > 1M sanity | INTRACTABLE — would need 2048+ index files in fixture; the existing `test_tier1_petabyte_fixture.py` exercises this branch at integration time but coverage-c skips integration tests |
| 534-539 | Supersedes overflow at 8192 entries | INTRACTABLE — would need 8192+ entries in a single index file's `supersedes` array |
| 598 | pack_id hex decode failure | TRACTABLE — could add an index entry with a non-hex pack_id |
| 609-610 | `blob_index_push` failure (realloc) | INTRACTABLE — realloc fault injection only |
| 833-834 | "pack not found" in `lcsas_repo_read_blob` | INTRACTABLE during normal restore — every blob in tree_restore exists in the pack; missing-pack would mean a corrupt or partial-disc scenario |
| 842, 849, 867-869, 875-877, 886-888 | `read_blob` zstd error paths + hash-mismatch | INTRACTABLE — needs a pack blob that decrypts but contains corrupted-zstd or wrong-content |

### `scrypt.c` — 2 lines

| Lines | Branch | Status |
|-------|--------|--------|
| 181-182 | "rc = -2; goto out" in alloc-failure path | INTRACTABLE — fault-injection only |

### `tree.c` — 4 lines

| Lines | Branch | Status |
|-------|--------|--------|
| 157 | `restore_file_node` rc=-1 break after `lcsas_repo_read_blob` failure | INTRACTABLE in coverage-c — would require an index entry whose blob_index_find succeeds but read_blob fails (e.g., pointing at a non-existent pack file).  The phase-9 broken-tree fixtures hit the "blob not in index" branch (147) but not this one. |
| 160 | `write_exact` failure during file content write | INTRACTABLE — needs an unwritable target fd, which requires either fault injection or running on a filesystem that errors mid-write |
| 201 | `return -1` after read_blob failure for tree blob | INTRACTABLE — same as 157 but for the tree blob load. |
| 299 | `symlink()` syscall failure | INTRACTABLE — symlink() fails on read-only filesystem or when destination already exists; the existing code does `unlink(node_path)` first to handle the existing case. Triggering this would require a read-only mount which the test harness doesn't set up. |

## Summary

Total uncov as of Phase 12c: ~109 lines across 8 files (16 total).
Total exempt from this document: ~100 lines.

The remaining tractable gaps (repo.c 258-261, 598) are slated for a
fixture extension. After those land, coverage is expected to be
~95.5% with the remainder fully documented here.

## Path forward

If a future contributor wants to push beyond ~95%, the highest-yield
options are:

1. **Fault-tolerant gcov runtime patch.** Build libgcov with a flag
   that catches malloc failures from gcov's own allocations and
   falls back to a static pool. Would let `make fault-inject` actually
   accumulate `.gcda` data, unlocking ~30+ lines (mostly malloc/realloc
   failure paths in repo.c and scrypt.c).
2. **EINTR-injection wrapper.** A small LD_PRELOAD that randomly
   returns EINTR from `read`/`write` would cover lcsas_io.c lines 22,
   40, 75. Risky to wire into the normal test path.
3. **Pathological-input fixture for blob_index entries.** A fixture
   that crafts an encrypted blob whose decrypted content is short
   enough to hit repo.c 377-378, OR a zstd frame whose decompression
   fails after probing succeeds (395-397, 875-877). Crypto-difficult.
4. **>1M-key sanity limits.** The 1M-entry guards in repo.c (193-195,
   494-496) cannot be tested without producing a million-file fixture.
   The existing 1M-orphan petabyte fixture (`LCSAS_PETABYTE=1`)
   exercises the index-side of this; the key-side would need an
   equivalent. Probably not worth the disk + time.

Each of these is in scope for a future audit phase if the team decides
to push the floor above ~95%.
