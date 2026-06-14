# UX-07: restore silently overwrites a non-empty target directory

**Priority:** P2 · **Severity:** medium · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: recovery/docs/UX_CONCERNS.txt ID 009 (OPEN, rated LOW)
**Suggested GH issue title:** Warn before restoring into a non-empty, non-resume target dir

## Problem

`lcsas-restore` writes directly into `--target`; `restore.sh` does
`mkdir -p "$TARGET_DIR"` and proceeds; `restore.bat` creates the chosen folder and
runs. An heir who restores into an existing folder of their own files with
colliding names — or restores twice with different snapshots — silently overwrites
**live** data with decades-old archive content. This is the only point in the
journey where a confused user causes irreversible loss of current data rather than
just a failed restore, and no tier asks for confirmation.

A hard refusal would be wrong: RECOVER.txt's RETRY SAFETY section correctly
documents that re-running into the same target is the supported recovery from an
interrupted restore (idempotent resume). The right shape is: warn only when the
target is non-empty **and** does not look like a previous LCSAS restore.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/scripts/restore.sh:740` — `mkdir -p "$TARGET_DIR"`, no emptiness check;
  password prompt precedes it at lines 727-737.
- `recovery/scripts/restore.bat:151` — `if not exist "%TARGET%" mkdir "%TARGET%"`,
  then proceeds.
- `recovery/docs/UX_CONCERNS.txt:171-183` — ID 009 OPEN: "files are overwritten
  without prompt"; proposed mitigations (`--verify-only`, refuse non-empty without
  `--force`) unimplemented.
- `recovery/docs/RECOVER.txt:135-148` — RETRY SAFETY documents idempotent re-runs
  into the same directory ("No 'delete the target directory first' dance"), so
  silent resume must be preserved.

## Fix design

Marker-file approach, implemented in the **drivers** (no C change required):

1. `recovery/scripts/restore.sh` — insert before the password block (~line 725), so
   the heir is warned before typing the password:
   ```sh
   MARKER="$TARGET_DIR/.lcsas-restore-marker"
   if [ -d "$TARGET_DIR" ] && [ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ] \
      && [ ! -f "$MARKER" ] && [ "${LCSAS_FORCE_NONEMPTY_TARGET:-0}" != "1" ]; then
       framed warning >&2:
         WARNING: the folder '$TARGET_DIR' already contains files that do not
         look like a previous LCSAS restore.  Restored files with the same
         names will OVERWRITE what is there now.
         Safest choice: stop, and restore into a NEW, empty folder instead.
       printf 'Type YES to overwrite, anything else to stop: ' >&2
       IFS= read -r ans
       [ "$ans" = "YES" ] || exit "$EXIT_USER_ABORT"   # new exit code, documented
   fi
   mkdir -p "$TARGET_DIR"
   : > "$MARKER" 2>/dev/null || true
   ```
   Non-TTY stdin without the env override → abort with the same message plus
   "set LCSAS_FORCE_NONEMPTY_TARGET=1 to proceed non-interactively".
2. `recovery/scripts/restore.bat` — equivalent before the password prompt
   (lines 158-168): `dir /b "%TARGET%" | findstr . >nul` emptiness test, check for
   `%TARGET%\.lcsas-restore-marker`, `set /p` YES confirmation, write the marker
   after confirmation/creation. Honor `LCSAS_FORCE_NONEMPTY_TARGET`.
3. Docs: add the marker + prompt to `recovery/docs/RECOVER.txt` RETRY SAFETY
   (re-runs stay silent because the marker exists) and to restore.sh `--help` env-var
   list (`LCSAS_FORCE_NONEMPTY_TARGET`). Flip UX_CONCERNS ID 009 to RESOLVED with a
   pointer.
4. Compat: targets from restores made **before** this change lack the marker, so one
   re-run prompts once — acceptable; typing YES then writes the marker. The marker is
   a hidden zero-byte file left in the restored tree; document it. `--verify-only`
   (ID 009's other mitigation) is a tier-1 C feature — out of scope here, leave ID
   009's bullet for it referenced to a T1C follow-up.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_nonempty_target_guard.py` — drives
  `recovery/scripts/restore.sh` directly (pattern of the existing
  `test_restore_sh_relocate.py`): (a) pre-populated foreign target, stdin "no" →
  exits non-zero before any tier runs, prints the warning; (b) target with marker →
  no prompt, proceeds; (c) empty target → no prompt; (d)
  `LCSAS_FORCE_NONEMPTY_TARGET=1` → no prompt; (e) non-TTY without override → aborts
  with the env hint. Runs in `make -C recovery test`; becomes CI-effective when the
  hardening tier is wired in (GATE plans).
- `tests/unit/test_restore_bat_dispatcher.py` — string-level assertions: the .bat
  contains the marker filename and a `Type YES` prompt (until INFRA-01's Windows e2e
  can drive it functionally).
- Blind-drill scoring (opt-in): add the prompt text to
  `tests/e2e/cdemu_blind_restore/verify.sh` expectations so an agent run that trips
  the guard is scored on handling it.

## Acceptance criteria

- [ ] Restoring into a non-empty foreign dir prompts and aborts by default
      (sh + bat); empty or marker-bearing dirs proceed silently.
- [ ] Interrupted-restore re-run (RECOVER.txt RETRY SAFETY scenario) still resumes
      with no prompt.
- [ ] `pytest tests/recovery_hardening/test_nonempty_target_guard.py -v` passes.
- [ ] UX_CONCERNS ID 009 updated; `--help` documents the new env var and exit code.

## Dependencies & related plans

- **UX-01 / INFRA-01** — functional .bat coverage of the guard rides the Windows e2e.
- **GATE** "recovery_hardening never runs in CI" — makes the new test a merge gate.
- T1C follow-up for `--verify-only` (optional, separate).

## Effort

1.5 days: 0.5 sh, 0.5 bat (quoting + non-TTY handling), 0.5 tests/docs.
Wine helpful for .bat sanity; real verification rides the UX-01 Windows drill.

---
**Implemented:** 2026-06-14. As planned, with two deviations: (1) the marker/exit-code constant (EXIT_USER_ABORT=65) is defined inline at the guard rather than alongside EXIT_NO_RECOVERY_BIN, matching that exit code's own inline definition; (2) the verify.sh blind-drill expectation was intentionally NOT added — the blind drill restores into an empty target so the guard never fires there, and a hard scoring check on absent guard text would fail every existing passing run. recovery_hardening + unit tests fully cover the behavior. Pre-existing unrelated failure noted: tests/recovery_hardening/test_restore_bat_wine_smoke.py::test_missing_repo_reports_error asserts the stale "no restic repo" string (replaced by UX-01) and fails on clean master, independent of this change.
