# T1C-02: Guard lcsas_json_decode_int against signed overflow; fuzz NUMBER tokens

> **STATUS: RESOLVED** — landed in `fe506a2` (recovery: guard json decode_int against signed overflow; fuzz NUMBER tokens [T1C-02]); guarded by `recovery/tests/test_json.c + recovery/fuzz/fuzz_json_parse.c`.

**Priority:** P2 · **Severity:** medium · **Dimension:** tier1-c · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Guard json decode_int against signed overflow; fuzz NUMBER tokens

## Problem

`lcsas_json_decode_int` — the sole numeric decoder in the tier-1 C restorer — accumulates
`v = v * 10 + (c - '0')` with no overflow check. Signed `long long` overflow is undefined behaviour in C.
This decoder parses every security-relevant number read off disc: blob `offset`/`length`/
`uncompressed_length`, file `size`, `mode`, `uid`, `gid`. A long digit run in an index entry is UB, and on
the offset/length path the overflowed values are *consumed before* the SHA-256 check: `need_end =
offset + length` can itself overflow (bypassing the truncation guard) and `malloc((size_t)loc->length)`
can attempt a wild allocation.

Mitigations are real but partial: the input is Poly1305-MAC-verified plaintext (triggering this requires
the repo key or a writer bug), and the downstream per-blob SHA-256 still rejects wrong bytes — so this is
defense-in-depth UB, not silent corruption. But the project's own `-fsanitize=undefined` gate would trap
it, and the JSON fuzz harness never reaches it: `fuzz_json_parse.c` decodes only STRING tokens, never
NUMBER tokens, so the always-on audit-gate sanitize+fuzz steps cannot find it.

## Evidence

Re-checked 2026-06-10:

- `recovery/src/lcsas-restore/json_q.c:351-370` — `lcsas_json_decode_int`; the loop at 362-366 is
  `v = v * 10 + (c - '0');` with no guard.
- `recovery/src/lcsas-restore/repo.c:434-435,447` — offset/length/uncompressed_length via `decode_int`
  in `parse_blob_entry`; no sign/range validation after decode.
- `recovery/src/lcsas-restore/repo.c:881-882` — `need_end = (long long)loc->offset + (long long)loc->length;`
  (overflowable); `repo.c:891` — `malloc((size_t)loc->length)`; SHA-256 check only at `repo.c:958-960`.
- `recovery/fuzz/fuzz_json_parse.c:39-45` — token walk calls `lcsas_json_decode_string` on STRING tokens
  only; `grep -r decode_int recovery/fuzz/` is empty.
- `recovery/tests/test_json.c:36` — the only decode_int assertion is the value 32768.

## Fix design

1. **`json_q.c` `lcsas_json_decode_int`** — overflow-checked accumulate (C89 + the already-used
   `long long` extension; `LLONG_MAX` via `<limits.h>` with a `#ifndef` fallback to
   `9223372036854775807` since strict C89 limits.h may lack it):
   ```c
   int d = c - '0';
   if (v > (LLONG_MAX - d) / 10) return -1;
   v = v * 10 + d;
   ```
   (Negation stays safe: we reject anything above LLONG_MAX, so `-v` never hits LLONG_MIN.)
2. **`repo.c` `parse_blob_entry` (434-447)** — after decode, reject `off < 0 || len <= 0 ||
   len > (long long)(512 * 1024 * 1024) || off > LLONG_MAX - len` with a printed
   `ERROR: index entry for blob %.64s has invalid offset/length` and return -1. (512 MiB is generous:
   the 256 MiB uncompressed cap bounds any valid blob's compressed length.) Note: today a
   `parse_blob_entry` failure is a silent per-entry `continue` in pass-2 — print the error so the skip is
   visible; making it fatal is covered by T1C-01's fail-loud load_index posture.
3. **`repo.c` read_blob (~881-893)** — keep the `need_end` fstat guard, now provably non-overflowing
   given (2); add a defensive `if (loc->length <= 0) return -1` before the mallocs.
4. **`recovery/fuzz/fuzz_json_parse.c`** — in the token walk, also call `lcsas_json_decode_int` on every
   `LCSAS_JSON_NUMBER` token (discard result). Seed `recovery/fuzz/corpus/json/` with a 40-digit number
   (`{"length":9999999999999999999999999999999999999999}`).

No format or catalog impact; read-side hardening only.

## Tests & gates

- `recovery/tests/test_json.c`: decode_int of `"99999999999999999999999999"` returns -1;
  boundary `"9223372036854775807"` returns 0 with the exact value; `"9223372036854775808"` returns -1;
  `"-9223372036854775807"` accepted. Runs in `make -C recovery test` and under UBSan in
  `make -C recovery sanitize` (audit-gate step 3, always-on in `.github/workflows/audit-gate.yml`).
- `recovery/tests/test_repo.c`: add a gen_fixture index variant with an astronomically large `length`;
  assert load succeeds but the entry is rejected (lookup of that blob id fails) with no crash — i.e. no
  wild malloc/pread.
- Fuzz: extended `fuzz_json_parse` runs 60 s under `-fsanitize=fuzzer,address,undefined` in
  `make -C recovery fuzz-json-smoke` → `fuzz-smoke` → audit-gate step 4 (always-on). Pre-fix, the new
  corpus seed must trap under UBSan (verify before committing the guard, then confirm green after).

## Acceptance criteria

- [ ] UBSan build (`make -C recovery sanitize`) passes with the 40-digit corpus seed present.
- [ ] `fuzz-json-smoke` exercises decode_int on NUMBER tokens (confirm via a temporary counter or
      coverage report) with 0 crashes.
- [ ] test_json.c boundary cases pass; oversized `length` index entry is rejected pre-malloc with a
      printed error.
- [ ] `make -C recovery audit-gate THRESHOLD=60` green.

## Dependencies & related plans

- Independent of T1C-01, but touches the same `parse_blob_entry`/load_index region — rebase whichever
  lands second. T1C-01's loud-skip posture upgrades this plan's per-entry error from warning to fatal.
- GATE "audit-gate path filter holes" — keeps this gate firing on the right paths.

## Effort

1 day (0.5 impl, 0.5 tests/fuzz corpus + pre/post UBSan verification). Local clang only.

---
**Implemented:** 2026-06-13. As planned. decode_int overflow guard (LLONG_MAX, with #ifndef fallback) added in json_q.c; parse_blob_entry range-rejects off<0/len<=0/len>512MiB/off+len overflow with a printed error; read_blob gains a defensive length<=0 check before its mallocs. fuzz_json_parse now decodes NUMBER tokens; corpus seeded with a 40-digit length (verified to trap signed-overflow UBSan pre-fix, green post-fix). test_json boundary cases + a two-bad-entry gen_fixture index variant (overflow + over-cap length) assert load succeeds with the bad blobs dropped. Sanitize clean; fuzz-json-smoke 0 crashes. Rebuilt all 5 git-tracked tier-1 lcsas-restore binaries (zig: musl-static x86_64/aarch64/armv7, PE x86_64-windows, Mach-O aarch64-macos; qemu/wine-verified). MANIFEST.sha256 left as-is per predecessor T1C-01 convention (already stale at HEAD; regenerating adds ~2300 unrelated corpus entries).
