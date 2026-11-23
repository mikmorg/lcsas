"""``lcsas password``: split, unsplit, combine, verify, card.

The card text itself is in :mod:`lcsas.cli.main`.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lcsas.cli.common import (
    _config_mirror,
    _confirm,
    _load_config_opt,
    _open_catalog,
    _resolve_db_path,
)
from lcsas.log import get_logger, log_to_stderr

if TYPE_CHECKING:
    from lcsas.config.settings import LCSASConfig

logger = get_logger()


def _write_private_file(path: Path, data: bytes) -> None:
    """Write *data* to *path* with owner-only (0600) permissions, TOCTOU-safe.

    The file is created with ``O_CREAT | O_EXCL`` and mode 0600 so it is never
    even briefly readable by other accounts (mirrors ``db/connection.py``).  An
    existing file is a hard error rather than a silent overwrite — share files
    are sensitive and clobbering them could destroy the only copy.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _read_password_file(path: Path) -> bytes:
    """The password in *path*: its first line, as rustic reads it (#641).

    The same rule as tiers 1 and 3 (``read_password_file`` in the tier-3
    reader), so a card or split never carries a password the readers
    disagree on.  Lines after the first are not part of the password;
    ``validate_config`` warns about them.
    """
    from lcsas.restore.restic_fallback import read_password_file

    return read_password_file(path)


def cmd_key_card(args: argparse.Namespace) -> int:
    """Print (or check) a paper password card for an archive whose password is not split."""
    # The card text is frozen in main.py; imported here, not at the top,
    # because main.py imports this module.
    from lcsas.cli.main import _check_code, _recovery_card_text

    # ── Check mode: recompute the check code from a typed password file ───
    if args.check is not None:
        if args.code is None:
            logger.error("--check requires --code (the code printed on the card).")
            return 1
        check_file: Path = args.check
        if not check_file.exists():
            logger.error("Password file does not exist: %s", check_file)
            return 1
        # The one password-file rule (#641): the first line, as rustic reads it.
        password = _read_password_file(check_file)
        computed = _check_code(password)
        expected = args.code.strip().lower()
        if computed == expected:
            print(f"MATCH: {computed}")
            return 0
        print(f"MISMATCH: typed file -> {computed}, card -> {expected}")
        return 1

    if args.code is not None:
        logger.error("--code is only valid with --check (check mode).")
        return 1

    # ── Render mode ──────────────────────────────────────────────────────
    from datetime import date


    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required to render a password card.")
        return 1

    repo_name = args.repo
    if repo_name is None:
        if len(config.repositories) != 1:
            logger.error(
                "--repo is required (config defines %d repositories).",
                len(config.repositories),
            )
            return 1
        repo_name = next(iter(config.repositories))

    repo_cfg = config.repositories.get(repo_name)
    if repo_cfg is None:
        logger.error(
            "Repository '%s' is not defined in the config file.", repo_name
        )
        return 1
    if repo_cfg.password_file is None:
        logger.error(
            "Repository '%s' has no password_file configured.", repo_name
        )
        return 1

    check_code: str | None = None
    if not args.no_check_code:
        pw_file = repo_cfg.password_file
        if not pw_file.exists():
            logger.error(
                "Password file does not exist: %s (use --no-check-code to "
                "render a card without a transcription check).", pw_file,
            )
            return 1
        password = _read_password_file(pw_file)
        check_code = _check_code(password)

    card = _recovery_card_text(
        repo=repo_name,
        key_file_name=repo_cfg.password_file.name,
        storage_hints=config.key_storage_hints,
        card_date=date.today().isoformat(),
        label_prefix=config.label_prefix,
        check_code=check_code,
    )

    if args.out is not None:
        _write_private_file(args.out, card.encode("utf-8"))
        print(f"Wrote password card for repo '{repo_name}' to {args.out}")
        print(
            "  Mode 0600. The password is NOT on the card — write it in by "
            "hand and store the card separate from the discs.",
            file=sys.stderr,
        )
    else:
        print(card, end="")
    return 0


