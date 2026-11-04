# Windows Restore Workflows

This document covers restoring LCSAS-backed data on a Windows host. The
intended audience is the **non-technical heir** scenario: an heir
(anyone who is handed the discs with no preparation) who owns a Windows
laptop, has the password, but does not have Linux, WSL, admin
rights, developer tools, or any pre-existing LCSAS installation. The
recovery toolchain ships a prebuilt `lcsas-restore.exe` and a
`restore.bat` orchestrator on the meta disc; the entire restore path
is "insert the meta disc, double-click `restore.bat`, answer its
questions, swap discs when asked."

The heir-facing walkthrough is `recovery/docs/RECOVER_WINDOWS.txt` on
the meta disc. This document is its companion for people who maintain
or check the Windows path: what `restore.bat` does, in order, and what
tests cover it.

Sibling docs:

- [`restore-host-linux.md`](restore-host-linux.md) – Linux host with the
  binaries on hand.
- [`restore-bare-metal.md`](restore-bare-metal.md) – a wiped machine
  (its boot path is not built today; no LCSAS disc is bootable).
- [`restore-disc-only.md`](restore-disc-only.md) – disc-only walkthrough
  ignoring host preinstall.

**References.** This document names `restore.bat`'s `REM -----` section
headings and its `:labels` (for example `:find_newest`) rather than line
numbers, which move with every change. `grep -n "^:find_newest"
recovery/scripts/restore.bat` finds one.

## Table of contents

