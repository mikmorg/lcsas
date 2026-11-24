"""LCSAS command-line interface: the ``lcsas`` entry point.

The parser is in :mod:`lcsas.cli.parser`, the command handlers are in
:mod:`lcsas.cli.commands` (one module per command group), and the helpers
they share are in :mod:`lcsas.cli.common`.  The printed card text (share,
recovery and estate cards) stays here: it is frozen.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import traceback
from typing import TYPE_CHECKING

from lcsas.cli.commands.key import _write_private_file  # noqa: F401 - re-exported
from lcsas.cli.commands.restore import (  # noqa: F401 - re-exported
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
from lcsas.cli.parser import build_parser, renamed_message
from lcsas.log import get_logger, setup_logging, silence_stdout, stdout_closed

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger()


def _holder_label(args: argparse.Namespace) -> str:
    """Human-readable command label stamped into the lock file when held.

    The subcommand is read from ``<command>_command``, the dest every
    command group uses in the parser, so a new group needs no entry here
    (#826: a fixed list had fallen behind, and ``lcsas session abort``
    showed as ``lcsas session``).
    """
    command = getattr(args, "command", "") or ""
    parts = ["lcsas", command]
    if command:
        sub = getattr(args, f"{command.replace('-', '_')}_command", None)
        if isinstance(sub, str) and sub:
            parts.append(sub)
    return " ".join(p for p in parts if p).strip()


def dispatch(args: argparse.Namespace) -> int:
    """Run the handler the parsed (sub)command set as ``args.func``."""
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        logger.error(f"Command '{args.command}' not yet implemented.")
        return 1
    return func(args)


#: The widest line of a printed card (#955).
_CARD_WIDTH = 66


def _card_lines(text: str, first: str, rest: str) -> list[str]:
    """*text* after *first*, wrapped to the card width, later lines
    indented by *rest*.  A name is never broken: a word longer than the
    width stays whole on its own line."""
    return textwrap.wrap(
        text, width=_CARD_WIDTH, initial_indent=first, subsequent_indent=rest,
        break_long_words=False, break_on_hyphens=False,
    ) or [first.rstrip()]


def _share_word_rows(mnemonic: str) -> list[str]:
    """The share words in numbered rows of four (#955), every row under
    66 characters: ``    1 change      2 marathon    3 academic    4 acid``.

    The combiners skip the numbers (digits, with an optional ``.`` or
    ``)``), so the card combines as printed and a card typed with its
    numbers does too; "word N" in their messages is the word numbered N.
    """
    words = mnemonic.split()
    return [
        "  " + "".join(f"{n:>3} {w:<9}"
                       for n, w in enumerate(words[i:i + 4], start=i + 1)).rstrip()
        for i in range(0, len(words), 4)
    ]


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

    #955: the card describes the restore the heir meets: start the
    restore, which asks for the share cards (since #607); rebuilding the
    password by hand is the fallback.  The words are numbered in rows of
    four, with their count; no line is over 66 characters.
    """
    words = mnemonic.split()
    lines = [
        "=============== LCSAS SHARE CARD ===============",
        f"Backup set : {repo}",
        f"Share      : {index} of {count}",
        f"Split on   : {split_date}",
        f"Split ID   : {split_id:05d}",
        "",
        "WHAT THIS IS",
        "  This card holds ONE share of the password that unlocks the",
        f"  backup set '{repo}'.",
        f"  The password was split into {count} shares using Shamir",
        "  Secret Sharing (SLIP-0039).",
        "",
        "HOW TO USE IT",
        f"  You need ANY {threshold} of the {count} shares to rebuild the",
        "  password.  Fewer than that reveal NOTHING about it.",
        "",
        "  Start the restore as the estate card says (or as START_HERE.txt",
        "  on any disc says).  When it asks for share card 1, type the",
        "  words of this card in order, then ENTER (the first 4 letters",
        f"  of each word are enough).  It asks for {threshold} cards: type a",
        "  different card at each question.",
        "",
        "  If the restore does not ask for the share cards, rebuild the",
        "  password by hand: put each card's words in a text file, then",
        "  on the LCSAS_META disc run",
        "      recovery/bin/<machine>/lcsas-shares <card1> <card2>",
        "  or, if that will not run,",
        "      python3 combine_shares.py <card1> <card2>",
        "  RESTORE.txt (RESTORE_WINDOWS.txt on Windows), \"SHARE CARDS\",",
        "  names the <machine> folder for your computer.  Type the",
        "  password it prints when the restore asks for the password.",
        "",
        "  Other people each hold one of the remaining shares.  Losing",
        f"  more than {count - threshold} of the {count} shares means the password",
        "  can never be rebuilt.",
        "",
        f"THE SHARE WORDS ({len(words)} words; keep every word, in order)",
        *_share_word_rows(mnemonic),
        "",
        "  Type the words only; the numbers are there to keep your place.",
        "================================================",
    ]
    return "\n".join(lines) + "\n"


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
    The heir types it when the restore asks for it.  The password file's
    name and the transcription check (``lcsas password card --check
    <typed-file> --code <code>``) are the owner's, under FOR THE OWNER
    (#955: the heir has neither the file nor ``lcsas``).
    """
    hints = storage_hints if storage_hints else "(fill in where copies are kept)"
    lines = [
        "=============== LCSAS PASSWORD CARD ===============",
        f"Backup set    : {repo}",
        f"Made on       : {card_date}",
        f"Data discs    : labeled {label_prefix}_*",
        "",
        "WHAT THIS IS",
        "  This card records the password that unlocks the backup set",
        f"  '{repo}'.",
        "  WITHOUT it the discs cannot be decrypted — nobody can",
        "  restore the files.  Store this card physically SEPARATE",
        "  from the discs.",
        "",
        "THE PASSWORD (write it here by hand — do NOT type it into a",
        "computer to print this card)",
        "  ┌────────────────────────────────────────────────────┐",
        "  │                                                      │",
        "  │                                                      │",
        "  └────────────────────────────────────────────────────┘",
        "",
        "WHERE COPIES ARE KEPT",
        *_card_lines(hints, "  ", "  "),
        "",
        "HOW TO USE IT",
        "  Start the restore as the estate card (or START_HERE.txt on",
        "  any disc) says, choose the backup set",
        f"  '{repo}',",
        "  and type this password when the restore asks for it.",
        "",
        "FOR THE OWNER (not needed for a restore)",
        f"  Your password file : {key_file_name}",
    ]
    if check_code is not None:
        lines += [
            f"  Check code         : {check_code}",
            "  After writing the password, save it to a file and run:",
            "      lcsas password card --check <file> --code "
            f"{check_code}",
            "  MATCH means you copied it correctly; MISMATCH means a typo.",
            "  (The code is the first 4 hex of SHA-256(password). It leaks",
            "  ~16 bits of an oracle — negligible for a strong password.)",
        ]
    lines.append("===================================================")
    return "\n".join(lines) + "\n"


def _retired_card_lines(sentences: tuple[str, ...]) -> list[str]:
    """The estate card's lines for the retired backup sets (#959)."""
    import textwrap

    if not sentences:
        return []
    lines = ["  Retired backup sets (not on the newest discs):"]
    for sentence in sentences:
        lines += textwrap.wrap(sentence, width=58, initial_indent="    ",
                               subsequent_indent="      ",
                               break_on_hyphens=False)
    return lines


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
    retired_sets: tuple[str, ...] = (),
) -> str:
    """Build the one-page whole-archive estate card (UX-09).

    ``retired_sets``: one sentence per retired backup set, naming the
    disc it is restored from (#959; ``RetiredSet.sentence``).

    ``key_split``/``key_threshold``/``key_shares`` describe an archive split
    alike throughout.  A mixed archive (#587) passes ``split_repos`` (name ->
    K/N) and ``unsplit_repos`` instead, and the card lists each backup set.

    Unlike the per-repo password card (``password card``, which records ONE
    password), this is the heir's starting sheet for the entire estate: who
    owns it, what is on the discs, how many data discs there are (the META
    disc named apart), how the password is stored, and the literal first
    command to run.  No secret is ever printed.  Every field degrades to a
    fill-in blank when its source (config or catalog) is absent, so the
    card is always printable.

    #955: the card describes the restore the heir meets: the first step
    starts the restore, which asks for the share cards of a split set;
    rebuilding the password by hand is the fallback, and its password is
    typed after P at the share-card question.  No line is over 66
    characters.
    """
    blank = "______________________________"

    def or_blank(value: str) -> str:
        return value if value else blank

    repos_line = ", ".join(repositories) if repositories else blank
    count = blank if disc_count is None else str(disc_count)

    lines = [
        "=================== LCSAS ESTATE CARD ====================",
        *_card_lines(or_blank(owner), "Owner       : ", " " * 14),
        *_card_lines(or_blank(description), "Description : ", " " * 14),
        f"Made on     : {card_date}",
        "",
        "WHAT THIS IS",
        "  This is the starting sheet for an entire LCSAS backup",
        "  archive.  Keep it WITH the discs; keep the password(s)",
        "  SOMEWHERE ELSE.  WITHOUT the password the discs cannot",
        "  be decrypted — nobody can restore the files.",
        "",
        "WHAT IS ON THE DISCS",
        *_card_lines(repos_line, "  Backup sets : ", " " * 16),
        f"  Data discs  : {count}, labeled {label_prefix}_*",
        "                (copies of a disc have the same label)",
        "  META disc   : LCSAS_META, the restore tools (use the newest)",
        *_retired_card_lines(retired_sets),
        "",
        "WHERE THE PASSWORD IS KEPT",
        *_card_lines(or_blank(key_storage_hints), "  ", "  "),
        "",
        "FIRST STEP — start the restore",
        "  Insert the newest META disc, then for your computer:",
        "    Windows : open the LCSAS_META disc and double-click",
        "              restore.bat",
        "    macOS   : open Terminal and type",
        "                sh /Volumes/LCSAS_META/restore.sh",
        "    Linux   : open a terminal and type",
        "                sh /media/$USER/LCSAS_META/restore.sh",
        "  Then answer its questions (START_HERE.txt on any disc has",
        "  the full walkthrough): the newest data disc, the backup set,",
        "  the folder to restore into, then the password.",
    ]
    by_hand = [
        "  If the restore does not ask for the cards, rebuild the",
        "  password by hand: put each card's words in a text file,",
        "  then on the LCSAS_META disc run",
        "      recovery/bin/<machine>/lcsas-shares <card1> <card2>",
        "  or, if that will not run,",
        "      python3 combine_shares.py <card1> <card2>",
        "  (RESTORE.txt or RESTORE_WINDOWS.txt names <machine>).",
        "  If you rebuilt the password by hand, type P at the",
        "  share-card question and then the password.",
    ]
    holders = [
        "    1. ______________________   2. ______________________",
        "    3. ______________________   4. ______________________",
        "    5. ______________________",
    ]
    if split_repos:
        lines += [
            "",
            "SOME PASSWORDS ARE SPLIT INTO SHARE CARDS",
            *[
                line
                for name, (k, n) in split_repos.items()
                for line in _card_lines(
                    f"{name}: any {k} of {n} share cards rebuild its password",
                    "  ", "    ")
            ],
            *[
                line
                for name in unsplit_repos
                for line in _card_lines(
                    f"{name}: an ordinary password (not split)", "  ", "    ")
            ],
            "  Fewer cards reveal nothing.  Share holders (and backup set):",
            *holders,
            "  For a split backup set, when the restore gets to the",
            "  password it asks for the share cards instead: type the words",
            "  of one card at each question (each share card says how).",
            *by_hand,
        ]
    elif key_split:
        lines += [
            "",
            "THE PASSWORD IS SPLIT INTO SHARE CARDS",
            f"  Any {key_threshold} of {key_shares} share cards reconstruct the",
            "  password; fewer reveal nothing.  Share holders:",
            *holders,
            "  When the restore gets to the password, it asks for the share",
            "  cards instead: type the words of one card at each question",
            "  (each share card says how).",
            *by_hand,
        ]
    lines += [
        "",
        "IF YOU GET STUCK",
        # #966: where a disc that will not read is repaired.
        "  A disc that will not read can often be repaired: see",
        '  "REPAIRING A DAMAGED DISC" in RESTORE.txt (Windows: "A DISC',
        '  IS MISSING OR WILL NOT READ" in RESTORE_WINDOWS.txt), on the',
        "  META disc in recovery/docs.",
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
    renamed = renamed_message(sys.argv[1:] if argv is None else list(argv))
    if renamed is not None:
        print(renamed, file=sys.stderr)
        return 2
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
        rc = dispatch(args)
        # print() output still in the buffer meets a closed pipe here, not
        # as "Exception ignored ... BrokenPipeError" at exit (#968).
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            silence_stdout()
        return rc
    except KeyboardInterrupt:
        # #968: Ctrl-C ends the command quietly.  Whatever it was doing
        # has already said what it left behind (stage, the lock wait).
        return 130
    except Exception as e:
        if isinstance(e, BrokenPipeError) and stdout_closed():
            # #968: the reader of stdout went away (`lcsas ... | head`).
            silence_stdout()
            return 141
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
        elif isinstance(e, OSError) and e.filename is not None:
            # #968: a file or folder the owner named that cannot be read
            # or written: say which and why, not "Unexpected error".
            where = e.filename if e.filename2 is None else (
                f"{e.filename} -> {e.filename2}")
            logger.error("Cannot use %s: %s.", where,
                         e.strerror or e.__class__.__name__)
            if getattr(args, "verbose", False):
                traceback.print_exc()
        else:
            logger.error("Unexpected error: %s", e)
            if getattr(args, "verbose", False):
                traceback.print_exc()
            else:
                logger.error("Run with --verbose for a full traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