def _verify_password_unlocks_repo(
    keys_dir: Path, password: bytes, repo: str, *, for_split: bool = True,
) -> str | None:
    """Authenticate *password* against a repo's ``keys/`` directory.

    Returns ``None`` on success; otherwise a human-readable error string
    naming the repo and the keys dir.  Fails closed: a missing or empty
    ``keys/`` is an error (a split is exactly where "probably fine" is
    wrong).  For ``password split`` (*for_split*) the error names the
    ``--no-verify-repo`` override; ``password verify`` has none, and says
    instead that a password which does not open the repository is most
    often the one from before a password change (#956).
    """
    from lcsas.restore.restic_fallback import (
        IntegrityError,
        NoKeyUnlockedError,
        try_keys,
    )

    def skip(dangerous: str) -> str:
        return f" Pass --no-verify-repo to skip ({dangerous})." if for_split else ""

    if not keys_dir.is_dir():
        return (
            f"keys directory not found for repository '{repo}': {keys_dir}. "
            "Cannot verify that the password unlocks the repo. Make the "
            "mirror's keys/ reachable."
            + skip("splitting an unchecked password is dangerous")
        )
    if not any(p.is_file() for p in keys_dir.iterdir()):
        return (
            f"no key files under {keys_dir} for repository '{repo}'. "
            "Cannot verify that the password unlocks the repo."
            + skip("dangerous")
        )
    try:
        try_keys(keys_dir, password)
    except NoKeyUnlockedError as e:
        damaged = "; ".join(why for _, why in e.corrupt)
        if not e.rejected:
            # #481: no key file could even be read, so this says nothing
            # about the password.  Calling it "wrong" would send the owner
            # hunting for a different password instead of the damage.
            return (
                f"cannot check the password for repository '{repo}': "
                f"every key file under {keys_dir} is unreadable or corrupt "
                f"({damaged}). Repair the mirror's keys/."
                + skip("dangerous")
            )
        msg = (
            f"the password in this file does NOT unlock repository "
            f"'{repo}' ({len(e.rejected)} key file(s) checked under "
            f"{keys_dir}). "
        )
        if e.corrupt:
            msg += (
                f"{len(e.corrupt)} more could not be read, so were not "
                f"checked: {damaged}. "
            )
        if for_split:
            return msg + (
                "You were about to split a wrong password. If you really "
                "intend this, pass --no-verify-repo."
            )
        return msg + (
            f"If the password of '{repo}' was changed (rustic key add and "
            f"key remove), this is the old one, and share cards made before "
            f"the change rebuild it too: split the new password (lcsas "
            f"password split --repo {repo}) and hand out new cards."
        )
    except IntegrityError as e:
        return f"cannot check the password for repository '{repo}': {e}"
    return None


def _unlocking_key_file(keys_dir: Path, password: bytes) -> str | None:
    """The name of the first key file in *keys_dir* that *password* opens.

    Tried in the order :func:`try_keys` tries them; None when none opens
    (or *keys_dir* cannot be read).  ``password split`` records it (#956).
    Each key file is tried alone through the reader's public ``try_keys``,
    from a private folder holding a copy of it: the restore path stays as
    it is.
    """
    import shutil
    import tempfile

    from lcsas.restore.restic_fallback import IntegrityError, try_keys

    try:
        key_files = sorted(p for p in keys_dir.iterdir() if p.is_file())
    except OSError:
        return None
    with tempfile.TemporaryDirectory(prefix="lcsas-key-") as tmp:
        one = Path(tmp)
        for kf in key_files:
            for old in one.iterdir():
                old.unlink()
            try:
                shutil.copyfile(kf, one / kf.name)
                try_keys(one, password)
            except (IntegrityError, OSError):
                continue
            return kf.name
    return None


def _recombine_roundtrip_error(
    mnemonics: list[str], expected: bytes,
) -> str | None:
    """Recombine K-subsets of *mnemonics* and confirm they reproduce *expected*.

    Returns ``None`` on success, else an error naming the failing subset.
    Bounds the C(N,K) blow-up at 64 subsets.  *mnemonics[0]*'s threshold is
    read from the share metadata so this works without the caller passing K.
    """
    from itertools import combinations

    from lcsas.keyshare import KeyShareError, decode_master_secret, recover_secret
    from lcsas.keyshare.slip39 import Share

    # Read K from share metadata.  A corrupted/short mnemonic here is itself a
    # verification failure (its checksum won't parse).
    threshold: int | None = None
    for i, mn in enumerate(mnemonics):
        try:
            threshold = Share.from_mnemonic(mn).member_threshold
            break
        except KeyShareError as e:
            return f"share {i + 1} is not a valid SLIP-0039 share: {e}"
    assert threshold is not None  # mnemonics is non-empty; loop above returns
    subsets = list(combinations(range(len(mnemonics)), threshold))
    if len(subsets) > 64:
        subsets = subsets[:64]
    for idx in subsets:
        subset = [mnemonics[i] for i in idx]
        try:
            got = decode_master_secret(recover_secret(subset))
        except KeyShareError as e:
            return (
                f"shares {[i + 1 for i in idx]} failed to reconstruct: {e}"
            )
        if got != expected:
            return (
                f"shares {[i + 1 for i in idx]} do not reconstruct the input "
                "password"
            )
    return None


def _estimate_password_bits(password: bytes) -> float:
    """Rough Shannon-style entropy estimate for an advisory check.

    Deliberately conservative and character-class based — this is a
    "is this obviously too weak?" gate, not a real strength meter.  It
    never blocks; it only decides whether to print the FUP-03 warning.
    """
    try:
        text = password.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode with replace never raises
        text = ""
    pool = 0
    if any(c.islower() for c in text):
        pool += 26
    if any(c.isupper() for c in text):
        pool += 26
    if any(c.isdigit() for c in text):
        pool += 10
    if any(not c.isalnum() for c in text):
        pool += 32
    if pool == 0:
        return 0.0
    import math

    return len(text) * math.log2(pool)


def _warn_if_weak_password(password: bytes) -> None:
    """Print a non-blocking weak-password advisory (FUP-03).

    Burned discs carry the password-locked key files forever, so the
    password faces unlimited offline scrypt guessing.  Warn when it is
    short (<16 chars) or low-entropy (<60 bits estimated).
    """
    chars = len(password.decode("utf-8", errors="replace"))
    bits = _estimate_password_bits(password)
    if chars < 16 or bits < 60.0:
        logger.warning(
            "Weak password: ~%d chars / ~%.0f bits estimated. Every burned "
            "disc carries the password-locked key files, so a disc thief can "
            "brute-force this password offline forever (scrypt N=2^15). Use a "
            "generated diceware passphrase of 7+ words. See "
            "docs/DISC_CONFIDENTIALITY.md.",
            chars, bits,
        )


