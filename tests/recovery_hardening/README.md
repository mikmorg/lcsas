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
| `test_meta_bundling_completeness.py` | Tier-1 binary for any "approved" target missing from a built meta disc (e.g. Phase 21 claimed Linux x86_64 was bundled, but the bundler silently skipped it because nobody had built it). |
| `test_readme_invocation_parity.py` | `README_RESTORE.md` documenting the obsolete flag UX (`./restore.sh --key X --target Y`) that production `restore.sh` no longer accepts. |
| `test_restore_discovery.py` | `restore.sh` failing to find a repo on a canonical meta-disc layout (`metadata/<tenant>/{keys,index}`); multi-tenant prompt; `LCSAS_REPO` env var honoring; legacy `/repo/` back-compat; empty-recovery error message actionability. |
| `test_restore_sh_ux.py` | `restore.sh` UX gates from the latest blind-restore transcript: no-data-discs hard-error and its `LCSAS_ALLOW_NO_PACK_SEARCH` escape hatch; numbered-list repo prompt (number-or-name); `QUICK START` section in `--help`. Catches regressions that would silently restore the old "march on into an opaque downstream failure" path. |
| `test_tier3_invocation.py` | `restore.sh` invoking `standalone_restorer.py` with the wrong CLI form (positional `$REPO $TARGET` instead of `--repo X --target Y --password-file Z`). Also pins `$TARGET_DIR` vs `$TARGET` semantics so the recovery binary doesn't get the arch triple as its target dir. |
| `test_verify_self.py` | `verify.sh` failing open: missing-fixture passing silently, regex bugs that let cheats slip through, removing a check entirely. Covers all 14 production checks + the fail-closed fixture guard. |
| `test_setup_static_guards.py` | Blind-test `setup.py` regressions: FIXTURE under `/mnt` (shadowable), missing source-tree lockdown step (lcsas-blind can `find / -path '*sources/alpha*' && cp`). |
| `test_readme_simplification.py` | `README_RESTORE` regressing to the old 4-step `mount`/`cp -r`/`cd`/`umount` recipe (restore.sh relocates itself), the `LCSAS_NO_RELOCATE` override going undocumented, or the Ctrl+Z single-terminal disc-swap advice being removed. |
| `test_operational_features.py` | Repeat-operator UX regressions: `disc-loader status` losing its `[meta]`/`[data]` role decoration; `restore.sh` no longer appending a session line to `~/.lcsas-restore-log` (tenant / target / snapshot / tier / disc-count, ISO-8601 UTC). |
| `test_tier3_progress.py` | Tier-3 pure-Python restorer falling silent: no periodic `N/M files, X MB` progress line on stderr (operators can't distinguish "working" from "frozen" on a slow ~1 MB/s restore); `LCSAS_PROGRESS=0` escape hatch breaking; concatenated `standalone_restorer.py` bundle dropping the new helpers. |
| `test_tier1_progress.py` | `lcsas-restore` going silent mid-restore (no anti-freeze signal): missing `progress: N/M blobs, X MB` stderr lines, or only emitting them after `restore complete`.  Pins the canonical format string used by log scrapers. |
| `test_tier1_rescan.py` | Tier-1 binary (`lcsas-restore`) regressing to "stat pack-search dirs only once at startup": after a press-Enter-to-retry prompt, the binary must re-enumerate the `--mount-parent` / `$LCSAS_MOUNT_DIRS` directories so a disc auto-mounted AFTER the binary started is discovered, and must re-pick the freshest catalog so prompt label hints stay accurate. |
| `test_pack_cache.py` | Silent removal of the opt-in opportunistic pack cache (`LCSAS_PACK_CACHE_DIR`): without it, the v3 blind run took 16 disc inserts for 3 needed discs because the tree walker asks for packs in tree order not disc order.  Pins the env-var contract, the C-side API, and restore.sh's `auto` shorthand. |
| `test_tier1_aarch64_qemu.py` | Cross-built aarch64 `lcsas-restore` binary drifting from source / segfaulting under qemu-user emulation: the binary is bundled on every meta disc but had zero runtime coverage outside the host-only blind-restore agent, so source changes that didn't trigger a rebuild shipped a stale aarch64 artifact undetected.  Mirrors the nine `test_tier1_unit.py` cases (arg parse, cache plumbing, catalog handling, crash-safety) against `recovery/bin/aarch64/lcsas-restore`, exercising it transparently via binfmt_misc + `qemu-aarch64-static`.  Skips honestly when either the binary or the qemu shim is absent. |
| `test_tier1_windows_wine.py` | Cross-built Windows `lcsas-restore.exe` drifting from source or failing under wine emulation: bundled on every meta disc for Windows recovery hosts but had zero runtime coverage. Mirrors the nine tier-1 unit cases via `wine` + `WINEPREFIX=/scratch/wine-prefix`. Skips when wine is absent. |
| `test_disc_swap_docs.py` | `RECOVER.txt` losing the operator disc-swap guide: the automated test rig uses `disc-loader` (a setuid wrapper) to simulate swaps, so a passing blind-restore test gave false confidence about real-user UX.  Asserts that the `MULTI-DISC RESTORE` section header, the `eject` physical-step command, and the `LCSAS_PACK_CACHE_DIR` cache-knob are all present in `RECOVER.txt`, and that `UX_CONCERNS.txt` ID 005 is marked CLOSED (closes #106). |
| `test_multi_disc_design_header.py` | MULTI_DISC_DESIGN.txt losing its "DESIGN DOCUMENT" banner or RECOVER.txt cross-reference — operators reading the design doc would mistakenly treat unshipped UX mockups as current behavior (closes #110). |
| `test_env_var_docs.py` | `ENV_VARS.txt` being deleted or stripped of its defaults rationale — operators can't predict restore behavior without knowing which knobs are opt-in vs opt-out (closes #89). |
| `test_agent_prompt.py` | `agent_prompt.txt` drifting from current feature set — blind-restore agent re-derives `LCSAS_PACK_CACHE_DIR` and disc-swap interaction from scratch each run, burning budget and risking wrong behavior (closes #103). |
| `test_readiness_checklist.py` | `READINESS_CHECKLIST.txt` being silently deleted or stripped of its monthly/annual operator drills — operators have no documented pre-flight gate before relying on the archive in a real disaster (closes #112). |

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
