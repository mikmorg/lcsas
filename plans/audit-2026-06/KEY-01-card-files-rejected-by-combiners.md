# KEY-01: Real share-card files are rejected by every combiner

**Priority:** P0 · **Severity:** high · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Make all three key-share combiners accept real -card.txt files

## Problem

`lcsas key split` writes two artifacts per share: a bare mnemonic file
(`{repo}-share-N.txt`, words only) and a plain-language printable **card**
(`{repo}-share-N-card.txt`) with header lines, prose, and the mnemonic under a
"THE SHARE WORDS" heading. The card is the artifact the owner is told to hand
to holders — it is the only thing a holder will physically possess after
printing. Every heir-facing document (on-disc `START_HERE.txt`, `KEY_INFO.txt`,
`docs/ESTATE_PLANNING.md`) says: gather K cards and run
`python3 keyshare_combine.py <card1> <card2>`.

That command fails on real card files. The Python combiner treats every
non-blank, non-`#` line as a mnemonic and dies on
`Unknown word in mnemonic: '================'`. The C combiner
(`lcsas-keyshare`, shipped on all 6 tier-1 targets) reads the whole file as one
mnemonic and dies with a generic "insufficient, corrupt, or mismatched shares".
The `lcsas key combine` CLI reads the whole file as one mnemonic too. The audit
reproduced this empirically: feeding two genuine card files to the Python and C
combiners both exit 1; only the bare `-share-N.txt` file works. The unit tests
are blind to this because their helper deliberately filters out `-card.txt`
files. A non-technical heir who did everything right — gathered K cards,
found the META disc, typed the documented command — dead-ends here, decades
from now, with no path forward.

## Evidence

Re-checked 2026-06-10 against master:

- `src/lcsas/cli/main.py:3183-3211` — `_share_card_text` produces the card with
  header lines (`"================ LCSAS KEY SHARE ================"`,
  `Repository : …`, `WHAT THIS IS`, …); mnemonic on a single line under
  `THE SHARE WORDS` (line 3209).
- `src/lcsas/meta/keyshare_combine.py:97-103` — `_read_mnemonics`: *every*
  non-blank non-`#` line becomes a mnemonic; header lines become bogus
  mnemonics.
- `src/lcsas/cli/main.py:3306-3313` — `cmd_key_combine`:
  `text = sf.read_text(...).strip(); mnemonics.append(text)` — whole file is
  one mnemonic.
- `recovery/src/lcsas-keyshare/main.c:33-72` — `read_file_mnemonic` reads the
  entire file as one mnemonic; `main.c:177-181` — single generic error.
- Docs telling heirs to pass cards: `src/lcsas/staging/metadata.py:61-66`
  (`python3 keyshare_combine.py <card1> <card2>` / "pass any {k} card files"),
  `:363-365` (same in START_HERE split block); `docs/ESTATE_PLANNING.md:84-91`.
- `tests/unit/test_cli_key.py:39-44` — `_share_mnemonic_files` helper:
  `if not p.name.endswith("-card.txt")` — card files are excluded from every
  round-trip test.

## Fix design

Make all three combiners card-tolerant by **wordlist-line filtering** (chosen
over "parse the THE SHARE WORDS section explicitly" because it survives card
format drift, hand-retyped files, and partial photocopies — the filter keys on
the cryptographic content, not on prose framing).