def _check_split_repo_registered(
    args: argparse.Namespace, config: LCSASConfig
) -> None:
    """Refuse a split whose repository is not in the catalog (#571).

    ``password split`` records the split under the catalog repo_id that
    ``repo add`` assigned, because that is the ID the burn pipeline looks
    it up by.  Raises ValueError (the message to print) when there is no
    catalog or the repository is not registered in it, so the caller can
    stop before any share exists.  A catalog that exists but cannot be
    read only logs a warning: the split goes ahead without being recorded.
    """
    from lcsas.db.repos import resolve_repo
    from lcsas.exceptions import CatalogError, CatalogUnreadableError

    name = args.repo
    repo_cfg = config.repositories.get(name)
    mirror = str(repo_cfg.mirror_path) if repo_cfg is not None else "<mirror_path>"
    db_path = _resolve_db_path(args, config)
    try:
        with _open_catalog(args, mode="read", db_path=db_path) as conn:
            repo = resolve_repo(conn, name, _config_mirror(config, name))
    except CatalogUnreadableError as e:  # a damaged file: there, not missing
        logger.warning(
            "Could not read the catalog at %s (%s). The split will go ahead, "
            "but it may not be recorded.", db_path, e,
        )
        return
    except CatalogError:  # no file, or never initialised
        # An unmounted drive looks the same as a new archive here, so the
        # mount check comes first: 'init' on an empty mount point would
        # make a second catalog.
        raise ValueError(
            f"No catalog at {db_path}, so the split would not be recorded. "
            f"If the catalog is on a drive that is not mounted, mount it and "
            f"split again. If this is a new archive, run 'lcsas --config ... "
            f"init', then 'lcsas --config ... repo add {name} {mirror}', then "
            f"split again. No shares were written."
        ) from None
    except ValueError as e:  # two repositories share the name
        raise ValueError(
            f"{e} The split cannot tell which one to record. No shares were "
            f"written."
        ) from None
    # RuntimeError: get_connection's integrity check failed (a damaged file).
    except (sqlite3.Error, OSError, RuntimeError) as e:
        logger.warning(
            "Could not read the catalog at %s (%s). The split will go ahead, "
            "but it may not be recorded.", db_path, e,
        )
        return
    if repo is None:
        raise ValueError(
            f"Repository '{name}' is not registered in the catalog at "
            f"{db_path}. Register it first with 'lcsas --config ... repo add "
            f"{name} {mirror}', then split again. No shares were written."
        )


def _record_split_by_name(
    conn: sqlite3.Connection,
    name: str,
    threshold: int,
    shares: int,
    split_id: int,
    mirror_path: Path | None = None,
    key_file: str | None = None,
    *,
    split_at: str | None = None,
) -> str:
    """Record a split under the catalog repo_id of the repository *name*.

    *name* is resolved like every other ``--repo`` (#687), with
    *mirror_path* (its configured mirror) first.  Also drops a record that
    an earlier build left under the name (#571), so one row stands for one
    repository.  *key_file*, the key file the split password opens, goes
    into ``repositories.encryption_key_id``: ``stage`` refuses once it is
    gone from the mirror (#956).  Returns the repository's repo_id.
    """
    from lcsas.db.key_escrow import clear_split, record_split
    from lcsas.db.repos import resolve_repo, set_encryption_key_id

    repo = resolve_repo(conn, name, mirror_path)
    if repo is None:
        raise ValueError(f"repository '{name}' is not registered in the catalog")
    record_split(conn, repo.repo_id, threshold, shares, split_id,
                 split_at=split_at, commit=False)
    if key_file is not None:
        set_encryption_key_id(conn, repo.repo_id, key_file, commit=False)
    name_is_an_id = conn.execute(
        "SELECT 1 FROM repositories WHERE repo_id = ?", (name,)
    ).fetchone()
    if name_is_an_id is None:
        clear_split(conn, name, commit=False)
    conn.commit()
    return repo.repo_id


def _existing_share_files(out_dir: Path, repo: str) -> list[Path]:
    """Files in *out_dir* named like ``password split``'s output for *repo* (#683)."""
    import glob

    if not out_dir.is_dir():
        return []
    return sorted(out_dir.glob(f"{glob.escape(repo)}-share-*"))


_CARD_SHARE_RE = re.compile(r"^\s*Share\s*:\s*(\d+)\s+of\s+(\d+)\s*$", re.M)
_CARD_SPLIT_ID_RE = re.compile(r"^\s*Split ID\s*:\s*(\d+)\s*$", re.M)
_CARD_BACKUP_SET_RE = re.compile(r"^\s*Backup set\s*:\s*(\S.*?)\s*$", re.M)
_CARD_SPLIT_ON_RE =re.compile(r"^\s*Split on\s*:\s*(\d{4}-\d\d-\d\d)\s*$", re.M)


