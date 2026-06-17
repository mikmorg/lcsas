# UX-08: the only end-to-end journey gate is Linux-only, local-only, opt-in

> **STATUS: RESOLVED** — landed in `0241ea4` (recovery: pin test_restore_bat_e2e.sh; refresh stale doc manifest hashes [UX-08]); guarded by `tests/recovery_hardening/test_readiness_checklist.py`.

**Priority:** P1 · **Severity:** medium · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: test.yml:69-74 documents the cdemu CI exclusion; restore-windows.md "Test coverage and gaps" documents the .bat gap; "no functional gate for any non-Linux journey" is tracked nowhere as a risk
**Suggested GH issue title:** Add always-on journey contract gates and a Windows journey gate

## Problem

Everything the heir journey depends on, across all OSes, is validated — when at
all — by `make blind-restore`: refuses to run without `LCSAS_BLIND_ACK_COST=1`
(~$5/run), needs sudo + cdemu/vhba, and is explicitly not run in CI. There is no
Windows equivalent at any level: `restore.bat` is never executed by any test
(static string checks only), the wine e2e drives the `.exe` directly and bypasses
the .bat, and macOS has nothing. The verifier strengthened the finding: CI runs only
`make test-unit` + `make test-integration`, so even the `tests/recovery_hardening/`
tier — where most journey-adjacent doc tests live — never gates a merge.

This process gap is *why* UX-01/02/04/05 (script/doc contract breaks on the
non-Linux and split-key paths) shipped and persisted: the journey-level safety net
covers only the one path that already works. This plan owns the wiring: make the
contract layer always-on, give Windows a functional gate, and make the opt-in blind
coverage at least auditable.

## Evidence

(Re-checked 2026-06-10.)

- `Makefile:79-99` — `blind-restore` exits 1 unless `LCSAS_BLIND_ACK_COST=1`; runs
  `sudo tests/e2e/cdemu_blind_restore/setup.py`. Variants at `Makefile:170-189`
  (`blind-restore-single-key`, `blind-restore-split-2of5`) gated identically.
- `.github/workflows/test.yml:69-74` — "cdemu is NOT installed in CI ... the e2e
  blind-restore suite ... is intentionally skipped here"; the workflow runs only
  `make test-unit` and `make test-integration`.
- `tests/unit/test_restore_bat_dispatcher.py:1-8` — "we settle for static-content
  assertions"; `recovery/tests/test_e2e_windows.sh` execs the `.exe` under wine,
  never the .bat (`docs/workflows/restore-windows.md:159-166`).
- No macOS journey test of any kind (see also GATE "macOS tier-1 binaries never
  executed").

## Fix design

Four layers, cheapest-first:

1. **Always-on contract layer (CI, every push).** The doc/script contract tests from
   UX-02/03/04/06 land in `tests/unit/test_doc_command_contract.py` and run via the
   existing `make test-unit` job — no new workflow needed. This plan adds a
   `journey-contracts` checklist to the PR template-free reality: verify in
   `.github/workflows/test.yml` that `make test-unit` collects the file (a one-line
   `pytest --collect-only` sanity step is enough), so a future test-tree
   reorganization can't silently drop the layer.
2. **Windows functional gate.** Adopt INFRA-01's two-job workflow (Linux job builds a
   `lcsas meta build` fixture tree; `windows-latest` job runs `restore.bat` against
   it and asserts a byte-correct restore). Make it required for changes under
   `recovery/scripts/restore.bat`, `src/lcsas/meta/`, and `src/lcsas/staging/metadata.py`.
   Locally, add `recovery/tests/test_restore_bat_e2e.sh` (wine `cmd /c`, smoke-only —
   wine cmd is not a faithful interpreter) wired into `make -C recovery test` when
   wine is present, and into `audit-gate.yml`'s path filter (coordinate with the GATE
   plan closing that filter's holes).
