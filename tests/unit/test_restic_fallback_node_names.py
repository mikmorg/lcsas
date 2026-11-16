"""Issue #781: tier 3 decodes rustic's escaped node names.

rustic (and restic) store a tree node's ``name`` as Go strconv.Quote text
without the outer quotes when the name holds ``"``, ``\\``, a control
character or a byte that is not valid UTF-8.  Tier 3 must restore the
real bytes, refuse a decoded name that is unsafe or that the host cannot
hold, and count every refused name as a failure (RESTORE_FAILURES.txt,
non-zero exit).  The C twin is tested in recovery/tests/test_path.c and
recovery/tests/test_tree.c.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback
from lcsas.restore.restic_fallback import (
    IntegrityError,
    NameDecodeError,
    PurePythonRestorer,
    node_name_refusal,
    unescape_node_name,
)

from .test_restic_fallback import PASSWORD
from .test_restic_fallback_path_traversal import _build_repo_with_tree


def _rustic_escape(raw: bytes) -> str:
    """rustic_core's escape_filename (crates/core/src/backend/node.rs)."""
    simple = {
        "\\": "\\\\", '"': '\\"', "\x07": "\\a", "\x08": "\\b",
        "\x0c": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\x0b": "\\v",
    }
    out: list[str] = []
    i = 0
    while i < len(raw):
        try:
            text = raw[i:].decode("utf-8")
            end = len(raw)
        except UnicodeDecodeError as exc:
            text = raw[i:i + exc.start].decode("utf-8")
            end = i + exc.start
        out.extend(simple.get(ch, ch) for ch in text)
        if end == len(raw):
            break
        # One invalid sequence: Rust's error_len() is the maximal invalid
        # prefix; Python reports it as exc.end - exc.start.
        try:
            raw[end:].decode("utf-8")
            bad_len = 0
        except UnicodeDecodeError as exc:
            bad_len = exc.end - exc.start
        out.extend(f"\\x{b:02x}" for b in raw[end:end + bad_len])
        i = end + bad_len
    return "".join(out)


# The stored text rustic 0.11.2 and restic 0.19 (Go strconv.Quote) wrote
# for real files, captured while fixing #781.
VECTORS = [
    # rustic 0.11.2
    ('quo\\"te.txt', b'quo"te.txt'),
    ("back\\\\slash.txt", b"back\\slash.txt"),
    ("tab\\there.txt", b"tab\there.txt"),
    ("nl\\nhere.txt", b"nl\nhere.txt"),
    ("latin\\xe9.txt", b"latin\xe9.txt"),
    ("lone-\\xc3", b"lone-\xc3"),
    ("surr-\\xed\\xa0\\x80x", b"surr-\xed\xa0\x80x"),
    ("over-\\xc0\\xafx", b"over-\xc0\xafx"),
    ("above-\\xf4\\x90\\x80\\x80x", b"above-\xf4\x90\x80\x80x"),
    ("vt\\vff\\f", b"vt\x0bff\x0c"),
    ("bel\\ax", b"bel\x07x"),
    ("bs\\bx", b"bs\x08x"),
    ("cr\\rhere", b"cr\rhere"),
    ("esc\x1bx", b"esc\x1bx"),            # rustic keeps ESC raw
    ("utf8-é.txt", b"utf8-\xc3\xa9.txt"),
    ("fffd-�.txt", b"fffd-\xef\xbf\xbd.txt"),
    # restic 0.19 (strconv.Quote)
    ("esc\\x1bx", b"esc\x1bx"),
    ("del\\x7fx", b"del\x7fx"),
    ("x01\\x01", b"x01\x01"),
    ("zw-\\u200bx", b"zw-\xe2\x80\x8bx"),
    ("c1-\\u0085x", b"c1-\xc2\x85x"),
    ("max-\\U0010ffffx", b"max-\xf4\x8f\xbf\xbfx"),
    ("nonchar-\\uffffx", b"nonchar-\xef\xbf\xbfx"),
    # the other escapes rustic's decoder accepts
    ("\\'\\`", b"'`"),
    ("\\xAB\\xff", b"\xab\xff"),
    ("\\U0001F600", "\U0001f600".encode()),
    ("plain.txt", b"plain.txt"),
    ("", b""),
]


@pytest.mark.parametrize(("stored", "raw"), VECTORS)
def test_unescape_vectors(stored: str, raw: bytes) -> None:
    assert unescape_node_name(stored) == raw


@pytest.mark.parametrize(
    "stored",
    [
        "abc\\",          # trailing backslash
        "back\\slash",    # unknown escape (\s)
        "\\101",          # octal: Go reads it, rustic does not
        "\\x4",           # short
        "\\xg0",          # not hex
        "\\x+f",          # int(..., 16) would take the sign
        "\\x_f",          # ... and an underscore
        "\\u12",
        "\\ud800",        # surrogate
        "\\U0000DFFF",
        "\\U00110000",    # above U+10FFFF
        "\\UFFFFFFFF",
        "lone\ud800",     # a lone surrogate straight from JSON
    ],
)
def test_unescape_refuses_text_no_encoder_writes(stored: str) -> None:
    with pytest.raises(NameDecodeError):
        unescape_node_name(stored)


def test_unescape_round_trips_rustic_escape() -> None:
    """Every byte string survives rustic's escape then our decode."""
    rng = random.Random(781)
    samples = [bytes(range(256)), b"\\x41", b"\\\\", "é😀".encode()]
    samples += [
        bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        for _ in range(2000)
    ]
    for raw in samples:
        assert unescape_node_name(_rustic_escape(raw)) == raw, raw


