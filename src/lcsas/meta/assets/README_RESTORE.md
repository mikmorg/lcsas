# LCSAS — Restore from Discs

This disc contains everything you need to restore data archived by
**LCSAS** (Linux Cold Storage Archival Suite) from optical discs or ISO
images. The **only** thing you must provide is your **password**.

## What's on This Disc

| Path | Description |
|---|---|
| `tools/` | Portable Linux x86_64 binaries: rustic, xorriso, Python 3 |
| `lcsas/` | LCSAS source code (Python, no external dependencies) |
| `docs/` | Architecture documentation + restic format specification |
| `restore.sh` | **Start here. Asks for the newest data disc, the set, the password.** |
| `README_RESTORE.md` | This file |
| `VOLUME_INFO.txt` | What this disc is, when it was built, and the LCSAS version, in plain text |
| `volume_info.json` | Machine-readable details of this disc (includes tool versions) |
| `recovery/scripts/restore-auto.sh` | Non-interactive restore (automation only) |
| `recovery/scripts/restore_legacy.sh` | Older Bash driver, superseded by `restore.sh` |

## Terminology

A **repository** (a backup set) is one encrypted backup dataset.
Archives may hold several repositories side by side.  Each one's
metadata is in `metadata/<id>/` on every disc; its
`LCSAS_REPO.txt` names it.  Pass its name or its id to `--repo`.

## Single-Drive Mode (DEFAULT)

This is the disaster scenario: you own one optical drive and a stack of
archive discs. You do **not** need to rip every disc up front — the
script walks you through the restore one disc at a time.

### 1. Mount the meta disc and run the restore

```sh
sudo mount /dev/sr0 /mnt
sh /mnt/restore.sh ~/restored/ latest
```

`restore.sh` detects that it is running off a read-only optical disc
and copies itself, this computer's recovery binaries and the backup
sets' metadata into its tools folder, `~/lcsas-restore-tools`, and
runs from there, so the meta disc can come out of the drive — no
manual `cp -r` step is needed.  The tools folder stays until the
restore has finished (see "Stopping and carrying on" below).

> **Advanced override:** set `LCSAS_NO_RELOCATE=1` in the environment
> if you want to keep running directly off the disc (e.g. for tests,
> or when running from a writable copy you placed yourself).

The two positional arguments are:

| Position | Meaning | Default |
|---|---|---|
| 1 | `TARGET_DIR` — where restored files are written | `/tmp/restored` |
| 2 | `SNAPSHOT_ID` or the word `latest` | `latest` |

The script auto-discovers the repository metadata from the meta
disc itself (`metadata/<repo>/`), so you do **not** need to
insert a data disc before starting — the script will prompt you
for each one as the recovery binary needs it.  The script will:

1. Ask you to take the meta disc out and put in your **newest**
   data disc: the latest year, then the highest number, in its
   label (e.g. `LCSAS_CD700_2026_0002`).  Its copy of the backup
   sets is the newest, so `latest` really is the latest.  Press
   Enter once it is in, or type `S` and Enter to skip and use the
   copies on the meta disc.
2. List the backup sets (repositories) on this archive by name.
   If more than one is present, it prints `Choose a repository`
   and waits for you to **type its number or name** and press
   Enter.  Archives with a single repository skip this prompt.
   You can also set `LCSAS_REPO=<name>` in the environment to
   preselect.
3. Print `Password:` and wait for the password.
   **Type your password** and press Enter; it is not shown as you
   type.  An empty or wrong password is asked for again, and `q`
   quits.
   When the set's password is split into share cards, it asks for
   the words of each card instead (`P` lets you type the password).
4. Run the highest-priority recovery tier available
   (`bin/<arch>/lcsas-restore` → `bin/<arch>/rustic-static` →
   bundled CPython + `standalone_restorer.py`).  Only one tier
   runs per invocation.
5. When a pack is missing from the disc currently in the drive,
   the recovery binary stops and prints a box on stderr:
   `Pack <id>... is required for the next file.` and, below it,
   `It is on any disc labelled:` with the disc's label.
   **Swap the disc in your optical drive and press Enter.**  The
   first time it sees a data disc it copies all of it to the tools
   folder, so each disc goes in about once.  Repeat until the
   binary prints `[lcsas-restore] restore complete`.
6. Ask whether to restore another backup set from the same discs;
   the meta disc is not needed again for it.

> **Working from a single terminal.** You do not need tmux or a second SSH
> session to handle disc swaps — a normal single-terminal flow works.
> Eject the disc with the drive's button or your file manager.  Or
> press Ctrl+Z to suspend the restore, run `sudo eject /dev/sr0`
> (on a Mac: `drutil eject`), put the next disc in, type `fg` to
> resume, and press Enter at the prompt.  On Linux and macOS the
> binary looks again every 2 seconds while it waits, so a disc that
> mounts by itself is found without Enter.

### Stopping and carrying on

To stop, type `q` and Enter at a disc prompt (exit code 75), or
press Ctrl-C.  The files restored so far stay in the target folder,
and the tools folder stays, with the discs already copied.  To carry
on later, with no meta disc, run the command the script prints:

```sh
sh ~/lcsas-restore-tools/resume.sh
```

It asks for the password (or the share cards) again (it never
stores the password) and does not ask again for the discs it already
copied.  Re-running the first command from the meta disc works too.
Once a restore succeeds and you decline another backup set, the tools
folder and its copies of the discs are deleted: a later restore needs
the discs again.

