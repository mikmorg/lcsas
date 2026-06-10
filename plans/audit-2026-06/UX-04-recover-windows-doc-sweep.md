# UX-04: RECOVER_WINDOWS.txt gives wrong binary paths and a wrong restorer command throughout

**Priority:** P0 · **Severity:** high · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Sweep RECOVER_WINDOWS.txt: fix triple paths and standalone-restorer commands

## Problem

When the Windows journey hits any snag (restore.bat failure, SmartScreen, missing
UCRT), the heir is routed to the burned manual `recovery/docs/RECOVER_WINDOWS.txt`.
Three independent classes of wrong instruction live there:

1. **Wrong binary paths.** All six binary-path references use the legacy
   `recovery\bin\x86_64-windows\`, but the meta builder ships binaries under the
   rust triple `recovery\bin\x86_64-pc-windows-gnu\`. The recommended integrity
   check (`certutil` + `findstr` against MANIFEST.sha256) therefore can't find the
   file — and the doc itself says a mismatch means "the disc has been tampered with
   ... do not run". The heir is instructed to distrust a healthy disc.
2. **Wrong standalone-restorer command.** Three fallback sections say
   `python standalone_restorer.py D:\repo C:\Users\me\restored` (positional args)
   and claim "the script prompts for the password on stdin". The generated script
   requires `--repo`, `--password-file`, and `--target` (all `required=True`);
   positionals make argparse error out, and it never prompts for a password.
3. **A repo path that doesn't exist.** `D:\repo` exists on no current-layout disc —
   repo metadata lives under `metadata\<tenant>\`, and pack data is on the data
   discs (reachable via `--mount-point`).

Combined with UX-01 (restore.bat dead-ends at repo discovery), every documented
Windows route fails for an heir following the text literally.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/docs/RECOVER_WINDOWS.txt:78,112,186,194-195,274` — all use
  `x86_64-windows` paths (e.g. line 78: `certutil -hashfile
  recovery\bin\x86_64-windows\lcsas-restore.exe SHA256`).
- `src/lcsas/meta/builder.py:2146-2165` — `tier1_map`: `"x86_64-pc-windows-gnu":
  ("x86_64-windows", "lcsas-restore.exe")` — `x86_64-windows` is the **source-tree**
  dir; the on-disc destination is `bin/x86_64-pc-windows-gnu/`.
- `recovery/docs/RECOVER_WINDOWS.txt:85-86` — "If they do not [match], the disc has
  been tampered with or is corrupted -- do not run."
- `recovery/docs/RECOVER_WINDOWS.txt:~207` (CASE C option c), `:~306` (option D),
  `:~346` (MANUAL PYTHON RECOVERY STEP 3) — positional invocations
  `python [D:\]standalone_restorer.py D:\repo C:\Users\me\restored`; `:~349-350` —
  "The script prompts for the password on stdin".
- `src/lcsas/restore/standalone_builder.py:141-152` — `--repo`, `--password-file`,
  `--target` all `required=True`; no positional args, no password prompt.
  `:165-173` — `--mount-point` (append) is the supported way to feed data-disc packs.
- `tests/unit/test_restore_bat_dispatcher.py` bans `x86_64-windows` from the **.bat**
  only — the doc was never covered.

## Fix design

Single sweep of `recovery/docs/RECOVER_WINDOWS.txt`:

1. **Paths:** replace every `recovery\bin\x86_64-windows\` with
   `recovery\bin\x86_64-pc-windows-gnu\` (lines 78, 112, 186, 194-195, 274). For the
   developer-facing copy hint at 194-195, distinguish source-tree
   (`recovery/bin/x86_64-windows/` on the build host) from on-disc destination
   (`recovery\bin\x86_64-pc-windows-gnu\`) explicitly — that asymmetry caused the
   drift.
2. **Standalone-restorer sections** (all three): replace with the real interface and
   the real layout, including the cache/mount-point pre-step:
   ```
   python D:\standalone_restorer.py ^
       --repo D:\metadata\<name> ^
       --password-file C:\path\to\pw.txt ^
       --target C:\Users\me\restored ^
       --mount-point E:\
   ```
   plus: `<name>` is the backup-set folder under `D:\metadata\` (dir containing
   `keys\` and `index\`); add one `--mount-point <letter>:\` per inserted data disc
   (the disc-swap prompt re-scans them); the password must be in a file — create it
   with Notepad — because the script takes `--password-file`, it does **not** prompt.
   Remove the "prompts for the password on stdin" sentence; the visible-typing caveat
   moves to the pw-file creation note.
3. **Tamper framing:** soften lines 85-86 to first suspect a path/version mismatch:
   "If `findstr` finds no line for the file, you are probably looking at the wrong
   path — check `recovery\bin\` for the actual folder name. A hash that EXISTS in the
   manifest but differs from `certutil`'s output means corruption or tampering — do
   not run that binary."
4. Cross-check the same legacy path / positional-command patterns in
   `docs/workflows/restore-windows.md` and `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`
   and fix any hits (restore-windows.md:106-107 currently restates the wrong
   `keys/`+`index/` probe — update alongside UX-01's discovery change).

No catalog/schema impact. Already-burned discs keep the wrong manual forever; the
corrected text ships on the next meta-disc burn (P0: do before it).

## Tests & gates

Extend `tests/unit/test_doc_command_contract.py` (skeleton from UX-02; always-on in
`make test-unit` / CI test.yml — deliberately **not** `tests/recovery_hardening/`,
which CI never runs, per the verifier's refinement):

- `test_no_legacy_windows_triple_in_docs` — assert `x86_64-windows\` (backslash form,
  i.e. an on-disc path) appears nowhere in `recovery/docs/*.txt` or
  `docs/workflows/*.md`; assert the triple used matches the `tier1_map` key in
  `src/lcsas/meta/builder.py` (import and read the mapping, don't hardcode).
- `test_standalone_restorer_invocations_use_real_flags` — regex-extract every
  `standalone_restorer.py` invocation across `docs/` and `recovery/docs/`; assert each
  contains `--repo`, `--password-file`, `--target`, and no positional path arguments.
- `test_no_stdin_password_claim_for_standalone` — assert the "prompts for the
  password" phrasing does not appear near standalone_restorer mentions.
- Manual: the UX-01 Win11 checklist drill should include one fallback rehearsal —
  run the documented MANUAL PYTHON RECOVERY steps verbatim on the VM.

## Acceptance criteria

- [ ] `grep -n 'x86_64-windows' recovery/docs/RECOVER_WINDOWS.txt` → no hits.
- [ ] `certutil`+`findstr` steps as written succeed against a built meta tree's
      MANIFEST.sha256.
- [ ] The MANUAL PYTHON RECOVERY steps, executed verbatim against a built meta tree
      (+ one data-disc dir as `--mount-point`), restore a fixture snapshot.
- [ ] `pytest tests/unit/test_doc_command_contract.py -v` passes and fails if any
      legacy path or positional invocation is reintroduced.

## Dependencies & related plans

- **UX-02** — contract-test skeleton; land first.
- **UX-01** — restore.bat discovery; its failure text points here, and
  restore-windows.md needs a coordinated update. **INFRA-01** indirectly (the Windows
  e2e can execute the documented fallback once running).
- **KEY** "heir-facing share guidance names only the python3 combiner" — the missing
  Windows key-share section in this doc is owned there.

## Effort

1 day: 0.5 doc sweep + verbatim rehearsal against a built tree, 0.5 contract-test
extensions. No special environment (Windows VM rehearsal can ride UX-01's drill).
