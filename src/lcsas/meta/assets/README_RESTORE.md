# LCSAS — Restore from Discs

This is the LCSAS **meta disc**. It holds the tools to restore the files
on the archive's data discs. The only thing you must bring is your
**password** (or your share cards, when the password was split).

The step-by-step instructions are on this disc, in one place:

| Your computer | Read |
|---|---|
| Linux or macOS | `recovery/docs/RECOVER.txt` |
| Windows | `recovery/docs/RECOVER_WINDOWS.txt` (you run `restore.bat`) |

`START_HERE.txt`, at the top of this disc, is the one-page version.
This file only shows the first two commands.

## Single-Drive Mode (the usual case)

You have one optical drive and a stack of discs. On Linux:

```sh
sudo mount /dev/sr0 /mnt
sh /mnt/restore.sh ~/restored latest
```

`restore.sh` asks for your newest data disc, the backup set and the
password, and then for each disc it needs. It first copies itself off
the disc, so the meta disc can come out of the drive (set
`LCSAS_NO_RELOCATE=1` to run it where it is). With a single terminal,
press Ctrl+Z to pause it, swap the disc (`sudo eject /dev/sr0`), type
`fg` and press Enter.

RECOVER.txt explains each question, how to stop and carry on later,
share cards, damaged discs, and what to do when the tools will not run.

## What's on This Disc

| Path | What it is |
|---|---|
| `restore.sh` | **Start here** (Linux, macOS). The one restore script. |
| `restore.bat` | The restore script for Windows. |
| `START_HERE.txt` | The one-page start. |
| `recovery/` | The recovery programs for each kind of computer; their manuals are in `recovery/docs/`. |
| `recovery/scripts/restore_auto.sh` | Automation only: restores every backup set with no questions. |
| `standalone_restorer.py` | The Python reader, the last resort. `restore.sh` uses it when it must. |
| `keyshare_combine.py` | Rebuilds a split password from its share cards. |
| `tools/` | Programs copied from the computer that made this disc. |
| `lcsas/` | The LCSAS source code. |
| `docs/` | Project documentation, including the restic format specification. |
| `VOLUME_INFO.txt` | What this disc is, when it was made, and the LCSAS version. |
| `volume_info.json` | The same, for computer programs. |

`restore_auto.sh` is for scripted tests, not for an heir. It needs the
password in a file:

```sh
LCSAS_PWFILE=/full/path/to/password.txt \
    sh /mnt/recovery/scripts/restore_auto.sh /mnt/recovery ~/restored
```

Each backup set goes into its own folder under `~/restored`.

## What Is NOT on This Disc

**Your password.** Without it (or enough share cards), the encrypted
data cannot be decrypted. Keep it safe and away from the discs.
