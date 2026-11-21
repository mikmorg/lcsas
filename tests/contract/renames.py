"""The interface names retired before v1.0.0, and what replaced them (#934).

ADR 0005 (docs/adr/0005-interface-names-follow-the-glossary.md): every
name a person types or reads follows CONTEXT.md.  The old names were
renamed outright, with no aliases.  An old flag or environment variable
fails with a one-line "renamed: use NEW" message and exit 2 (usage).

This table is the contract for the #934 work and the source of the
retired-name check of #933: a retired name must not appear in
heir-facing text except where a "renamed" message names it.

Code identifiers and the catalog schema keep their names (the C source
folder ``recovery/src/lcsas-keyshare/``, the Python package
``lcsas.keyshare``, the ``key_escrow`` table).  Program-facing names
that really are a repository keep it: tier 1's and the Python reader's
``--repo DIR`` and ``--repo-id``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rename:
    #: The retired name, exactly as it was typed or read.
    old: str
    #: Its replacement; "" when it was dropped with no replacement.
    new: str
    #: What kind of name: flag, env, file, program, command, config,
    #: label, field, folder.
    kind: str
    #: Where it is used.
    where: str
    note: str = ""


#: Every retired name.  Order: heir interfaces, then the owner's.
RENAMES: tuple[Rename, ...] = (
    # ── Heir: the restore scripts and the recovery tiers ──────────────
    Rename("--key", "--password-file", "flag", "restore.sh"),
    Rename("--target", "--restore-folder", "flag",
           "restore.sh, restore.bat (new there), lcsas-restore, python_reader.py"),
    Rename("TARGET_DIR", "--restore-folder", "flag",
           "restore.sh positional (removed: name the folder with the flag)"),
    Rename("SNAPSHOT_ID", "--snapshot", "flag",
           "restore.sh positional (removed: name the snapshot with the flag)",
           "RECOVERY_ROOT stays the one positional argument"),
    Rename("--repo", "--backup-set", "flag", "restore.sh, restore.bat",
           "only the scripts' set selector; tier 1's and tier 3's --repo DIR stay"),
    Rename("LCSAS_REPO", "LCSAS_BACKUP_SET", "env", "restore.sh, restore.bat"),
    Rename("LCSAS_PWFILE", "LCSAS_PASSWORD_FILE", "env", "restore.sh, restore.bat"),
    Rename("LCSAS_PW", "LCSAS_PASSWORD", "env", "restore.bat",
           "dropped; restore.bat's own prompt variable is renamed too"),
    Rename("LCSAS_TARGET", "LCSAS_PLATFORM", "env", "restore.sh, restore.bat",
           "the platform name that picks the binaries"),
    Rename("LCSAS_ALLOW_TMPFS_TARGET", "LCSAS_ALLOW_TMPFS_RESTORE_FOLDER", "env",
           "restore.sh"),
    Rename("LCSAS_FORBID_TMPFS_TARGET", "LCSAS_FORBID_TMPFS_RESTORE_FOLDER", "env",
           "restore.sh"),
    Rename("LCSAS_FORCE_NONEMPTY_TARGET", "LCSAS_FORCE_NONEMPTY_RESTORE_FOLDER", "env",
           "restore.sh, restore.bat"),
    Rename("LCSAS_KEYSHARE_PASSPHRASE", "LCSAS_SHARE_PASSPHRASE", "env",
           "lcsas-shares"),
    Rename("RECOVER.txt", "RESTORE.txt", "file", "the meta disc"),
    Rename("RECOVER_WINDOWS.txt", "RESTORE_WINDOWS.txt", "file", "the meta disc"),
    Rename("lcsas-keyshare", "lcsas-shares", "program",
           "recovery/bin/<platform>/ (and .exe)",
           "the built program's name only; its source folder keeps its name"),
    Rename("keyshare_combine.py", "combine_shares.py", "program", "the meta disc"),
    Rename("standalone_restorer.py", "python_reader.py", "program",
           "the meta disc, the tools folder"),
    Rename("LCSAS_REPO.txt", "LCSAS_BACKUP_SET.txt", "file",
           "metadata/<id>/ on every disc"),
    Rename("Repositories:", "Backup sets:", "label", "VOLUME_INFO.txt"),
    Rename("Repository :", "Backup set :", "label", "share and password cards"),
    Rename("Repositories :", "Backup sets :", "label", "estate card"),
    Rename("key_file", "password", "field",
           "the meta disc's volume_info.json requires.key_file"),
    Rename("target=", "restore_folder=", "field",
           "restore.bat resume.cfg, restore.sh ~/.lcsas-restore-log"),
    Rename("set=", "backup_set=", "field", "restore.bat resume.cfg"),
    Rename("tenant=", "backup_set=", "field", "restore.sh ~/.lcsas-restore-log"),
    Rename("lcsas-restore-tools", "lcsas-tools", "folder",
           "restore.sh's default tools folder (~/lcsas-tools)"),
    Rename("LCSAS-restore", "lcsas-tools", "folder",
           "restore.bat's default tools folder (%LOCALAPPDATA%\\lcsas-tools)"),
    # ── Owner: the lcsas command and its config ───────────────────────
    Rename("lcsas key", "lcsas password", "command",
           "split, unsplit, combine, verify, card"),
    Rename("key_split", "split", "config", "lcsas config"),
    Rename("key_threshold", "split_threshold", "config", "lcsas config"),
    Rename("key_shares", "split_shares", "config", "lcsas config"),
    Rename("key_storage_hints", "password_hints", "config", "lcsas config"),
    Rename("[paths].database", "[paths].catalog", "config", "lcsas config"),
    Rename("--allow-escrow-drift", "--allow-split-drift", "flag", "lcsas stage"),
    Rename("--db", "--catalog", "flag", "lcsas (top-level option)"),
    Rename("--db-path", "--catalog-path", "flag", "lcsas init"),
    Rename("archive.db", "catalog.db", "file", "the master catalog's default name"),
    Rename("target_path", "restore_folder", "flag",
           "lcsas restore exec / from-disc (positional)"),
    Rename("lcsas restore standalone", "lcsas restore from-disc", "command",
           "lcsas restore"),
    Rename("--target-media", "--media-type", "flag", "lcsas consolidate"),
    Rename("keyshares-", "shares-", "folder",
           "lcsas password split --out default (shares-<repository>)"),
)


def renamed(old: str) -> Rename:
    """The entry for ``old`` (KeyError if it was not retired)."""
    for r in RENAMES:
        if r.old == old:
            return r
    raise KeyError(old)
