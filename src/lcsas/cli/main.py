"""LCSAS command-line interface: the ``lcsas`` entry point.

The parser is in :mod:`lcsas.cli.parser`, the command handlers are in
:mod:`lcsas.cli.commands` (one module per command group), and the helpers
they share are in :mod:`lcsas.cli.common`.  The printed card text (share,
recovery and estate cards) stays here: it is frozen.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import TYPE_CHECKING

from lcsas.cli.commands.key import _write_private_file  # noqa: F401 - re-exported
from lcsas.cli.commands.restore import (  # noqa: F401 - re-exported
    _find_sibling_iso,
    _report_missing_by_label,
    _retry_from_alternates_batch,
    _retry_from_alternates_interactive,
    cmd_restore_exec,
    cmd_restore_from_disc,
    cmd_restore_plan,
)
from lcsas.cli.common import (  # noqa: F401 - re-exported
    _load_config_opt,
    _resolve_db_path,
    _resolve_device,
    _resolve_repo_names_to_ids,
)
from lcsas.cli.parser import build_parser
from lcsas.log import get_logger, setup_logging

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger()


def _holder_label(args: argparse.Namespace) -> str:
    """Human-readable command label stamped into the lock file when held."""
    parts = ["lcsas", getattr(args, "command", "") or ""]
    for sub in ("repo_command", "staging_command", "copy_command",
                "volume_command", "catalog_command"):
        val = getattr(args, sub, None)
        if val:
            parts.append(val)
            break
    return " ".join(p for p in parts if p).strip()


def dispatch(args: argparse.Namespace) -> int:
    """Run the handler the parsed (sub)command set as ``args.func``."""
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        logger.error(f"Command '{args.command}' not yet implemented.")
        return 1
    return func(args)


def _share_card_text(
    repo: str,
    index: int,
    threshold: int,
    count: int,
    mnemonic: str,
    split_date: str,
    split_id: int,
) -> str:
    """Build a plain-language printable 'share card' for one share.

    The ``Split on`` date and ``Split ID`` (SLIP-0039 identifier, shared by
    every card of one split) let a share holder spot a superseded card set
    after a new split — see docs/ESTATE_PLANNING.md "ROTATION".
    """
    return (
        "=============== LCSAS SHARE CARD ===============\n"
        f"Repository : {repo}\n"
        f"Share      : {index} of {count}\n"
        f"Split on   : {split_date}\n"
        f"Split ID   : {split_id:05d}\n"
        "\n"
        "WHAT THIS IS\n"
        "  This card holds ONE share of the password that unlocks the\n"
        f"  repository '{repo}'.  The password was split into {count}\n"
        "  shares using Shamir Secret Sharing (SLIP-0039).\n"
        "\n"
        "HOW TO USE IT\n"
        f"  You need ANY {threshold} of the {count} shares to rebuild the\n"
        "  password.  Fewer than that reveal NOTHING about it.  Gather at\n"
        f"  least {threshold} share cards and, on the META disc, run:\n"
        "      recovery/bin/<machine>/lcsas-keyshare <card1> <card2> ...\n"
        "  If that program will not run, the fallback is:\n"
        "      python3 keyshare_combine.py <card1> <card2> ...\n"
        "  (or follow the META disc's instructions).\n"
        "\n"
        "  Other people each hold one of the remaining shares.  Losing more\n"
        f"  than {count - threshold} of the {count} shares means the\n"
        "  password can never be rebuilt.\n"
        "\n"
        "THE SHARE WORDS (keep every word, in order)\n"
        f"  {mnemonic}\n"
        "\n"
        "  Tip: you may type just the first 4 letters of each word — the\n"
        "  combiner expands any unambiguous prefix to the full word.\n"
        "================================================\n"
    )


def _check_code(password: bytes) -> str:
    """4-char transcription check code: first 4 hex of SHA-256(password).

    Lets an owner (and later an heir) confirm a hand-copied password was
    transcribed correctly without printing the password itself.  Discloses
    ~16 bits of an oracle on the password — negligible against a
    high-entropy password but stated honestly on the card.
    """
    import hashlib

    return hashlib.sha256(password).hexdigest()[:4]


def _recovery_card_text(
    repo: str,
    key_file_name: str,
    storage_hints: str,
    card_date: str,
    label_prefix: str,
    check_code: str | None,
) -> str:
    """Build a paper password card for a DEFAULT archive (password not split).

    Mirrors the visual style of ``_share_card_text``.  The password itself
    is NEVER rendered — the owner writes it by hand into the PASSWORD box.
    When *check_code* is given, an heir can confirm a correct transcription
    with ``lcsas key card --check <typed-file> --code <code>``.
    """
    hints = storage_hints if storage_hints else "(fill in where copies are kept)"
    lines = [
        "=============== LCSAS PASSWORD CARD ===============",
        f"Repository    : {repo}",
        f"Password file : {key_file_name}",
        f"Made on       : {card_date}",
        f"Discs         : labeled {label_prefix}_*",
        "",
        "WHAT THIS IS",
        "  This card records the password that unlocks the",
        f"  repository '{repo}'.  WITHOUT it the discs cannot",
        "  be decrypted — nobody can restore the files.  Store",
        "  this card physically SEPARATE from the discs.",
        "",
        "THE PASSWORD (write it here by hand — do NOT type it into a",
        "computer to print this card)",
        "  ┌────────────────────────────────────────────────────┐",
        "  │                                                      │",
        "  │                                                      │",
        "  └────────────────────────────────────────────────────┘",
        "",
        "WHERE COPIES ARE KEPT",
        f"  {hints}",
        "",
        "HOW TO USE IT",
        "  Mount any archive disc and follow START_HERE.txt; supply",
        f"  the password above for repository '{repo}'.",
    ]
    if check_code is not None:
        lines += [
            "",
            "TRANSCRIPTION CHECK CODE",
            f"  {check_code}",
            "  After writing the password, save it to a file and run:",
            "      lcsas key card --check <file> --code "
            f"{check_code}",
            "  MATCH means you copied it correctly; MISMATCH means a typo.",
            "  (The code is the first 4 hex of SHA-256(password). It leaks",
            "  ~16 bits of an oracle — negligible for a strong password.)",
        ]
    lines.append("===================================================")
    return "\n".join(lines) + "\n"


def _estate_card_text(
    *,
    owner: str,
    description: str,
    technical_contact: str,
    repositories: list[str],
    key_storage_hints: str,
    key_split: bool,
    key_threshold: int,
    key_shares: int,
    label_prefix: str,
    disc_count: int | None,
    card_date: str,
    split_repos: dict[str, tuple[int, int]] | None = None,
    unsplit_repos: tuple[str, ...] = (),
) -> str:
    """Build the one-page whole-archive estate card (UX-09).

    ``key_split``/``key_threshold``/``key_shares`` describe an archive split
    alike throughout.  A mixed archive (#587) passes ``split_repos`` (name ->
    K/N) and ``unsplit_repos`` instead, and the card lists each repository.

    Unlike the per-repo password card (``key card``, which records ONE
    password), this is the heir's starting sheet for the entire estate: who
    owns it, what is on the discs, how many discs there are, how the password
    is stored, and the literal first command to run.  No secret is ever
    printed.  Every field degrades to a fill-in blank when its source
    (config or catalog) is absent, so the card is always printable.
    """
    blank = "______________________________"

    def or_blank(value: str) -> str:
        return value if value else blank

    repos_line = ", ".join(repositories) if repositories else blank
    if disc_count is None:
        disc_line = f"{blank}  (labeled {label_prefix}_*)"
    else:
        disc_line = f"{disc_count} disc(s), labeled {label_prefix}_*"

    lines = [
        "=================== LCSAS ESTATE CARD ====================",
        f"Owner       : {or_blank(owner)}",
        f"Description : {or_blank(description)}",
        f"Made on     : {card_date}",
        "",
        "WHAT THIS IS",
        "  This is the starting sheet for an entire LCSAS backup",
        "  archive.  Keep it WITH the discs; keep the password(s)",
        "  SOMEWHERE ELSE.  WITHOUT the password the discs cannot",
        "  be decrypted — nobody can restore the files.",
        "",
        "WHAT IS ON THE DISCS",
        f"  Repositories : {repos_line}",
        f"  Discs        : {disc_line}",
        "",
        "WHERE THE PASSWORD IS KEPT",
        f"  {or_blank(key_storage_hints)}",
    ]
    if split_repos:
        lines += [
            "",
            "SOME PASSWORDS ARE SPLIT INTO SHARE CARDS",
            *[
                f"  {name}: any {k} of {n} share cards rebuild its password"
                for name, (k, n) in split_repos.items()
            ],
            *[
                f"  {name}: an ordinary password (not split)"
                for name in unsplit_repos
            ],
            "  Fewer cards reveal nothing.  Share holders (and repository):",
            "    1. ______________________   2. ______________________",
            "    3. ______________________   4. ______________________",
            "    5. ______________________",
            "  Gather one repository's cards and, on the LCSAS_META disc, run:",
            "      recovery/bin/<machine>/lcsas-keyshare <card1> <card2>",
            "  If that program will not run, the fallback is:",
            "      python3 keyshare_combine.py <card1> <card2>",
            "  It prints the password; enter it at the restore prompt.",
        ]
    elif key_split:
        lines += [
            "",
            "THE PASSWORD IS SPLIT INTO SHARE CARDS",
            f"  Any {key_threshold} of {key_shares} share cards reconstruct the",
            "  password; fewer reveal nothing.  Share holders:",
            "    1. ______________________   2. ______________________",
            "    3. ______________________   4. ______________________",
            "    5. ______________________",
            "  Gather any K share cards and, on the LCSAS_META disc, run:",
            "      recovery/bin/<machine>/lcsas-keyshare <card1> <card2>",
            "  If that program will not run, the fallback is:",
            "      python3 keyshare_combine.py <card1> <card2>",
            "  It prints the password; enter it at the restore prompt.",
        ]
    lines += [
        "",
        "FIRST STEP — restore the files",
        "  Insert the newest META disc, then for your computer:",
        "    Windows : open the LCSAS_META disc and double-click",
        "              restore.bat",
        "    macOS   : open Terminal and run",
        "              sh /Volumes/LCSAS_META/restore.sh ~/restored",
        "    Linux   : open a terminal and run",
        "              sh /media/$USER/LCSAS_META/restore.sh ~/restored",
        "  Then follow the on-screen prompts (START_HERE.txt on any",
        "  disc has the full walkthrough).",
        "",
        "IF YOU GET STUCK",
        f"  Technical contact: {or_blank(technical_contact)}",
        "  Or take ALL the discs AND the password to any computer",
        "  professional — the instructions travel on the discs.",
        "",
        "STORE THIS SHEET WITH THE DISCS — STORE THE PASSWORD ELSEWHERE.",
        "==========================================================",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=getattr(args, "verbose", False))

    if not args.command:
        parser.print_help()
        return 0

    # Stamp the lock file with the actual command being run so a concurrent
    # waiter can name the holder.
    from lcsas.db.connection import set_lock_holder_label
    set_lock_holder_label(_holder_label(args))

    try:
        return dispatch(args)
    except Exception as e:
        from lcsas.exceptions import CatalogLockTimeout, LcsasError
        if isinstance(e, CatalogLockTimeout):
            logger.error("%s", e)
            if e.recovery_hint:
                logger.error("Hint: %s", e.recovery_hint)
            return 75  # EX_TEMPFAIL — the lock holder is busy; retry later.
        if isinstance(e, LcsasError):
            logger.error("%s", e)
            if e.recovery_hint:
                logger.error("Hint: %s", e.recovery_hint)
        else:
            logger.error("Unexpected error: %s", e)
            if getattr(args, "verbose", False):
                traceback.print_exc()
            else:
                logger.error("Run with --verbose for a full traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
