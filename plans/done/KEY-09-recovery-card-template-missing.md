# KEY-09: Promised Recovery Card template doesn't exist; single-key printed artifacts are manual homework

> **STATUS: RESOLVED** — landed in `9b0b3df` (cli+docs: ship single-key Recovery Card generator + template [KEY-09]); guarded by `tests/unit/test_cli_key.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: recovery/docs/UX_CONCERNS.txt ID 006 (mitigation bullet, unimplemented; item marked WONTFIX-CRYPTOGRAPHIC)
**Suggested GH issue title:** Ship docs/RECOVERY_CARD.txt + `lcsas key card` generator

## Problem

For the default single-key archive, the entire key-availability story is the
owner manually transcribing the password per the ESTATE_PLANNING checklist.
UX_CONCERNS ID 006 — rated "IMPACT: HIGHEST … the actual #1 failure mode in
real-world archive inheritance" — lists a concrete mitigation: "a
paper-printable Recovery Card template under docs/RECOVERY_CARD.txt". The
file does not exist, no generator produces a printable single-key sheet, and a
hand-copied password has no transcription check of any kind. The split-key
path got machine-generated cards with checksummed mnemonics; the *more common*
single-key path got nothing — an owner's transcription typo is discovered
decades later, at restore time, as an unrecoverable archive.

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/docs/UX_CONCERNS.txt:121-139` — ID 006, STATUS
  WONTFIX-CRYPTOGRAPHIC, IMPACT HIGHEST; mitigation bullet at 134-136 names
  `docs/RECOVERY_CARD.txt`.
- `ls docs/RECOVERY_CARD.txt` → No such file or directory; repo-wide grep
  finds RECOVERY_CARD only in that UX_CONCERNS line.
- `docs/ESTATE_PLANNING.md:41-53` — manual key-management checklist, no
  generator, no transcription check.
- `src/lcsas/cli/main.py:3183-3211` — `_share_card_text` exists for the split
  path only.

## Fix design

Two artifacts — a static fill-in template and a generator (generator chosen
as primary because it can embed a transcription check code; the static
template covers owners who never run the CLI):

1. **`lcsas key card --repo R --config lcsas.toml [--out FILE]`** — new `key`
   subcommand beside split/combine (`main.py:483-520` parser, dispatch
   3155-3162). Renders a printable single-key recovery card in the
   `_share_card_text` visual style: repo name, key file name
   (`repo_cfg.password_file.name`), `config.key_storage_hints`, creation
   date, the disc label prefix, a blank PASSWORD box for *handwriting*, and a
   **check code**: first 4 hex chars of SHA-256(password bytes). The password
   itself is never printed by the tool — the owner writes it by hand; the
   check code lets them (and later the heir) verify the transcription:
   `lcsas key card --check FILE` recomputes the code from a typed-in file and
   prints MATCH/MISMATCH. Security note on the card and in docs: the code
   leaks ~16 bits of an oracle on the password — negligible against a
   high-entropy password but stated honestly; `--no-check-code` opts out.
2. **`docs/RECOVERY_CARD.txt`** — static fill-in-the-blanks version of the
   same card (owner, repo, key file name, storage locations, date, optional
   check code with the one-liner to compute it:
   `python3 -c "import sys,hashlib;print(hashlib.sha256(open(sys.argv[1],'rb').read().rstrip(b'\n')).hexdigest()[:4])" KEYFILE`).
3. **References** — ESTATE_PLANNING §2 checklist gains "print a Recovery Card
   (`lcsas key card` or docs/RECOVERY_CARD.txt) per storage location";
   `key_storage_hints` guidance in START_HERE stays unchanged (the card is an
   off-disc artifact by design — never staged onto a volume).
4. **Ledger** — update UX_CONCERNS ID 006: keep WONTFIX-CRYPTOGRAPHIC for the
   core impossibility, but mark the Recovery Card mitigation IMPLEMENTED with
   the file/command names so the promise and reality match.

No schema/catalog impact; nothing is burned to disc (deliberately — key/data
separation).

## Tests & gates

Always-on (`make test-unit` → `gate`):

- `tests/unit/test_cli_key.py::test_key_card_renders` — card contains repo
  name, key file name, hints, date, 4-char check code; `--no-check-code`
  omits it; output never contains the password bytes.
- `tests/unit/test_cli_key.py::test_key_card_check_mode` — `--check` on the
  correct password file → MATCH rc 0; on a one-char typo → MISMATCH rc 1.
- `tests/recovery_hardening/test_recovery_card_docs.py` (pattern of
  `test_env_var_docs.py`) — `docs/RECOVERY_CARD.txt` exists, contains the
  check-code one-liner, and `docs/ESTATE_PLANNING.md` references it; the
  UX_CONCERNS ID 006 mitigation line marked implemented.

## Acceptance criteria

- [ ] `lcsas key card --repo alpha --config …` prints a complete card; password absent from output.
- [ ] `--check` verifies/refutes a typed transcription deterministically.
- [ ] `docs/RECOVERY_CARD.txt` exists and is referenced from ESTATE_PLANNING.
- [ ] UX_CONCERNS ID 006 mitigation bullet no longer dangling.

## Dependencies & related plans

- **KEY-03** — `lcsas key verify --password-file` is the live-repo check that
  complements the card's offline check code; share the CLI plumbing.
- **UX** "The 'printed sheet' leg of the journey is unbacked by tooling" —
  same gap seen from the UX dimension; this plan is the implementation, that
  plan should be folded into or reference this one.
- **FUP-03** (disc-confidentiality) — reviewer of the 16-bit check-code
  disclosure note.

## Effort

1.5 days: 0.75 generator + check mode, 0.25 static template + doc refs, 0.5
tests.

---
**Implemented:** 2026-06-13. As planned: added `lcsas key card` (render + `--check`/`--code` verify, `--no-check-code`), `docs/RECOVERY_CARD.txt`, ESTATE_PLANNING §2 reference, UX_CONCERNS ID 006 marked IMPLEMENTED. Note: `--config` is the existing global flag (placed before the subcommand), not a per-`card` flag, to satisfy the doc-command contract gate.
