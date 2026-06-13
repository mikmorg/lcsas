# FMT-07: Temper M-DISC longevity claims; disclose the BDXL drive requirement

**Priority:** P2 · **Severity:** medium · **Dimension:** format-durability · **Audit status:** confirmed (medium confidence) · **Ledger:** partially tracked: recovery/docs/UX_CONCERNS.txt ID 007 (generic optical-drive rarity, DEFERRED) — the BDXL-specific drive class and the M-DISC claim accuracy are untracked
**Suggested GH issue title:** Qualify M-DISC 1000-year claim; add BDXL drive caveat to disc docs

## Problem

`DISC_CARE.txt` — burned onto every disc — states M-DISC is "Rated for 1000+ years … Best
choice for archival" and "strongly recommended". Millenniata is defunct, and M-DISC Blu-ray
uses substantially the same inorganic HTL recording layer as standard BD-R HTL, which the same
doc rates at 50–100 years. Presenting unverifiable vendor marketing as fact biases the
archivist toward a price premium instead of toward what actually moves durability: more
copies, offsite sets, and re-burn cadence (ESTATE_PLANNING.md already says re-burn every 5–10
years, contradicting the 1000-year framing).

The sharper half: the 100 GB tiers (BDXL100/MDISC100) require a BDXL-capable drive — a
strictly rarer class than ordinary BD drives — but the drive-availability advice says only
"Keep at least one USB Blu-ray drive". An heir (or the archivist stocking the binder)
replacing a dead drive can buy a non-BDXL drive that reads **zero** of the 100 GB discs, with
nothing on any disc explaining why. That is a real heir dead-end, which is what lifts this to
medium. Concentrating 4x data per disc also quadruples per-disc blast radius — worth one
honest sentence.

## Evidence

Re-checked against current code:

- `src/lcsas/staging/metadata.py:600-607` — "M-DISC (Millenniata): Rated for 1000+ years …
  Best choice for archival." / :607 "M-DISC is strongly recommended for archival purposes."
- `src/lcsas/staging/metadata.py:628-637` — DRIVE AVAILABILITY section: generic "Keep at least
  one USB Blu-ray drive"; `grep -in bdxl src/lcsas/staging/metadata.py` → zero matches.
- `src/lcsas/config/media.py:19-21` — `BDXL100` and `MDISC100` are 100 GB members.
- `docs/ESTATE_PLANNING.md:165` — "Re-burn discs every 5-10 years (even M-Disc degrades
  eventually)"; no BDXL mention anywhere in the file.
- `write_disc_care()` (`metadata.py:553`) takes no config — DISC_CARE.txt is a static string.
  Callers: `burn/orchestrator.py:432`, `meta/builder.py:2427` and `:2431` (all have
  `self._config` in scope).

## Fix design

1. **Make DISC_CARE generation media-aware** (verifier note: required before the conditional
   test is possible). Change the signature to `write_disc_care(self, config: LCSASConfig)`;
   update the three call sites listed above (each already holds `self._config`).
2. **Reword MEDIA LONGEVITY** in the template:
   - "M-DISC: manufacturer-rated 1000+ years. This rating is vendor marketing that cannot be
     independently verified (the manufacturer is defunct); the recording layer is similar to
     standard BD-R HTL (50–100 years). Treat M-DISC as a good BD-R HTL, not as immortal."
   - New headline line above the media list: "Durability comes from REDUNDANCY and RE-BURN
     CADENCE (see PERIODIC VERIFICATION), not from media choice."
   - Drop "strongly recommended"; keep M-DISC as a reasonable premium option.
3. **BDXL drive caveat** in DRIVE AVAILABILITY:
   - Always: "100 GB discs (BDXL / M-DISC 100) require a BDXL-capable drive. Many BD drives
     are NOT BDXL-capable — check the spec sheet before buying a replacement."
   - Conditionally (when `config.default_media_type in (MediaType.BDXL100,
     MediaType.MDISC100)`), prepend: "THIS ARCHIVE USES 100 GB BDXL MEDIA. The binder drive
     MUST list BDXL support, or it will read none of these discs."
   - One honest sentence on blast radius: a 100 GB disc concentrates 4x the data of a BD25 —
     keep at least the same copy count.
4. **ESTATE_PLANNING.md** §5 maintenance checklist: add "- [ ] If your archive uses 100 GB
   BDXL media, verify the stored drive is BDXL-capable" near line 165.

No catalog/schema impact. Already-burned discs keep the old text; corrected guidance ships on
every disc burned after this lands and on the newest meta disc.

## Tests & gates

1. `tests/unit/test_disc_care_media_guidance.py` — always-on (`make test-unit`, CI):
   - generate DISC_CARE.txt via `HolographicInjector.write_disc_care(config)` with
     `default_media_type=BDXL100` and `MDISC100` → assert the "THIS ARCHIVE USES 100 GB BDXL"
     warning present; with `BD25` → assert it is absent but the generic BDXL spec-sheet caveat
     remains;
   - assert "1000+" appears only qualified by "manufacturer-rated" and "strongly recommended"
     does not appear;
   - assert the redundancy/re-burn headline line is present.
2. Extend the existing estate-planning/docs static checks (recovery_hardening doc-pin pattern,
   e.g. `test_env_var_docs.py`) with the ESTATE_PLANNING.md BDXL checklist line — joins the
   docs-vs-reality contract gate family.

## Acceptance criteria

- [ ] DISC_CARE.txt for a BDXL100 archive opens DRIVE AVAILABILITY with the must-be-BDXL
      warning; BD25 output carries only the generic caveat.
- [ ] "strongly recommended" gone; 1000-year figure qualified as manufacturer-rated.
- [ ] ESTATE_PLANNING.md contains the BDXL checklist item.
- [ ] All three `write_disc_care` call sites compile under `make typecheck` (mypy strict).

## Dependencies & related plans

- UX: "printed Recovery Card template never created" + UX_CONCERNS ID 007 — drive-availability
  guidance overlaps; keep wording consistent.
- FMA: "no blast-radius reporting" — the per-disc blast-radius sentence here is doc-level; the
  tooling answer lives in that plan.
- No ordering constraints; standalone.

## Effort

**1 focused day** (0.5 impl across the three call sites + wording, 0.5 tests). No special
environment.

---
**Implemented:** 2026-06-13. As planned. `write_disc_care` made media-aware (signature `write_disc_care(self, config: LCSASConfig | None = None)` — `| None` accommodates the meta-volume caller whose `self._config` is optional; both production call sites pass their config). MEDIA LONGEVITY reworded with redundancy/re-burn headline and qualified M-DISC rating; DRIVE AVAILABILITY gains the conditional must-be-BDXL warning + always-on generic caveat + blast-radius sentence. ESTATE_PLANNING.md §5 checklist item added. Tests: tests/unit/test_disc_care_media_guidance.py (always-on) + doc-pin in tests/recovery_hardening/test_recovery_card_docs.py.