def _record_existing_split(args: argparse.Namespace) -> int:
    """``password split --record-existing``: record a split already made,
    read from its share cards, and make no new shares (#963, L8).

    A catalog rebuilt from discs has no record of a split made after the
    newest disc was staged, and ``stage`` then refuses.  Splitting again
    would void the cards already handed out.  This reads K and the split
    ID from the share words of each card (N from the card's "Share : i of
    N" line, else lcsas.toml), checks them against each other and against
    the config, and writes the record.  With K cards it also rebuilds the
    password and checks that it unlocks the repository.
    """
    from lcsas.db.key_escrow import get_split_for_repo
    from lcsas.db.repos import resolve_repo
    from lcsas.keyshare import (
        KeyShareError,
        check_share,
        decode_master_secret,
        extract_mnemonic,
        recover_secret,
        share_identifier,
    )
    from lcsas.keyshare.slip39 import Share

    name = args.repo
    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for password split --record-existing.")
        return 1
    if not args.share_files:
        logger.error(
            "password split --record-existing reads the split from its share "
            "cards: pass at least one --share-file."
        )
        return 1
    if args.threshold is not None or args.shares is not None:
        logger.error(
            "password split --record-existing reads K and N from the cards: "
            "leave out --threshold/--shares."
        )
        return 1
    if name not in config.repositories:
        logger.error("Repository '%s' is not defined in the config file.", name)
        return 1
    split, cfg_k, cfg_n = config.repo_split(name)
    if not split:
        logger.error(
            "lcsas.toml says the password of repository '%s' is not split. "
            "Set split = true (with split_threshold and split_shares) under "
            "[repos.%s] first, so the config and the recorded split agree. "
            "Nothing was recorded.", name, name,
        )
        return 1

    # ── Read every card ───────────────────────────────────────────────
    mnemonics: list[str] = []
    seen: set[tuple[int, int]] = set()
    card_n: set[int] = set()
    split_on: set[str] = set()
    for card in args.share_files:
        if not card.is_file():
            logger.error("Share file does not exist: %s", card)
            return 1
        text = card.read_text(encoding="utf-8")
        try:
            mnemonic = extract_mnemonic(text, source=str(card))
        except KeyShareError as e:
            logger.error("%s", e)
            return 1
        problem = check_share(mnemonic)
        if problem is not None:
            logger.error("%s: %s", card, problem)
            return 1
        k = Share.from_mnemonic(mnemonic).member_threshold
        split_id = share_identifier(mnemonic)
        printed_id = _CARD_SPLIT_ID_RE.search(text)
        if printed_id is not None and int(printed_id.group(1)) != split_id:
            logger.error(
                "%s prints Split ID %s, but its share words belong to split "
                "%05d. The card is not one card of one split. Nothing was "
                "recorded.", card, printed_id.group(1), split_id,
            )
            return 1
        backup_set = _CARD_BACKUP_SET_RE.search(text)
        if backup_set is not None and backup_set.group(1).strip() != name:
            logger.error(
                "%s is a card of backup set '%s', not '%s'. Nothing was "
                "recorded.", card, backup_set.group(1).strip(), name,
            )
            return 1
        share_line = _CARD_SHARE_RE.search(text)
        if share_line is not None:
            card_n.add(int(share_line.group(2)))
        date_line = _CARD_SPLIT_ON_RE.search(text)
        if date_line is not None:
            split_on.add(date_line.group(1))
        seen.add((k, split_id))
        mnemonics.append(mnemonic)
    if len(seen) != 1 or len(card_n) > 1 or len(split_on) > 1:
        logger.error(
            "These cards are of more than one split (split IDs %s). Pass "
            "cards of the split that was handed out only. Nothing was "
            "recorded.", ", ".join(f"{i:05d}" for _, i in sorted(seen)),
        )
        return 1
    ((k, split_id),) = seen
    if card_n:
        n = next(iter(card_n))
    else:
        n = cfg_n
        logger.info(
            "The card does not print how many shares were made; taking N = "
            "%d from lcsas.toml.", cfg_n,
        )

    # ── The cards against the config ──────────────────────────────────
    if (k, n) != (cfg_k, cfg_n):
        logger.error(
            "The cards are of a %d-of-%d split, but lcsas.toml says %d-of-%d "
            "for repository '%s'. Fix split_threshold/split_shares under "
            "[repos.%s] (or [defaults]) to match the cards. Nothing was "
            "recorded.", k, n, cfg_k, cfg_n, name, name,
        )
        return 1

    # ── With K cards, the password they rebuild must unlock the repo ──
    key_file: str | None = None
    if len(mnemonics) >= k and not args.no_verify_repo:
        try:
            password = decode_master_secret(recover_secret(mnemonics))
        except KeyShareError as e:
            logger.error(
                "The cards do not rebuild a password: %s. Nothing was "
                "recorded.", e,
            )
            return 1
        err = _verify_password_unlocks_repo(
            config.repositories[name].mirror_path / "keys", password, name,
        )
        if err is not None:
            logger.error("%s Nothing was recorded.", err)
            return 1
        logger.info("The %d cards rebuild the password of '%s'.", len(mnemonics), name)
        # #956: the key file the cards' password opens, for stage's check.
        key_file = _unlocking_key_file(
            config.repositories[name].mirror_path / "keys", password,
        )
    elif len(mnemonics) < k:
        logger.info(
            "%d card(s) cannot rebuild the password (it takes %d); to check "
            "the cards, run 'lcsas --config ... password verify --repo %s' "
            "with %d of them.", len(mnemonics), k, name, k,
        )

    # ── Record ────────────────────────────────────────────────────────
    with _open_catalog(args, config, mode="write-existing") as conn:
        try:
            repo = resolve_repo(conn, name, _config_mirror(config, name))
        except ValueError as e:
            logger.error("%s Nothing was recorded.", e)
            return 1
        if repo is None:
            logger.error(
                "Repository '%s' is not registered in the catalog. Register "
                "it first with 'lcsas --config ... repo add %s <mirror>'. "
                "Nothing was recorded.", name, name,
            )
            return 1
        record = get_split_for_repo(conn, repo.repo_id)
        if record is not None:
            if (record.threshold, record.shares, record.slip39_id) == (k, n, split_id):
                if key_file is not None and key_file != repo.encryption_key_id:
                    # #956: these cards open the mirror's current key file;
                    # record it, so stage does not take the split for one
                    # made before a password change.
                    from lcsas.db.repos import set_encryption_key_id
                    set_encryption_key_id(conn, repo.repo_id, key_file)
                    logger.info(
                        "The catalog already records this %d-of-%d split "
                        "(split ID %05d) of repository '%s'; recorded that "
                        "its cards open key file %s.",
                        k, n, split_id, name, key_file,
                    )
                    return 0
                logger.info(
                    "The catalog already records this %d-of-%d split (split "
                    "ID %05d) of repository '%s'. Nothing to do.",
                    k, n, split_id, name,
                )
                return 0
            logger.error(
                "The catalog records a different split of repository '%s' "
                "(%d-of-%d, split ID %05d, on %s). If that split was "
                "abandoned, clear it with 'lcsas --config ... password "
                "unsplit --repo %s', then record these cards. Nothing was "
                "recorded.", name, record.threshold, record.shares,
                record.slip39_id, record.split_at, name,
            )
            return 1
        # The split's own date, from the card, in the catalog's timestamp
        # form (utc_now's); now when the card prints none.
        when = next(iter(split_on)) if split_on else None
        _record_split_by_name(
            conn, name, k, n, split_id, _config_mirror(config, name),
            key_file, split_at=f"{when}T00:00:00.000+00:00" if when else None,
        )

    from lcsas.utils.timestamps import utc_now
    logger.warning(
        "AUDIT %s password split --record-existing: recorded the existing "
        "%d-of-%d split of repository '%s' (split ID %05d%s) from its cards. "
        "No shares were made.",
        utc_now(), k, n, name,
        split_id, f", split on {when}" if when else "",
    )
    print(
        f"Recorded the existing {k}-of-{n} split of repository '{name}' "
        f"(split ID {split_id:05d}). No shares were made: the cards already "
        "handed out stay valid, and burned discs will name this split."
    )
    return 0


