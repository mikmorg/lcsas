# KEY-05: Heir docs name only the python3 combiner; C/Windows/printed-guide paths uncovered

> **STATUS: RESOLVED** — landed in `e310881` (docs+meta: lcsas-keyshare is the primary combiner in all heir docs [KEY-05]); guarded by `tests/unit/test_staging_metadata.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: recovery/docs/UX_CONCERNS.txt ID 001 (general Windows gap only — keyshare omission untracked)
**Suggested GH issue title:** Document lcsas-keyshare as primary combiner in all heir docs

## Problem

Phase 5 shipped a tier-1-grade C combiner (`lcsas-keyshare`) on all 6 targets
precisely so split-key reconstruction needs no Python — and a blind run proved
that path works. But every heir-facing document says only
`python3 keyshare_combine.py`: the START_HERE split block, the KEY_INFO share
lines, and the ESTATE_PLANNING letter template. Nothing on any disc points the
heir at `recovery/bin/<arch>/lcsas-keyshare`. If python3 won't run — the very
scenario the tier-1 design exists for — the heir has no documented fallback.

It is worse on Windows: `lcsas-keyshare.exe` ships in
`recovery/bin/x86_64-windows/`, bare Windows has no python3 at all, yet
`restore.bat` and `RECOVER_WINDOWS.txt` contain zero key-share mentions. And
`docs/RECOVERY_GUIDE.md` — the document ESTATE_PLANNING tells the owner to
print for the physical binder — never mentions shares or the
reconstruct-first pre-step. `RECOVER.txt`'s PASSWORD RECOVERY section flatly
says "There is no password recovery path", which is false for split-key
archives.

## Evidence

Re-checked 2026-06-10 against master:

- `src/lcsas/staging/metadata.py:61` and `:363` — `python3
  keyshare_combine.py <card1> <card2>` is the only combiner named in
  KEY_INFO/START_HERE.
- `grep -ril 'keyshare|share card|key share|SLIP'` over
  `docs/RECOVERY_GUIDE.md`, `recovery/docs/RECOVER.txt`,
  `recovery/docs/RECOVER_WINDOWS.txt`, `recovery/scripts/restore.bat` → no
  matches (rc 1). `RECOVER.txt:166-170` — "There is no password recovery
  path."
- `recovery/bin/` — all 6 target dirs present; `x86_64-windows/` contains
  `lcsas-keyshare.exe` (verified by ls).
- `.claude/skills/key-escrow/PLAN.md` C5.4/C5.5 — C combiner is the proven
  python-free path, but only `agent_prompt_split.txt` was updated, no heir
  doc.

## Fix design

Mirror the tier ordering (C binary primary, python fallback) everywhere the
heir can read:

1. **`src/lcsas/staging/metadata.py`** — `_share_recovery_lines` (40-81) and
   the START_HERE `split_block` (352-374): STEP 1 becomes
   ```
   On the META disc, run the share combiner for your machine:
       recovery/bin/<machine>/lcsas-keyshare <card1> <card2>
   <machine> is x86_64 for most PCs (aarch64 = newer ARM/Apple,
   x86_64-windows = Windows: use lcsas-keyshare.exe).
   If that program will not run, the fallback is:
       python3 keyshare_combine.py <card1> <card2>
   ```
   Keep line-width ≤ the existing card framing; KEY-02's revised STEP 2 text
   stays.
2. **`recovery/docs/RECOVER.txt`** — amend PASSWORD RECOVERY (166): "…no
   password recovery path **unless the archive used split keys** — see
   KEY SHARES below"; add a `KEY SHARES (SPLIT PASSWORDS)` section: when
   KEY_INFO.txt says the password is split, run
   `bin/<arch>/lcsas-keyshare` (python fallback), then proceed normally.
3. **`recovery/docs/RECOVER_WINDOWS.txt`** — add the same pre-step section
   using `recovery\bin\x86_64-windows\lcsas-keyshare.exe card1.txt card2.txt >
   repo.key` (coordinate exact paths with the UX plan fixing that file's
   wrong binary paths).
4. **`recovery/scripts/restore.bat`** — comment block + the password prompt
   help text: one line pointing split-key users at `lcsas-keyshare.exe` and
   KEY_INFO.txt.
5. **`docs/RECOVERY_GUIDE.md`** — add a conditional "If KEY_INFO.txt says the
   password is split…" pre-step (the guide is generic/printed, so the wording
   is conditional rather than gated on config).
6. Track closure against UX_CONCERNS ID 001's Windows bullet.

No schema/catalog impact. Already-burned discs keep the python3-only text;
new META discs carry the corrected static docs, which is the surface heirs
use.

## Tests & gates

- `tests/unit/test_staging_metadata.py::test_split_block_names_c_combiner` —
  rendered KEY_INFO + START_HERE (key_split=True) mention
  `recovery/bin` and `lcsas-keyshare`, with `python3` present only after the
  C path (assert ordering); single-key render mentions neither.
- `tests/recovery_hardening/test_keyshare_docs.py` — static doc test (pattern
  of `test_disc_swap_docs.py`): `RECOVER.txt` and `RECOVER_WINDOWS.txt` each
  contain a key-shares section naming `lcsas-keyshare(.exe)`; `RECOVER.txt`'s
  "no password recovery" sentence carries the split-key exception;
  `restore.bat` mentions `lcsas-keyshare.exe`. Runs in
  `make test-recovery-hardening`.
- KEY-02's `test_heir_doc_commands.py` automatically covers any flags the new
  text references.
- Optional follow-on (with KEY-04): a docs-driven blind variant with python3
  removed from the bundled tools, forcing the C-combiner path from on-disc
  docs alone.

## Acceptance criteria

- [ ] Rendered START_HERE/KEY_INFO present `lcsas-keyshare` first, python3 as fallback.
- [ ] RECOVER.txt, RECOVER_WINDOWS.txt, restore.bat, RECOVERY_GUIDE.md each have a key-share pre-step; static tests pin all four.
- [ ] `grep -ril keyshare` over those four files returns all four.
- [ ] Single-key archives still render zero share instructions.

## Dependencies & related plans

- **KEY-01** must land first (don't document the C combiner against cards it
  still rejects); **KEY-02** supplies the STEP 2 wording.
- **UX** "RECOVER_WINDOWS.txt gives wrong binary paths" — coordinate path
  conventions in the same file; **INFRA-01** (Windows e2e scaffolding) is
  where any executable check of the `.exe` instructions would run.
- **KEY-04** — the python3-removed blind variant extension.

## Effort

1 day (0.5 doc/text edits across 5 surfaces, 0.5 render + static tests).

---
**Implemented:** 2026-06-11. As planned, with two deviations: (1) `_bundle_tier1_binaries` now also relocates `lcsas-keyshare[.exe]` into the on-disc rust-triple dirs — the UX-04 doc-contract gates forbid the legacy `x86_64-windows\` path in burned Windows docs/restore.bat, so the documented `bin\x86_64-pc-windows-gnu\lcsas-keyshare.exe` path had to actually exist; (2) KEY-01 had not landed at implementation time, so the documented invocations still reject real `-card.txt` files until it does (no worse than the prior python3-only text).
