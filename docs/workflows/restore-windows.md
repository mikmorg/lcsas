# Windows Recovery Workflows

This document covers restoring LCSAS-backed data on a Windows host. The
intended audience is the **non-technical recipient** scenario: an
inheritor (or anyone who is handed the discs cold) who owns a Windows
laptop, has the archive password, but does not have Linux, WSL, admin
rights, developer tools, or any pre-existing LCSAS installation. The
recovery toolchain ships a prebuilt `lcsas-restore.exe` and a
`restore.bat` orchestrator on the meta-disc; the entire recovery path
is "insert disc, double-click `restore.bat`, type password."

Sibling docs:

- [`restore-host-linux.md`](restore-host-linux.md) – Linux host with the
  binaries on hand.
- [`restore-bare-metal.md`](restore-bare-metal.md) – boot from disc onto
  a wiped machine.
- [`restore-disc-only.md`](restore-disc-only.md) – disc-only walkthrough
  ignoring host preinstall.

## Table of contents

1. [Common context](#common-context)
2. [Workflow A: Meta-disc + multi-drive (happy path)](#workflow-a-meta-disc--multi-drive-happy-path)
3. [Workflow B: Single-drive variant (RAM staging + disc swap)](#workflow-b-single-drive-variant-ram-staging--disc-swap)
4. [Manual Python fallback (not orchestrated by `restore.bat`)](#manual-python-fallback-not-orchestrated-by-restorebat)
5. [Path / drive-letter handling differences from Linux](#path--drive-letter-handling-differences-from-linux)
6. [Test coverage and gaps](#test-coverage-and-gaps)
7. [Consolidated source refs](#consolidated-source-refs)

## Common context

The Windows recovery stack is the file `recovery/scripts/restore.bat`
plus the prebuilt `lcsas-restore.exe`. The binary lives at
`recovery/bin/x86_64-windows/` in the **source tree** (build-host
output dir), but the meta-builder bundles it onto the disc under the
rust target triple `recovery\bin\x86_64-pc-windows-gnu\` — on-disc
paths always use the triple (see `recovery/docs/RECOVER_WINDOWS.txt`
for the user-facing walkthrough and
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt` for the design rationale).

The orchestrator runs a two-tier cascade. The Python fallback that
shipped on the disc is no longer chained from the .bat (the inner
cascade depended on the `py` launcher being installed on the target
Windows host, which is not guaranteed for the headless-recovery
scenario the script targets); users who need it invoke
`standalone_restorer.py` manually (see [Manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)).

**Split-key archives:** if `KEY_INFO.txt` says the password was split
into SLIP-0039 key shares, rebuild it **first** with the bundled
combiner before running `restore.bat`. The primary tool is the static
`bin\x86_64-pc-windows-gnu\lcsas-keyshare.exe` (the .bat prints this
hint at the password prompt); the Python fallback is
`keyshare_combine.py` run under the meta-disc's bundled CPython. See
`recovery/docs/RECOVER_WINDOWS.txt` ("KEY SHARES (SPLIT PASSWORDS)"),
[`docs/KEY_SHARE_FORMAT.md`](../KEY_SHARE_FORMAT.md), and
[`docs/guides/recovery-runbook.md`](../guides/recovery-runbook.md).

| Tier | What runs                                              | Status on Windows |
|------|--------------------------------------------------------|-------------------|
| 1    | `bin\<arch>\lcsas-restore.exe`                         | Primary           |
| 2    | `bin\<arch>\rustic-static.exe` (cross-check)           | Secondary         |

Architecture detection maps `PROCESSOR_ARCHITECTURE` (`AMD64`/`x86`)
and `PROCESSOR_ARCHITEW6432` to the `x86_64-pc-windows-gnu` target
triple (ARM64 is explicitly rejected with a winget/rustic workaround
message) at `recovery/scripts/restore.bat:93`-`123`. The binary
itself is the C99 codebase with the Win32/POSIX shim
`recovery/src/lcsas-restore/posix_compat.h` papering over `mkdir`,
`lseek` (to `_lseeki64` for 64-bit pack offsets), `symlink` (stubbed to
`-1`/`EPERM`), `chmod` (no-op), and `fsync` (`_commit`).

Windows version floor is **Windows 10 1709 / October 2017** (or Windows
7 SP1 / 8 / 8.1 with KB2999226), because UCRT
(`api-ms-win-crt-*.dll`) is a load-time dependency
(`recovery/docs/RECOVER_WINDOWS.txt:11`-`19`,
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:14`-`22`).

## Workflow A: Meta-disc + multi-drive (happy path)

**Purpose:** Recover files on a Windows 10/11 machine when the user has
either (a) a writable mount point for the meta-disc contents, or (b)
multiple optical drives so the meta-disc can stay inserted while data
discs are read from another drive.

**Prerequisites:**

- Windows 10 build 1709 or later, or Windows 11 (any build); ARM64
  Windows 11 also supported but **not runtime-tested**
  (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:18`-`22`).
- USB optical drive (or built-in) that reads the disc format the
  archive was burned to.
- The meta-disc plus all data discs available; if a multi-drive setup
  is used the data disc(s) sit in additional drives.
- Archive password.
- No admin rights, no installer, no toolchain
  (`recovery/docs/RECOVER_WINDOWS.txt:22`-`28`).

**Steps:**

1. Plug in the USB optical drive and insert the disc labelled
   `LCSAS_META` (or any meta-volume disc). Confirm in File Explorer
   that it appears under "This PC" as a drive letter such as `D:` or
   `E:` (`recovery/docs/RECOVER_WINDOWS.txt:39`-`44`).
2. (Optional, paranoid users) Verify the script and binary against the
   per-disc manifest with `certutil`:
   `certutil -hashfile restore.bat SHA256` and
   `certutil -hashfile recovery\bin\x86_64-pc-windows-gnu\lcsas-restore.exe SHA256`,
   then `findstr` the expected values out of
   `recovery\MANIFEST.sha256` (`recovery/docs/RECOVER_WINDOWS.txt:70`-`86`).
3. Double-click `restore.bat` in File Explorer (or run it from CMD /
   PowerShell at the disc root) (`recovery/scripts/restore.bat:2`-`7`,
   `recovery/docs/RECOVER_WINDOWS.txt:44`-`45`).
4. If SmartScreen says "Windows protected your PC," click **More info
   → Run anyway**. The signing certificate is intentionally absent
   because Authenticode certs expire on a 1-3 year cycle and are
   incompatible with long-term archival
   (`recovery/docs/RECOVER_WINDOWS.txt:88`-`107`,
   `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:381`-`386`).
5. The script auto-discovers the recovery root and architecture
   (`recovery/scripts/restore.bat:84`-`105`) and the restic repo by
   probing for `keys/` + `index/` (`recovery/scripts/restore.bat:107`-`116`).
6. When prompted for the restore folder, accept the default
   (`%USERPROFILE%\Documents\restored`) by pressing Enter, or type a
   different absolute path (`recovery/scripts/restore.bat:118`-`137`).
7. Type the password. It is **visible while typing** because plain CMD
   has no `read -s` equivalent (`recovery/scripts/restore.bat:139`-`149`,
   `recovery/docs/RECOVER_WINDOWS.txt:114`-`134`). The password is
   written to a transient file under `%TEMP%` whose name embeds
   `%RANDOM%` plus the current time and is deleted on every exit path
   (`recovery/scripts/restore.bat:151`-`157`, `:210`, `:229`, `:256`,
   `:268`).
8. `restore.bat` walks drive letters `D-Z` and registers any drive
   containing `data\` or `repo\data\` as a `--pack-search` argument so
   `lcsas-restore.exe` finds packs that live on the data discs without
   the user having to type paths
   (`recovery/scripts/restore.bat:159`-`178`). It also auto-selects
   the freshest `catalog.db` across drives — the meta-disc deliberately
   carries none (it would be stale at burn time)
   (`recovery/scripts/restore.bat:185`-`201`).
9. Tier 1 fires: `lcsas-restore.exe` runs with `--repo`,
   `--password-file`, `--target`, `--snapshot latest`, plus the
   collected `--pack-search` / `--catalog` / `--meta-disc` args. If
   it exits non-zero the script demotes to Tier 2
   (`rustic-static.exe`). If Tier 2 is also missing or fails, the
   script prints an error (with a hint pointing at
   `standalone_restorer.py` for users with Python installed) and
   exits non-zero.
10. On success the script prints "Recovery complete" with the target
    folder and `pause`s so a double-click console does not vanish.

**Expected outcome:** Files materialise under
`%USERPROFILE%\Documents\restored` (or the chosen folder), byte-equal
to the original snapshot. The `%TEMP%` password file is deleted on
every exit path.

**Variant axes that apply:**

- **OS:** Windows 10 1709+ / Windows 11 on x86_64. ARM64 Windows is a
  deferred target: `restore.bat` rejects it with a winget/rustic
  workaround message and no aarch64 Windows binary ships (see the
  target-platforms list at the top of
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`).
  Windows 7/8/8.1 with KB2999226 is best-effort.
- **Optical drive count:** 2+ drives is the cleanest layout — the
  meta-disc stays mounted, data discs sit in other drives, the
  drive-letter sweep at `recovery/scripts/restore.bat:167`-`178` picks
  them all up automatically. For 1 drive see [Workflow B](#workflow-b-single-drive-variant-ram-staging--disc-swap).
- **Recovery tier:** Tier 1 first; falls back to Tier 2 (bundled
  rustic) on non-zero exit. There is no third (Python) tier inside
  the .bat; if both tiers fail the script exits with an error and a
  hint pointing at the manual `standalone_restorer.py` path
  (see [Manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)).

**Test coverage:** `restore.bat` is driven through a CMD interpreter on a
real `windows-latest` runner by `.github/workflows/windows-e2e.yml`
(happy path with the meta ISO and four data ISOs mounted on separate drive
letters, so the drive-letter sweep runs across several data discs;
wrong password exits 77 with no partial tree) and, locally, through wine's `cmd` by
`tests/recovery_hardening/test_restore_bat_wine_smoke.py`. The Linux-host
Wine test at `recovery/tests/test_e2e_windows.sh` exercises Tier 1 only
(it invokes the `.exe` directly). Gaps: no automated test that
SmartScreen "Run anyway" works post-quarantine (manual drill, #476).
The remaining real-Windows verification is the manual checklist in
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt` ("ON A REAL WINDOWS VM
(manual, recurring)").

**Source refs:** `recovery/scripts/restore.bat:84`-`220`,
`recovery/docs/RECOVER_WINDOWS.txt:35`-`58`,
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:14`-`24`,
`recovery/src/lcsas-restore/posix_compat.h:30`-`77`, `lcsas-restore.exe`
binary at `recovery/bin/x86_64-windows/lcsas-restore.exe`.

## Workflow B: Single-drive variant (RAM staging + disc swap)

**Purpose:** Same as Workflow A, but the user has **only one** optical
drive — the most common cheap-laptop layout. The script must free the
drive after starting so the user can eject the meta-disc and feed in
data discs through the same drive.

**Prerequisites:**

- Same Windows version requirements as Workflow A.
- Single optical drive (USB or built-in).
- Writable `%TEMP%` with enough free space to mirror the
  `recovery\bin\` tree (typically a few MB; the meta-disc bins are
  measured in MB, not GB).
- Sufficient RAM or pagefile for the script and binary to run from
  `%TEMP%` while disc swaps happen.

**Steps:**

1. Insert the meta-disc and double-click `restore.bat` exactly as in
   Workflow A
   (`recovery/docs/RECOVER_WINDOWS.txt:54`-`58`).
2. The script's single-drive guard kicks in
   (`recovery/scripts/restore.bat:25`-`46`): it tries to create a
   throwaway `lcsas-probe-*.tmp` file next to itself; if the disc is
   read-only the file does **not** exist after the redirect, signalling
   "I am running off a read-only volume." The guard is skipped if
   `LCSAS_NO_RELOCATE=1` or if it has already relocated itself (sentinel
   `LCSAS_RELOCATED`) (`recovery/scripts/restore.bat:36`-`37`).
3. The script creates a unique `%TEMP%\lcsas-restore-<RANDOM>-<RANDOM>`
   directory and rebuilds the on-disc `recovery\scripts\` and
   `recovery\bin\` layout under it
   (`recovery/scripts/restore.bat:47`-`57`). If `mkdir` fails it falls
   back to running in-place
   (`recovery/scripts/restore.bat:53`-`56`).
4. The bat copies itself with `copy /Y "%~f0"` and mirrors the binary
   tree with `robocopy` (preferred — handles long paths), falling back
   to `xcopy /E /I /Y /Q` if `robocopy` is absent
   (`recovery/scripts/restore.bat:60`-`66`). If a catalog is on the
   meta-disc it is copied too (`recovery/scripts/restore.bat:67`).
5. The user sees `[lcsas-restore] copied recovery files to %RAMDIR%` /
   `you may eject the recovery disc when the binary prompts for a data disc.`
   (`recovery/scripts/restore.bat:69`-`71`). The script captures the
   meta-disc drive letter into `LCSAS_RELOCATED` (e.g. `D:\`), `cd`s
   out of the disc to `%SystemDrive%\` so the new CMD does not inherit
   the disc cwd, and `call`s the relocated `restore.bat` with the same
   args (`recovery/scripts/restore.bat:73`-`81`).
6. The relocated copy runs everything from Workflow A steps 5-10. Now
   the meta-disc has no file handles outstanding, so the user can
   eject it when prompted and load the first data disc into the same
   drive.
7. Two cooperating mechanisms keep the swapped drive in scope:
   - `restore.bat` skips the meta-disc drive letter when building
     `--pack-search` so the data disc that lands on `D:\` is treated
     as a fresh data volume rather than the (now ejected) meta-disc
     (`recovery/scripts/restore.bat:167`-`172`).
   - The bat passes `--meta-disc %LCSAS_RELOCATED%` through to the
     binary so the locator inside `lcsas-restore.exe` excludes that
     path from its own search list and refuses to keep cwd inside it
     (`recovery/scripts/restore.bat:180`-`183`,
     `recovery/src/lcsas-restore/main.c:28`-`32`).
8. When `lcsas-restore.exe` is missing a pack it prompts the user to
   swap discs; the operator ejects the current disc, inserts the next,
   and presses Enter. Restore continues until the target folder is
   filled.

**Expected outcome:** Identical to Workflow A; the user only needs one
optical drive and never has to manually copy disc contents to local
disk first.

**Variant axes that apply:**

- **OS:** Same as Workflow A. The relocation logic depends on
  `%TEMP%`, `%RANDOM%`, and `cmd.exe` semantics present in every
  supported Windows version.
- **Optical drive count:** **1** is the whole point of this variant.
  Multi-drive users transparently skip it because the writability
  probe succeeds when the script is already running from disk
  (`recovery/scripts/restore.bat:39`-`45`); the multi-drive case also
  works correctly from the relocated copy if it triggers anyway.
- **Recovery tier:** Same cascade as Workflow A. The relocation
  happens before tier dispatch, so all tiers benefit.

**Test coverage:** The relocation runs on a real `windows-latest` runner
in `.github/workflows/windows-e2e.yml`: the steps *Relocation leg —
`recovery\scripts\restore.bat` layout* (copy made under `%TEMP%`, tier 1 run
from the copy, restored tree byte-exact) and *Eject leg — relocated restore
survives a meta-disc eject* (the meta ISO is dismounted once the relocated
copy is up, and the restore still finishes byte-exact). The wine suite
(`tests/recovery_hardening/test_restore_bat_wine_smoke.py`) and the
happy-path and wrong-password steps of `windows-e2e.yml` still set
`LCSAS_NO_RELOCATE=1`; `recovery/tests/test_e2e_windows.sh` invokes the
`.exe` directly; and the multi-disc / disc-swap design has its own pytest at
`recovery/tests/test_multidisc.py` but that does not exercise the Windows
.bat copy/relocate logic.

What the real-runner legs found (the first run of the relocation leg
surfaced the first item):

- **The relocation is broken when started from the disc-root `restore.bat`
  (the copy a user double-clicks).** The bin tree is copied from
  `%~dp0..\bin`, which is `E:\bin` for `E:\restore.bat` (the binaries are in
  `E:\recovery\bin`), so the RAM copy gets an empty `recovery\bin` and the
  relocated script ends in `ERROR: no working recovery method on this
  system` with exit code 1. From `E:\recovery\scripts\restore.bat` it works.
  The windows-e2e step *Relocation leg — restore.bat at the disc root (the
  heir's double-click)* asserts the working behaviour and is
  `continue-on-error` until this is fixed (#531).
- **What a working relocation leaves behind:** the copy
  `%TEMP%\lcsas-restore-<n>-<n>\` (about 40 files, 16.5 MB with the fixture
  meta tree) is never removed by `restore.bat`, and the script does not claim
  to remove it; it also prints `Access is denied.` once, from the
  writability probe on the read-only disc. The password file is deleted.
- **After the meta disc is ejected,** the restore completes and is
  byte-exact, but the outer `cmd` can no longer re-read its own
  `restore.bat` (it sits on the ejected disc) and prints `The system cannot
  find the path specified.` twice after the final "Press any key"; the exit
  code is then 1 although the recovery succeeded (#532).

Gaps: no test for `%TEMP%` exhaustion or for the `robocopy`/`xcopy`
fallback; the eject leg uses `Dismount-DiskImage`, which detaches the image
without asking whether anything still holds a handle on it, so it shows the
restore survives the loss of the meta volume but not that Explorer's own
Eject would be allowed (open handles or a cwd on the disc make it refuse);
no test of a real disc swap on one drive (the legs mount every data disc at
once, so the swap prompt never appears).

**Source refs:** `recovery/scripts/restore.bat:25`-`81`,
`recovery/scripts/restore.bat:159`-`183`,
`recovery/src/lcsas-restore/main.c:1`-`32`,
`recovery/docs/RECOVER_WINDOWS.txt:54`-`58`.

## Manual Python fallback (not orchestrated by `restore.bat`)

**Purpose:** Recover when both `lcsas-restore.exe` (Tier 1) and the
vendored `rustic-static.exe` (Tier 2) are unavailable on the host —
e.g. older Windows missing UCRT, antivirus quarantine, or broken
.exe bits — and the user is willing to install Python 3 themselves.

The pure-Python `standalone_restorer.py` still ships on every meta
disc, but `restore.bat` no longer launches it. The previous inner
cascade depended on the `py` launcher being installed on the target
Windows host, which is not a safe assumption for the
headless-recovery scenario the script targets; on a stock Windows
machine the launcher is absent and the cascade was effectively
unreachable. The user invokes the Python path explicitly when they
need it.

**Prerequisites:**

- Any Windows that still runs Python 3.6 – 3.12
  (`recovery/docs/RECOVER_WINDOWS.txt`).
- Python 3 installed from python.org (which adds `python` and `py`
  to `PATH` by default).
- `standalone_restorer.py` on the meta disc (it is bundled by the
  meta-volume builder; see `meta/`).

**Steps:**

1. Install Python 3 from python.org. The standard installer adds
   `python` to `PATH`.
2. Open a Command Prompt at the meta-disc root, e.g.:

   ```
   D:
   cd D:\
   ```

3. Put the password in a file (the script only reads it via
   `--password-file`; it never asks for it interactively), then
   invoke the standalone restorer directly. `--repo`,
   `--password-file` and `--target` are all required flags; there
   are no positional arguments:

   ```
   python standalone_restorer.py ^
       --repo D:\metadata\<name> ^
       --password-file C:\path\to\pw.txt ^
       --target C:\Users\me\restored ^
       --mount-point E:\
   ```

   `<name>` is the backup-set folder under `D:\metadata\` (the
   directory containing `keys\` and `index\`; `dir D:\metadata`
   lists them). Pack data lives on the data discs: add one
   `--mount-point <letter>:\` per inserted data disc — the
   disc-swap pause re-scans the mount points after a swap.
   Use `--snapshot <id>` to pick a specific snapshot (default:
   latest), or `--list-snapshots` to enumerate them.  See
   `python standalone_restorer.py --help` for the full flag surface.

4. The `restore.bat` orchestrator is **not** involved in this path.
   If you have already double-clicked `restore.bat` and watched it
   exit with "no working recovery method on this system", the
   manual Python invocation above is the documented next step.

**Expected outcome:** Files restored via the pure-Python AES/zstd
restorer (which has no external binary dependencies and no UCRT
requirement). Symlink and ACL caveats from
[Path / drive-letter handling](#path--drive-letter-handling-differences-from-linux)
still apply.

**Variant axes that apply:**

- **OS:** Windows 7 / 8 / 8.1 / 10 / 11 — anything that still runs
  Python 3. Notably this **is** the path for Windows 7 SP1 / 8 / 8.1
  hosts that never received KB2999226 (UCRT)
  (`recovery/docs/RECOVER_WINDOWS.txt`).
- **Optical drive count:** Manual; the user is responsible for
  pointing the standalone restorer at whichever drive has the
  repo. The single-drive RAM-relocation trick in
  [Workflow B](#workflow-b-single-drive-variant-ram-staging--disc-swap)
  applies only to the .bat-orchestrated tiers and does not run here.
- **Recovery tier:** This is the manual escape hatch for cases
  where both Tier 1 and Tier 2 are unusable.

**Test coverage:** The pure-Python restorer is exercised by the
Linux-side pytest suite (`src/lcsas/restore/restic_fallback.py`); there is
no automated Wine/Windows test of the manual invocation path. The
`Phase W5 — legacy msvcrt build` documented at
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt` is the long-term plan to
give XP/Vista a native binary path, but it is not yet implemented (build
or non-goal is an open decision, #491).

**Source refs:** `standalone_restorer.py` (root of the meta disc),
`recovery/docs/RECOVER_WINDOWS.txt` (user-facing manual recovery
section), `src/lcsas/restore/restic_fallback.py` (the underlying
pure-Python AES/zstd restorer).

## Path / drive-letter handling differences from Linux

These are not workflow steps in themselves but constraints every
Windows workflow inherits. Source refs in parentheses.

- **Drive letters vs mount points:** the Linux `restore.sh` walks
  `/media/`, `/mnt/`, `/run/media/`; the Windows .bat sweeps drive
  letters `D` through `Z` and registers each one containing `data\`
  or `repo\data\` as a `--pack-search` directory
  (`recovery/scripts/restore.bat:166`-`178`). Drive letters below `D`
  are skipped to avoid the OS volume and any system reserved drives.
- **Path separators:** the C source always passes forward slashes; the
  Win32 runtime accepts them transparently, so no path translation is
  needed
  (`recovery/src/lcsas-restore/posix_compat.h:30`-`58`,
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:60`-`64`).
- **Backslash quoting in CMD:** the .bat quotes every path passed to
  binaries (`"%REPO%"`, `"%TARGET%"`, `"%PWFILE%"`) so spaces and
  embedded `&`/`(`/`)` survive
  (`recovery/scripts/restore.bat:208`, `:227`, `:254`).
- **Long paths (> 260 chars):** Windows historically caps paths at
  `MAX_PATH=260`. Modern Windows 10/11 supports longer paths if the
  registry key `LongPathsEnabled=1` is set or via group policy. Files
  with paths > 260 chars fail to write on default installs; documented
  workaround is to restore to a short root like `C:\r`
  (`recovery/docs/RECOVER_WINDOWS.txt:149`-`156`).
- **Symlinks:** `posix_compat.h` stubs `symlink()` to return `-1` with
  `EPERM` because Windows symlink creation requires
  `SeCreateSymbolicLinkPrivilege`, which the binary deliberately does
  not request. Affected paths are reported on stderr
  (`recovery/src/lcsas-restore/posix_compat.h:14`-`17`, `:54`;
  `recovery/docs/RECOVER_WINDOWS.txt:157`-`161`).
- **chmod / fsync / lseek:** `chmod` is a no-op; `fsync` maps to
  `_commit`; `lseek` maps to `_lseeki64` so 64-bit pack offsets
  survive — without this remap, pack files > 4 GiB would silently
  truncate (`recovery/src/lcsas-restore/posix_compat.h:50`-`58`,
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:134`-`143`).
- **Filename case:** Windows is case-insensitive; an archive that
  contains both `foo.txt` and `FOO.TXT` will lose one on restore
  (`recovery/docs/RECOVER_WINDOWS.txt:163`-`166`).
- **Owner/group/UID/GID/xattrs:** Windows has no equivalent and the
  binary silently skips them; hardlinks across volumes silently
  degrade to copies
  (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:365`-`378`).
- **UTF-8 / code page:** the binary embeds an application manifest
  requesting `<activeCodePage>UTF8</activeCodePage>` on Windows 10
  1903+. On older Windows non-ASCII filenames may be mangled
  (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:118`-`131`).
- **`O_BINARY`:** open flags include `O_BINARY` on Windows (to suppress
  CRLF translation); on POSIX the shim defines `O_BINARY` to `0` so
  call sites use the same constants
  (`recovery/src/lcsas-restore/posix_compat.h:70`-`75`).

## Test coverage and gaps

**Existing automated coverage:**

- `recovery/tests/test_e2e_windows.sh` builds a synthetic v2 (zstd)
  restic repo via the helper in `recovery/tests/test_e2e.py`, runs
  `bin/x86_64-windows/lcsas-restore.exe` under Wine (with
  `WINEDEBUG=-all`), and verifies three fixture files
  (`hello.txt`, `binary.bin`, `compress.txt`) are restored
  byte-for-byte. The script skips cleanly when the .exe or Wine is
  missing. It invokes the .exe directly, so it exercises tier 1 only.
- `.github/workflows/windows-e2e.yml` (run weekly through
  `windows-e2e-weekly.yml`, and on any PR touching `recovery/scripts/**`,
  `src/lcsas/meta/**`, `recovery/bin/x86_64-windows/**`,
  `tests/e2e/fixture_lib.py` or `tests/e2e/windows_e2e_lib.ps1`) is the
  real-Windows gate. A Linux job builds a fixture (real rustic repo, data
  ISOs and a meta ISO, via
  `tests/e2e/fixture_lib.py::build_windows_fixture`, sized to 8 x 512 KiB
  files so the burn spans several TEST_TINY data discs: four discs holding
  nine packs between them in the validating runs; the job fails if it
  yields fewer than two); a
  `windows-latest` job mounts every ISO and runs, in order:
  - *Mount ISOs and run restore.bat (happy path)* and *Verify restored tree
    matches manifest (byte-exact)*: `restore.bat` unattended (answers fed on
    stdin, `LCSAS_NO_RELOCATE=1`), then the restored tree compared
    byte-for-byte against a SHA-256 manifest. All four data discs are
    mounted, so the drive-letter sweep hands them all to the tier-1 binary.
  - *Multi-disc — the restore swept more than one data disc*: more than one
    mounted disc holds packs and none holds them all. `restore.bat` prints
    nothing about its `--pack-search` drives and `--repo` carries no `data\`,
    so with the byte-exact check this is the proof that packs came from
    several discs.
  - *Relocation leg — `recovery\scripts\restore.bat` layout*: the same
    restore without `LCSAS_NO_RELOCATE`, started with its cwd on the disc.
    It requires the `copied recovery files to` message, a tier-1 binary that
    ran from the `%TEMP%` copy, exit 0, a byte-exact tree and no password file
    left in `%TEMP%`, and reports what the script leaves behind.
  - *Relocation leg — restore.bat at the disc root (the heir's
    double-click)*: the same from `E:\restore.bat`. It currently FAILS (see
    the Workflow B coverage note) and is `continue-on-error`.
  - *Negative case — wrong password fails cleanly*: requires exit 77, no
    tier-2 fall-through, the "could not decrypt the repository" banner and no
    partial tree left behind.
  - *Eject leg — relocated restore survives a meta-disc eject*: `restore.bat`
    runs with a stdin pipe held open; once the relocated copy is up the meta
    ISO is dismounted, and only then are the target and password fed in, so
    tier 1 runs entirely after the eject. Requires "Recovery complete" and a
    byte-exact tree; the exit code is reported, not asserted.
- `tests/recovery_hardening/test_restore_bat_wine_smoke.py` runs the real
  `restore.bat` through wine's `cmd` against fake meta and data trees on
  mapped drive letters: `test_happy_path_discovers_repo_and_invokes_tier1`,
  `test_wrong_password_is_terminal_no_tier2_fallthrough` and
  `test_missing_repo_reports_error`. It is a local pre-filter, not a gate
  (wine's `cmd` is an approximation of Windows').
- `tests/recovery_hardening/test_restore_bat_ecc_dispatch_wine.py`
  (`--check-disc` on a clean and a damaged image),
  `tests/recovery_hardening/test_restore_bat_password_lifetime.py`
  (the `%TEMP%` password file is deleted on every terminal path, checked
  statically) and `tests/unit/test_restore_bat_dispatcher.py`
  (target naming, the ARM64 rejection message, `LCSAS_TARGET`, the
  non-empty-target warning) cover the rest of the script's branches.
- `tests/recovery_hardening/test_tier1_windows_wine.py` runs the
  tier-1 .exe under wine.

**Gaps:**

- **`restore.bat` itself is now tested** (previously "no automated test of
  `restore.bat`"; resolved by the wine suite and `windows-e2e.yml` above).
  The RAM relocation is now exercised on real Windows by the
  `windows-e2e.yml` relocation and eject legs (next bullet); the wine suite
  and the happy-path and wrong-password steps still set
  `LCSAS_NO_RELOCATE=1`.
- **The single-drive RAM relocation (Workflow B) is covered only in part.**
  Covered by `windows-e2e.yml` (*Relocation leg* and *Eject leg* steps):
  `robocopy` mirrors the bin tree and the relocated copy runs tier 1 to a
  byte-exact restore when started from `recovery\scripts\restore.bat`, and
  the restore survives a meta-disc eject (`Dismount-DiskImage`). Still open:
  the disc-root `restore.bat` entry point fails the relocation leg (bin tree
  not copied; the step is `continue-on-error` until fixed); `%TEMP%`
  exhaustion; the `xcopy` fallback (the runner always has `robocopy`);
  whether `cd /d %SystemDrive%\` really frees the disc for Explorer's Eject
  (the forced `Dismount-DiskImage` would succeed regardless); and a real
  disc swap on one drive.
- **No automated test for older Windows / msvcrt (intentional: Windows 10
  1709+ is the stated support floor).** Windows XP/Vista/unpatched 7 are
  listed as REJECTED platforms in `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`;
  Phase W5 there (an msvcrt-linked binary and a Wine-with-UCRT-disabled
  harness) is "planned; not yet started" and awaits a build-or-non-goal
  decision (#491). Until then users on those systems are pushed to the
  [manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)
  or to move the disc to a newer host.
- **No automated test of SmartScreen / antivirus interactions
  (intentional: user-driven, cannot be CI-tested).** The documented "Run
  anyway" / quarantine-restore steps in `recovery/docs/RECOVER_WINDOWS.txt`
  are manual, and Authenticode signing is a documented NON-GOAL in
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt`. The manual real-Windows
  double-click drill is tracked as a release gate in #476.
- **No automated test of UAC (intentional: manual gate #476).** The
  recovery path is designed to need no elevation, but no test verifies
  behaviour when the user runs `restore.bat` from an elevated CMD (e.g.
  whether the password file in `%TEMP%` lands somewhere unexpected,
  whether `cd /d %SystemDrive%\` works under UAC virtualisation). #476
  lists UAC explicitly.
- **No automated test of ARM64 Windows (intentional: not shipped).**
  `restore.bat` explicitly rejects ARM64 with a winget/rustic workaround
  message (no bundled aarch64 Windows binary ships); the rejection text is
  pinned by `tests/unit/test_restore_bat_dispatcher.py::test_restore_bat_rejects_windows_arm64_with_explanation`.
- **A drive-letter sweep across several data discs is now exercised on real
  Windows; a disc *swap* is not.** `windows-e2e.yml` builds its fixture from
  8 x 512 KiB files (the `build_windows_fixture` default of 12 x 64 KiB fits
  one TEST_TINY volume: 2 MiB less the 700 kB holographic reserve), which
  lands on four data discs, mounts all of them at once, and checks
  structurally that more than one holds packs (step *Multi-disc — the restore
  swept more than one data disc*) before the byte-exact comparison.
  `restore.bat` prints nothing about the drives it sweeps, so that structural
  check is the evidence. A *swap* between discs on one drive (the prompt
  and re-scan inside `lcsas-restore.exe`) is not exercised on real Windows;
  it is covered only through the POSIX `recovery/tests/test_multidisc.py`.

## Consolidated source refs

- `recovery/scripts/restore.bat` (full file) — orchestrator,
  RAM-relocation guard, tier cascade, drive sweep, catalog
  auto-selection.
- `recovery/docs/RECOVER_WINDOWS.txt` — user-facing walkthrough,
  SmartScreen / antivirus guidance, command-line reference, older
  Windows options, physical disc problems.
- `recovery/docs/WINDOWS_RECOVERY_PLAN.txt` — design rationale, target
  platforms, source portability plan, cross-compile (zig cc), driver
  script design, testing strategy, Phase W5 msvcrt plan,
  out-of-scope/non-goals.
- `recovery/src/lcsas-restore/posix_compat.h` — POSIX/Win32 shim
  (`mkdir`, `lseek`/`_lseeki64`, `symlink` stub, `chmod` no-op,
  `fsync`/`_commit`, `O_BINARY`).
- `recovery/src/lcsas-restore/main.c:1`-`32` — `--meta-disc` / 
  `--pack-search` / `--catalog` flag surface used by `restore.bat`.
- `recovery/bin/x86_64-windows/lcsas-restore.exe` — prebuilt static
  Windows binary (UCRT, MinGW-w64 via `zig cc`) in the source tree;
  bundled onto the disc at `recovery\bin\x86_64-pc-windows-gnu\`,
  which is the path Tier 1 invokes.
- `recovery/tests/test_e2e_windows.sh` — Wine-based end-to-end test.
