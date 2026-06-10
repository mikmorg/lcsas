# UX-01: restore.bat cannot discover the repo on a real meta-volume

**Priority:** P0 · **Severity:** critical · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix restore.bat repo discovery for the holographic metadata layout

## Problem

The first instruction a Windows heir reads on the meta disc (START_HERE.txt, both
variants) is "Double-click restore.bat in this folder." On every meta-volume the
current builder can produce, that double-click terminates at step 1 with
`ERROR: no restic repo (keys\ + index\) found under %RECOVERY%` and exit 1 —
before the password prompt, with no actionable next step. Windows is statistically
the most likely OS an heir will be holding; the advertised journey for it has never
worked against a real disc.

The cause is a layout contract break. `restore.bat` probes exactly two repo
locations: `%RECOVERY%\repo\{keys,index}` and `%RECOVERY%\{keys,index}`. But the
meta builder writes per-tenant repo metadata to `<disc-root>/metadata/<repo_id>/`
(the holographic layout used on every disc), and `restore.bat` is surfaced at the
disc root, so `%RECOVERY%` resolves to `<disc>\recovery` — a directory that never
contains `repo\`, `keys\`, or `metadata\`. The POSIX `restore.sh` was ported to
this layout (it probes `$RECOVERY/metadata/*` plus every mounted disc);
`restore.bat` never got the equivalent. Worse, the single-drive RAM relocation
copies only `bin\` and `catalog.db` to `%TEMP%`, so after relocation `%RECOVERY%`
points at a RAM dir that can never contain a repo even if the probes were fixed.

This shipped because the only tests of `restore.bat` are static string assertions
(target-triple naming, ARM64 message); nothing executes it against a built
meta-volume tree.

## Evidence

(All re-checked against current code, 2026-06-10.)

- `recovery/scripts/restore.bat:127-135` — the entire repo probe:
  ```bat
  set "REPO="
  if exist "%RECOVERY%\repo\keys" if exist "%RECOVERY%\repo\index" set "REPO=%RECOVERY%\repo"
  if "%REPO%"=="" if exist "%RECOVERY%\keys" if exist "%RECOVERY%\index" set "REPO=%RECOVERY%"
  if "%REPO%"=="" (
      echo ERROR: no restic repo (keys\ + index\) found under %RECOVERY%
  ```
  No `metadata\` probe anywhere in the file.
- `recovery/scripts/restore.bat:46-66` — relocation copies only `..\bin` (robocopy/xcopy,
  lines 62/64) and `..\catalog.db` (line 66) into `%RAMDIR%\recovery`; the relaunched
  copy's `%RECOVERY%` is `%RAMDIR%\recovery`.
- `src/lcsas/meta/builder.py:2264-2279` — `_bundle_metadata` writes
  `<output>/metadata/<repo_id>/{config,keys,index,snapshots}` (the only repo material
  on a meta-volume; there is no `repo/` dir).
- `src/lcsas/meta/builder.py:1994-2000` — `restore.bat` is copied to the meta-volume
  **root**, so `SCRIPT_DIR=<disc>\` and the `%SCRIPT_DIR%recovery\bin` probe (line 87)
  sets `RECOVERY=<disc>\recovery`.
- `recovery/scripts/restore.sh:487-547` — the POSIX driver probes `$RECOVERY/repo`,
  `$RECOVERY`, `$RECOVERY/metadata/*`, **and** `metadata/*` on every mounted disc under
  `LCSAS_MOUNT_DIRS`; the .bat got none of this.
- `src/lcsas/meta/builder.py:2442-2443` — START_HERE: ">>> Windows 10 or 11 <<<
  Double-click restore.bat in this folder."
- `tests/unit/test_restore_bat_dispatcher.py:1-8` — "Windows `.bat` scripts can't be
  executed on Linux, so we settle for static-content assertions."
- The repo probe (line 127) precedes the password prompt (line 158-168): the heir
  fails before ever being asked for the password.

## Fix design

All changes in `recovery/scripts/restore.bat` (plus one builder no-op check and the
tests below). Port `restore.sh`'s candidate model:

**1. Candidate collection (replace lines 126-135).**
A candidate is any dir with both `keys\` and `index\`. Collect in order:

```bat
REM Disc root = parent of %RECOVERY% (restore.bat lives at root or recovery\scripts\).
set "DISC_ROOT=%RECOVERY%\.."
REM legacy layouts (keep, cheap):
  %RECOVERY%\repo            -> candidate
  %RECOVERY%                 -> candidate
REM holographic layout on this volume:
  for /d %%T in ("%DISC_ROOT%\metadata\*") do  -> candidate %%T
  for /d %%T in ("%RECOVERY%\metadata\*") do   -> candidate %%T   (relocated case, see #3)
REM holographic layout on any other mounted disc (data discs also carry metadata\<tenant>\):
  for %%L in (D..Z) do for /d %%T in ("%%L:\metadata\*") do -> candidate %%T
```

Validity check per candidate: `if exist "%%T\keys" if exist "%%T\index"`. De-duplicate
by tenant name (last path component); prefer the meta-disc copy.

**2. Tenant selection.**
- `LCSAS_REPO` env var set → pick the candidate whose dir name matches; error if absent
  (parity with restore.sh `--repo`/`LCSAS_REPO`).
- Exactly one candidate → use it silently.
- Multiple → numbered prompt:
  ```
  This archive contains more than one backup set:
    [1] alpha
    [2] beta
  Which one do you want to restore? [1]:
  ```
  `set /p`, default 1, re-prompt on garbage. (Delayed expansion is already enabled.)

**3. Relocation must carry the repo metadata (extend lines 57-66).**
Add after the bin copy:
```bat
if exist "%~dp0..\..\metadata\" robocopy "%~dp0..\..\metadata" "%RAMDIR%\recovery\metadata" /E ... 
if exist "%~dp0metadata\"       robocopy "%~dp0metadata"       "%RAMDIR%\recovery\metadata" /E ...
```
(first form when run from `recovery\scripts\`, second when run from disc root; same
xcopy fallback as bin). `metadata\` holds config/keys/index/snapshots only — small
(KBs–low MBs), safe for `%TEMP%`. Post-relocation, the `%RECOVERY%\metadata\*` probe in
step 1 then finds it, so discovery works after the meta disc is ejected — which is the
entire point of relocation.

**4. Error message rewrite (the dead-end today).**
```
ERROR: could not find an LCSAS backup set (a folder containing keys\ and index\).
Looked in:
  %RECOVERY%\repo
  %RECOVERY%
  <disc-root>\metadata\<name>\
  D:..Z:\metadata\<name>\
Insert the disc labelled LCSAS_META (or any LCSAS data disc), wait for it to
appear in File Explorer, then run restore.bat again.
```

**5. Downstream args.** `%REPO%` already flows into tier 1/2 invocations
(lines 227, 246) unchanged. The catalog scan (lines 208-220) and pack-search scan
(lines 185-197) need no change.

No catalog/schema impact. Old discs with the legacy `repo\` layout (if any exist)
keep working because the legacy probes stay first.

## Tests & gates

1. **Always-on string guard (lands with the fix):**
   `tests/unit/test_restore_bat_dispatcher.py::test_restore_bat_probes_holographic_metadata_layout`
   — assert the .bat contains a `metadata\` probe and that the relocation block copies
   `metadata`. Runs in `make test-unit` / CI `test.yml`.
2. **Wine smoke (opt-in, local):** `recovery/tests/test_restore_bat_e2e.sh` — build a
   meta tree with `MetaVolumeBuilder` (metadata/<tenant>/ layout, **no** `repo/` dir),
   run `wine cmd /c restore.bat` with `LCSAS_REPO` + a piped password, assert it gets
   past repo discovery to tier-1 dispatch. Wire into `make -C recovery test` when
   `wine` is present. **Caveat (verifier-confirmed):** wine 9.0's cmd is not a faithful
   .bat interpreter (known delayed-expansion divergence) — treat this as a smoke gate
   only, never as proof of Windows behavior.
3. **Authoritative automated gate — depends on INFRA-01:** the two-job CI workflow
   (Linux job: `lcsas meta build` fixture tree as artifact; `windows-latest` job: run
   `restore.bat` against the tree as a directory, assert a snapshot restores
   byte-identically and exit 0). This is the gate that would have caught this finding
   and must block merge for `recovery/scripts/restore.bat` changes.
4. **Manual checklist:** copy the real-Windows drill from
   `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:320-331` into
   `recovery/docs/READINESS_CHECKLIST.txt` as a per-meta-disc-build item
   ("double-click restore.bat on a stock Win11 VM reaches the Password: prompt").

## Acceptance criteria

- [ ] On a tree produced by `lcsas meta build` (no `repo/` dir), `restore.bat` discovers
      `metadata\<tenant>\` and reaches the target/password prompts (verified on the
      INFRA-01 Windows runner and manually on a Win11 VM).
- [ ] With ≥2 tenants, a numbered selection prompt appears; `LCSAS_REPO=<name>` skips it.
- [ ] After single-drive relocation (read-only disc), discovery still succeeds with the
      meta disc ejected (metadata\ present under `%RAMDIR%\recovery`).
- [ ] The failure message lists every probed path and tells the user what to insert.
- [ ] `pytest tests/unit/test_restore_bat_dispatcher.py -v` passes, including the new
      holographic-probe guard.
- [ ] INFRA-01 Windows e2e job is green and required for the touched paths.

## Dependencies & related plans

- **INFRA-01** (Windows-e2e CI scaffolding) — blocking for test #3; #1/#2 can land first.
- **UX-04** (RECOVER_WINDOWS.txt doc sweep) — the doc the .bat's failure path points at;
  fix together so the fallback text matches the new probes.
- **UX-08** (cross-OS journey gates) — wires the wine smoke + cadence recording.
- **KEY** "share guidance names only the python3 combiner" — the .bat still has no
  combiner step; out of scope here, noted there.

## Effort

3 days: 1.5 impl (.bat discovery + relocation + prompts; batch quoting is fiddly),
1.5 test (wine smoke + INFRA-01 job iteration + Win11 VM manual pass).
Needs: wine locally; a real Windows VM or the INFRA-01 runner for sign-off.
