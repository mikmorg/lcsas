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

## Opt-in gates (off by default)

A handful of tests in this tier are slow or need a special toolchain, so
they self-skip unless an env var opts them in. They are NOT part of the
default `make gate`; run them explicitly when touching the tier-1 C
binary or before a release:

| Env var | Enables |
|---------|---------|
| `LCSAS_COVERAGE=1` | `test_tier1_coverage_baseline.py` — runs `make coverage-c`, asserts `recovery/build/coverage.txt` is written. |
| `LCSAS_SANITIZE=1` | `test_tier1_sanitize.py` — runs `make sanitize`, asserts 0 ASan/UBSan/LSan findings. |
| `LCSAS_FAULT_INJECT=1` | `test_tier1_fault_inject.py` — malloc fault-injection sweep over every tier-1 unit binary. |
| `LCSAS_ZSTD_QEMU=1` | `test_tier3_zstd_qemu.py` — tier-3 zstd restore under aarch64 CPython via qemu-user (`make test-tier3-qemu`). |
| `LCSAS_OPTIONAL_ARCHES` | suppresses skips for cross-compile arches a dev doesn't have a toolchain for. |

Adjacent gates that live in OTHER tiers (named here only so it is clear
they are not in this directory): `LCSAS_ECC_REPAIR=1` drives the real
dvdisaster RS03 repair proof under `tests/integration/`, and
`LCSAS_BLIND_ACK_COST=1` acknowledges the LLM API cost of the
`make blind-restore*` blind-agent e2e harness under
`tests/e2e/cdemu_blind_restore/`.

## Catalogue