def cmd_key_split(args: argparse.Namespace) -> int:
    """Split a repository password into K-of-N SLIP-0039 shares."""
    from datetime import date

    # #963: record a split already made, from its cards; no new shares.
    if getattr(args, "record_existing", False):
        return _record_existing_split(args)
    if getattr(args, "share_files", None):
        logger.error(
            "--share-file goes with --record-existing (record a split "
            "already made). A new split reads the password from the "
            "repository's password_file or --password-file."
        )
        return 1

    # The card text is frozen in main.py (see cmd_key_card).
    from lcsas.cli.main import _share_card_text
    from lcsas.keyshare import (
        KeyShareError,
        encode_master_secret,
        extract_mnemonic,
        share_identifier,
        split_secret,
    )

    # Resolve K/N: explicit flags override config defaults.
    threshold = args.threshold
    shares = args.shares
    repo_pw_file: Path | None = args.password_file

    config = _load_config_opt(args)
    # #587: the repository's own [repos.<name>] K/N, else [defaults].
    _, repo_k, repo_n = (
        config.repo_split(args.repo) if config is not None else (False, 2, 5)
    )
    if threshold is None:
        threshold = repo_k
    if shares is None:
        shares = repo_n
    # #682: the same K/N rule the config loader applies, for -k/-n too.
    from lcsas.config.settings import key_split_pair_error
    pair_problem = key_split_pair_error(threshold, shares)
    if pair_problem is not None:
        logger.error("Could not split password: %s.", pair_problem)
        return 1

    # #683: never mix two splits in one folder.  A second split with a
    # smaller N would leave the old higher-numbered cards beside the new
    # ones, and they cannot combine with them.  Checked before the password
    # is read, so nothing is written.
    out_dir: Path = args.out if args.out is not None else Path(f"./shares-{args.repo}")
    old_cards = _existing_share_files(out_dir, args.repo)
    if old_cards:
        logger.error(
            "%s already holds share files for repository '%s' from an "
            "earlier split:\n  %s\nNew cards must not sit beside them: cards "
            "of two splits cannot be combined with each other. Move these "
            "files into a dated subfolder (or destroy them, once every holder "
            "has the new cards), or pass --out with an empty folder, then "
            "split again. No shares were written.",
            out_dir, args.repo, "\n  ".join(str(p) for p in old_cards),
        )
        return 1

    # Resolve the password source.
    if repo_pw_file is None:
        if config is None:
            logger.error(
                "No password source: pass --password-file, or pass --config "
                "so the repo's configured password_file can be used."
            )
            return 1
        repo_cfg = config.repositories.get(args.repo)
        if repo_cfg is None:
            logger.error(
                "Repository '%s' is not defined in the config file.", args.repo
            )
            return 1
        if repo_cfg.password_file is None:
            logger.error(
                "Repository '%s' has no password_file configured; "
                "pass --password-file instead.", args.repo
            )
            return 1
        repo_pw_file = repo_cfg.password_file

    if not repo_pw_file.exists():
        logger.error("Password file does not exist: %s", repo_pw_file)
        return 1

    # The split is recorded under the repository's catalog repo_id.  Check
    # it is registered before any share exists (#571): a split the catalog
    # cannot record would have to be done again, with new share cards.
    if config is not None:
        try:
            _check_split_repo_registered(args, config)
        except ValueError as e:
            logger.error("%s", e)
            return 1

    # Read the password as rustic does: the file's first line (#641).
    password = _read_password_file(repo_pw_file)

    # Weak-password advisory (FUP-03).  Every burned disc carries the
    # password-locked key files, so the password faces unlimited offline
    # scrypt guessing forever.  Warn (never block) when the password looks
    # too weak to bet decades of GPU progress against.  See
    # docs/DISC_CONFIDENTIALITY.md.
    _warn_if_weak_password(password)

    try:
        master_secret = encode_master_secret(password)
        mnemonics = split_secret(master_secret, threshold, shares)
    except (KeyShareError, ValueError) as e:
        # KeyShareError: oversized password / bad master secret.
        # ValueError: invalid threshold/shares (e.g. K>N, non-positive).
        logger.error("Could not split password: %s", e)
        return 1

    key_file: str | None = None
    # ── Repo-unlock verification (default on) ─────────────────────────
    # Authenticate the password against the real key files BEFORE writing
    # anything, so a failed split leaves no wrong-password card on disk.
    if args.no_verify_repo:
        logger.warning(
            "Skipping repo-unlock check (--no-verify-repo): the password "
            "in %s was NOT verified to unlock repository '%s'.",
            repo_pw_file, args.repo,
        )
    elif config is None:
        logger.warning(
            "No --config given: the password was NOT verified against any "
            "repository. Run `lcsas --config ... password verify --repo %s "
            "--password-file %s` once the mirror is reachable.",
            args.repo, repo_pw_file,
        )
    else:
        repo_cfg = config.repositories.get(args.repo)
        if repo_cfg is None:
            logger.error(
                "Repository '%s' is not defined in the config file.", args.repo
            )
            return 1
        err = _verify_password_unlocks_repo(
            repo_cfg.mirror_path / "keys", password, args.repo
        )
        if err is not None:
            logger.error("%s", err)
            return 1
        # #956: the key file this password opens; stage refuses once a
        # password change has removed it from the mirror.
        key_file = _unlocking_key_file(repo_cfg.mirror_path / "keys", password)

    # ── In-memory recombine round-trip (always on) ────────────────────
    err = _recombine_roundtrip_error(mnemonics, password)
    if err is not None:
        logger.error("SPLIT FAILED VERIFICATION: %s. Nothing written.", err)
        return 1

    split_date = date.today().isoformat()
    try:
        split_id = share_identifier(mnemonics[0])
    except KeyShareError as e:  # pragma: no cover - just-built share is valid
        logger.error("Could not read share identifier: %s", e)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    for i, mnemonic in enumerate(mnemonics, start=1):
        share_path = out_dir / f"{args.repo}-share-{i}.txt"
        card_path = out_dir / f"{args.repo}-share-{i}-card.txt"
        _write_private_file(share_path, (mnemonic + "\n").encode("utf-8"))
        card = _share_card_text(
            args.repo, i, threshold, shares, mnemonic, split_date, split_id
        )
        _write_private_file(card_path, card.encode("utf-8"))

    # ── Post-write round-trip (write-path check) ──────────────────────
    # Re-read the CARD files from disk and verify the printed artifact —
    # not just the bare files — reconstructs the password.  Catches a
    # corrupted/short write before any card is distributed.
    card_files = [
        out_dir / f"{args.repo}-share-{i}-card.txt"
        for i in range(1, shares + 1)
    ]
    try:
        written_mnemonics = [
            extract_mnemonic(p.read_text(encoding="utf-8"), source=str(p))
            for p in card_files
        ]
    except KeyShareError as e:
        logger.error(
            "SPLIT FAILED VERIFICATION: written card unreadable: %s. "
            "Do NOT distribute these cards.", e,
        )
        return 1
    err = _recombine_roundtrip_error(written_mnemonics, password)
    if err is not None:
        logger.error(
            "SPLIT FAILED VERIFICATION: written cards %s. Do NOT distribute "
            "these cards.", err,
        )
        return 1

    # ── Record the split durably (KEY-08) ─────────────────────────────
    # The catalog row is what the burn pipeline reconciles against and what
    # disc share-instructions are derived from.  It is keyed by the
    # repository's catalog repo_id, not its name (#571).  Without --config
    # there is no catalog to write to, so drift-checking is unavailable —
    # warn loudly.
    if config is not None:
        try:
            with _open_catalog(args, config) as conn:
                repo_id = _record_split_by_name(
                    conn, args.repo, threshold, shares, split_id,
                    _config_mirror(config, args.repo), key_file,
                )
        except Exception as e:  # noqa: BLE001 - cards are written; don't fail the split
            logger.warning(
                "Split succeeded but recording it in the catalog failed: %s. "
                "Once the catalog is reachable, re-run 'lcsas --config ... "
                "password split --repo %s' so burn drift-checking works.",
                e, args.repo,
            )
        else:
            from lcsas.utils.timestamps import utc_now
            logger.warning(
                "AUDIT %s password split: recorded a %d-of-%d split of "
                "repository '%s' (id %s, split ID %05d), %s.",
                utc_now(), threshold,
                shares, args.repo, repo_id, split_id,
                f"made against key file {key_file}" if key_file is not None
                else "key file not checked (--no-verify-repo): a later "
                "password change cannot be told from this split",
            )
    else:
        logger.warning(
            "No --config given: the split was NOT recorded in any catalog, so "
            "burn-time split drift checking is unavailable. Re-run with "
            "--config once the catalog is reachable.",
        )

    print(
        f"Wrote {shares} share(s) ({threshold}-of-{shares}) for repo "
        f"'{args.repo}' to {out_dir}"
    )
    print(
        "  Each share and its plain-language card is mode 0600.\n"
        f"  Give the {shares} shares to separate share holders."
    )
    print(
        f"NEXT STEP: set split = true (and split_threshold = {threshold}, "
        f"split_shares = {shares}) under [repos.{args.repo}] in lcsas.toml, or "
        "under [defaults] if every repository is split, so burned discs "
        "print share instructions."
    )
    print(
        "SECURITY: the password was never printed. Store shares apart; any "
        f"{threshold} of {shares} reconstruct it, fewer reveal nothing.",
        file=sys.stderr,
    )
    return 0


