"""Meta-volume: self-contained rescue volume with tools and source.

A meta-volume is burned alongside data volumes at each storage location,
providing everything needed to restore data without ANY system-installed
software — the only missing piece is the encryption key file.

Contents of a meta-volume:
    tools/          Portable binaries (restic, xorriso, python3) + shared libs
    lcsas/          LCSAS source code
    docs/           Architecture and project documentation
    restore.sh      Bootstrap restore script (pure bash + bundled tools)
    README_RESTORE.md   Human-readable restore instructions
    volume_info.json    Self-describing volume metadata
"""