3. **Opt-in coverage made auditable.** Add a "JOURNEY DRILL LOG" section to
   `recovery/docs/READINESS_CHECKLIST.txt`: one line per blind variant
   (single-key, split-2of5, tier1-missing, windows) recording last-run date, model
   (haiku per project policy), and score; require it updated per meta-disc build.
   Add `Makefile` target `blind-restore-windows`: wine-based agent variant driving
   the .bat journey end-to-end, opt-in via `LCSAS_BLIND_ACK_COST=1` like its
   siblings (run_variant.sh gains a `windows` case that swaps cdemu mounts for a
   directory tree + wine).
4. **macOS.** Functional macOS journey coverage (executing the mac tier-1 binaries)
   is owned by the GATE plan "macOS tier-1 binaries never executed"; this plan only
   ensures the contract layer (1) covers the macOS-facing doc text (START_HERE
   `/Volumes/...` commands from UX-05).

## Tests & gates

- `tests/unit/test_doc_command_contract.py` collected and green in CI test.yml
  (layer 1) — always-on.
- INFRA-01 workflow (e.g. `.github/workflows/windows-e2e.yml`) green and required on
  the listed paths (layer 2) — always-on once INFRA-01 lands.
- `recovery/tests/test_restore_bat_e2e.sh` in `make -C recovery test` — local,
  conditional on wine.
- `make blind-restore-windows` — opt-in, cost-gated; logged in READINESS_CHECKLIST.
- Meta-test: a unit test asserting READINESS_CHECKLIST.txt contains the JOURNEY
  DRILL LOG section headers (keeps the ledger from being deleted silently).

## Acceptance criteria

- [ ] A PR that reintroduces any UX-02/03/04/06 doc drift fails CI on test.yml.
- [ ] A PR that breaks restore.bat repo discovery fails the Windows e2e workflow.
- [ ] `make blind-restore-windows` exists, refuses without the cost ack, and
      completes a scored run locally.
- [ ] READINESS_CHECKLIST.txt shows a dated drill-log entry per variant after the
      next meta-disc build.

## Dependencies & related plans

- **INFRA-01** — hard dependency for layer 2 (wine-cmd loop, tmate debug, two-job
  gate). **UX-01** must land before the Windows e2e can pass at all.
- **UX-02/03/04/06** — supply the contract tests layer 1 wires in.
- **GATE** plans: recovery_hardening/e2e-in-CI, audit-gate path-filter holes,
  blind-variant XFAIL, macOS execution — this plan stays out of their scope and only
  references them.

## Effort

2 days: 0.5 CI wiring/collect-sanity, 1 blind-restore-windows variant, 0.5 drill log
+ checklist meta-test. Needs wine locally; GitHub windows-latest via INFRA-01.

---
**Implemented:** 2026-06-13. Layers 1, 3, and the local arm of layer 2 landed;
the layer-2 windows-e2e.yml CI workflow is intentionally deferred to its hard
prerequisites. Done: (1) always-on `journey-contracts` collect-only sanity step
in test.yml guarding `test_doc_command_contract.py`; (2-local)
`recovery/tests/test_restore_bat_e2e.sh` wine smoke (no-repo guard + repo
discovery/arch detect) wired into `make -C recovery test`, conditional on wine,
already inside the audit-gate path filter (recovery/tests/** + recovery/Makefile,
GATE-04); (3) JOURNEY DRILL LOG section in READINESS_CHECKLIST.txt + quick-ref
row, `blind-restore-windows` Makefile target (cost-gated like its siblings),
`windows` case in run_variant.sh, and a checklist meta-test
(test_readiness_checklist_has_journey_drill_log). DEVIATION: INFRA-01 and UX-01
have NOT landed (no commits/markers), so the windows variant + the
windows-latest CI workflow cannot complete a real scored run — the variant and
the Makefile target FAIL LOUD naming UX-01/INFRA-01 as the blocker rather than
faking a score, and windows-e2e.yml is left for the INFRA-01 plan to add.
Acceptance criteria #3 (target exists + refuses without cost ack) met; #2/#4's
green scored run blocked on those prerequisites. Layer 4 (macOS) already covered
by the existing contract tests — verify-only, no change.