def cmd_key_unsplit(args: argparse.Namespace) -> int:
    """Clear a repository's recorded password split (#681).

    The record (KEY-08) is what burned discs derive their share-card
    instructions from, and what the burn's drift check compares the config
    against.  When a split is abandoned (the cards were never handed out,
    or the owner went back to a single password), this is the command
    that makes the catalog agree.  It asks first, and logs one audit line
    naming what was cleared.
    """
    from lcsas.db.key_escrow import clear_split, get_split_for_repo
    from lcsas.db.repos import resolve_repo

    config = _load_config_opt(args)
    db_path = _resolve_db_path(args, config)
    # No catalog: say so, don't make one.
    with _open_catalog(args, mode="write-existing", db_path=db_path) as conn:
        try:
            repo = resolve_repo(conn, args.repo, _config_mirror(config, args.repo))
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
        if repo is None:
            logger.error(
                "Repository '%s' is not registered in the catalog at %s. "
                "See `lcsas repo list`.", args.repo, db_path,
            )
            return 1
        record = get_split_for_repo(conn, repo.repo_id)
        if record is None:
            logger.info(
                "The catalog records no password split for repository '%s'. "
                "Nothing to clear.", repo.name,
            )
            return 0

        logger.warning(
            "Repository '%s' has a recorded %d-of-%d split (split ID %05d, "
            "on %s). Clearing it means burned discs will tell the heir the "
            "password is NOT split. Its share cards are not touched.",
            repo.name, record.threshold, record.shares, record.slip39_id,
            record.split_at,
        )
        if not args.yes:
            try:
                confirmed = _confirm(
                    "Type 'yes' to clear the recorded split, or anything else "
                    "to cancel: "
                )
            except EOFError:
                logger.error(
                    "No terminal available for confirmation (redirected "
                    "input). Pass --yes to clear without asking."
                )
                return 1
            if not confirmed:
                logger.info("Nothing cleared.")
                return 0

        # Under the repo_id, and under the name for a record an earlier
        # build made (#571), as `repo remove` does.
        clear_split(conn, repo.repo_id, commit=False)
        if conn.execute(
            "SELECT 1 FROM repositories WHERE repo_id = ? AND repo_id != ?",
            (repo.name, repo.repo_id),
        ).fetchone() is None:  # the name isn't another repository's id
            clear_split(conn, repo.name, commit=False)
        conn.commit()

    from datetime import UTC, datetime
    logger.warning(
        "AUDIT %s password unsplit: cleared the recorded %d-of-%d split of "
        "repository '%s' (id %s, split ID %05d, split on %s).",
        datetime.now(UTC).isoformat(timespec="seconds"), record.threshold,
        record.shares, repo.name, repo.repo_id, record.slip39_id,
        record.split_at,
    )
    table = (
        f"[repos.{args.repo}]"
        if config is not None and args.repo in config.repositories
        else f"the repository's [repos.<name>] table ('{repo.name}' in the catalog)"
    )
    print(
        f"NEXT STEP: make sure split is false for this repository in "
        f"lcsas.toml ({table}, else [defaults]), so the config and the "
        f"catalog agree."
    )
    return 0


