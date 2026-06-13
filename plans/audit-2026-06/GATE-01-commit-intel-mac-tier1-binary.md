# GATE-01: Intel-Mac tier-1 binary is gitignored and absent from the repo

**Priority:** P1 · **Severity:** high · **Dimension:** tests-gates-map · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Commit x86_64-macos lcsas-restore and gate git-tracked status of all 6 tier-1 bins

## Problem

The meta disc bundles whatever pre-built tier-1 binaries exist under
`recovery/bin/<arch>/`. Five of the six approved targets' `lcsas-restore`
binaries were force-added to git at some point, but
`recovery/bin/x86_64-macos/lcsas-restore` exists only on this dev VM's disk:
`recovery/.gitignore` line 1 ignores `bin/*/lcsas-restore`, and the Intel-Mac
binary was never force-added. Any fresh clone — including the LCSAS source
tree that is itself burned onto every meta disc — lacks it.

The failure is silent end to end: `MetaVolumeBuilder._bundle_tier1_binaries`
skips missing binaries with a bare `continue`, so `lcsas meta build` from a
clean checkout produces a 5/6-target meta disc with exit code 0. An heir on an
Intel Mac (macOS x86_64 is a fully approved tier-1 target per
`docs/CROSS_PLATFORM_META_RFC.md` §6 Q6) then has no tier-1 binary at all —
their bare-minimum path simply isn't on the disc. The test written to catch
exactly this (`test_tier1_source_binary_present`) checks only disk presence,
and its docstring's claim that CI enforces all six targets is false: no CI
workflow runs `tests/recovery_hardening/` at all (see GATE-02).

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/.gitignore:1` — `bin/*/lcsas-restore` (also `:2-3` ignore
  `bin/*/lcsas-init` and `bin/*/busybox`).
- `git ls-files recovery/bin` lists `lcsas-restore` for `x86_64`, `aarch64`,
  `armv7`, `aarch64-macos`, plus `x86_64-windows/lcsas-restore.exe` — but for
  `x86_64-macos` only `lcsas-keyshare` is tracked. The binary is on disk
  (built May 29) but untracked.
- `src/lcsas/meta/builder.py:2161-2162` —
  `if not src_bin.is_file(): continue` — silent skip per target.
- `tests/recovery_hardening/test_meta_bundling_completeness.py:60` — "CI runs
  unset and therefore requires all six" (false today: `.github/workflows/test.yml:78-88`
  runs only test-unit / test-integration / typecheck / lint); the per-target
  test (`test_tier1_source_binary_present`, lines 84-112) asserts
  `src_bin.is_file()` only — disk presence, not git-tracked status.

## Fix design

1. **Commit the binary.**
   `git add -f recovery/bin/x86_64-macos/lcsas-restore` (current artifact,
   1,357,625 bytes, built via `zig cc -target x86_64-macos`). GATE-08's
   staleness gate will later prove it matches source; GATE-03 will execute it.

2. **Remove the blanket ignore.** Delete line 1 of `recovery/.gitignore`
   (`bin/*/lcsas-restore`). All six restore binaries are *intended* to be
   committed, so the ignore is pure hazard: it hides exactly the
   missing-artifact state this finding describes, and it suppresses dirty-tree
   signals after local rebuilds (which GATE-08 wants visible). Keep
   `bin/*/lcsas-init` and `bin/*/busybox` ignored — those belong to the boot
   scaffolding being demoted (BOOT plans). Chosen over per-target `!` negation
   lines because negations must be hand-extended for every new target and the
   failure mode of forgetting one is this exact finding again.

3. **Gate git-tracked status, not disk presence.** Add to
   `tests/recovery_hardening/test_meta_bundling_completeness.py`:

   ```python
   @pytest.mark.parametrize("rust_triple,short_arch,exe", APPROVED_TIER1_TARGETS,
                            ids=[t[0] for t in APPROVED_TIER1_TARGETS])
   def test_tier1_binary_git_tracked(rust_triple, short_arch, exe):
       """Every approved tier-1 binary must be COMMITTED, not merely present:
       a fresh clone builds the meta disc from tracked files only."""
       rel = f"recovery/bin/{short_arch}/{exe}"
       if shutil.which("git") is None or subprocess.run(
               ["git", "rev-parse", "--is-inside-work-tree"],
               cwd=REPO_ROOT, capture_output=True).returncode != 0:
           pytest.skip("not a git checkout (e.g. burned source tree)")
       res = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                            cwd=REPO_ROOT, capture_output=True, text=True)
       assert res.returncode == 0, (
           f"{rel} is not git-tracked; fresh clones will silently build a "
           f"meta disc without the {rust_triple} tier-1 binary. "
           f"Fix: git add -f {rel}")
   ```

   No `OPTIONAL_TARGETS` escape hatch for this test: tracked-ness is a repo
   property, not a host-toolchain property, so it must hold everywhere.

4. **Wire it into CI immediately** (don't block on GATE-02's full job): add a
   step to `.github/workflows/test.yml` after "Run unit tests":

   ```yaml
   - name: Meta bundling completeness (six-target contract)
     run: pytest tests/recovery_hardening/test_meta_bundling_completeness.py -v
   ```

   Runs in seconds, no external tools. Fold into GATE-02's
   `recovery-hardening` job when that lands.

5. **Correct the docstring** at `test_meta_bundling_completeness.py:53-60` to
   say which CI step enforces the contract, instead of the current false
   claim.

The builder-side fix (fail loud instead of `continue` when an approved-target
binary is missing at `lcsas meta build` time) is owned by RST-05 — do not
duplicate it here; this plan makes the repo state correct and gated so RST-05's
check passes on every clone.

## Tests & gates

- `tests/recovery_hardening/test_meta_bundling_completeness.py::test_tier1_binary_git_tracked`
  — fails on master today (x86_64-macos); passes after step 1. Always-on
  within the hardening suite.
- Existing `test_tier1_source_binary_present` keeps disk-presence coverage
  (catches a tracked-but-deleted file).
- CI: new test.yml step (step 4) runs the whole completeness file on every
  push/PR — this is the always-on gate; superseded-by-inclusion once GATE-02's
  recovery-hardening job exists.
- Manual verification: `git clone` into a temp dir,
  `ls recovery/bin/x86_64-macos/lcsas-restore` succeeds; run the new test there.

## Acceptance criteria

- [ ] `git ls-files recovery/bin | grep -c lcsas-restore` returns 5 and
      `... | grep -c 'lcsas-restore'` including `.exe` covers all 6 targets
      (5 × `lcsas-restore` + 1 × `lcsas-restore.exe`).
- [ ] `recovery/.gitignore` no longer contains `bin/*/lcsas-restore`.
- [ ] `pytest tests/recovery_hardening/test_meta_bundling_completeness.py -v`
      passes in a fresh clone with no recovery build artifacts.
- [ ] `.github/workflows/test.yml` runs the completeness file; a branch that
      deletes the tracked binary (or re-adds the ignore + untracks) goes red.

## Dependencies & related plans

- **RST-05** (meta-volume completeness gate) — owns the `_bundle_tier1_binaries`
  fail-loud product fix; this plan is its repo-state prerequisite.
- **GATE-02** (wire recovery-hardening into CI) — absorbs the interim CI step.
- **GATE-03** (execute macOS tier-1 binaries) — proves the committed binary runs.
- **GATE-08** (binary staleness gate) — proves it matches current source.
- Order: GATE-01 first (cheap, unblocks the other three).

## Effort

0.5 day (commit + ignore edit + one test + one CI step). No special
environment; verify with a fresh clone.

---
**Implemented:** 2026-06-13. As planned — committed a freshly-rebuilt
x86_64-macos lcsas-restore (zig cc -target x86_64-macos, 1362298 bytes;
size differs from the plan's stale 1357625 because the artifact was
rebuilt from current source), removed `bin/*/lcsas-restore` from
recovery/.gitignore, added `test_tier1_binary_git_tracked`, wired the
completeness file into .github/workflows/test.yml, and corrected the
docstring's CI claim. No C source changed, so no full per-target rebuild.
