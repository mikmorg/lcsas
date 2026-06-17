# KEY-07: C combiner gives no actionable typo feedback; 4-letter-prefix property unused

> **STATUS: RESOLVED** — landed in `a5d6280` (keyshare+cli+docs: per-share typo diagnostics + 4-letter prefix entry [KEY-07]); guarded by `tests/integration/test_keyshare_c_python_crosscheck.py`.

**Priority:** P2 · **Severity:** medium (borderline high — typing 40+ words from print is the real heir input mode) · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Per-share typo diagnostics + 4-letter prefix entry in combiners

## Problem

A share card carries 20 or 33 words the heir must retype perfectly — 40+
words across a default 2-of-5 reconstruction. The Python path at least names
the unrecognized word; the C binary — the python-free *primary* path per
KEY-05 — collapses every failure (under-threshold, typo, checksum mismatch,
foreign share) into one generic message: `failed to recover the password
(insufficient, corrupt, or mismatched shares)`. There is no per-share
validation ("share 2, word 7 'buidling' is not a word"), no indication of
WHICH card is bad when one of K is mistyped, and although KEY_SHARE_FORMAT.md
advertises that every wordlist word is uniquely identified by its first 4
letters, no combiner accepts prefixes — exact full words only. For a
non-technical heir, a single typo yields an undiagnosable dead end on the
primary path.

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/src/lcsas-keyshare/main.c:177-181` — single generic error for all
  failure modes of `lcsas_keyshare_recover_password`.
- `recovery/src/lcsas-keyshare/slip39.c:144-171` — `word_to_index` is an
  exact binary search; no prefix logic.
- `src/lcsas/keyshare/slip39.py:179-183` — `_mnemonic_to_indices` exact dict
  lookup; `KeyShareError(f"Unknown word in mnemonic: {…}")` names the word
  (python only), but not the share index/file.
- `docs/KEY_SHARE_FORMAT.md:36-38` — "1024 words, each uniquely identified by
  its first 4 letters" — advertised, unused.

## Fix design

Same behavior in both implementations; C is the priority.

1. **Prefix expansion at word lookup.** Wordlist guarantees 4-letter prefix
   uniqueness, so accept any token of length ≥4 that prefix-matches exactly
   one wordlist word (full words always win; tokens <4 chars that aren't a
   full word are errors).
   - C: extend `word_to_index(w, len)` — on exact-match miss, binary-search
     the prefix range; return the index iff the range size is 1 (the existing
     comparator already does length-limited compare, so this is a small
     range-scan addition).
   - Python: in `_mnemonic_to_indices`, fall back to a precomputed
     `prefix4 -> word` dict.
2. **Per-share pre-pass with named diagnostics.** Before combining, validate
   each share independently: word lookup (report position + offending token)
   and per-share RS1024 checksum. New C API:
   ```c
   /* Validate one mnemonic. Returns 0 (OK) or a reason code; writes a
    * human line into errbuf, e.g.:
    *   word 7 'buidling' is not a share word
    *   checksum FAILED - recheck your typing against the card */
   int lcsas_keyshare_check_share(const char *mnemonic,
                                  char *errbuf, size_t errcap);
   ```
   `main.c` runs it over every input and prints per-share verdicts naming the
   *file* (or "stdin line N"):
   ```
   share 1 (alpha-share-1-card.txt): OK
   share 2 (alpha-share-2-card.txt): word 7 'buidling' is not a share word
   ```
   then exits 1 without attempting recovery if any share fails. If all shares
   pass individually but recovery still fails, print the current generic
   message plus the hint the Python combiner already ships ("at least K
   shares from the SAME archive"). Mirror the pre-pass in
   `keyshare_combine.py` and `cmd_key_combine` (a `check_share(mnemonic)`
   helper in `lcsas/keyshare`).
3. **Docs**: one line on cards (`_share_card_text`) and in
   KEY_SHARE_FORMAT.md §2: "You may type just the first 4 letters of each
   word." Only after both implementations accept prefixes.

Security note: per-share diagnostics reveal nothing beyond what the share
holder already possesses (validity of their own share); checksums are public
structure, not secret-dependent. No schema/catalog impact; rebuild all 6
target bins (`make -C recovery keyshare-arches`) and commit.

## Tests & gates

- `recovery/tests/test_keyshare.c` — new cases: (a) one mistyped word in
  share 2 of 2 → `lcsas_keyshare_check_share` names word position + token;
  (b) every word of a valid vector truncated to 4 letters → recovery
  succeeds byte-exact; (c) 3-letter token → error; (d) all-shares-valid but
  mismatched-identifier set → generic error with hint. Runs in
  `make -C recovery test` (CI-triggered once KEY-06 lands).
- `tests/unit/test_keyshare.py::test_prefix_words_accepted` and
  `::test_check_share_names_word_position` — Python parity; always-on.
- Cross-implementation gate:
  `tests/integration/test_keyshare_c_python_crosscheck.py` (skips without the
  built binary) — prefix-typed and typo'd inputs through both; assert
  identical accept/reject verdicts and byte-identical recovered passwords.
- KEY-06's fuzz harness gains `lcsas_keyshare_check_share` as a second entry
  point.

## Acceptance criteria

- [ ] C binary with a typo'd card prints the share file name + word position + token; exit 1.
- [ ] All-prefix (4-letter) entry recovers the password byte-exact in C, Python module, and CLI.
- [ ] Python/C verdicts agree on the cross-check matrix.
- [ ] Cards + KEY_SHARE_FORMAT.md document prefix entry.
- [ ] 6 target bins rebuilt and committed.

## Dependencies & related plans

- **KEY-01** first — this builds on its extracted parsing layer
  (`lcsas_keyshare_extract` / `cards.py`); diagnostics must name card files,
  which only exist as valid inputs after KEY-01.
- **KEY-06** — gates the new C code (coverage/fuzz/CI); land before or with
  this.
- **KEY-05** — heir docs that make the C path primary; its wording should
  mention the per-share OK/FAILED output once this lands.

## Effort

2 days: 1.0 C (prefix search + check_share + main.c output + tests), 0.75
Python parity + cross-check, 0.25 docs + rebuild.

---
**Implemented:** 2026-06-13. As planned, with two scoped additions: (1) a token-dense-line fallback in `main.c` (`copy_candidate_line`) so a typo'd card — which strict extraction drops — still reaches the per-share pre-pass for an actionable diagnostic; (2) surgical (4-line) update of `recovery/MANIFEST.sha256` for the touched C sources only — the manifest was already broadly stale for unrelated files (e.g. ENV_VARS.txt), and a full `make manifest` regen would have swept that out-of-scope drift into this commit. KEY-06's fuzz/CI gate is not yet landed (not a blocker for this change). Cross-arch bins rebuilt for all 6 targets and verified via qemu/wine.
