# GATE-06: tier1-missing blind variant is permanently XFAIL — the cascade's headline hedge is unproven and can never fail a run

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (medium confidence) · **Ledger:** tracked: issue #227 + in-file comments at tests/e2e/cdemu_blind_restore/run_variant.sh:99-112 and Makefile:123-147
**Suggested GH issue title:** Promote tier1-missing blind variant out of permanent XFAIL

## Problem

The recovery cascade's whole reason for having tiers 2 and 3 is the scenario
where tier 1 (the C `lcsas-restore` binary) won't run on the heir's machine.
The blind-restore variant that simulates exactly this — `tier1-missing` — is
the *default* member of `LCSAS_VARIANT_XFAIL` in
`tests/e2e/cdemu_blind_restore/run_variant.sh`: a red score prints `XFAIL` and
**exits 0**, with no expiry date and no mechanism that ever forces promotion.
On top of that, all blind variants are local-only (sudo + cdemu, impossible on
hosted CI per test.yml's own comment) and cost-gated (~$5/run behind
`LCSAS_BLIND_ACK_COST=1`). So even when an operator pays to run the sweep, the
heir's most plausible cascade failure structurally cannot fail it.

The verifier's nuance matters for scoping: the *tier-3 machinery itself* is no
longer unproven. The `tier1-tier2-missing` variant (tier-3 takeover including
disc swaps) was promoted out of XFAIL at 15/15 in cycle 9 (2026-05-28, PRs
#285/#286), and deterministic hardening tests pin the tier-3 swap protocol
(`test_tier3_disc_swap.py`) and the tier-2 multi-disc skip
(`test_restore_skips_tier2_on_multi_disc.py`). What remains unproven is the
specific `tier1-missing` chain — dispatcher detects no tier-1 binary → tier-2
preflight → tier-2 skipped on multi-disc (or fails) → tier-3 completes — as a
live, end-to-end heir journey. Issue #227's own comment says the fix is
partial: "falls to tier-3, but tier-3 disc-swap protocol still needs
verification" *in this variant*. Until that scores 15/15, the TIERS.txt
promise that tiers 2/3 are working hedges is unvalidated for the case they
exist for, and the XFAIL guarantees nobody is ever forced to notice.

## Evidence

Re-checked 2026-06-10 against master:

- `tests/e2e/cdemu_blind_restore/run_variant.sh:113` —
  `XFAIL="${LCSAS_VARIANT_XFAIL:-tier1-missing}"`; lines 99-112 document the
  rationale ("issue #227 partial fix: falls to tier-3, but tier-3 disc-swap
  protocol still needs verification") and record the cycle-7/8/9 promotions of
  the other four variants.
- `run_variant.sh:126-129` — `if [ "$is_xfail" -eq 1 ]; then echo "XFAIL...";
  exit 0; fi` — a red score on this variant exits 0, forever.
- `run_variant.sh:119-124` — an XPASS also exits 0 (prints "drop from
  LCSAS_VARIANT_XFAIL" but enforces nothing).
- `Makefile:123-147` — variant catalogue comment: "tier1-missing — ... (XFAIL
  pending #227)"; `Makefile:148-160` — `blind-restore-variants` cost gate
  (`LCSAS_BLIND_ACK_COST != 1` → error).
- `.github/workflows/test.yml:68-73` — cdemu cannot run on hosted runners
  (vhba kernel module), so no blind variant runs in CI by construction.
- `tests/recovery_hardening/test_tier_fallback.py:1-25` — docstring: the
  production fall-through itself is opt-in (`LCSAS_TIER_FALLBACK=1`); the
  deterministic tests pin script-level fall-through, not the live multi-disc
  tier1-missing journey.
- `recovery/scripts/restore.sh:1008-1029` — tier-2 multi-disc skip + tier-3
  fall-through code that the missing deterministic e2e must drive.

## Fix design

Three parts: a $0 deterministic gate for the untested chain, an XFAIL ledger
that expires, and the actual live promotion.

1. **Deterministic tier1-missing multi-disc e2e** (the verifier's refined
   scope: drive `restore.sh` itself, not the restorer — the restorer-level
   swap protocol is already pinned by `test_tier3_disc_swap.py`). New
   `tests/recovery_hardening/test_restore_sh_tier1_missing_multidisc.py`:

   - Build a fake meta tree with **no** `lcsas-restore` for the host target
     (reuse `_install_failing_binary`/repo-layout helpers from
     `test_restore_skips_tier2_on_multi_disc.py`, which already stub
     `recovery/bin/x86_64-unknown-linux-musl/` binaries) and a multi-disc
     repo (no `$REPO/data/` subtree) whose packs are split across two plain
     directories standing in for disc mounts.
   - Run `recovery/scripts/restore.sh` non-interactively, feeding the
     swap prompt by repointing/refilling the mount directory between
     prompts (same driver technique as `test_tier3_disc_swap.py`).
   - Assert, in order: stderr contains the tier-1-absent probe message;
     `[tier 2] skipped: rustic-static cannot drive multi-disc`
     (restore.sh:1018); tier-3 starts; restore completes rc=0; restored
     content is byte-identical to the source fixture (sha256 compare).
   - No cdemu, no sudo, no LLM: runs in `make test-recovery-hardening` and
     in CI once GATE-02's job lands. Needs python3 (tier-3) only; if the
     fixture uses zstd-compressed packs, gate on `zstandard` availability
     with an honest skip plus inclusion in GATE-02's passed-count floor.

2. **XFAIL ledger with expiry + strict XPASS.**
   - Move the default XFAIL set out of the inline `:-tier1-missing` default
     into a tracked file `tests/e2e/cdemu_blind_restore/XFAIL.list`, one line
     per entry: `tier1-missing  issue=#227  expires=2026-09-01`.
     `run_variant.sh` parses it (env `LCSAS_VARIANT_XFAIL` still overrides
     for ad-hoc runs).
   - New `tests/recovery_hardening/test_blind_variant_xfail_ledger.py`
     (always-on, pure file parsing): every XFAIL entry must carry an
     `issue=#N` and a future `expires=` date; an expired entry fails the
     hardening suite — the team must either promote the variant or
     consciously re-date it in a reviewed diff.
   - In `run_variant.sh`, make XPASS exit **1** (strict-xpass): a variant
     that scores 15/15 while still listed forces the ledger cleanup
     immediately instead of relying on someone reading the log.

3. **Live promotion.** Once (1) is green: run the live variant
   (`LCSAS_BLIND_ACK_COST=1 sudo -E bash run_variant.sh tier1-missing`,
   haiku model per project policy) until 15/15, then delete the entry from
   `XFAIL.list` and update the promotion log comment at
   `run_variant.sh:103-112`. If a real product gap surfaces (the #227
   residual), file it against the owning dimension and re-date the ledger
   entry — visibly, not silently.

4. **Interim honesty in docs.** Until promotion, add one line to
   `recovery/docs/TIERS.txt` (tier-2 section): "the tier-1-missing fallback
   journey has not yet passed a live blind restore (issue #227); deterministic
   coverage only." Remove it at promotion. This keeps the on-disc/operator
   contract truthful in the window.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_restore_sh_tier1_missing_multidisc.py` —
  always-on in `make test-recovery-hardening`; in CI via GATE-02's job.
  Asserts the full dispatcher → tier-2-skip → tier-3 → byte-identical chain.
- `tests/recovery_hardening/test_blind_variant_xfail_ledger.py` — always-on;
  fails on missing issue ref or expired date; fails if `run_variant.sh`
  reintroduces a hard-coded non-empty XFAIL default.
- `run_variant.sh` strict-XPASS — exercised on the next live sweep
  (`make blind-restore-variants`, local, cost-gated; unchanged cadence).
- Live `tier1-missing` 15/15 — local-only by necessity (cdemu/vhba); the
  promotion record lives in the `run_variant.sh` comment block like cycles
  7-9 did.

## Acceptance criteria

- [ ] New deterministic test passes on master and fails when restore.sh's
      tier-2 multi-disc skip is reverted on a scratch branch.
- [ ] `XFAIL.list` exists; `grep tier1-missing` shows issue=#227 and an
      explicit expiry; ledger test fails when the date is moved to the past.
- [ ] `run_variant.sh`: red score on an unlisted variant exits 1; 15/15 on a
      listed variant exits 1 (XPASS strict); red on a listed, unexpired
      variant exits 0.
- [ ] Live `tier1-missing` run recorded at 15/15 and the entry deleted — or
      the residual gap filed as its own issue with the ledger re-dated.
- [ ] TIERS.txt caveat present until promotion, removed after.

## Dependencies & related plans

- **GATE-02** (recovery-hardening in CI) — makes the deterministic test and
  the ledger test merge-blocking; land GATE-02 first or together.
- **UX-08** (cross-OS journey gates) — owns the broader "blind restore is
  Linux/local/cost-gated" problem; this plan fixes the one variant that can
  never fail.
- **RST-03** (tier-3 skip-and-continue) / **RST-01** (multi-disc false
  failure) — product fixes that the live run may depend on; run the live
  promotion after they land to avoid burning $5 on known-red runs.
- Issue #227 — this plan closes its verification residue.

## Effort

2.5 days: 1.5 deterministic e2e (fixture + prompt driver), 0.5 ledger +
strict-XPASS + tests, 0.5 live runs/promotion. Live leg needs the local VM
with cdemu/vhba and sudo (not CI-able); ~$5-15 of blind-test compute, haiku
model only.