1. [Common context](#common-context)
2. [The restore, step by step](#the-restore-step-by-step)
3. [Carrying on a restore that stopped](#carrying-on-a-restore-that-stopped)
4. [A run that does not relocate (`in-place\`)](#a-run-that-does-not-relocate-in-place)
5. [More than one optical drive](#more-than-one-optical-drive)
6. [Exit codes](#exit-codes)
7. [Manual Python fallback (not orchestrated by `restore.bat`)](#manual-python-fallback-not-orchestrated-by-restorebat)
8. [Path / drive-letter handling differences from Linux](#path--drive-letter-handling-differences-from-linux)
9. [Where restore.bat and restore.sh differ](#where-restorebat-and-restoresh-differ)
10. [Test coverage and gaps](#test-coverage-and-gaps)
11. [Consolidated source refs](#consolidated-source-refs)

## Common context

The Windows recovery stack is the file `recovery/scripts/restore.bat`
(also at the meta disc's root) plus three prebuilt programs:
`lcsas-restore.exe` (tier 1), `lcsas-keyshare.exe` (the share card
combiner) and `lcsas-ecc.exe` (the disc repair tool for
`restore.bat --check-disc`). They live at
`recovery/bin/x86_64-windows/` in the **source tree** (build-host output
dir); the meta disc builder bundles them onto the disc under the rust
target triple `recovery\bin\x86_64-pc-windows-gnu\`, next to the
upstream `rustic-static.exe` (tier 2). On-disc paths always use the
triple (see `recovery/docs/WINDOWS_RECOVERY_PLAN.txt` for the design
rationale). Sizes in the source tree: `lcsas-restore.exe` about 1.4 MB,
`lcsas-keyshare.exe` and `lcsas-ecc.exe` about 0.1 MB each.

The orchestrator runs a two-tier cascade:

| Tier | What runs                                              | When on Windows |
|------|--------------------------------------------------------|-----------------|
| 1    | `bin\<triple>\lcsas-restore.exe`                       | Always first    |
| 2    | `bin\<triple>\rustic-static.exe` (cross-check)         | Only for a backup set whose folder holds its own `data\` |

A backup set read from the discs (the normal layout: `metadata\<id>\` on
each disc, packs in each disc's `data\`) has no `data\` in its folder.
`rustic-static.exe` cannot ask for discs, so for such a set a tier-1
failure is final: the script stops at `:tier1_stopped`, keeps the tools
folder and says how to carry on (#615). There is no tier 3 in the .bat:
the Python reader ships on the disc, but `restore.bat` does not launch
it (it would need a Python install the host may not have); heirs who
need it run `standalone_restorer.py` by hand (see
[Manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)).

**Split passwords:** when a backup set's `LCSAS_REPO.txt` (in its
`metadata\<id>\` folder) says `split=K-of-N`, `restore.bat` asks for K
share cards itself and rebuilds the password with
`bin\x86_64-pc-windows-gnu\lcsas-keyshare.exe` (`:shares_ask`, #607).
When only `PASSWORD_INFO.txt` says the password is split (an older
disc, or a split that was never recorded), the heir rebuilds it first
by hand; the .bat prints that hint at the password prompt. See
`recovery/docs/RECOVER_WINDOWS.txt` ("SHARE CARDS (SPLIT PASSWORDS)"),
[`docs/SHARE_FORMAT.md`](../SHARE_FORMAT.md), and
[`docs/guides/recovery-runbook.md`](../guides/recovery-runbook.md).

Architecture detection (section "Detect target (arch + OS)") maps
`PROCESSOR_ARCHITECTURE` (`AMD64`/`x86`) and `PROCESSOR_ARCHITEW6432` to
the `x86_64-pc-windows-gnu` target triple; ARM64 is rejected with a
winget/rustic workaround message, and `LCSAS_TARGET` overrides the
choice. The triple is known before the relocation, which copies only
that triple's programs. The binary itself is the C codebase with the
Win32/POSIX shim `recovery/src/lcsas-restore/posix_compat.h` papering
over `mkdir`, `lseek` (to `_lseeki64` for 64-bit pack offsets),
`symlink` (stubbed to `-1`/`EPERM`), `chmod` (no-op), and `fsync`
(`_commit`).

Windows version floor is **Windows 10 1709 / October 2017** (or Windows
7 SP1 / 8 / 8.1 with KB2999226), because UCRT
(`api-ms-win-crt-*.dll`) is a load-time dependency
(`recovery/docs/RECOVER_WINDOWS.txt`, "WHY 1709?";
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt`, target platforms).

## The restore, step by step

**Purpose:** Restore files on a Windows 10/11 machine from the meta disc
and the data discs, with **one** optical drive (the usual cheap-laptop
layout) or several.

**Prerequisites:**

- Windows 10 build 1709 or later, or Windows 11 (any build). ARM64
  Windows is rejected (no aarch64 Windows binary ships).
- A USB optical drive (or built-in) that reads the disc format the
  archive was burned to.
- The meta disc plus the data discs.
- The password, or enough of the set's share cards.
- No admin rights, no installer, no toolchain.
- Free space on the drive that holds `%LOCALAPPDATA%` (usually C:) for
  the tools folder: a few MB for the programs and the backup sets'
  metadata, plus the pack cache, which grows to about the size of the
  data discs read. `restore.bat` makes no space check of its own
  (`restore.sh` does); `RECOVER_WINDOWS.txt` ("DESTINATION FOLDER
  NOTES") shows how to size the restore with `--list-pending-packs`.

**Steps:**

1. Insert the newest meta disc (labelled `LCSAS_META`; if it fails, use
   the meta disc burned closest in date to the data discs). Put it in the
   drive you will use for the data discs. `restore.bat` passes tier 1
   only the drives that hold a disc when it starts (`if exist X:\` is
   false for an empty drive); tier 1 itself also looks at the root of
   every optical drive each time it looks for a disc, so an empty
   second drive is still found.
   Optionally check the script and binary with `certutil -hashfile`
   against `recovery\MANIFEST.sha256` (`RECOVER_WINDOWS.txt`, "CHECK THE
   BINARY BEFORE RUNNING IT").
2. Double-click `restore.bat` in File Explorer (or run it from CMD /
   PowerShell). If SmartScreen says "Windows protected your PC," click
   **More info → Run anyway**. The tools are unsigned on purpose:
   Authenticode certificates expire on a 1-3 year cycle.
3. **QuickEdit warning** (section "A click in this window pauses the
   restore", #613). The first lines say that a click in the window
   selects text and pauses the restore (the window title then starts
   with "Select"), and that Esc or a right-click carries on. The script
   does not change the console's settings. The relocated copy does not
   say it again.
4. **The tools folder** (section "The tools folder", #609). `TOOLS` is
   `%LOCALAPPDATA%\LCSAS-restore` (or `LCSAS_TOOLS_DIR`). Because the
   folder is deleted at the end, the script uses only a folder that is
   absent, empty, or holds `.lcsas-tools`, the marker it writes there
   first (`:claim_tools`). Any other folder, and a whole drive such as
   `E:\`, is refused with exit 1 before anything in it changes
   (`:tools_taken`). Only the copy that runs from `TOOLS` (relocated, or
   `--resume`) and `--check-disc` skip this check.
5. **The copy off the disc** (section "Single-drive guard: copy the
   tools off the disc"). The script tries to write a probe file next to
   itself, with its error output sent to nul, so a read-only disc
   prints nothing (up to rc5 every start printed "Access is denied.",
   #611). If the probe can be written (a writable copy) or
   `LCSAS_NO_RELOCATE=1`, it does not relocate (see
   [`in-place\`](#a-run-that-does-not-relocate-in-place)). Otherwise:
   - It finds the recovery root on the disc for both places the script
     lives (`..` from `recovery\scripts\restore.bat`, `recovery\` from
     the disc-root `restore.bat`, #531) and the disc's `metadata\`.
   - **Reuse.** When `TOOLS` already holds a whole copy whose
     `restore.bat` is byte-identical to this one (`fc /b`) and whose
     `archive.txt` matches this META disc's `volume_info.json`
     `created_at` (`:archive_id`), it says `using the recovery files
     already copied to ...`, adds only the META disc's sets the copy
     lacks, and relaunches.
   - **Fresh copy** (`:tools_fresh`). Otherwise it removes what an
     earlier copy left, by name, keeping `pack-cache\` only when that
     copy's `lcsas-restore.exe` is byte-identical to the disc's. It
     writes `.lcsas-tools` first, then copies, naming each file on
     screen (`:copy_one`, #612): the three programs of
     `bin\<triple>\`, plus `rustic-static.exe` only when a set on the
     disc holds its own `data\`; `docs\RECOVER_WINDOWS.txt`; each
     `metadata\<id>\` (`xcopy`); a `catalog.db` beside `bin\` if there
     is one; `archive.txt`; and `restore.bat` last, so a copy cut short
     is never taken for a whole one. That is a few MB. Up to rc5 the
     relocation copied the whole `recovery\bin` with `robocopy` or
     `xcopy`, 744 MB and 17,063 files for six kinds of computer, with
     nothing on screen, into a random `%TEMP%` folder that every run
     deleted. The copy now uses `copy` and `xcopy` only: wine runs them,
     so the local tests check it.
   - A copy with no `.exe` in it, or one that failed (a copy that
     errors, or comes out a different size), is removed, and the script
     stays on the disc: `could not copy the recovery programs to ... --
     staying on disc.` The restore still works, but the disc cannot
     come out until it has finished.
   - It prints `copied recovery files to <TOOLS>` and `you may eject
     the meta disc when the binary prompts for a data disc.`
   - **The relaunch line** (`:relaunch`) ends the copy on the disc with
     `(goto) 2>nul`, `cd`s to `%SystemDrive%\`, sets `LCSAS_RELOCATED`
     to the disc's drive letter (e.g. `D:\`) and `call`s the copy in
     `TOOLS` with the same arguments. Nothing is read from the disc
     after that. When the copy returns, the same line deletes `TOOLS`
     only when both `.done` and `.lcsas-tools` are in it (the copy
     writes `.done` after a restore that finished), and hands back the
     copy's exit code with `cmd /c exit` (#532, #545). The relocated
     copy takes `LCSAS_RELOCATED` into its own `setlocal` scope and out
     of the caller's environment.
6. **The newest data disc** (section "The newest data disc", #605; C2).
   The META disc's copy of each backup set is as old as the META disc,
   and with one drive no data disc is in at the start. So, run from a
   META disc (relocated, or with no `Label:` line in `VOLUME_INFO.txt`
   and `recovery\scripts\restore.bat` at the root) with no data disc in
   any drive, the script asks:

   ```
   Take the META disc out and put in your NEWEST data disc: the one
   with the latest year, and then the highest number, e.g.
   LCSAS_CD700_2026_0002.  File Explorer shows only the first 16
   characters of the label; the full label is printed on the disc.

   Press ENTER when it is in; S then ENTER to skip (use the copies
   on the META disc); Q then ENTER to quit.
   Newest data disc:
   ```

   - `:find_newest` / `:consider_disc` look at D: to Z:. A data disc is
     one with `data\`, or with `volume_info.json` and no
     `recovery\scripts\restore.bat`. `:read_label` takes the label from
     the `  Label:` line of its `VOLUME_INFO.txt` (else
     `volume_info.json` `"label"`), never the drive's own name, which
     Joliet cuts to 16 characters, the same for every CD of a year.
     `:label_ok` keeps a label only when it holds nothing but
     `A-Z a-z 0-9 _`. `:parse_label` reads the year and number from the
     right; ties keep the lower drive letter.
   - On ENTER it prints `[lcsas-restore] newest data disc: <label> in
     drive D:`, or says no data disc is in yet, or that the disc has no
     LCSAS label, and asks again. `S` skips, `Q` stops with exit 65
     (`:newest_quit`). cmd cannot tell an empty answer from the end of
     the input, so after five tries it goes on with the META copies.
   - With data discs already in at the start it asks nothing and takes
     the newest of them. `LCSAS_NEWEST_DISC_PROMPT=0` skips the step, a
     resumed run (`--resume`) skips it, and so does a run that stayed on
     the disc.
   - `:merge_newest` / `:merge_set` then bring the tools folder's
     `metadata\` up to date: for each set on that disc, its
     `snapshots\` and `index\` come from that disc only, its `keys\` are
     the union of both copies, and its `LCSAS_REPO.txt` and `config`
     come along. Each set's new copy is built in `metadata-new\` and
     swapped in whole; a disc that cannot be read leaves the old copy as
     it was. A set only on the META disc keeps its META copy. Never a
     union of `index\` or `snapshots\`. The disc's label goes into
     `merged-from.txt`; a later run never merges an older disc over it.
7. **The backup set** (section "Find the restic repository",
   `:choose_set`, #606). Candidates are folders with `keys\` and
   `index\`: legacy layouts first, then the merged copy, then this
   disc's `metadata\`, then `D:\metadata\` to `Z:\metadata\`; the first
   copy of each id wins. With more than one, the menu shows each set's
   `name_cmd` from its `LCSAS_REPO.txt` (`:read_marker`; only when the
   marker's `id=` is the folder name; a set with no marker shows its id)
   and its split:

   ```
   This archive contains more than one backup set:
      [1] documents
      [2] photos (password split: any 2 of 3 share cards)

   Type the number of the one to restore, or Q then ENTER to quit.
   Which one do you want to restore? [1]:
   ```

   It takes a number (ENTER means 1). `Q` stops with exit 65
   (`:menu_quit`). `LCSAS_REPO` picks a set without the menu, by its
   id or `name_cmd` (`:match_set`), else by its `name=` line
   (`:match_name`, case-blind); no match exits 1 with the list.
8. **The folder** (section "Ask for target directory", `:ask_target`).
   `Type a folder, or ENTER for the one in brackets; Q then ENTER
   quits.` then `Restore to which folder? [<default>]:`. The default is
   `%USERPROFILE%\Documents\restored`. Quotes typed around the folder
   and trailing backslashes are dropped (a `\` before the closing quote
   would swallow the rest of tier 1's command line, #615). A whole
   drive (`E:\`) is refused and asked again; so is a folder without a
   drive letter or a `\\` prefix. `Q` stops with exit 65
   (`:target_quit`). A resumed run takes the folder from `resume.cfg`.
9. **The non-empty-folder guard** (section "Non-empty-target guard",
   UX-07). A folder that holds files and no `.lcsas-restore-marker`
   gets a warning; anything but `YES` stops with exit 65.
   `LCSAS_FORCE_NONEMPTY_TARGET=1` skips it. The marker is written
   empty (`restore.sh`'s names the sets restored there).
10. **The pack cache and the catalog copy** (section "Pack cache and
    the local copy of the catalog", #610). The pack cache is
    `TOOLS\pack-cache` unless `LCSAS_PACK_CACHE_DIR` names another
    folder; the script makes it and checks it exists (tier 1 can only
    make the last folder of the path). Tier 1 copies a data disc into
    it the first time it sees the disc, so each disc goes in about
    once. The catalog tier 1 gets is a copy, `TOOLS\catalog.db`, never
    a file on a disc (tier 1 keeps it open, which would hold the disc in
    the drive). It is copied from the newest data disc found in step 6;
    else, when carrying on or when `merged-from.txt` exists, the copy
    already in `TOOLS` is kept; else the last `catalog.db` found on D:
    to Z:.
11. **The drives tier 1 searches** (section "Where tier 1 looks for
    discs", #604). `DISC_LETTERS` is the META disc's own letter
    (`LCSAS_RELOCATED`) always, the letters a resumed run saved, and
    every other letter that holds a data disc at launch. Each goes to
    tier 1 as `--pack-search X:\`, and as `--mount-parent X:\` too once
    the pack cache folder exists (without a cache, tier 1 would open a
    disc's `catalog.db` in place). The META letter also goes as
    `--meta-disc X:\`, the very same string, so tier 1 skips it only
    while the META disc is in it. All `X:\` arguments are unquoted, so
    a `\"` cannot swallow the command line. The script prints
    `[lcsas-restore] drives searched for discs: D:\ ...`. Up to rc5 the
    META letter was left out, so a one-drive restore could never find a
    data disc (#604). `LCSAS_INTERACTIVE=on|off|auto` is passed as
    `--interactive`.
12. **`resume.cfg` and the launcher** (section "How to carry on if the
    restore stops", `:save_resume`; relocated runs only). Before the
    password is asked, the script writes `TOOLS\resume.cfg` (the set's
    id, the target folder, the META letter and the drive letters;
    never the password), reads it back, and writes `TOOLS\Resume LCSAS
    restore.bat`, with a copy on the Desktop (the folder Explorer has
    in HKCU `Shell Folders`, else `%USERPROFILE%\Desktop`).
13. **The password** (section "The password", #607, #608). It reaches
    the programs in a file in `%TEMP%` (`lcsas-pw-*.txt`), deleted on
    every way out; the first pass also deletes any `lcsas-pw-*.txt` and
    `lcsas-shares-*` a closed window left. The source is the first of
    `LCSAS_PWFILE` (copied byte for byte with `copy /b`),
    `LCSAS_PASSWORD`, `LCSAS_PW`, `LCSAS_SHARES_DIR`, the share cards
    (when the marker says the set is split), and the keyboard. The
    script says once that a typed password is visible. Before tier 1
    starts, `lcsas-restore.exe --list-snapshots` tries it
    (`:pw_check`): exit 77 means a key rejected it, and the script says
    `That password is wrong: it does not open the backup set <name>.`
    and asks again. A wrong or unreadable preset falls back to the
    keyboard. An empty answer asks again; `Q`, or the third empty
    answer in a row, ends with exit 77 (`:pw_gave_up`).
    - **Share cards** (`:shares_ask`): `The password for <name> is
      split into N share cards; you need any K of them (Split ID
      NNNNN).`, then for each card `Share card i of K: type all the
      words on one line, then ENTER` / `(or Q to quit).` and `Words of
      card i:`. Each card goes in its own file in a new
      `%TEMP%\lcsas-shares-*` folder and is checked alone with
      `lcsas-keyshare.exe <file>`: exit 3 or 2 names the problem and
      asks for that card again. Then the K cards are combined; cards
      that do not fit together are asked for again. `P` types the
      rebuilt password instead.
    - **`LCSAS_SHARES_DIR`**: every file in the folder is a card (more
      than K is fine). One mistyped file makes `lcsas-keyshare` refuse
      the whole folder, and the script falls back to the keyboard.
14. **Tier 1** (section "Tier 1: prebuilt lcsas-restore.exe"). It runs
    `lcsas-restore.exe --repo ... --password-file ... --target ...
    --snapshot latest` with the arguments of steps 10-11. Its disc box
    reads, for example:

    ```
    +----------------------------------------------------------+
    | Pack 4f3e2a1b9c8d0e7f... is required for the next file.  |
    | It is on any disc labelled:                              |
    |   LCSAS_CD700_2026_0001                                  |
    |                                                          |
    | Currently searching:                                     |
    |   D:\                                                    |
    |                                                          |
    | Insert the right disc and press ENTER to retry.          |
    | Type 'q' then ENTER to abort.                            |
    +----------------------------------------------------------+
    ```

    With no catalog yet the second line is `(no disc catalog yet:
    insert your newest data disc)`. The three "Single-drive restore"
    lines show only while the META disc is still in its drive. On
    Windows the box always waits for ENTER. Exit codes:
    - 0: `:restore_done`.
    - 77 (wrong password): the "could not decrypt the repository" box,
      then exit 77 from top level (`:wrong_password`; an `exit /b`
      inside a bracketed block does not reach `cmd /c`, #384).
    - 75 (`q` at the disc box) and any other exit for a set with no
      `data\`: `:tier1_stopped` prints `The restore stopped before it
      finished (code N).`, where the files are, and how to carry on,
      then exits with that code. The tools folder stays.
    - any other exit for a set with its own `data\`: tier 2.
15. **Another set** (section "The restore has finished"). After
    `Restore complete.  Files restored to: <folder>`, an archive with
    more than one set asks `Restore another backup set from these
    discs? [y/N]:` (unless `LCSAS_ANOTHER_SET=0`). `y` goes back to the
    menu (step 7) with the tools folder, the pack cache and the catalog
    copy as they are; the next folder defaults to
    `Documents\restored-<name>`. `Q` at the menu or the folder question
    in that pass ends the run with 0.
16. **The end** (`:all_done`). The pack cache goes if this run chose
    it, and the Desktop launcher goes. Relocated, the copy writes `.done`
    and the relaunch line (step 5) or the launcher deletes `TOOLS` once
    the copy has returned. Exit 0.

**Expected outcome:** Files materialise under
`%USERPROFILE%\Documents\restored` (or the chosen folder), byte-equal
to the original snapshot. The `%TEMP%` password file is deleted on
every exit path; the tools folder is gone after exit 0, and stays after
any other exit.

## Carrying on a restore that stopped

**Purpose:** Finish a restore that stopped (`q` at a disc box, a window
closed, the computer restarted) without the meta disc and without
copying anything again (#609). Up to rc5 the copy went into a random
`%TEMP%` folder that every run deleted, so carrying on meant the meta
disc and the whole copy again.

**Steps:**

1. Double-click `Resume LCSAS restore.bat` on the Desktop (or in the
   tools folder). It exists once the folder question (step 8) was
   answered on a relocated run.
2. The launcher ends its own batch with `(goto)`, `cd`s to
   `%SystemDrive%\` and runs `TOOLS\recovery\scripts\restore.bat
   --resume`.
3. `--resume` reads `resume.cfg` (`:read_resume`): the set, the target
   folder, the META letter (as `LCSAS_RELOCATED`) and the saved drive
   letters, which are searched even when empty now. It skips the
   newest-disc step (the same snapshot as before the stop), keeps
   `TOOLS\catalog.db`, and asks only for the password.
4. Tier 1 skips every file that is already complete (#92), and the
   pack cache keeps the discs already read.
5. After exit 0 the launcher deletes itself and `TOOLS` (when `.done`
   and `.lcsas-tools` are there).

Running `restore.bat` from the meta disc again works too: it asks the
questions again and reuses the tools folder (step 5).

## A run that does not relocate (`in-place\`)

A run that does not relocate (`LCSAS_NO_RELOCATE=1`, a writable copy of
the META disc such as a USB stick, or the script staying on the disc
after a failed copy) still needs a pack cache, a catalog copy and,
after the newest-disc step, a merged `metadata\`. It keeps them in
`TOOLS\in-place\` (section "A run that does not relocate"), never in
`TOOLS` itself, so a stopped restore waiting in `TOOLS` for its
launcher is left as it is. `in-place\` is removed after exit 0, and
`TOOLS` too if nothing else remains (`:all_done_in_place`). Such runs
write no `resume.cfg` and no launcher; to carry on, run `restore.bat`
again and choose the same folder.

## More than one optical drive

With data discs in other drives at launch, step 6 asks nothing and
merges the newest of them; step 11 passes every such letter to tier 1,
so the restore may need no swap at all. The META disc's letter is
passed too, and searched once the META disc is out.

## Exit codes

| Code | Meaning |
|------|---------|
| 0  | The restore finished: every backup set the reader chose. |
| 1  | A failure: unsupported processor, a tools folder that is not the restore's own, no backup set found, `LCSAS_REPO` matches nothing, the target cannot be made, or no tier works. The message says which. |
| 65 | The reader typed Q at a question `restore.bat` asked (the newest data disc, the backup set, the folder), or did not type YES at the non-empty-folder warning. |
| 75 | The reader typed q at tier 1's disc box. Carry on with the launcher. |
| 77 | Wrong password, or Q (or three empty answers) at the password or a share card. |
| other | The code tier 1 stopped with, for a set read from the discs. |

`restore.bat --check-disc IMAGE` has its own codes: 0 (no damage, or
repaired), 1 (damage left, by request or beyond the parity), 2 (no RS03
layer) and 3 (no image, no `lcsas-ecc.exe`, or a read error).

## Manual Python fallback (not orchestrated by `restore.bat`)

**Purpose:** Restore when both `lcsas-restore.exe` (Tier 1) and the
vendored `rustic-static.exe` (Tier 2) are unavailable on the host —
e.g. older Windows missing UCRT, antivirus quarantine, or broken
.exe bits — and the heir is willing to install Python 3 themselves.

The Python reader (`standalone_restorer.py`) still ships on every meta
disc, but `restore.bat` no longer launches it. The previous inner
cascade depended on the `py` launcher being installed on the target
Windows host, which is not a safe assumption for the
headless-restore scenario the script targets; on a stock Windows
machine the launcher is absent and the cascade was effectively
unreachable. The heir invokes the Python path explicitly when they
need it.

**Prerequisites:**

- Any Windows that runs Python 3.10 or newer, which rules out
  Windows 7 and older (`recovery/docs/RECOVER_WINDOWS.txt`).
- Python 3 installed from python.org (which adds `python` and `py`
  to `PATH` by default).
- `standalone_restorer.py` on the meta disc (it is bundled by the
  meta disc builder; see `meta/`).

**Steps:**

1. Install Python 3 from python.org. The standard installer adds
   `python` to `PATH`.
2. Open a Command Prompt at the meta disc root, e.g.:

   ```
   D:
   cd D:\
   ```

3. Put the password in a file (the script only reads it via
   `--password-file`; it never asks for it interactively), then
   invoke the Python reader directly. `--repo`,
   `--password-file` and `--target` are all required flags; there
   are no positional arguments:

   ```
   python standalone_restorer.py ^
       --repo D:\metadata\<id> ^
       --password-file C:\path\to\pw.txt ^
       --target C:\Users\me\restored ^
       --mount-point E:\
   ```

   `<id>` is the repository's folder under `D:\metadata\` (the
   directory containing `keys\` and `index\`; `dir D:\metadata`
   lists them, and the `name=` line of each folder's
   `LCSAS_REPO.txt` says which set it is). The newest data disc's
   `metadata\<id>\` has the newest snapshots. Pack data lives on the
   data discs: add one `--mount-point <letter>:\` per inserted data
   disc — the disc-swap pause re-scans the mount points after a swap.
   Use `--snapshot <id>` to pick a specific snapshot (default:
   latest), or `--list-snapshots` to enumerate them.  See
   `python standalone_restorer.py --help` for the full flag surface.

4. The `restore.bat` orchestrator is **not** involved in this path.
   If you have already double-clicked `restore.bat` and watched it
   exit with "no working recovery method on this system", the
   manual Python invocation above is the documented next step.

**Expected outcome:** Files restored via the Python reader (pure-Python
AES/zstd, with no external binary dependencies and no UCRT
requirement). Symlink and ACL caveats from
[Path / drive-letter handling](#path--drive-letter-handling-differences-from-linux)
still apply.

**Variant axes that apply:**

- **OS:** any Windows that runs Python 3.10 or newer (Windows 8.1, 10
  and 11). This **is** the path for a Windows 8.1 host that never
  received KB2999226 (UCRT); Windows 7 and older cannot install
  Python 3.10.
- **Optical drive count:** Manual; the heir is responsible for
  pointing the Python reader at whichever drive has the
  repository. The tools folder and disc swaps of
  [the restore](#the-restore-step-by-step) apply only to the
  .bat-orchestrated tiers and do not run here.
- **Recovery tier:** This is the manual escape hatch for cases
  where both Tier 1 and Tier 2 are unusable.

**Test coverage:** The Python reader is exercised by the
Linux-side pytest suite (`src/lcsas/restore/restic_fallback.py`); there is
no automated Wine/Windows test of the manual invocation path. The
`Phase W5 — legacy msvcrt build` in
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt` (a native binary for XP/Vista)
is not planned: it was decided against in #491. This path does not
replace it, because Python 3.10 does not install on Windows 7 or older.
Heirs on those systems move the discs to a newer machine, or on
Windows 7 install KB2999226.

**Source refs:** `standalone_restorer.py` (root of the meta disc),
`recovery/docs/RECOVER_WINDOWS.txt` ("MANUAL PYTHON RESTORE"),
`src/lcsas/restore/restic_fallback.py` (the underlying pure-Python
AES/zstd code).

## Path / drive-letter handling differences from Linux

These are not workflow steps in themselves but constraints every
Windows workflow inherits. Source refs in parentheses.

- **Drive letters vs mount points:** the Linux `restore.sh` walks
  `LCSAS_MOUNT_DIRS` (`/Volumes`, `/media`, `/mnt`, `/run/media` ...);
  `restore.bat` looks at drive letters `D` through `Z` (step 11).
  Letters below `D` are skipped to avoid the OS drive and any system
  reserved drives. `lcsas-restore.exe` itself also searches the root of
  every optical drive (`GetDriveTypeA == DRIVE_CDROM`) each time it
  looks for a disc, and splits `LCSAS_MOUNT_DIRS` on `;` (not `:`).
- **Drive names:** File Explorer shows a disc's Joliet volume name, cut
  to 16 characters: the prefix, the media and the year, the same for
  every CD of a year. The
  scripts and tier 1 read the full label from the disc's
  `VOLUME_INFO.txt` / `volume_info.json` instead.
- **Path separators:** the C source always passes forward slashes; the
  Win32 runtime accepts them transparently, so no path translation is
  needed (`recovery/src/lcsas-restore/posix_compat.h`;
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`).
- **Backslash quoting in CMD:** the .bat quotes every path passed to
  binaries (`"%REPO%"`, `"%TARGET%"`, `"%PWFILE%"`) so spaces and
  embedded `&`/`(`/`)` survive. A path that ends in `\` must not be
  quoted: `"E:\"` ends in `\"`, which the program reads as a quote. So
  the drive-letter arguments go unquoted, and trailing backslashes are
  stripped from the target (step 8).
- **Long paths (> 260 chars):** Windows historically caps paths at
  `MAX_PATH=260`. Modern Windows 10/11 supports longer paths if the
  registry key `LongPathsEnabled=1` is set or via group policy. Files
  with paths > 260 chars fail to write on default installs; documented
  workaround is to restore to a short root like `C:\r`
  (`recovery/docs/RECOVER_WINDOWS.txt`, "DESTINATION FOLDER NOTES").
  Tier 1's handling of such paths, of non-ASCII names, of `:` and of
  `..\` is tracked in #619.
- **Symlinks:** `posix_compat.h` stubs `symlink()` to return `-1` with
  `EPERM` because Windows symlink creation requires
  `SeCreateSymbolicLinkPrivilege`, which the binary deliberately does
  not request. Affected paths are reported on stderr.
- **chmod / fsync / lseek:** `chmod` is a no-op; `fsync` maps to
  `_commit`; `lseek` maps to `_lseeki64` so 64-bit pack offsets
  survive — without this remap, pack files > 4 GiB would silently
  truncate (`recovery/src/lcsas-restore/posix_compat.h`).
- **Filename case:** Windows is case-insensitive; an archive that
  contains both `foo.txt` and `FOO.TXT` will lose one on restore.
- **Owner/group/UID/GID/xattrs:** Windows has no equivalent and the
  binary silently skips them; hardlinks across drives silently
  degrade to copies (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt`).
- **UTF-8 / code page:** the binary embeds an application manifest
  requesting `<activeCodePage>UTF8</activeCodePage>` on Windows 10
  1903+. On older Windows non-ASCII filenames may be mangled. The
  menu shows only a set's `name_cmd` (plain letters), because cmd reads
  marker files in the console's code page.
- **`O_BINARY`:** open flags include `O_BINARY` on Windows (to suppress
  CRLF translation); on POSIX the shim defines `O_BINARY` to `0` so
  call sites use the same constants.

## Where restore.bat and restore.sh differ

Both scripts agree on the exit codes 0/65/75/77, the newest-disc merge
rule, the catalog copy passed as `--catalog`, the marker names and the
"carry on with the same snapshot" rule. These differences remain, by
design or as open owner decisions (DESIGN "sh vs bat differences
LEFT"):

| Topic | restore.bat | restore.sh |
|---|---|---|
| Set menu | `[1] name`, a number only, ENTER means 1 | `  1) name`, "Choose a repository (number or name):", number, name, `name_cmd` or id; no default |
| Wrong `LCSAS_PASSWORD` / `LCSAS_PWFILE` | says so, asks on the keyboard; reused for the next set | exits 77; dropped for the next set |
| `LCSAS_SHARES_DIR` with one bad card file | refuses the folder, asks on the keyboard | leaves that file out, combines the rest; exits 77 if they cannot |
| `LCSAS_TOOLS_DIR` naming a folder with other files | refused, exit 1 | a temporary folder; the run cannot be carried on |
| `LCSAS_INTERACTIVE` | passed to tier 1 only; the script's own questions are always asked | also decides whether the script's own questions are asked |
| Newest-disc step | also from a writable META copy; five ENTERs go on | only when the META mount is known and a terminal is there; end of input quits 65 |
| Another set | asks the set, then the folder (default `restored-<name>`) | asks the folder (default `<target>-2`), then the set |
| `LCSAS_REPO` match | case-blind, first match | exact; none when two sets share the name |
| Catalog copy | the newest label's disc | the freshest file by mtime |
| Tools folder marker | `.lcsas-tools` | `.lcsas-restore-tools` |
| Tier cascade | tier 2 only for a set with its own `data\`; no tier 3 | `LCSAS_TIER_FALLBACK=1` walks tiers 2, 2b and 3 |

## Test coverage and gaps

**Existing automated coverage:**

- `.github/workflows/windows-e2e.yml` (run weekly through
  `windows-e2e-weekly.yml`, by hand with `gh workflow run windows-e2e.yml
  --ref <branch>`, and on any PR touching `recovery/scripts/**`,
  `src/lcsas/meta/**`, `src/lcsas/staging/metadata.py`,
  `recovery/bin/x86_64-windows/**`, `tests/e2e/fixture_lib.py`,
  `tests/e2e/windows_e2e_lib.ps1` or the workflow itself; it is
  path-filtered and not a required check) is the real-Windows gate. A
  Linux job builds a fixture (real rustic repository, data ISOs and a
  meta ISO, via `tests/e2e/fixture_lib.py::build_windows_fixture`, sized
  so the burn spans several TEST_TINY data discs; the job fails if it
  yields fewer than two); a `windows-latest` job runs `restore.bat`
  through a real `cmd`, in order:
  - *Mount ISOs and run restore.bat (happy path)* and *Verify restored
    tree matches manifest (byte-exact)*: every ISO mounted at its own
    letter, `LCSAS_NO_RELOCATE=1`, answers fed on stdin, the tree
    compared byte for byte against a SHA-256 manifest.
  - *Multi-disc — the restore swept more than one data disc*: more than
    one mounted disc holds packs and none holds them all.
  - *Relocation leg — restore.bat at the disc root (the heir's
    double-click)* and *Relocation leg — `recovery\scripts\restore.bat`
    layout*: the copy into `%LOCALAPPDATA%\LCSAS-restore`, tier 1 run
    from it, exit 0, a byte-exact tree, no password file and no tools
    folder left, and no "Access is denied." (#611).
  - *Negative case — wrong password fails cleanly* and *Relocated wrong
    password — exit 77 survives the cleanup*: exit 77, no tier-2
    fall-through, the "could not decrypt the repository" banner, no
    partial tree.
  - *Eject leg — relocated restore survives a meta-disc eject*: the
    meta ISO is dismounted once the relocated copy is up; exit 0, no
    "The system cannot find the path specified." (#532).
  - *Single-drive leg — every data disc swapped into one drive letter*
    (#604, #617): only the meta ISO is mounted; the newest-disc step is
    answered by putting the newest data ISO into the meta disc's letter,
    and at each disc box the named data ISO is moved into that SAME
    letter (`Dismount-DiskImage`, `Mount-DiskImage -NoDriveLetter`,
    `mountvol`) with `LCSAS_INTERACTIVE=on`. It asserts exit 0, a
    byte-exact tree, swaps at most one more than the data discs, and no
    "Access is denied.".
  - *Stop-and-resume leg — q at a disc box, then the Desktop launcher*
    (#609, #615): `q` at the second box exits 75 and keeps the tools
    folder, `resume.cfg` (no password) and the Desktop launcher; the
    launcher then finishes the restore with no meta disc, exit 0, and
    the tools folder and launcher are gone.
- The wine suites run the real `restore.bat` through wine's `cmd`. They
  are local pre-filters, not gates: wine's `cmd` is an approximation of
  Windows', and it does not end a batch at `(goto) 2>nul`, so it cannot
  run the relaunch line or the launcher. The relocated tests lay out the
  tools folder as the copy does and start that copy with
  `LCSAS_RELOCATED` set.
  - `tests/recovery_hardening/test_restore_bat_wine_smoke.py`: discovery,
    tier 1, wrong password (77, no tier 2), missing repository.
  - `tests/recovery_hardening/test_restore_bat_tools_folder_wine.py`:
    the copy into the tools folder (#609, #611, #612, #613), stop and
    carry on, `LCSAS_TOOLS_DIR` refusing a busy folder.
  - `tests/recovery_hardening/test_restore_bat_single_drive_wine.py`: a
    one-drive, two-disc restore swapped in the same letter by
    re-pointing `dosdevices/d:`; `q` at the box with no tier 2; the
    catalog passed as a local copy.
  - `tests/recovery_hardening/test_restore_bat_journey_wine.py`: the
    newest-disc step, names in the menu (UUID ids), the password asked
    again, 2-of-3 share cards, `LCSAS_SHARES_DIR`, another set, the
    folder question, disc text that is never run.
  - `tests/recovery_hardening/test_restore_bat_ecc_dispatch_wine.py`
    (`--check-disc` on a clean and a damaged image).
  - `tests/recovery_hardening/test_tier1_windows_wine.py`,
    `test_tier1_windows_drive_swap_wine.py` (tier 1 alone: drive-root
    discovery, drain on first sight, exit 75, `;` in
    `LCSAS_MOUNT_DIRS`) and `test_keyshare_windows_wine.py`.
- Static checks: `tests/recovery_hardening/test_restore_bat_password_lifetime.py`
  (the `%TEMP%` password file is deleted on every terminal path) and
  `tests/unit/test_restore_bat_dispatcher.py` (target naming, the ARM64
  rejection message, `LCSAS_TARGET`, the non-empty-target warning).
- `recovery/tests/test_e2e_windows.sh` runs
  `bin/x86_64-windows/lcsas-restore.exe` under wine against a synthetic
  v2 (zstd) repository (tier 1 only).

**Gaps:**

- **A real optical drive.** Every Windows leg mounts ISO images.
  `Dismount-DiskImage` detaches an image without asking whether anything
  holds a handle on it, so the legs show the restore survives the loss
  of the meta disc, not that Explorer's own Eject would be allowed. The
  manual drill (#476, a Windows 10 VM on the lab host with one optical
  drive) covers that.
- **`%TEMP%` / `%LOCALAPPDATA%` exhaustion** has no test.
- **A root target** (`E:\`) is refused; lifting that needs a
  windows-e2e leg that proves it (for example with a `subst` drive).
- **Older Windows / msvcrt (intentional: Windows 10 1709+ is the stated
  support floor).** Windows XP/Vista/unpatched 7 are listed as REJECTED
  platforms in `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`; Phase W5 there
  (an msvcrt-linked binary and a Wine-with-UCRT-disabled harness) was
  decided against (#491). Heirs on those systems move the discs to a
  newer host. The
  [manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)
  is no way round it on Windows 7 or older: Python 3.10 does not install
  there.
- **SmartScreen / antivirus (intentional: heir-driven, cannot be
  CI-tested).** The "Run anyway" / quarantine-restore steps in
  `recovery/docs/RECOVER_WINDOWS.txt` are manual, and Authenticode
  signing is a documented NON-GOAL in
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`. The manual double-click
  drill is a release gate in #476.
- **UAC (intentional: manual gate #476).** The restore path is designed
  to need no elevation; no test runs `restore.bat` from an elevated CMD.
- **ARM64 Windows (intentional: not shipped).** The rejection text is
  pinned by
  `tests/unit/test_restore_bat_dispatcher.py::test_restore_bat_rejects_windows_arm64_with_explanation`.

## Consolidated source refs

- `recovery/scripts/restore.bat` (full file) — orchestrator; see its
  `REM -----` sections and `:labels` named above.
- `recovery/docs/RECOVER_WINDOWS.txt` — heir-facing walkthrough,
  QuickEdit, the tools folder, the newest data disc, swaps, stopping
  and carrying on, the password, share cards, SmartScreen / antivirus,
  command-line reference, exit codes, older Windows options, physical
  disc problems.
- `recovery/docs/ENV_VARS.txt` — every `LCSAS_*` variable, with what
  `restore.bat` does with it.
- `recovery/docs/MULTI_DISC_DESIGN.txt` — the swap design, the newest
  data disc, the tools folder and carrying on, for both scripts.
- `recovery/docs/WINDOWS_RECOVERY_PLAN.txt` — design rationale, target
  platforms, source portability plan, cross-compile (zig cc), testing
  strategy, Phase W5 msvcrt plan, out-of-scope/non-goals.
- `recovery/src/lcsas-restore/posix_compat.h` — POSIX/Win32 shim
  (`mkdir`, `lseek`/`_lseeki64`, `symlink` stub, `chmod` no-op,
  `fsync`/`_commit`, `O_BINARY`).
- `recovery/src/lcsas-restore/main.c` (`usage()`) — the `--meta-disc` /
  `--pack-search` / `--mount-parent` / `--catalog` / `--interactive`
  flag surface and exit codes used by `restore.bat`.
- `recovery/bin/x86_64-windows/lcsas-restore.exe` — prebuilt static
  Windows binary (UCRT, MinGW-w64 via `zig cc`) in the source tree;
  bundled onto the disc at `recovery\bin\x86_64-pc-windows-gnu\`,
  which is the path Tier 1 invokes.
- `.github/workflows/windows-e2e.yml` and `tests/e2e/windows_e2e_lib.ps1`
  — the real-Windows legs.
