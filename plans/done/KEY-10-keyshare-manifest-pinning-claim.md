# KEY-10: KEY_SHARE_FORMAT.md falsely claims wordlist/combiner are pinned in MANIFEST.sha256

> **STATUS: RESOLVED** — landed in `9232815` (docs+test: truthful wordlist provenance with doc-embedded hash [KEY-10]); guarded by `tests/unit/test_keyshare_wordlist_provenance.py`.

**Priority:** P2 · **Severity:** low · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: .claude/skills/key-escrow/PLAN.md K2.1 (documents the non-pinning decision, not the contradicting doc claim)
**Suggested GH issue title:** Fix KEY_SHARE_FORMAT.md wordlist provenance; pin wordlist hash in spec

## Problem

The 50-year re-implementation spec states the bundled `wordlist.txt` "is
pinned in `recovery/MANIFEST.sha256` alongside the combiner." Neither the
wordlist nor any combiner artifact appears in that manifest — and PLAN.md
K2.1 records this as a deliberate decision ("the recovery/-rooted MANIFEST
intentionally doesn't pin them; git-pinned + 45-vector guarded"). An engineer
decades out, told by the spec to verify provenance via the manifest, finds no
entry — undermining trust in the one document meant to be authoritative, for
an artifact whose exact bytes are load-bearing for reconstruction.

## Evidence

Re-checked 2026-06-10 against master:

- `docs/KEY_SHARE_FORMAT.md:108-110` — "Wordlist provenance. The bundled
  `wordlist.txt` is the official 1024-word SLIP-0039 list (unique 4-letter
  prefixes); it is pinned in `recovery/MANIFEST.sha256` alongside the
  combiner."
- `grep -in 'keyshare|wordlist|combine' recovery/MANIFEST.sha256` → rc 1, no
  matches.
- `.claude/skills/key-escrow/PLAN.md:55` (K2.1) — the intentional non-pinning
  decision.

## Fix design

Make the spec self-authenticating rather than re-architecting the
recovery/-rooted manifest (the manifest covers `recovery/` paths; the
wordlist/combiner live under `src/` and are copied at meta-build time — K2.1's
layout decision stands; the *doc claim* is what's wrong).

1. **`docs/KEY_SHARE_FORMAT.md` §5 "Wordlist provenance"** — replace the
   manifest sentence with the real provenance and embed the hash in the spec
   itself:
   - "git-pinned in this repository; guarded by the 45 official SLIP-0039
     test vectors (`recovery/tests/test_keyshare.c`,
     `tests/unit/test_keyshare.py`)";
   - "SHA-256 of `wordlist.txt` (1024 LF-terminated words):
     `<computed hash>` — verify with `sha256sum wordlist.txt`";
   - note that the C combiner's `wordlist.c` is generated from the same list.
2. **Guard tests** so the embedded hash and the three wordlist copies can
   never drift apart silently — new
   `tests/unit/test_keyshare_wordlist_provenance.py`:
   - parse the hash out of KEY_SHARE_FORMAT.md and assert it equals
     SHA-256 of `src/lcsas/keyshare/wordlist.txt`;
   - assert the 1024 words extracted from
     `recovery/src/lcsas-keyshare/wordlist.c` equal the txt list;
   - assert the doc contains no remaining "pinned in
     recovery/MANIFEST.sha256" claim for keyshare artifacts (regression
     guard on the false sentence).

Chosen over adding `../src/...` lines to MANIFEST.sha256: it puts the
verification bytes where the future reader already is — inside the spec,
which is bundled on every meta-volume — and avoids bending the manifest's
recovery/-rooted contract.

## Tests & gates

- `tests/unit/test_keyshare_wordlist_provenance.py` (above) — always-on,
  `make test-unit` → `make gate`.
- No CI change needed; the test runs in the standard unit job.

## Acceptance criteria

- [ ] KEY_SHARE_FORMAT.md §5 states git-pinned provenance and embeds the wordlist SHA-256.
- [ ] Provenance test green; mutating one word in wordlist.txt or wordlist.c fails it.
- [ ] `grep 'MANIFEST.sha256' docs/KEY_SHARE_FORMAT.md` no longer asserts keyshare pinning.
- [ ] PLAN.md K2.1 note updated to reference the doc-embedded hash.

## Dependencies & related plans

- Independent; can land any time. Touches the same doc as **KEY-07** (prefix
  entry note in §2) — trivial merge coordination.
- **GATE** "No gate verifies committed recovery/bin artifacts were built from
  current source" — binary staleness is that plan's scope; this one covers
  source-artifact provenance only.

## Effort

0.5 days.

---
**Implemented:** 2026-06-11. As planned; mutation checks for wordlist.txt and wordlist.c verified to fail the new guards.
