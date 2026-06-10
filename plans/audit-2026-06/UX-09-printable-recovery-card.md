# UX-09: promised printable Recovery Card was never built

**Priority:** P2 · **Severity:** low · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** tracked-adjacent: recovery/docs/UX_CONCERNS.txt ID 006 (WONTFIX-CRYPTOGRAPHIC overall; the RECOVERY_CARD.txt mitigation bullet is unimplemented and untracked as its own item)
**Suggested GH issue title:** Add lcsas estate card: generated printable Recovery Card

## Problem

The heir scenario assumes a printed sheet stored with the discs (password location,
disc inventory, where to start). UX_CONCERNS ID 006 — whose impact is rated HIGHEST,
"the actual #1 failure mode in real-world archive inheritance" being password loss —
promises "a paper-printable Recovery Card template under docs/RECOVERY_CARD.txt".
The file does not exist, and ESTATE_PLANNING.md offers only a manual checklist the
owner must hand-assemble. The split-key path *does* generate printable per-share
cards (`lcsas key split` writes `<repo>-share-N-card.txt`), but no artifact covers
the whole-archive sheet: repo names, disc count, K-of-N scheme, key-storage hints,
and the literal first command. The highest-impact mitigation is left entirely to
owner diligence.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/docs/UX_CONCERNS.txt:129-139` — ID 006 mitigation: "Add a
  paper-printable 'Recovery Card' template under docs/RECOVERY_CARD.txt".
  `docs/RECOVERY_CARD.txt` does not exist.
- `docs/ESTATE_PLANNING.md:34-37` — "Maintain a paper manifest — Print a list of all
  disc labels" — manual, no generator.
- `src/lcsas/cli/main.py:3183-3212` — `_share_card_text` exists (per-share cards
  only); its combine instruction (`:3201`) names `lcsas key combine`, which assumes
  an installed LCSAS rather than the on-disc `keyshare_combine.py`.

## Fix design

1. **New subcommand `lcsas estate card`** (in `src/lcsas/cli/main.py`, alongside the
   `key` family): `--config CONFIG [--output PATH] [--db PATH]`. Emits a one-page
   plain-text `RECOVERY_CARD.txt` populated from config (+ catalog when available):
   - owner (`archive_owner`), description, technical_contact;
   - key location: `key_storage_hints`; if `key_split`, the K-of-N line
     ("any {key_threshold} of {key_shares} share cards; holders listed below: ____");
   - disc inventory: volume count + label prefix from the catalog via the existing
     db queries when `--db` resolves (fall back to fill-in blanks `______` so the
     card is still printable with no catalog);
   - the literal first steps per OS, matching the real interface (UX-02/05 text):
     "Windows: open the LCSAS_META disc, double-click restore.bat. macOS/Linux:
     `sh /mnt/restore.sh ~/restored`";
   - a "store this sheet WITH the discs, and the password SOMEWHERE ELSE" footer.
2. **Share-card fix** (verifier refinement): `_share_card_text` (`main.py:3201`) —
   after the `lcsas key combine` line add: "or, on the recovery disc:
   `python3 keyshare_combine.py <card1> <card2>`" (keep wording in lockstep with
   KEY's combiner decisions — coordinate, don't duplicate).
3. Reference the generator from `docs/ESTATE_PLANNING.md`'s paper-manifest checklist
   and resolve UX_CONCERNS ID 006's bullet with a pointer.

Catalog note: read-only queries against schema v5; no schema change. With an old or
absent catalog the card degrades to blanks — never fails.

## Tests & gates

- `tests/unit/test_recovery_card.py` — generate from a fixture config (split and
  non-split): assert owner, K/N values, key_storage_hints, and a `restore.sh`
  command that passes UX-02's flag-contract check (import the same
  `accepted_restore_sh_flags()` helper); with a fixture catalog assert the volume
  count appears; without one assert blanks. Always-on via `make test-unit`.
- UX-02's `test_doc_command_contract.py` corpus gains the generated card output.

## Acceptance criteria

- [ ] `lcsas estate card --config tests/fixtures/... --output /tmp/card.txt` produces
      a one-page card with all fields above; prints fine as plain text.
- [ ] Card commands contain no phantom flags (contract test green).
- [ ] Share cards mention the on-disc combiner path.
- [ ] ESTATE_PLANNING.md and UX_CONCERNS ID 006 reference the generator.

## Dependencies & related plans

- **UX-02** (real command text + contract helper) and **UX-05** (canonical per-OS
  commands) — land first so the card inherits correct text.
- **KEY** "Recovery Card / printed artifacts" item (KEY-09-area) — same gap from the
  key side; implement once here, KEY plan should reference this.
- **KEY** combiner decisions — affects step-2 wording on the card.

## Effort

1 day: 0.5 generator + CLI, 0.5 tests/docs. No special environment.
