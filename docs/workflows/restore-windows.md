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
plus the prebuilt binary `recovery/bin/x86_64-windows/lcsas-restore.exe`
(see `recovery/docs/RECOVER_WINDOWS.txt` for the user-facing walkthrough
and `recovery/docs/WINDOWS_RECOVERY_PLAN.txt` for the design rationale).

The orchestrator runs a two-tier cascade. The Python fallback that
shipped on the disc is no longer chained from the .bat (the inner
cascade depended on the `py` launcher being installed on the target
Windows host, which is not guaranteed for the headless-recovery
scenario the script targets); users who need it invoke
`standalone_restorer.py` manually (see [Manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)).

| Tier | What runs                                              | Status on Windows |
|------|--------------------------------------------------------|-------------------|
| 1    | `bin\<arch>\lcsas-restore.exe`                         | Primary           |
| 2    | `bin\<arch>\rustic-static.exe` (cross-check)           | Secondary         |

Architecture detection maps `PROCESSOR_ARCHITECTURE` (`AMD64`/`x86`/
`ARM64`) and `PROCESSOR_ARCHITEW6432` to `x86_64-windows` or
`aarch64-windows` at `recovery/scripts/restore.bat:94`-`105`. The binary
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
   `certutil -hashfile recovery\bin\x86_64-windows\lcsas-restore.exe SHA256`,
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

- **OS:** Windows 10 1709+ / Windows 11. ARM64 Windows is supported in
  source but **not yet runtime-tested**
  (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:18`-`22`). Windows 7/8/8.1
  with KB2999226 is best-effort.
- **Optical drive count:** 2+ drives is the cleanest layout — the
  meta-disc stays mounted, data discs sit in other drives, the
  drive-letter sweep at `recovery/scripts/restore.bat:167`-`178` picks
  them all up automatically. For 1 drive see [Workflow B](#workflow-b-single-drive-variant-ram-staging--disc-swap).
- **Recovery tier:** Tier 1 first; falls back to Tier 2 (bundled
  rustic) on non-zero exit. There is no third (Python) tier inside
  the .bat; if both tiers fail the script exits with an error and a
  hint pointing at the manual `standalone_restorer.py` path
  (see [Manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)).

**Test coverage:** Linux-host Wine end-to-end test at
`recovery/tests/test_e2e_windows.sh` exercises Tier 1 only (it invokes
the `.exe` directly under Wine; `restore.bat` is not driven through a
CMD interpreter). Gaps: no automated test of the multi-drive
`--pack-search` sweep on a real Windows host; no automated test that
SmartScreen "Run anyway" works post-quarantine. Real-Windows
verification is a manual checklist
(`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:320`-`331`).

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

**Test coverage:** No automated test exists for the single-drive RAM
relocation flow. `recovery/tests/test_e2e_windows.sh` invokes the
`.exe` directly under Wine and bypasses `restore.bat` entirely; the
multi-disc / disc-swap design has its own pytest at
`recovery/tests/test_multidisc.py` but that does not exercise the
Windows .bat copy/relocate logic. Gaps: no test that the script
correctly survives a meta-disc eject after relocation; no test for
`%TEMP%` exhaustion or for the `robocopy`/`xcopy` fallback.

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

3. Invoke the standalone restorer directly. The script takes the
   repo path and the output directory as positional arguments, and
   prompts for the password on stdin:

   ```
   python standalone_restorer.py D:\repo C:\Users\me\restored
   ```

   Pass `--password-file path\to\pw.txt` if you prefer a password
   file. See `python standalone_restorer.py --help` for the full
   flag surface.

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
Linux-side pytest suite (`src/restore/restic_fallback.py`); there is
no automated Wine/Windows test of the manual invocation path. The
`Phase W5 — legacy msvcrt build` documented at
`recovery/docs/WINDOWS_RECOVERY_PLAN.txt` is the long-term plan to
give XP/Vista a native binary path, but it is not yet implemented.

**Source refs:** `standalone_restorer.py` (root of the meta disc),
`recovery/docs/RECOVER_WINDOWS.txt` (user-facing manual recovery
section), `src/restore/restic_fallback.py` (the underlying
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
  byte-for-byte (`recovery/tests/test_e2e_windows.sh:9`-`94`). The
  script skips cleanly when the .exe or Wine is missing
  (`recovery/tests/test_e2e_windows.sh:14`-`24`).

**Gaps:**

- **No automated test of `restore.bat` itself.** The Wine test
  invokes the .exe directly; the entire .bat cascade (architecture
  detection, repo discovery, RAM relocation, drive sweep, two-tier
  fall-through) is verified only by code review and manual
  real-Windows runs
  (`recovery/docs/WINDOWS_RECOVERY_PLAN.txt:320`-`331`).
- **No automated test of the single-drive RAM relocation
  (Workflow B).** Whether `robocopy`/`xcopy` correctly mirrors the
  bin tree, whether `cd /d %SystemDrive%\` actually frees the disc,
  and whether the relocated process tolerates a meta-disc eject are
  all manual verifications.
- **No automated test for older Windows / msvcrt.** Phase W5 in
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:415`-`548` plans an
  msvcrt-linked binary and a Wine-with-UCRT-disabled test harness;
  neither is implemented today, so Windows 7/8/8.1 without KB2999226
  (and Windows XP/Vista) have no tested binary path. Users on those
  systems are pushed to the [manual Python fallback](#manual-python-fallback-not-orchestrated-by-restorebat)
  or to move the disc to a newer host.
- **No automated test of SmartScreen / antivirus interactions.** The
  documented "Run anyway" / quarantine-restore steps
  (`recovery/docs/RECOVER_WINDOWS.txt:88`-`113`) are user-driven and
  cannot be CI-tested.
- **No automated test of UAC.** The recovery path is designed to need
  no elevation, but no test verifies behaviour when the user runs
  `restore.bat` from an elevated CMD (e.g. whether the password file
  in `%TEMP%` lands somewhere unexpected, whether `cd /d %SystemDrive%\`
  works under UAC virtualisation).
- **No automated test of ARM64 Windows.** The `aarch64-windows` arch
  is plumbed through `restore.bat:98` but
  `recovery/docs/WINDOWS_RECOVERY_PLAN.txt:18`-`22` explicitly notes
  it is not runtime-tested.
- **Multi-drive sweep is not exercised on real Windows.** The
  drive-letter sweep (`recovery/scripts/restore.bat:167`-`178`) is
  not covered by `test_e2e_windows.sh` because that test passes a
  single `--repo` path; multi-disc behaviour on Windows is observed
  only via the cross-platform `recovery/tests/test_multidisc.py`,
  which runs the binary on POSIX paths.

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
  Windows binary (UCRT, MinGW-w64 via `zig cc`); the artifact Tier 1
  invokes.
- `recovery/tests/test_e2e_windows.sh` — Wine-based end-to-end test.
