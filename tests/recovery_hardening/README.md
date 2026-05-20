# Recovery hardening tests

These are the **last gate** before a build is considered shippable.
Every test in this directory exists because a specific bug slipped
through the unit/integration/e2e tiers and was only caught when a
real blind-restore agent ran the production recovery path end-to-end.

They are pedantic by design: many are static-analysis or stub-binary
tests with no real e2e cost, but each one closes a concrete failure
mode that we lived through.

Run them as the final step of `make`:

```
make                       # default target == `make gate`
make test-recovery-hardening   # this tier only
```

## Catalogue

| File | Catches |
|------|---------|
| `test_agent_prompt.py` | Hardening test: agent_prompt.txt staying current with lcsas-restore features. |
| `test_disc_swap_docs.py` | test_disc_swap_docs.py -- static regression guard for the MULTI-DISC RESTORE |
| `test_env_var_docs.py` | Hardening test: ENV_VARS.txt inventory + opt-in/opt-out principle. |
| `test_meta_bundling_completeness.py` | Hardening test #1: meta-disc tier-1 bundling completeness. |
| `test_multi_disc_design_header.py` | test_multi_disc_design_header.py -- static regression guard ensuring |
| `test_operational_features.py` | Hardening test: operational-friendliness features for repeat operators. |
| `test_pack_cache.py` | Hardening test: tier-1 opportunistic pack cache (LCSAS_PACK_CACHE_DIR). |
| `test_readiness_checklist.py` | test_readiness_checklist.py -- static regression guard for the operator |
| `test_readme_invocation_parity.py` | Hardening test #2: README ↔ restore.sh invocation parity. |
| `test_readme_simplification.py` | Hardening test: README_RESTORE simplification (Unit 5). |
| `test_restore_discovery.py` | Hardening test #3: restore.sh repo discovery on canonical layouts. |
| `test_restore_sh_ux.py` | Hardening tests: restore.sh UX improvements (recommendations #3, #4, #8). |
| `test_setup_static_guards.py` | Hardening tests #7 + #8: static guards on the blind-test setup. |
| `test_tier1_aarch64_qemu.py` | Issue #107: tier-1 aarch64 cross-built binary coverage via qemu-user. |
| `test_tier1_armv7_qemu.py` | Issue #119: tier-1 armv7 cross-built binary coverage via qemu-user. |
| `test_tier1_progress.py` | Hardening test for tier-1 restore progress output (recommendation #9). |
| `test_tier1_rescan.py` | Hardening test: tier-1 binary rescans mount parents on each retry. |
| `test_tier1_unit.py` | Issue #115: tier-1 C unit-test harness — fast, agent-free. |
| `test_tier1_windows_wine.py` | Issue #118: tier-1 Windows cross-built binary coverage via wine. |
| `test_tier3_invocation.py` | Hardening test #4: restore.sh tier-3 invocation flag correctness. |
| `test_tier3_progress.py` | Hardening test: tier-3 pure-Python restorer emits periodic progress. |
| `test_tier_fallback.py` | Hardening test #10: restore.sh tier fallback under |
| `test_verify_self.py` | Hardening test #6 + #9: verify.sh must fail closed on every known |

## Adding a new hardening test

1. Trace the bug back to its underlying failure mode.  If you find
   yourself writing "we should also..." while triaging, that's a
   candidate.
2. Name the file after the failure surface it covers, not the bug.
   `test_tier3_invocation.py` outlives the specific tier-3-arg bug;
   `test_tier3_arg_order_bug.py` does not.
3. Write a docstring that explains *what failure mode this catches*
   and *how the production code regressed*.  The catalogue above
   reads from these.
4. Hard-fail on any regression — no warnings, no skips except for
   honestly-optional hosts (e.g. cross-compile toolchains a dev
   doesn't have).  Use env vars like `LCSAS_OPTIONAL_ARCHES` for
   the rare legitimate skip.
5. Add a row to the catalogue table above.

## Why these are the LAST step

Unit tests verify functions.  Integration tests verify subsystems.
e2e tests verify pipelines.  Hardening tests verify that **the
production code path a real user runs** doesn't have any of the
specific failure modes we've already paid for in pain.  If any
hardening test fails, no other green light matters.