def cmd_key_combine(args: argparse.Namespace) -> int:
    """Reconstruct a repository password from K SLIP-0039 share mnemonics.

    stdout carries the rebuilt password and nothing else: a script runs
    ``lcsas password combine > pw``, so every message, log line included, goes
    to stderr.  On failure stdout stays empty.
    """
    with log_to_stderr():
        return _key_combine(args)


def _key_combine(args: argparse.Namespace) -> int:
    from lcsas.keyshare import (
        KeyShareError,
        check_share,
        decode_master_secret,
        decode_share_text,
        extract_mnemonic,
        read_share_stream,
        recover_secret,
        split_shares,
    )

    mnemonics: list[str] = []
    labels: list[str] = []
    if args.share_files:
        for sf in args.share_files:
            if not sf.exists():
                logger.error("Share file does not exist: %s", sf)
                return 1
            # Each file is one share: a bare mnemonic file or a printed
            # share card.  extract_mnemonic ignores card header/prose,
            # expands 4-letter prefixes, keeps a share line with typos for
            # the check below to name, and raises KeyShareError (naming the
            # file + word count) on a truncated/prose-only file.
            # #967: UTF-16 (PowerShell's `>`) and non-text bytes are read
            # as the C combiner reads them.
            text = decode_share_text(sf.read_bytes())
            try:
                mnemonics.append(
                    extract_mnemonic(text, source=str(sf), keep_mistyped=True)
                )
            except KeyShareError as e:
                logger.error("%s", e)
                return 1
            labels.append(str(sf))
    else:
        # No --share-file: read shares from stdin, a share a line or a
        # card's rows of words.  Skip blanks and card prose so
        # `cat card1 card2 | ...` works; keep a share that is nearly all
        # share words, so a typo is named below rather than the share
        # silently dropped (the C combiner's rule).
        shares, _skipped = split_shares(read_share_stream(sys.stdin))
        for lineno, mnemonic in shares:
            mnemonics.append(mnemonic)
            labels.append(f"stdin line {lineno}")

    if not mnemonics:
        logger.error(
            "No shares supplied. Pass one or more --share-file, or pipe "
            "shares on stdin (one mnemonic per line)."
        )
        return 1

    # Per-share pre-pass: validate each share independently and print a
    # named verdict, so a single mistyped card is pinpointed (file + word
    # position + token) instead of collapsing into one generic failure.
    # Verdicts go to stderr so stdout stays password-only (the combiner's
    # raw-bytes contract); mirrors the C lcsas-shares pre-pass.
    any_bad = False
    for label, mnemonic in zip(labels, mnemonics, strict=True):
        reason = check_share(mnemonic)
        if reason is None:
            print(f"share ({label}): OK", file=sys.stderr)
        else:
            print(f"share ({label}): {reason}", file=sys.stderr)
            any_bad = True
    if any_bad:
        logger.error(
            "One or more shares failed individual validation; fix the "
            "flagged words above and retry."
        )
        return 1

    try:
        master_secret = recover_secret(mnemonics)
        password = decode_master_secret(master_secret)
    except KeyShareError as e:
        logger.error(
            "Could not reconstruct the password: %s\n"
            "Check that you supplied at least the threshold (K) of valid, "
            "uncorrupted shares from the SAME split.", e
        )
        return 1

    if args.out is not None:
        _write_private_file(args.out, password)
        print(
            f"Reconstructed password written to {args.out} (mode 0600).",
            file=sys.stderr,
        )
    else:
        # Raw bytes: a password may not be valid UTF-8 and must not gain a
        # trailing newline, or it would no longer match the original.
        sys.stdout.buffer.write(password)
        sys.stdout.buffer.flush()
    return 0


