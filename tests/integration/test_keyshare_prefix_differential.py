"""Share words typed as 4-letter prefixes work in every combiner (#797).

Each share card says "you may type just the first 4 letters of each word".
This feeds ONE synthetic share set (split from a throwaway password made
in the test) to every combiner, in every input mode, with the words typed
as full words, as 4-letter prefixes, and as a mix, and asserts every run
gives the same answer:

- tools: the committed C ``lcsas-keyshare`` (skipped when it cannot run
  here), the meta disc's ``keyshare_combine.py``, and ``lcsas key combine``;
- modes: ``file`` (share card files given by name; the card's title, tip
  line and other text must be ignored), ``prompt`` (shares typed at a
  terminal, one per line, ended with Ctrl-D; skipped without a pty) and
  ``stdin`` (``cat card1 card2 | combiner``).

A mistyped word (unknown, too short to be a prefix, or a longer token that
is no word's prefix) must be rejected by every tool in every mode, naming
the word and its position, with no password printed.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from lcsas.cli.main import _share_card_text
from lcsas.keyshare import encode_master_secret, split_secret
from lcsas.keyshare.wordlist import WORDLIST

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KEYSHARE_BIN = _REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-keyshare"
_COMBINER = _REPO_ROOT / "src" / "lcsas" / "meta" / "keyshare_combine.py"

# `make test-integration` selects with `-m integration` (#426).
pytestmark = pytest.mark.integration

# A throwaway password, new on every run: never a real secret.
_PASSWORD = b"throwaway-" + secrets.token_hex(12).encode("ascii")
_THRESHOLD, _COUNT = 2, 3


def _c_runnable() -> bool:
    if not _KEYSHARE_BIN.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(_KEYSHARE_BIN), "--help"], capture_output=True, timeout=30,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


_C_OK = _c_runnable()

_TOOLS: dict[str, list[str]] = {
    "c": [str(_KEYSHARE_BIN)],
    "meta": [sys.executable, str(_COMBINER)],
    "cli": [sys.executable, "-m", "lcsas.cli.main", "key", "combine"],
}
_TOOL_PARAMS = [
    pytest.param(
        "c",
        marks=pytest.mark.skipif(
            not _C_OK, reason="committed lcsas-keyshare cannot run here"
        ),
    ),
    "meta",
    "cli",
]


# ── the share set and how it is typed ────────────────────────────────


@pytest.fixture(scope="module")
def shares() -> list[str]:
    """The first K shares of a fresh K-of-N split of the throwaway password."""
    mnemonics = split_secret(encode_master_secret(_PASSWORD), _THRESHOLD, _COUNT)
    return mnemonics[:_THRESHOLD]


def _full(m: str) -> str:
    return m


def _prefix4(m: str) -> str:
    return " ".join(w[:4] for w in m.split())


def _mixed(m: str) -> str:
    # Alternate full words and 4-letter prefixes.
    return " ".join(w if i % 2 else w[:4] for i, w in enumerate(m.split()))


def _mixed_case(m: str) -> str:
    # Prefixes and full words in UPPER, Title and lower case.
    styles = (str.upper, str.title, str.lower)
    return " ".join(
        styles[i % 3](w if i % 2 else w[:4]) for i, w in enumerate(m.split())
    )


def _longer_prefix(m: str) -> str:
    # 5- and 6-letter prefixes where the word is long enough.
    return " ".join(w[: 5 + i % 2] for i, w in enumerate(m.split()))


_FORMS: dict[str, Callable[[str], str]] = {
    "full": _full,
    "prefix4": _prefix4,
    "mixed": _mixed,
    "mixed_case": _mixed_case,
    "longer_prefix": _longer_prefix,
}


def _card(index: int, words: str) -> str:
    return _share_card_text(
        "alpha", index, _THRESHOLD, _COUNT, words, "2026-01-01", 12345
    )


# ── running the tools ────────────────────────────────────────────────


def _env() -> dict[str, str]:
    env = dict(os.environ)
    src = str(_REPO_ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("LCSAS_KEYSHARE_PASSPHRASE", None)
    return env


def _run_pty(cmd: list[str], lines: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run *cmd* with a terminal on stdin and type *lines*, then Ctrl-D."""
    pty = pytest.importorskip("pty")
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_env(), cwd=_REPO_ROOT,
        )
    finally:
        os.close(slave)
    try:
        for line in lines:
            os.write(master, line.encode("ascii") + b"\n")
            time.sleep(0.05)
        os.write(master, b"\x04")  # Ctrl-D at the start of a line: end of input
        out, err = proc.communicate(timeout=60)
    finally:
        os.close(master)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _run(
    tool: str, mode: str, typed: list[str], tmp_path: Path
) -> subprocess.CompletedProcess[bytes]:
    """Give the *typed* shares to *tool* in *mode*."""
    cmd = list(_TOOLS[tool])
    cards = [_card(i, words) for i, words in enumerate(typed, start=1)]
    if mode == "file":
        files = []
        for i, text in enumerate(cards, start=1):
            f = tmp_path / f"alpha-share-{i}-card.txt"
            f.write_text(text, encoding="utf-8")
            files.append(str(f))
        if tool == "cli":
            for f in files:
                cmd += ["--share-file", f]
        else:
            cmd += files
        return subprocess.run(
            cmd, capture_output=True, env=_env(), cwd=_REPO_ROOT,
            timeout=60, check=False,
        )
    if mode == "stdin":
        return subprocess.run(
            cmd, input="".join(cards).encode("utf-8"), capture_output=True,
            env=_env(), cwd=_REPO_ROOT, timeout=60, check=False,
        )
    assert mode == "prompt"
    return _run_pty(cmd, typed)