| File | Catches |
|------|---------|
| `test_agent_prompt.py` | Hardening test: agent_prompt.txt staying current with lcsas-restore features. |
| `test_audit_gate_threshold_parity.py` | Hardening: CI coverage threshold tracks the local measured floor (GATE-07). |
| `test_bin_parity_exempt_ledger.py` | Hardening test (GATE-08): the bin-parity exemption ledger must stay honest. |
| `test_blind_variant_xfail_ledger.py` | Hardening test (GATE-06): the blind-restore XFAIL ledger must stay |
| `test_boot_config_paths.py` | BOOT-03: boot menu entry paths must equal the staged-tree paths. |
| `test_boot_docs_reality.py` | test_boot_docs_reality.py -- docs-vs-reality contract gate for `lcsas ...` |
| `test_boot_path_quarantined.py` | Boot-path quarantine tripwire (BOOT-08) + BOOT-06 record-keeping. |
| `test_ci_workflow_parity.py` | Hardening: CI workflow ↔ `make gate` suite parity (GATE-02). |
| `test_cross_build_deps.py` | Guard the recovery cross-build source dependencies. |
| `test_disc_swap_docs.py` | test_disc_swap_docs.py -- static regression guard for the MULTI-DISC RESTORE |
| `test_ecc_capacity_claims.py` | Hardening test: ECC repair-capacity claims must match configured redundancy. |
| `test_ecc_tooling_on_meta.py` | FMT-01: the RS03 repair tool ships on every meta volume. |
| `test_env_var_docs.py` | Hardening test: ENV_VARS.txt inventory + opt-in/opt-out principle. |
| `test_format_canary.py` | FMT-03 format canary: round-trip LATEST rustic through the pinned readers. |
| `test_format_txt_path_safety.py` | Hardening test: FORMAT.txt PATH SAFETY must match binary behaviour. |
| `test_initramfs_manifest_sources.py` | BOOT-02: build_initramfs.sh must hard-fail on missing sources. |
| `test_keyshare_docs.py` | test_keyshare_docs.py -- static guard: heir docs name the C combiner [KEY-05]. |
| `test_last_verified_writer_exists.py` | Hardening test: volume_copies.last_verified_at must have a writer. |
| `test_live_usb_procedure_docs.py` | test_live_usb_procedure_docs.py -- static doc-pinning gate for the |
| `test_meta_bundles_dvdisaster_source.py` | Hardening test (FMT-02): the dvdisaster RS03 source must be pinned, |
| `test_meta_bundling_completeness.py` | Hardening test #1: meta-disc tier-1 bundling completeness. |
| `test_multi_disc_design_header.py` | test_multi_disc_design_header.py -- static regression guard ensuring |
| `test_no_boot_deadend_routing.py` | test_no_boot_deadend_routing.py -- static regression guard against the |
| `test_no_bypass_check.py` | Unit tests for the blind-restore #13 bypass check. |
| `test_no_unpinned_boot_artifacts.py` | BOOT-07: nothing executable reaches a meta-volume tree unpinned. |
| `test_nonempty_target_guard.py` | Hardening tests: restore.sh non-empty-target guard [UX-07]. |
| `test_operational_features.py` | Hardening test: operational-friendliness features for repeat operators. |
| `test_pack_cache.py` | Hardening test: tier-1 opportunistic pack cache (LCSAS_PACK_CACHE_DIR). |
| `test_readiness_checklist.py` | test_readiness_checklist.py -- static regression guard for the operator |
| `test_readme_invocation_parity.py` | Hardening test #2: README ↔ restore.sh invocation parity. |
| `test_readme_simplification.py` | Hardening test: README_RESTORE simplification (Unit 5). |
| `test_rebuild_docs.py` | test_rebuild_docs.py -- static regression guard for the mixed-age-discs |
| `test_recovery_card_docs.py` | Hardening test: the single-key Recovery Card artifact + its references. |
| `test_restore_bat_ecc_dispatch_wine.py` | FMT-01: restore.bat --check-disc ECC repair path, end-to-end under wine. |
| `test_restore_bat_wine_smoke.py` | INFRA-01: local wine smoke loop for recovery/scripts/restore.bat. |
| `test_restore_discovery.py` | Hardening test #3: restore.sh repo discovery on canonical layouts. |
| `test_restore_no_recovery_binaries.py` | Hardening test (issue #225): restore.sh terminal error UX when |
| `test_restore_platform_detect.py` | Shell-coverage tests for OS/arch detection branches in restore.sh. |
| `test_restore_sh_binary_corrupt.py` | Issue #223 — restore.sh detects present-but-corrupted tier binaries. |
| `test_restore_sh_catalog_invalidation.py` | Hardening test: mtime-based locator-catalog cache invalidation in restore.sh (Issue #108). |
| `test_restore_sh_ecc_dispatch.py` | Hardening tests: restore.sh --check-disc ECC dispatch [FMT-01]. |
| `test_restore_sh_relocate.py` | Hardening tests: restore.sh relocation path (read-only SCRIPT_DIR). |
| `test_restore_sh_repo_flag.py` | Hardening test: --repo NAME flag on restore.sh (Issue #91). |
| `test_restore_sh_target_guards.py` | Hardening tests: restore.sh target-safety guards [UX-07] + [BOOT-05]. |
| `test_restore_sh_target_key_flags.py` | Hardening tests: --target / --key flags on restore.sh [KEY-02]. |
| `test_restore_sh_tier1_missing_multidisc.py` | Hardening test (GATE-06): restore.sh drives the full tier1-missing |
| `test_restore_sh_tmpfs_target_warns.py` | Hardening tests: RAM-backed (tmpfs) restore-target warning [BOOT-05]. |
| `test_restore_sh_ux.py` | Hardening tests: restore.sh UX improvements (recommendations #3, #4, #8). |
| `test_restore_sh_version_flag.py` | Hardening test: --version flag on restore.sh (Issue #96). |
| `test_restore_skips_tier2_on_multi_disc.py` | Hardening test (issue #227): restore.sh skips tier 2 when the |
| `test_restore_space_preflight.py` | Hardening tests: restore-side free-space preflight [FMA-09]. |
| `test_setup_static_guards.py` | Hardening tests #7 + #8: static guards on the blind-test setup. |
| `test_shell_coverage_contract.py` | Hardening: the `shell-coverage` Makefile recipe stays a real gate (GATE-09). |
| `test_standalone_zstandard_guard.py` | Hardening test: standalone_restorer.py's `import zstandard` guard. |
| `test_tier1_aarch64_qemu.py` | Issue #107: tier-1 aarch64 cross-built binary coverage via qemu-user. |
| `test_tier1_armv7_qemu.py` | Issue #119: tier-1 armv7 cross-built binary coverage via qemu-user. |
| `test_tier1_coverage_baseline.py` | Opt-in gate: make coverage-c must complete without error. |
| `test_tier1_deep_tree.py` | T1C-04: tier-1 tree-walk recursion-depth cap. |
| `test_tier1_dense_index.py` | T1C-01: one dense index file the old fixed-cap parser could not read. |
| `test_tier1_drive_disconnect.py` | Issue #222 — tier-1 must surface a useful error on drive read failure. |
| `test_tier1_fat32_target.py` | Issue #224 — tier-1 must degrade gracefully on a FAT32 target. |
| `test_tier1_fault_handling.py` | Tier-1 operator-error fault handling tests. |
| `test_tier1_fault_inject.py` | Audit phase 6 (issue #165): malloc fault-injection sweep — zero crashes. |
| `test_tier1_hardlinks.py` | Issue #192 -- tier-1 hardlink reconstruction. |
| `test_tier1_large_file.py` | T1C-01: one file with 40,000 content chunks (huge content array). |
| `test_tier1_macos_native.py` | GATE-03: tier-1 macOS (Mach-O) binary execution coverage. |
| `test_tier1_meta_disc_live.py` | Hardening test: tier-1 meta-disc exclusion respects a LIVE check. |
| `test_tier1_mtime.py` | Issue #188 — tier-1 must preserve file/directory mtime. |
| `test_tier1_no_ftruncate_on_failure.py` | Issue #245 — tier-1 must NOT ftruncate to expected_size on failure. |
| `test_tier1_ownership.py` | Issues #189 — tier-1 must restore uid/gid (when running as root). |
| `test_tier1_petabyte_fixture.py` | Issue #160: petabyte-scale stress fixture for tier-1 C binary. |
| `test_tier1_progress.py` | Hardening test for tier-1 restore progress output (recommendation #9). |
| `test_tier1_rescan.py` | Hardening test: tier-1 binary rescans mount parents on each retry. |
| `test_tier1_sanitize.py` | Opt-in gate: make sanitize must complete without any ASan/UBSan/LSan findings. |
| `test_tier1_snapshot_selection.py` | Regression test for issue #194. |
| `test_tier1_sparse.py` | Issue #193 — tier-1 must preserve sparseness of restored files. |
| `test_tier1_target_full.py` | Issue #221 — tier-1 must surface ENOSPC on the target filesystem. |
| `test_tier1_unit.py` | Issue #115: tier-1 C unit-test harness — fast, agent-free. |
| `test_tier1_vs_tier2_differential.py` | Phase 14: tier-1 ↔ tier-2 differential oracle. |
| `test_tier1_wide_directory.py` | T1C-01: one very wide directory (5,000 entries in a single tree blob). |
| `test_tier1_windows_wine.py` | Issue #118: tier-1 Windows cross-built binary coverage via wine. |
| `test_tier1_xattrs.py` | Issue #190 — tier-1 must restore extended attributes (xattrs). |
| `test_tier3_catalog_auto_discovery.py` | Hardening test: tier-3 catalog auto-discovery (#253). |
| `test_tier3_catalog_resolution.py` | Hardening test: tier-3 catalog-aware disc-swap prompt (#248). |
| `test_tier3_disc_swap.py` | Hardening test: tier-3 standalone restorer disc-swap protocol (#234). |
| `test_tier3_invocation.py` | Hardening test #4: restore.sh tier-3 invocation flag correctness. |
| `test_tier3_progress.py` | Hardening test: tier-3 pure-Python restorer emits periodic progress. |
| `test_tier3_pythonpath.py` | Hardening test: restore.sh tier-3 PYTHONPATH wiring for bundled zstandard. |
| `test_tier3_stderr_capture.py` | Hardening test: restore.sh tier-3 stderr capture-and-replay on failure. |
| `test_tier3_tolerant_restore.py` | Hardening guard: tier-3 standalone restorer is SKIP-AND-CONTINUE. |
| `test_tier3_zstd_portability.py` | Hardening test: tier-3 zstd works on every approved target (RST-04). |
| `test_tier3_zstd_qemu.py` | Opt-in cross-arch proof: tier-3 zstd restore under aarch64 CPython (RST-04). |
| `test_tier_fallback.py` | Hardening test #10: restore.sh tier fallback under |
| `test_verify_self.py` | Hardening test #6 + #9: verify.sh must fail closed on every known |
| `test_windows_doc_multiextent_caveat.py` | Hardening doc-pin: RECOVER_WINDOWS.txt documents the >4 GiB CDFS trap (FMT-04). |
| `test_workflow_path_filter.py` | Hardening: audit-gate path-filter has no ghost entries and no holes (GATE-04). |

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
5. Run `make gen-catalogue` to regenerate the catalogue table above from your docstring.

## Why these are the LAST step

Unit tests verify functions.  Integration tests verify subsystems.
e2e tests verify pipelines.  Hardening tests verify that **the
production code path a real user runs** doesn't have any of the
specific failure modes we've already paid for in pain.  If any
hardening test fails, no other green light matters.
