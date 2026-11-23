#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Standalone Share Combiner
#
#  Reconstructs the repository password from SLIP-0039 shares so an
#  heir can feed it to the normal restore (restore.sh -> "Password:").
#
#  This is a PRE-STEP, not a restore.  It depends ONLY on the bundled
#  ``lcsas.keyshare`` package (which itself imports nothing else from
#  LCSAS), so reconstruction survives even if the rest of LCSAS is
#  broken.  stdlib only.
#
#  Usage:
#    python3 combine_shares.py SHARE_FILE [SHARE_FILE ...]
#    cat share1.txt share2.txt | python3 combine_shares.py
#
#  Each SHARE_FILE is either a bare mnemonic file or a printed share
#  CARD (the {repo}-share-N-card.txt artifact you were handed).  The
#  share words are recognised by their content — header lines and prose
#  in a card are ignored — so passing the cards directly works.  Supply
#  any K of the N shares.  On stdin (cat card1 card2 | ...) each line is
#  treated as words-or-prose the same way.  A word may be cut to its
#  first 4 letters, in a file or on stdin.  The reconstructed password
#  is written to stdout as RAW BYTES with no trailing newline:
#
#    python3 combine_shares.py ~/card1.txt ~/card2.txt > ~/repo.key
#    sh /mnt/restore.sh --password-file ~/repo.key --restore-folder ~/restored
#
#  Give each file by its full path, as above (~/repo.key).
#
#  ...or paste the printed password at restore.sh's "Password:" prompt.
#
#  More than K shares is fine.  If you supply fewer than K shares, or a
#  share is mistyped, or the shares come from a different archive, the
#  tool prints a clear error to stderr and exits non-zero (the password
#  is never partially printed).  Exit status, as for the C lcsas-shares:
#    0  the password was rebuilt and written
#    1  the shares could not be combined (too few, or mixed splits)
#    2  usage error, a file could not be read, or no shares given
#    3  a share is not valid (a word is mistyped or missing)
#
#  A card file may be UTF-8 or UTF-16 with a BOM (what Windows
#  PowerShell's `>` writes).  No passphrase is read (#967): the cards
#  are made without one, as for lcsas-shares.
# ═══════════════════════════════════════════════════════════════════
"""Standalone SLIP-0039 share combiner for the LCSAS meta disc.

Reads share mnemonics from file paths given on the command line and/or
from stdin (one mnemonic per line), reconstructs the repository
password, and writes it to stdout as raw bytes.

The only LCSAS dependency is :mod:`lcsas.keyshare`.  On the meta disc
that package is bundled at the top level (importable as ``keyshare``);
in a normal source checkout it is importable as ``lcsas.keyshare``.
Both import forms are tried so the same script works in either layout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Make the bundled keyshare package importable ─────────────────────
# On the meta disc the ``keyshare`` package is bundled by the builder's
# ``bundle_python_package("lcsas.keyshare")`` under
# ``tools/lib/pythonX.Y/keyshare`` (it lands top-level, not as
# ``lcsas.keyshare``).  This script itself sits at the meta disc's root.
# Add both the script's own directory AND any bundled-stdlib dir(s) to
# sys.path so a bare ``python3 combine_shares.py`` resolves the package
# without PYTHONPATH or ambient setup.  In a dev/source tree the glob is
# empty and the import succeeds via the installed ``lcsas.keyshare``.
_HERE = Path(__file__).resolve().parent
for _cand in [_HERE, *sorted(_HERE.glob("tools/lib/python*"))]:
    _path = str(_cand)
    if _cand.is_dir() and _path not in sys.path:
        sys.path.insert(0, _path)

try:  # source checkout / dev: lcsas.keyshare
    from lcsas.keyshare import (
        KeyShareError,
        check_share,
        decode_master_secret,
        decode_share_text,
        extract_mnemonic,
        is_share_line,
        normalize_share_line,
        read_share_stream,
        recover_secret,
    )
except ImportError:  # pragma: no cover - meta disc top-level bundle path
    # On the meta disc the package is bundled as top-level ``keyshare``.
    # This branch is unreachable in-process (lcsas is always importable in
    # the dev/test tree); it is exercised by the subprocess isolation test
    # in tests/unit/test_combine_shares.py with ``lcsas`` blocked.
    from keyshare import (  # type: ignore[no-redef, import-not-found]
        KeyShareError,
        check_share,
        decode_master_secret,
        decode_share_text,
        extract_mnemonic,
        is_share_line,
        normalize_share_line,
        read_share_stream,
        recover_secret,
    )

# Exit status (the C lcsas-shares uses the same values).
EXIT_CANNOT_COMBINE = 1
EXIT_USAGE = 2
EXIT_BAD_SHARE = 3

_PROG = "combine_shares.py"

_USAGE = (
    "Usage: python3 combine_shares.py SHARE_FILE [SHARE_FILE ...]\n"
    "       cat share1 share2 | python3 combine_shares.py\n"
    "\n"
    "Reconstructs the repository password from any K SLIP-0039\n"
    "shares (more than K is fine) and writes it (raw bytes, no\n"
    "newline) to stdout.\n"
    "A word may be cut to its first 4 letters.\n"
    "\n"
    "Exit status:\n"
    "  0  the password was rebuilt and written\n"
    "  1  the shares could not be combined: too few, or not all\n"
    "     from the same split (compare the Split ID on the cards)\n"
    "  2  usage error, a file could not be read, or no shares given\n"
    "  3  a share is not valid: a word is mistyped or missing\n"
)


def _skipped_hint(skipped: int) -> str:
    """The hint for skipped stdin lines when the shares read did not do (#844).

    Printed when the password could not be rebuilt from the shares that
    were read.  The C combiner prints the same text.
    """
    return (
        f"{skipped} line(s) of input were not read as shares (blank lines "
        "are not counted).\nType each card's share words on ONE line: a "
        "share split over two lines\nis skipped.\n"
    )


def _read_mnemonics(
    paths: list[str], skipped: list[int] | None = None,
) -> list[tuple[str, str]]:
    """Collect share mnemonics from *paths* and/or stdin.

    For each file path, the whole file is treated as ONE share — a bare
    mnemonic file or a printed share card — and its embedded mnemonic is
    extracted (header/prose lines ignored).  When no paths are given,
    mnemonics are read from stdin keeping one-mnemonic-per-line
    semantics, but non-mnemonic lines (blanks, comments, card prose) are
    skipped rather than fatal, so ``cat card1 card2 | ...`` works.  In
    every mode a word may be typed as its first 4 letters, in any case.
    A file or line that is nearly all share words is kept as a share with
    typing mistakes, so the per-share check names the bad word.
    Returns ``(label, mnemonic)`` pairs in the order encountered; the label
    (a file path or ``"stdin line N"``) is used for per-share diagnostics.
    When *skipped* is given, the number of non-blank stdin lines that were
    not read as shares is appended to it.
    """
    mnemonics: list[tuple[str, str]] = []
    if paths:
        for p in paths:
            text = decode_share_text(Path(p).read_bytes())
            mnemonics.append(
                (p, extract_mnemonic(text, source=p, keep_mistyped=True))
            )
    else:
        n_skipped = 0
        stdin = read_share_stream(sys.stdin)
        for lineno, line in enumerate(stdin.splitlines(), start=1):
            if is_share_line(line):
                mnemonics.append(
                    (f"stdin line {lineno}", normalize_share_line(line))
                )
            elif line.strip():
                n_skipped += 1
        if skipped is not None:
            skipped.append(n_skipped)
    return mnemonics


def main(argv: list[str] | None = None) -> int:
    """Reconstruct the password from shares and write it to stdout.

    Returns 0 on success (password written as raw bytes, no trailing
    newline) and a non-zero exit code on any failure: 1 the shares could
    not be combined, 2 usage / unreadable file / no shares, 3 a share is
    not valid.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    # #934, #967: the old variable is refused first, as lcsas-shares
    # refuses it, so a passphrase set under it is never silently ignored.
    if "LCSAS_KEYSHARE_PASSPHRASE" in os.environ:
        sys.stderr.write(
            f"{_PROG}: LCSAS_KEYSHARE_PASSPHRASE was renamed: use "
            "LCSAS_SHARE_PASSPHRASE\n"
        )
        return EXIT_USAGE

    # Options, as lcsas-shares reads them: --help, and -- before a file
    # whose name starts with '-'.  Any other option (--passphrase among
    # them: the cards are made without one) is a usage error.
    files: list[str] = []
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            sys.stderr.write(_USAGE)
            return 0
        if arg == "--":
            break
        if arg.startswith("-") and arg != "-":
            sys.stderr.write(f"{_PROG}: unknown option '{arg}'\n{_USAGE}")
            return EXIT_USAGE
        files.append(arg)
        break
    files += args
    args = files

    skipped: list[int] = []
    try:
        pairs = _read_mnemonics(args, skipped)
    except OSError as exc:
        sys.stderr.write(f"error: could not read share file: {exc}\n")
        return EXIT_USAGE
    except KeyShareError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_BAD_SHARE

    if not pairs:
        sys.stderr.write(
            "error: no share mnemonics supplied.  Pass share file paths as\n"
            "arguments, or pipe mnemonics (one per line) on stdin.\n"
        )
        if skipped and skipped[0]:
            sys.stderr.write(_skipped_hint(skipped[0]))
        return EXIT_USAGE

    # Per-share pre-pass: validate each share independently and print a
    # named verdict, so a single mistyped card is pinpointed (file + word
    # position + token) rather than collapsing into one generic failure.
    any_bad = False
    for label, mnemonic in pairs:
        reason = check_share(mnemonic)
        if reason is None:
            sys.stderr.write(f"share ({label}): OK\n")
        else:
            sys.stderr.write(f"share ({label}): {reason}\n")
            any_bad = True
    if any_bad:
        sys.stderr.write(
            "error: one or more shares failed individual validation; fix the\n"
            "flagged words above and retry.\n"
        )
        return EXIT_BAD_SHARE

    mnemonics = [mnemonic for _, mnemonic in pairs]
    try:
        master_secret = recover_secret(mnemonics)
        password = decode_master_secret(master_secret)
    except KeyShareError as exc:
        sys.stderr.write(
            f"error: could not rebuild the password: {exc}\n"
            "Check that you supplied at least K shares from the SAME archive\n"
            "and that every word was transcribed correctly.\n"
        )
        if skipped and skipped[0]:
            sys.stderr.write(_skipped_hint(skipped[0]))
        return EXIT_CANNOT_COMBINE

    sys.stdout.buffer.write(password)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess test
    raise SystemExit(main())
