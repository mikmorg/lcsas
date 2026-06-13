# T1C-05: Fail loud when an index file exceeds the decompress cap

**Priority:** P2 · **Severity:** low · **Dimension:** tier1-c · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Make oversized-index decompress cap fatal in tier-1 load_index

## Problem

The tier-1 C restorer refuses to decompress any repository file whose zstd frame declares
more than 256 MiB (`decrypt_repo_file`, repo.c). For *index* files, both passes of
`lcsas_repo_load_index` treat the resulting NULL as `if (!plain) continue;` — the index
file is dropped from the load (an `ERROR: zstd frame ... reports invalid size` line is
printed, so it is not fully silent, but the load proceeds as if the file never existed),
along with every blob it described. The restore then aborts much later with a cryptic
`blob not in index` for whatever files those blobs backed, with nothing connecting the
abort back to the cap. An heir sees two unrelated-looking errors and a dead restore, with
no hint that tier-2 (rustic) would succeed.

Severity is low because the scenario is remote: restic/rustic writers flush index files
at ~50k blobs (~10 MB of JSON), and the 32768-token parse cap (T1C-01) already rejects
any index past ~3,000 entries — so today the 256 MiB decompress cap on an index file is
practically unreachable. But T1C-01 removes the token cap, and the *posture* is wrong
either way: load_index must never continue past a file it could not read, for any reason
it cannot positively classify as ignorable.

## Evidence

Re-checked 2026-06-10 (`recovery/src/lcsas-restore/repo.c`):

- repo.c:384-390 — `if (dsz <= 0 || dsz > (long)(256 * 1024 * 1024))` →
  `fprintf(stderr, "ERROR: zstd frame at %s reports invalid size %ld\n", ...)` →
  `return NULL`. (Audit correction confirmed: the cap path *does* print and name the
  file; the defect is the non-fatal continue, not silence.)
- repo.c:520 (pass 1) and repo.c:574 (pass 2) — `if (!plain) continue;`: NULL from
  decrypt_repo_file — for ANY reason (I/O, MAC failure, cap, zstd error) — skips the
  index file and the load reports success.
- tree.c:636-639 — the downstream symptom: `blob not in index: %.64s`, rc −1.
- repo.c:935-944 — the same 256 MiB cap on inline data blobs (`read_blob`), which is
  defensible (restic blobs are ≤ ~8 MB) and stays as-is.

## Fix design

Fold into T1C-01's `lcsas_repo_load_index` surgery (same lines, same PR).

1. **`decrypt_repo_file`** — add an out-param reason code so callers can classify NULL:
   ```c
   enum lcsas_decrypt_err { LCSAS_DEC_OK = 0, LCSAS_DEC_IO,
                            LCSAS_DEC_MAC, LCSAS_DEC_TOOBIG, LCSAS_DEC_ZSTD };
   static unsigned char *decrypt_repo_file(const char *path,
                                           const lcsas_master_key *mk,
                                           size_t *out_len, int *why);
   ```
   Set `LCSAS_DEC_TOOBIG` on the repo.c:384 cap, `LCSAS_DEC_MAC` on the
   `lcsas_repo_decrypt` failure (repo.c:368-371), `LCSAS_DEC_IO`/`_ZSTD` for the rest.
   (`why` may be NULL for the other call sites — key/config/snapshot loads keep their
   current behaviour.)
2. **`lcsas_repo_load_index` both passes (repo.c:520, 574)** — replace the blanket skip:
   - `LCSAS_DEC_TOOBIG` / `LCSAS_DEC_ZSTD` → print
     `ERROR: index file %s exceeds the 256 MiB tier-1 decompress cap; use tier-2 (rustic)`
     (or `...is corrupt (zstd)...`) and `goto out` with rc −1 — load fails BEFORE any
     restore I/O.
   - `LCSAS_DEC_MAC` / `LCSAS_DEC_IO` → `WARNING: index file %s failed
     authentication; skipping` + an end-of-load skipped-file count. Stays non-fatal so
     the petabyte fixture's premise (3,000 undecryptable stubs alongside a valid index)
     and BUG-3 regression behaviour hold.
3. Document the cap in `recovery/docs/FORMAT.txt` next to T1C-01's "TIER-1 LIMITS" note.
   Do **not** raise the cap or stream-parse: with T1C-01's adaptive tokens the realistic
   index ceiling is memory, and 256 MiB of decompressed index JSON remains far beyond
   what any restic writer emits.

No catalog/schema impact; read-side C only. Binaries on already-burned meta discs keep
the old skip behaviour forever; fix ships with the next `recovery/bin/*` regeneration.

## Tests & gates

Per the verifier's refinement, no literal >256 MiB fixture (slow, pointless): a zstd
frame *header* can declare any content size, so a tiny fixture triggers the cap.

- `recovery/tests/test_repo.c` — fixture index file whose v2 zstd frame header declares
  300 MiB (gen_fixture helper writes the crafted frame, MAC-valid): assert
  `lcsas_repo_load_index` returns <0 and stderr names the file and says "use tier-2";
  assert no later "blob not in index" is reachable (load already failed).
- `recovery/tests/test_repo.c` — companion case: one MAC-corrupt index alongside a valid
  one still loads (warn + skip + summary count), pinning the petabyte/BUG-3 premise.
- Runs in `make -C recovery test` → audit-gate steps 1/3 (always-on for recovery paths
  in `.github/workflows/audit-gate.yml`).

## Acceptance criteria

- [ ] An index file with a >256 MiB-declaring zstd frame makes `lcsas_repo_load_index`
      fail immediately with the cap message naming the file; exit non-zero; no
      `blob not in index` ever printed for that run.
- [ ] MAC-failure index skips remain non-fatal, warned, and counted in a summary line.
- [ ] `test_tier1_petabyte_fixture.py` still green.
- [ ] `make -C recovery audit-gate` green.

## Dependencies & related plans

- **T1C-01** (adaptive token caps / fail-loud load_index) — same function, same PR;
  T1C-01's plan already reserves this split (its fix-design step 1 note). Implement
  together; this plan is the decrypt-reason half.
- **GATE** "audit-gate path filter holes" — keeps the gate firing on repo.c changes.

## Effort

0.5 day inside the T1C-01 PR (reason-code plumbing + two unit cases + crafted-frame
fixture helper). Local toolchain only.

---
**Implemented:** 2026-06-13. As planned: `decrypt_repo_file` gained an `int *why`
reason-code out-param (LCSAS_DEC_IO/MAC/TOOBIG/ZSTD); both `lcsas_repo_load_index`
passes now `goto out` (rc -1) with a file-naming "use tier-2 (rustic)" message on
TOOBIG/ZSTD and stay warn+skip+count on MAC/IO. FORMAT.txt TIER-1 LIMITS updated.
Two test_repo.c cases (crafted 300 MiB-FCS zstd frame → fatal; MAC-corrupt sibling
→ non-fatal) via on-the-fly AES-CTR+Poly1305 encryption helpers; the old graceful
bad-zstd fixture (FOURTH index) removed from gen_fixture.py and its blob deleted.
All 5 tracked per-target `lcsas-restore` bins rebuilt with zig cc (x86_64/aarch64/
armv7 linux-musl, aarch64-macos, x86_64-windows-gnu) and verified under qemu/wine.
