"""Meta disc: the disc with the recovery tools, instructions and source.

A meta disc is burned alongside the data discs for each location,
providing everything needed to restore data without ANY system-installed
software — the only missing piece is the password.

Contents of a meta disc:
    tools/          Portable binaries (rustic, xorriso, python3) + shared libs
    lcsas/          LCSAS source code
    docs/           Architecture and project documentation
    restore.sh      Bootstrap restore script (pure bash + bundled tools)
    recovery/       Recovery tools; recovery/scripts/ also holds
                    restore-auto.sh (automation) and restore_legacy.sh
    README_RESTORE.md   Human-readable restore instructions
    VOLUME_INFO.txt     What this disc is, in plain text
    volume_info.json    Self-describing disc metadata
"""
