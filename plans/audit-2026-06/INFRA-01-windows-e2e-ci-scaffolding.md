# INFRA-01: Windows-journey e2e on GitHub CI (no local Windows, no home2)

**Priority:** P0 (scaffolding for UX-01 and the Windows journey gate) · **Severity:** n/a (infrastructure) · **Dimension:** infra · **Audit status:** designed in review discussion 2026-06-10 · **Ledger:** untracked
**Suggested GH issue title:** Add Windows-journey e2e: wine smoke loop, tmate debug workflow, two-job CI gate

## Problem

The single most likely heir journey — Windows machine, double-click `restore.bat` — has zero functional coverage anywhere, and the audit confirmed it currently dead-ends at step 1 (UX: restore.bat repo discovery probes a layout no built meta-volume has). Nothing can catch that class of break today: the only journey-level gate (cdemu blind restore) is Linux-only and local-only, and the Windows tier-1 binary has only ever been executed under wine.

Constraints settled during review: no local Windows machine, no changes to the home2 hypervisor (nested virt is not exposed to this VM — `/dev/kvm` absent — so a local Windows VM is not an option), and the repo is **public**, so GitHub-hosted `windows-latest` runners are free with unlimited minutes, including long interactive debug sessions.

The plan has three tiers: a fast local smoke loop under wine, an on-demand interactive session on a real Windows runner for development, and the permanent two-job CI gate.

## Evidence