> **Tip:** with a second drive, a data disc that is already mounted
> (say under `/media`) when you start counts as your newest data
> disc, and the script does not ask for one.  Best is the disc whose
> label is newest (the latest year, then the highest number): its
> list of snapshots is the newest and its catalog the fullest.

> **Automation / CI only.** Heirs restoring after a disaster
> must use the interactive `restore.sh` above. The non-interactive
> driver `recovery/scripts/restore-auto.sh` exists only for scripted
> pipelines — see Appendix A.

## Directory Mode (opt-in, legacy)

If you already have the ISO image of every data disc on disk (e.g.
pre-rsynced to a NAS) you can side-load them via the legacy bash driver
(`recovery/scripts/restore_legacy.sh`):

```bash
bash /mnt/recovery/scripts/restore_legacy.sh --key /path/to/password.txt \
     --isos /path/to/iso/directory/ \
     --target ~/restored/
```

This extracts every ISO up front and copies every pack into the cache
before restoring. Faster when disks are cheap; wrong for the
single-drive disaster scenario.

## If the Bundled Tools Don't Work

The bundled tools are Linux x86_64 binaries.  If they don't run on your
system (wrong architecture, incompatible libraries), you have options:

1. **Try rustic-static** — a statically-linked binary may be included
   at `tools/bin/rustic-static` (no shared library dependencies).

2. **Install rustic yourself** — https://rustic.cli.rs/ (or the
   compatible `restic` at https://restic.net/).

3. **Use the LCSAS Python CLI** (advanced):
```bash
export LD_LIBRARY_PATH="$(pwd)/tools/lib:${LD_LIBRARY_PATH:-}"
export PYTHONHOME="$(pwd)/tools"
export PYTHONPATH="$(pwd)/lcsas/src"
./tools/bin/python3 -m lcsas --help
```

4. **Read the format specification** — `docs/RESTIC_FORMAT_SPEC.md`
   documents the restic repository format in detail.  A programmer can
   use this to write a decoder in any language.

5. **Run in a virtual machine** — x86_64 Linux can be emulated on any
   platform using QEMU, VirtualBox, or similar.  Install a basic Linux
   distribution (e.g. Ubuntu) in the VM and use these tools there.

## Notes

- **Python reader fallback (tier 3):** If no rustic/restic binary works on your system,
  `restore.sh` will automatically fall back to `standalone_restorer.py` which
  requires only Python 3.10+ (no compiled extensions).  This is slower (~1 MB/s)
  but functional.  For zstd-compressed repositories (rustic v2 default), the
  `zstandard` Python package is bundled in `tools/lib/python/`.  The fallback
  requires ~2 GB of RAM for large repositories.

- **Re-running after failure:** If a restore is interrupted (power loss, Ctrl+C,
  disk full), run `sh ~/lcsas-restore-tools/resume.sh`, or re-run the
  restore command.  The pack cache in the tools folder stays until a
  restore succeeds, so a re-run does not ask again for the discs it
  already copied, and files that are already complete are skipped.
  On a live USB stick the home folder is in RAM: the tools folder is
  lost when the computer is turned off.

## What Is NOT on This Disc

**Your password** — you must provide this yourself.
Without the password, the encrypted backup data cannot be decrypted.

> **Important:** Store your password securely and *separately* from
> your backup discs. Consider writing it on a password card kept in a
> fireproof safe, or splitting it into shares kept in different places.

## About LCSAS

Linux Cold Storage Archival Suite orchestrates Rustic (restic-compatible) backup
repositories onto optical media (Blu-ray, M-DISC) for long-term archival
storage. Every data disc is self-describing, carrying full repository
metadata so that any disc can bootstrap a restore independently.

See `docs/architecture.md` for the complete system architecture, and
`docs/RESTIC_FORMAT_SPEC.md` for the data format specification.

---

## Appendix A — restore-auto.sh (automation only)

> Skip this section unless you are scripting LCSAS into a CI
> pipeline, a backup-validation harness, or another fully
> automated environment. Heirs restoring after a disaster
> should use `restore.sh` (Single-Drive Mode above).

`recovery/scripts/restore-auto.sh` runs the same four phases as
`restore.sh` (bootstrap, ingest, finalize, rustic restore) but never
calls `read` and never waits for keyboard input.  It is the right tool
for an environment where another process can mechanically swap
discs and feed in the password file.  (Do not confuse it with
`recovery/scripts/restore_auto.sh`, a separate POSIX-sh wrapper that
restores every repository from a recovery tree.)

```bash
bash /mnt/recovery/scripts/restore-auto.sh --key /path/to/password.txt \
     --target ~/restored/ \
     --repo REPO_NAME \
     --disc-cmd "disc-loader"
```

| Option | Description |
|---|---|
| `--key FILE` | **(required)** Password file |
| `--target DIR` | **(required)** Restore destination |
| `--repo NAME` | **(required)** Repository to restore |
| `--disc-cmd CMD` | Robotic loader command (`CMD insert LABEL` / `CMD eject`). |
| `--drive DEV` | Optical drive (default: `/dev/sr0`) |
| `--snapshot ID` | Snapshot to restore (default: latest) |
| `--work-dir DIR` | Temp directory (default: auto) |