def test_rustic_escape_helper_matches_captured_vectors() -> None:
    """The test's reference encoder writes what rustic 0.11.2 wrote."""
    for stored, raw in VECTORS[:16]:
        assert _rustic_escape(raw) == stored


@pytest.mark.parametrize(
    ("raw", "windows", "macos", "refused"),
    [
        (b"a.txt", False, False, False),
        (b"...", False, False, False),
        (b"tab\there", False, False, False),
        (b"latin\xe9", False, False, False),
        (b"", False, False, True),
        (b".", False, False, True),
        (b"..", False, False, True),
        (b"a/b", False, False, True),
        (b"a\0b", False, False, True),
        (b"caf\xc3\xa9", True, False, False),
        (b"back\\slash", True, False, True),
        (b"a:b", True, False, True),
        (b"a*b", True, False, True),
        (b"a?", True, False, True),
        (b'a"', True, False, True),
        (b"<>", True, False, True),
        (b"|", True, False, True),
        (b"tab\there", True, False, True),
        (b"latin\xe9", True, False, True),
        (b"latin\xe9", False, True, True),
        (b"caf\xc3\xa9", False, True, False),
        (b"back\\slash", False, True, False),
        (b"\xed\xa0\x80", False, True, True),  # surrogate bytes
        (b"\xc0\xaf", False, True, True),      # overlong
    ],
)
def test_name_refusal(raw: bytes, windows: bool, macos: bool,
                      refused: bool) -> None:
    assert (node_name_refusal(raw, windows=windows, macos=macos)
            is not None) == refused


def _node(name: object, **extra: object) -> dict[str, object]:
    node: dict[str, object] = {"name": name, "type": "file", "content": []}
    node.update(extra)
    return node


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte names")
def test_restore_writes_decoded_names(tmp_path: Path) -> None:
    stored = [s for s, _ in VECTORS if s]
    repo = _build_repo_with_tree(tmp_path, [_node(s) for s in stored])
    target = tmp_path / "restored"
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=target)
    assert restorer.failures == 0
    want = {raw for s, raw in VECTORS if s}
    assert set(os.listdir(os.fsencode(target))) == want
    assert not (target / "RESTORE_FAILURES.txt").exists()


def test_refused_names_are_failures(tmp_path: Path) -> None:
    repo = _build_repo_with_tree(
        tmp_path,
        [
            _node("../escape"),
            _node("foo/bar"),
            _node("\\x2e\\x2e", type="dir"),
            _node("a\\x00b"),
            _node("bad\\q"),
            _node(""),
            _node("."),
            _node(7),
            _node("tab\\there.txt"),
            _node("ok.txt"),
        ],
    )
    target = tmp_path / "restored"
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=target)
    assert restorer.failures == 8
    assert sorted(os.listdir(target)) == sorted(
        ["RESTORE_FAILURES.txt", "ok.txt", "tab\there.txt"]
    )
    assert not (tmp_path / "escape").exists()
    manifest = (target / "RESTORE_FAILURES.txt").read_text(encoding="utf-8")
    rows = [r for r in manifest.splitlines() if "name refused" in r]
    assert len(rows) == 8
    assert any("bad\\q\tname refused: the name cannot be decoded" in r
               for r in rows)
    assert any("\\x2e\\x2e\tname refused: the name is '.' or '..'" in r
               for r in rows)


def test_refused_name_strict_raises(tmp_path: Path) -> None:
    repo = _build_repo_with_tree(tmp_path, [_node("bad\\q")])
    restorer = PurePythonRestorer(repo, password=PASSWORD, strict=True)
    with pytest.raises(IntegrityError, match="NOT RESTORED"):
        restorer.restore(target=tmp_path / "restored")


@pytest.mark.parametrize(
    ("flag", "stored"),
    [("_WINDOWS_NAMES", 'quo\\"te.txt'), ("_WINDOWS_NAMES", "a:b"),
     ("_WINDOWS_NAMES", "latin\\xe9"), ("_MACOS_NAMES", "latin\\xe9")],
)
def test_host_rule_refuses_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, stored: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A name the host cannot hold is reported, never renamed."""
    monkeypatch.setattr(restic_fallback, flag, True)
    repo = _build_repo_with_tree(tmp_path, [_node(stored), _node("ok.txt")])
    target = tmp_path / "restored"
    restorer = PurePythonRestorer(repo, password=PASSWORD)
    restorer.restore(target=target)
    assert restorer.failures == 1
    assert sorted(os.listdir(target)) == ["RESTORE_FAILURES.txt", "ok.txt"]
    assert "cannot store a name" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte names")
def test_manifest_survives_non_utf8_and_tab_paths(tmp_path: Path) -> None:
    """A failed file under a non-UTF-8, tab-holding directory name still
    gets one readable manifest row."""
    restorer = PurePythonRestorer.__new__(PurePythonRestorer)
    bad = tmp_path / os.fsdecode(b"lat\xe9\tdir") / "f"
    restorer._failures = [(str(bad), "", "IntegrityError: x")]
    restorer._rows = list(restorer._failures)  # #783: the manifest's rows
    restorer._write_failure_manifest(tmp_path)
    text = (tmp_path / "RESTORE_FAILURES.txt").read_text(encoding="utf-8")
    row = text.splitlines()[-1]
    assert row.count("\t") == 2
    assert "lat\\xe9\\x09dir/f" in row