def cmd_key_verify(args: argparse.Namespace) -> int:
    """Verify that shares (or a password file) unlock a repository.

    The annual password drill (docs/development/recovery/READINESS_CHECKLIST.txt): reconstruct
    the password from the supplied shares/cards (or read --password-file), then
    authenticate it against the repo's real ``keys/`` files.  Exit code is the
    contract — rc 0 only when the password actually unlocks the repo.
    """
    from lcsas.keyshare import (
        KeyShareError,
        decode_master_secret,
        extract_mnemonic,
        recover_secret,
    )

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for password verify.")
        return 1
    if bool(args.share_files) == bool(args.password_file):
        logger.error(
            "Pass EITHER one or more --share-file (to reconstruct), OR a "
            "single --password-file (to test directly)."
        )
        return 1

    repo_cfg = config.repositories.get(args.repo)
    if repo_cfg is None:
        logger.error(
            "Repository '%s' is not defined in the config file.", args.repo
        )
        return 1

    # Resolve the candidate password.
    if args.password_file is not None:
        if not args.password_file.exists():
            logger.error("Password file does not exist: %s", args.password_file)
            return 1
        password = _read_password_file(args.password_file)
        n_shares = 0
    else:
        mnemonics: list[str] = []
        for sf in args.share_files:
            if not sf.exists():
                logger.error("Share file does not exist: %s", sf)
                return 1
            try:
                mnemonics.append(
                    extract_mnemonic(
                        sf.read_text(encoding="utf-8"), source=str(sf)
                    )
                )
            except KeyShareError as e:
                logger.error("%s", e)
                return 1
        try:
            password = decode_master_secret(recover_secret(mnemonics))
        except KeyShareError as e:
            logger.error(
                "Reconstruction failed: %s\n"
                "Supply at least the threshold (K) of valid shares from the "
                "SAME split.", e,
            )
            return 1
        n_shares = len(mnemonics)

    keys_dir = repo_cfg.mirror_path / "keys"
    err = _verify_password_unlocks_repo(
        keys_dir, password, args.repo, for_split=False,
    )
    if err is not None:
        logger.error("%s", err)
        return 1

    n_keys = sum(1 for p in keys_dir.iterdir() if p.is_file())
    if n_shares:
        print(
            f"OK: these {n_shares} share(s) reconstruct the password that "
            f"unlocks '{args.repo}' ({n_keys} key file(s) checked)."
        )
    else:
        print(
            f"OK: the password unlocks '{args.repo}' "
            f"({n_keys} key file(s) checked)."
        )
    return 0