_MODES = ["file", "prompt", "stdin"]


# ── wordlist invariant the prefix rule rests on ──────────────────────


def test_every_word_is_unique_in_its_first_four_letters() -> None:
    assert all(len(w) >= 4 for w in WORDLIST)
    assert len({w[:4] for w in WORDLIST}) == len(WORDLIST) == 1024


# ── accept: every form, every mode, every tool, same password ───────


@pytest.mark.parametrize("tool", _TOOL_PARAMS)
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("form", list(_FORMS))
def test_every_form_rebuilds_the_password(
    shares: list[str], tool: str, mode: str, form: str, tmp_path: Path
) -> None:
    typed = [_FORMS[form](m) for m in shares]
    proc = _run(tool, mode, typed, tmp_path)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, err
    assert proc.stdout == _PASSWORD, err
    # Every share was read, and only the shares (no card prose).
    assert err.count("OK") == len(shares), err


def test_forms_are_really_different(shares: list[str]) -> None:
    """Guard the test itself: the typed forms differ from the full words."""
    full = shares[0]
    for name, form in _FORMS.items():
        if name != "full":
            assert form(full) != full, name


# ── reject: a mistyped word is named, by position, everywhere ───────


def _bad_tokens(word: str) -> dict[str, str]:
    """Typing mistakes for *word*, none of which is a share word."""
    longer = next(
        word[:4] + c for c in "qxzjv" if word[:4] + c not in WORDLIST
        and not any(w.startswith(word[:4] + c) for w in WORDLIST)
    )
    return {
        "unknown": "qqqq",  # no word starts with "qq"
        "too_short": word[:3],  # 3 letters may start several words
        "not_a_prefix": longer,  # first 4 letters fine, then wrong
    }


@pytest.mark.parametrize("tool", _TOOL_PARAMS)
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("mistake", ["unknown", "too_short", "not_a_prefix"])
def test_mistyped_word_is_named_everywhere(
    shares: list[str], tool: str, mode: str, mistake: str, tmp_path: Path
) -> None:
    assert not any(w.startswith("qq") for w in WORDLIST)
    words = _prefix4(shares[0]).split()
    bad = _bad_tokens(shares[0].split()[6])[mistake]
    words[6] = bad  # word 7
    typed = [" ".join(words), _mixed(shares[1])]

    proc = _run(tool, mode, typed, tmp_path)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == (1 if tool == "cli" else 3), err
    assert f"word 7 '{bad}' is not a share word" in err
    assert _PASSWORD not in proc.stdout
    if tool != "cli":  # `lcsas key combine` logs its summary to stdout
        assert proc.stdout == b""
