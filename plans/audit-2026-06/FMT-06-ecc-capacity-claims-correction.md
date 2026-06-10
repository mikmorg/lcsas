# FMT-06: Correct the inflated "~30%" ECC repair-capacity claims

**Priority:** P2 · **Severity:** medium · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix ~30% ECC capacity claims to match configured 15% redundancy

## Problem

Two heir/operator-facing recovery documents claim dvdisaster repair "restores up to ~30% of
unreadable sectors" / "can recover approximately 30%". The configured default is 15%
redundancy, and the project's own format doc correctly states 15% redundancy ≈ ~15% of
sectors tolerable. Reed-Solomon erasure capacity ≈ the redundancy fraction, so the real
margin is roughly 13–15% (the ~30% figure was likely confused with dvdisaster's "high" 33%
preset). The inflated number shapes real decisions made under stress: an operator paces
re-burn cadence off the readability-scan guidance, and an heir triages a failing disc
believing they have double the actual margin — delaying re-burn until the disc is past
repairable.

## Evidence

All four cites verified verbatim:

- `recovery/docs/RECOVER.txt:161` — "This restores up to ~30% of unreadable sectors."
- `recovery/docs/READINESS_CHECKLIST.txt:95-96` — "The DVDisaster RS03 ECC layer can recover
  approximately 30% of unreadable sectors".
- `src/lcsas/config/settings.py:32` — `default_ecc_redundancy_pct: int = 15`.
- `docs/DVDISASTER_RS03_FORMAT.md:48-50` — "15% redundancy ≈ can tolerate ~15% of sectors
  being unreadable" (the correct statement).

## Fix design

Edit both docs to derive the figure from the configured redundancy, stated conservatively:

> "With the default 15% redundancy, roughly 13–15% of unreadable sectors can be repaired.
> Higher configured redundancy raises this proportionally. Treat ANY read error as a signal
> to re-burn — do not wait."

One nuance to fold in (cross-ref BURN: "default_ecc_redundancy_pct is silently ignored by
RS03 augmented mode"): dvdisaster's augmented mode sizes parity to the *remaining medium
capacity*, so actual margin can exceed the configured figure — the docs should present 13–15%
as the conservative floor and never the 30% number. Files: `recovery/docs/RECOVER.txt`
(physical-recovery section, line 161 area) and `recovery/docs/READINESS_CHECKLIST.txt`
(readability-scan rationale, lines 95-96).

No code or schema change. Already-burned discs carry the old text forever; corrected text
reaches heirs via the newest meta disc (the documented convention).

## Tests & gates

1. `tests/recovery_hardening/test_ecc_capacity_claims.py` — always-on static doc test
   (pattern of `test_env_var_docs.py`): scan RECOVER.txt and READINESS_CHECKLIST.txt;
   assert `30%` does not appear in a repair-capacity context, and that the stated percentage
   range brackets `LCSASConfig`'s `default_ecc_redundancy_pct` (import the constant so the
   docs can never drift from the configured math again). Runs under
   `make test-recovery-hardening`; gates merges once the GATE plan wires that suite into CI.

## Acceptance criteria

- [ ] `grep -n '30%' recovery/docs/RECOVER.txt recovery/docs/READINESS_CHECKLIST.txt` → empty.
- [ ] Both docs state the 13–15% conservative figure tied to the 15% default.
- [ ] `test_ecc_capacity_claims.py` green; changing `default_ecc_redundancy_pct` without
      touching the docs makes it fail.

## Dependencies & related plans

- FMT-01 (RS03 repair in-house) rewrites the same RECOVER.txt section — land this tiny plan
  first so FMT-01 rebases on correct numbers.
- BURN: redundancy-pct-ignored finding (shared nuance above).
- GATE: recovery-hardening-in-CI (makes the doc test a merge gate).

## Effort

**0.5 focused days** (doc edits + one static test).