**Rule:** a line is a *mnemonic line* iff it is non-blank and every
whitespace-separated token is in the SLIP-0039 wordlist (case-insensitive).
Within one input **file**, all mnemonic lines are joined into ONE mnemonic
(each card holds exactly one share; joining also tolerates print-wrap when a
card is retyped across lines). If the joined word count is neither 20 nor 33,
fail loudly naming the file and the count. On **stdin**, keep
one-mnemonic-per-line semantics but *skip* (don't fatal on) non-mnemonic lines,
so `cat card1.txt card2.txt | combiner` works.

1. **Shared Python extractor** — new `src/lcsas/keyshare/cards.py`:
   ```python
   def extract_mnemonic(text: str) -> str:
       """Return the single share mnemonic embedded in *text* (a bare
       mnemonic file OR a printed share card). Raises KeyShareError
       naming the offending word count if the result is not 20/33 words."""
   def is_mnemonic_line(line: str) -> bool: ...
   ```
   Lives inside `lcsas.keyshare` so the meta-volume bundle
   (`bundle_python_package("lcsas.keyshare")`) ships it automatically and
   `keyshare_combine.py` can import it under both layouts.
2. **`src/lcsas/meta/keyshare_combine.py`** — `_read_mnemonics`: per file path,
   call `extract_mnemonic`; stdin path uses `is_mnemonic_line` filtering.
   Update the module docstring usage notes (lines 13-25).
3. **`src/lcsas/cli/main.py` `cmd_key_combine` (~3306-3313)** — replace the
   whole-file append with `extract_mnemonic(text)`; stdin loop filters with
   `is_mnemonic_line`.
4. **C combiner** — extract parsing from `main.c` into a testable function in
   `recovery/src/lcsas-keyshare/slip39.c` (or new `cards.c`):
   ```c
   /* Extract the share mnemonic from file text (bare or card format).
    * Writes the joined mnemonic into out. Returns 0, or nonzero with a
    * one-line reason in errbuf (e.g. "joined 7 words, expected 20 or 33"). */
   int lcsas_keyshare_extract(const char *text, char *out, size_t cap,
                              char *errbuf, size_t errcap);
   ```
   Token check reuses the existing `word_to_index` binary search
   (`slip39.c:144`). `main.c` `read_file_mnemonic` calls it; stdin loop skips
   lines containing any non-wordlist token.
5. **Rebuild the 6 target binaries** with `make -C recovery keyshare-arches`
   (`recovery/Makefile:398-433`) and commit `recovery/bin/**` per the
   established regeneration flow.
6. **Anti-ambiguity guard:** the card template must never contain a prose line
   composed entirely of wordlist words (or the filter would absorb it and the
   20/33 count check would reject the card). Pin this with a unit test over
   `_share_card_text`'s fixed lines (see Tests). The 20/33 count + RS1024
   checksum remain the backstop.

Error wording (Python and C alike) when extraction fails:
`"'{file}': found {n} share words, expected 20 or 33 — is this a complete share card?"`

No catalog/schema impact. Already-printed cards and already-burned discs are
the *inputs* this fix exists to accept — no migration needed; bare
`-share-N.txt` files keep working unchanged.

## Tests & gates

Always-on (run in `make test-unit`, hence `make gate` via `test-all`):

- `tests/unit/test_keyshare_cards.py` — `extract_mnemonic` accepts: bare
  mnemonic, full card text, card with the mnemonic wrapped over 3 lines,
  CRLF line endings, mixed case. Rejects (named error): empty file, prose-only
  file, truncated card (word count off).
- `tests/unit/test_keyshare_cards.py::test_card_template_lines_unambiguous` —
  every fixed prose line of `_share_card_text` output contains ≥1 non-wordlist
  token (drift guard for design point 6).
- `tests/unit/test_keyshare_combine.py::test_combine_accepts_real_card_files` —
  run `cmd_key_split` into tmp, feed the two `-card.txt` files (NOT the share
  files) to `keyshare_combine.main`; assert byte-exact password on stdout.
  Also the stdin path: `cat`-style concatenated card text piped in.
- `tests/unit/test_cli_key.py::test_cli_combine_accepts_card_files` — same via
  `lcsas key combine --share-file alpha-share-1-card.txt …`; remove the
  card-exclusion blindspot by adding a `_share_card_files` helper alongside
  `_share_mnemonic_files` (keep the latter's tests as-is).
- `recovery/tests/test_keyshare.c` — new cases calling
  `lcsas_keyshare_extract` + `lcsas_keyshare_recover_password` on embedded full
  card text (header lines included): success byte-exact; truncated card →
  nonzero with word-count reason. Runs in `make -C recovery test` (TESTS list,
  `recovery/Makefile:84`) — but note that suite only triggers in CI when
  keyshare paths are added to `.github/workflows/audit-gate.yml` (KEY-06).
- Integration (binary-level, skipped if `recovery/bin/x86_64/lcsas-keyshare`
  absent): `tests/integration/test_keyshare_binary_cards.py` — subprocess the
  committed static binary with two real card files; assert rc 0 + byte-exact
  password. Catches "library fixed but shipped bins stale".

## Acceptance criteria

- [ ] `lcsas key split` then `python3 keyshare_combine.py alpha-share-1-card.txt alpha-share-2-card.txt` prints the exact password, rc 0.
- [ ] Same inputs through `lcsas key combine --share-file …` and through `recovery/bin/x86_64/lcsas-keyshare` (the committed binary): rc 0, byte-exact.
- [ ] `cat alpha-share-1-card.txt alpha-share-2-card.txt | <each combiner>` succeeds.
- [ ] Bare `-share-N.txt` round trip still passes (no regression in existing tests).
- [ ] Truncated/prose-only input fails with an error naming the file and word count.
- [ ] All 6 `recovery/bin/<target>/lcsas-keyshare*` rebuilt from the fixed source and committed.
- [ ] `make gate` runs the card round-trip tests with no opt-in env var.

## Dependencies & related plans

- **KEY-06** (keyshare outside audit gates) — adds the CI path trigger so the
  new C tests actually gate merges; land together or immediately after.
- **KEY-04** (docs-driven blind variant) — its setup stages real `-card.txt`
  artifacts; it is the end-to-end proof of this fix. This plan first.
- **KEY-07** (typo feedback / prefix entry) — touches the same parsing code;
  rebase on this plan's extractor.
- **UX** "On-disc split-key instructions … flags that do not exist" /
  **KEY-02** — same heir-journey step 2; independent code, same docs.

## Effort

2.5 days: 1.0 Python (extractor + two call sites + tests), 1.0 C (extract
function + main.c + test cases + ASan run), 0.5 cross-target rebuild
(`zig cc` already installed per keyshare-arches flow) + integration test.