- `recovery/scripts/restore.bat:186-193` — data-disc discovery is a drive-letter loop (`for %%L in (D E F ... Z)` probing `%%L:\data\`), which works unmodified against ISOs mounted with PowerShell `Mount-DiskImage` (real read-only volumes with real drive letters; no optical hardware needed).
- `restore.bat:36,102` — automation hooks already exist (`LCSAS_NO_RELOCATE=1`, `LCSAS_TARGET`); the interactive reads (`set /p` at `:148`, `:163`) consume redirected stdin, so `cmd /c "restore.bat < answers.txt"` drives the happy path.
- `restore.bat:114,122,133,154,166,235,254,274` — every error path ends in `pause`; with exhausted stdin this hangs until job timeout (must be handled, see Fix design).
- `tests/e2e/cdemu_blind_restore/setup.py:240,349` — fixture building (rustic repo + xorriso ISO mastering) already exists and is separable from the cdemu/tmux/claude drill machinery.
- `tests/recovery_hardening/test_tier1_windows_wine.py` — wine-9.0 harness exists (`WINEPREFIX=/scratch/wine-prefix`); the e2e additionally executes `lcsas-restore.exe` **natively**, closing GATE's "macOS/Windows binaries never executed on real OS" finding for the Windows half.

## Fix design

**Tier 1 — local wine smoke loop (development pre-filter, not a gate).**
Wine maps drive letters via symlinks: `ln -s <fixture-dir> $WINEPREFIX/dosdevices/e:` makes `if exist E:\data\` resolve against a Linux directory. Add `tests/recovery_hardening/test_restore_bat_wine_smoke.py`:
- Builds a minimal fake meta tree + data tree in `tmp_path`, symlinks them as `d:`/`e:` in a throwaway WINEPREFIX, runs `wine cmd /c restore.bat < answers.txt` with `LCSAS_NO_RELOCATE=1`.
- Asserts only coarse outcomes (repo discovered, tier-1 invoked, expected error strings on the sad paths). Skips honestly when wine is absent.
- Explicit comment: wine's `cmd` is an incomplete reimplementation (delayed expansion and `set /p` edge cases differ) — green here is "probably right", the Tier-3 job is the truth source.

**Tier 2 — on-demand interactive Windows session (`.github/workflows/windows-debug.yml`).**
`workflow_dispatch`-only; `windows-latest`; steps: checkout → download the latest fixture artifact (see Tier 3) if present → `mxschmitt/action-tmate@v3` with `limit-access-to-actor: true`. Gives an SSH session (MSYS2 shell; `cmd`/`powershell` available) on a real runner for up to the 6-hour job limit, free on this public repo. This is the restore.bat development loop: edit/re-run in-session, paste the final script back into a commit. Never triggered automatically.

**Tier 3 — the gate (`.github/workflows/windows-e2e.yml`), two jobs.**

*Job `fixture` (ubuntu-latest):*
1. Install pinned rustic + xorriso (reuse the install steps from `test.yml`).
2. Build a TEST_TINY-media fixture: real rustic repo with a few-MB known tree, `lcsas stage`/ISO mastering via the extracted fixture-builder (refactor the repo/ISO-building functions out of `tests/e2e/cdemu_blind_restore/setup.py` into `tests/e2e/fixture_lib.py` so cdemu paths and this workflow share one builder), `lcsas meta build` for the meta tree, master both as `.iso` (extension is load-bearing: `Mount-DiskImage` requires it).
3. Emit `manifest.json`: relative path → SHA-256 for every file in the source tree, plus the repo password in a sidecar consumed only by the test (fixture-only secret; generated per-run).
4. `actions/upload-artifact`: `meta.iso`, `data-01.iso`, `manifest.json`.

*Job `restore` (windows-latest, `needs: fixture`, `timeout-minutes: 20`):*
1. Download artifacts; `Mount-DiskImage` both ISOs; resolve assigned drive letters via `Get-Volume`.
2. Write `answers.txt` (target dir line, password line, generous trailing newlines so stray `pause`/`set /p` reads never block on an empty stream).
3. Run `cmd /c "<metaDrive>:\restore.bat < answers.txt"` with `LCSAS_NO_RELOCATE=1` (relocation-to-%TEMP% gets its own case later; start with the direct flow). Capture stdout/stderr to the job log.
4. Assert exit code 0, then hash-compare the restored tree against `manifest.json` (PowerShell `Get-FileHash`), byte-exact, no extra files.
5. Negative case in the same job (cheap, reuses mounts): wrong password → assert non-zero exit and that the error text matches the documented message, no partial tree left behind.

*Triggers:* `pull_request` filtered on `recovery/scripts/**`, `src/lcsas/meta/**`, `src/lcsas/staging/metadata.py`, `recovery/bin/x86_64-windows/**`; plus `schedule:` weekly cron; plus `workflow_dispatch`.

**Landmines handled up front:**
- `.gitattributes`: add `*.bat eol=crlf` (or `-text`) — LF-mangled batch files fail in ways that look like logic bugs.
- All error paths hang-proofed by `timeout-minutes` + padded `answers.txt`; longer-term, restore.bat gains `LCSAS_NONINTERACTIVE=1` to skip `pause` (owned by UX-01's restore.bat changes; this plan only requires the padding workaround).
- Defender real-time scanning slows many-small-file restores: keep the fixture at TEST_TINY scale (tens of files).

**Sequencing.** (1) `windows-debug.yml` + `.gitattributes` + wine smoke test land first (they are pure additions and the development environment for UX-01). (2) The fixture-builder refactor + `fixture` job land second and red-first. (3) The `restore` job goes in expected-red against today's broken restore.bat, flips green in the same PR series as the UX-01 discovery fix.

**What this gate does not prove (recorded, not solved):** double-click Explorer semantics (CI invokes `cmd /c`), real optical-drive read quirks, multi-disc swap UX (Phase 2: a PowerShell driver dismounting/mounting ISOs between stdin reads — only after single-disc is green), and Windows S-mode machines that refuse unsigned executables entirely (doc-level residual risk; note in RECOVER_WINDOWS.txt rewrite, see UX plans).

## Tests & gates

- `tests/recovery_hardening/test_restore_bat_wine_smoke.py` — local pre-filter, skips without wine; runs in `make test-recovery-hardening`.
- `.github/workflows/windows-debug.yml` — `workflow_dispatch` only; never a gate.
- `.github/workflows/windows-e2e.yml` — the gate: PR path-filter + weekly cron; happy path (hash-exact restore) + wrong-password negative; both jobs always-on when triggered.
- Step-zero contract test (owned by UX-01, listed here as a dependency): Linux-only parse of restore.bat's probed paths vs `meta/builder.py` layout — catches the current critical in hours, before any of this lands.

## Acceptance criteria

- [ ] `windows-debug.yml` dispatches and yields a usable tmate session on `windows-latest`.
- [ ] Fixture job produces mountable ISOs + manifest as artifacts in under 10 minutes.
- [ ] Restore job mounts ISOs, runs restore.bat unattended, and fails today for exactly the UX-01 reason (red-first proof).
- [ ] After UX-01 lands: restore job green; restored tree hash-identical to manifest; wrong-password case red-path verified.
- [ ] `lcsas-restore.exe` executed natively on Windows in CI (closes the Windows half of GATE's never-executed-binaries finding).
- [ ] Total workflow wall-clock ≤ 25 min; no run has ever hit `timeout-minutes` from a `pause` hang.

## Dependencies & related plans

- **UX-01 (restore.bat repo discovery)** — the gate exists to prove this fix; develop the fix against Tiers 1-2.
- **UX docs-vs-reality contract gate** — step-zero, independent of Windows entirely.
- **GATE plans (CI wiring)** — this workflow is referenced there as the Windows leg.
- **KEY plans** — a later variant adds the split-key journey (combine from share cards, then restore) on the same scaffolding.

## Effort

~1.5–2 weeks total alongside UX-01: tmate workflow + `.gitattributes` + wine smoke ≈ 1–2 days; fixture-lib refactor + fixture job ≈ 1–2 days; restore job + flake-hunting ≈ 2–3 days; the remainder is UX-01's restore.bat work developed on this scaffolding. Multi-disc swap variant: +1 week, deferred until single-disc is green.

---
**Implemented:** 2026-06-14. As planned, single-disc tiers landed: `.gitattributes` (`*.bat eol=crlf`); `tests/recovery_hardening/test_restore_bat_wine_smoke.py` (Tier-1 wine smoke, throwaway WINEPREFIX, coarse asserts, skips without wine); fixture-builder refactor into `tests/e2e/fixture_lib.py` (shared by cdemu `setup.py` + workflow, plus a `build_windows_fixture` CLI verified end-to-end locally with real rustic+xorriso); `.github/workflows/windows-debug.yml` (Tier-2 tmate, dispatch-only) and `.github/workflows/windows-e2e.yml` (Tier-3 two-job gate, expected RED at restore.bat repo discovery until UX-01). Deferred per plan: multi-disc swap variant; restore.bat `LCSAS_NONINTERACTIVE=1` (UX-01). Step-zero docs-vs-reality contract test is UX-01-owned (not in this plan's scope).
