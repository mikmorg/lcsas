#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Standalone Key-Share Combiner
#
#  Reconstructs the repository password from SLIP-0039 key shares so an
#  heir can feed it to the normal restore (restore.sh -> "Password:").
#
#  This is a PRE-STEP, not a restore.  It depends ONLY on the bundled
#  ``lcsas.keyshare`` package (which itself imports nothing else from
#  LCSAS), so reconstruction survives even if the rest of LCSAS is
#  broken.  stdlib only.
#
#  Usage:
#    python3 keyshare_combine.py SHARE_FILE [SHARE_FILE ...]
#    cat share1.txt share2.txt | python3 keyshare_combine.py
#
#  Each SHARE_FILE is either a bare mnemonic file or a printed share
#  CARD (the {repo}-share-N-card.txt artifact you were handed).  The
#  share words are recognised by their content — header lines and prose
#  in a card are ignored — so passing the cards directly works.  Supply
#  any K of the N shares.  On stdin (cat card1 card2 | ...) each line is
#  treated as words-or-prose the same way.  The reconstructed password
#  is written to stdout as RAW BYTES with no trailing newline:
#
#    python3 keyshare_combine.py card1.txt card2.txt > repo.key
#    ./restore.sh --key repo.key --target ~/restored
#
#  ...or paste the printed password at restore.sh's "Password:" prompt.
#
#  If you supply fewer than K shares, or a share is corrupted, or the
#  shares come from a different archive, the tool prints a clear error
#  to stderr and exits non-zero (the password is never partially
#  printed).
# ═══════════════════════════════════════════════════════════════════
"""Standalone SLIP-0039 key-share combiner for the LCSAS meta-volume.

Reads share mnemonics from file paths given on the command line and/or
from stdin (one mnemonic per line), reconstructs the repository
password, and writes it to stdout as raw bytes.

The only LCSAS dependency is :mod:`lcsas.keyshare`.  On the meta-volume
that package is bundled at the top level (importable as ``keyshare``);
in a normal source checkout it is importable as ``lcsas.keyshare``.
Both import forms are tried so the same script works in either layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Make the bundled keyshare package importable ─────────────────────
# On the meta-volume the ``keyshare`` package is bundled by the builder's
# ``bundle_python_package("lcsas.keyshare")`` under
# ``tools/lib/pythonX.Y/keyshare`` (it lands top-level, not as
# ``lcsas.keyshare``).  This script itself sits at the meta-volume root.
# Add both the script's own directory AND any bundled-stdlib dir(s) to
# sys.path so a bare ``python3 keyshare_combine.py`` resolves the package
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
        extract_mnemonic,
        is_mnemonic_line,
        recover_secret,
    )
except ImportError:  # pragma: no cover - meta-volume top-level bundle path
    # On the meta-volume the package is bundled as top-level ``keyshare``.
    # This branch is unreachable in-process (lcsas is always importable in
    # the dev/test tree); it is exercised by the subprocess isolation test
    # in tests/unit/test_keyshare_combine.py with ``lcsas`` blocked.
    from keyshare import (  # type: ignore[no-redef, import-not-found]
        KeyShareError,
        check_share,
        decode_master_secret,
        extract_mnemonic,
        is_mnemonic_line,
        recover_secret,
    )


def _read_mnemonics(paths: list[str]) -> list[tuple[str, str]]:
    """Collect share mnemonics from *paths* and/or stdin.

    For each file path, the whole file is treated as ONE share — a bare
    mnemonic file or a printed share card — and its embedded mnemonic is
    extracted (header/prose lines ignored).  When no paths are given,
    mnemonics are read from stdin keeping one-mnemonic-per-line
    semantics, but non-mnemonic lines (blanks, comments, card prose) are
    skipped rather than fatal, so ``cat card1 card2 | ...`` works.
    Returns ``(label, mnemonic)`` pairs in the order encountered; the label
    (a file path or ``"stdin line N"``) is used for per-share diagnostics.
    """
    mnemonics: list[tuple[str, str]] = []
    if paths:
        for p in paths:
            text = Path(p).read_text(encoding="utf-8")
            mnemonics.append((p, extract_mnemonic(text, source=p)))
    else:
        for lineno, line in enumerate(sys.stdin.read().splitlines(), start=1):
            if is_mnemonic_line(line):
                mnemonics.append((f"stdin line {lineno}", " ".join(line.split())))
    return mnemonics


def main(argv: list[str] | None = None) -> int:
    """Reconstruct the password from shares and write it to stdout.

    Returns 0 on success (password written as raw bytes, no trailing
    newline) and a non-zero exit code on any failure.
    """
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in ("-h", "--help"):
        sys.stderr.write(
            "Usage: python3 keyshare_combine.py SHARE_FILE [SHARE_FILE ...]\n"
            "       cat share1 share2 | python3 keyshare_combine.py\n"
            "\n"
            "Reconstructs the archive password from any K SLIP-0039 key\n"
            "shares and writes it (raw bytes, no newline) to stdout.\n"
        )
        return 0

    try:
        pairs = _read_mnemonics(args)
    except OSError as exc:
        sys.stderr.write(f"error: could not read share file: {exc}\n")
        return 2
    except KeyShareError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if not pairs:
        sys.stderr.write(
            "error: no share mnemonics supplied.  Pass share file paths as\n"
            "arguments, or pipe mnemonics (one per line) on stdin.\n"
        )
        return 2

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
        return 1

    mnemonics = [mnemonic for _, mnemonic in pairs]
    try:
        master_secret = recover_secret(mnemonics)
        password = decode_master_secret(master_secret)
    except KeyShareError as exc:
        sys.stderr.write(
            f"error: could not reconstruct the password: {exc}\n"
            "Check that you supplied at least K shares from the SAME archive\n"
            "and that every word was transcribed correctly.\n"
        )
        return 1

    sys.stdout.buffer.write(password)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess test
    raise SystemExit(main())
