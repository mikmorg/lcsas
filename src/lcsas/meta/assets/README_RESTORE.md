# LCSAS — Restore from Discs

This is the LCSAS **meta disc**. It holds the tools to restore the files
on the archive's data discs. The only thing you must bring is your
**password** (or your share cards, when the password was split).

The step-by-step instructions are on this disc, in one place:

| Your computer | Read |
|---|---|
| Linux or macOS | `recovery/docs/RESTORE.txt` |
| Windows | `recovery/docs/RESTORE_WINDOWS.txt` (you run `restore.bat`) |

`START_HERE.txt`, at the top of this disc, is the one-page version.
This file only shows the first two commands.

## Single-Drive Mode (the usual case)

You have one optical drive and a stack of discs. On Linux:

```sh
sudo mount /dev/sr0 /mnt
sh /mnt/restore.sh --restore-folder ~/restored --snapshot latest
```

`restore.sh` asks for your newest data disc, the backup set and the
password, and then for each disc it needs. It first copies itself off
the disc, so the meta disc can come out of the drive (set
`LCSAS_NO_RELOCATE=1` to run it where it is). To swap a disc that you
mounted by hand, leave the restore waiting at its question and type
the commands (`sudo umount /mnt`, `sudo eject /dev/sr0`, then
`sudo mount /dev/sr0 /mnt` for the next disc) in a second terminal
window.

RESTORE.txt explains each question, how to stop and carry on later,
share cards, damaged discs, and what to do when the tools will not run.

## What's on This Disc

| Path | What it is |
|---|---|
| `restore.sh` | **Start here** (Linux, macOS). The one restore script. |
| `restore.bat` | The restore script for Windows. |
| `START_HERE.txt` | The one-page start. |
| `recovery/` | The recovery programs for each kind of computer; their manuals are in `recovery/docs/`. |
| `recovery/scripts/restore_auto.sh` | Automation only: restores every backup set with no questions. |
| `python_reader.py` | The Python reader, the last resort. `restore.sh` uses it when it must. |
| `combine_shares.py` | Rebuilds a split password from its share cards. |
| `tools/` | Programs copied from the computer that made this disc. |
| `lcsas/` | The LCSAS source code. |
| `docs/` | Project documentation, including the restic format specification. |
| `VOLUME_INFO.txt` | What this disc is, when it was made, and the LCSAS version. |
| `volume_info.json` | The same, for computer programs. |

`restore_auto.sh` is for scripted tests, not for an heir. It needs the
password in a file:

```sh
LCSAS_PASSWORD_FILE=/full/path/to/password.txt \
    sh /mnt/recovery/scripts/restore_auto.sh /mnt/recovery ~/restored
```

Each backup set goes into its own folder under `~/restored`. Mount your
newest data disc first: the script also restores the backup sets on it.
If it stops (you typed `q`, or the disk is full), run the same command
again to carry on.

## What Is NOT on This Disc

**Your password.** Without it (or enough share cards), the encrypted
data cannot be decrypted. Keep it safe and away from the discs.
