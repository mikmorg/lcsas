"""Meta disc: the disc with the recovery tools, instructions and source.

A meta disc is burned alongside the data discs for each location,
providing everything needed to restore data without ANY system-installed
software — the only missing piece is the password.

Contents of a meta disc:
    tools/          Portable binaries (rustic, xorriso, python3) + shared libs
    lcsas/          LCSAS source code
    docs/           Architecture and project documentation
    restore.sh      The one restore driver (POSIX sh, from recovery/scripts/)
    recovery/       Recovery tools: tier-1 binaries, restore_auto.sh
                    (automation), docs (RECOVER.txt, RECOVER_WINDOWS.txt)
    README_RESTORE.md   Short pointer to the restore manuals
    VOLUME_INFO.txt     What this disc is, in plain text
    volume_info.json    Self-describing disc metadata
"""
