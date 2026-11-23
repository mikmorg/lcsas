"""Restore journeys (#895): one list of what a person at the keyboard does,
run through both ``restore.sh`` and ``restore.bat``.

``restore.sh`` and ``restore.bat`` implement the same questions twice
(the newest data disc, the set menu, the password, the share cards,
"another set?") and must behave alike for the person answering them.
Each script's own tests drive it with its own fixtures; nothing ran the
same journey through both, so differences surfaced as drill findings
(#627).  Here a journey is data:

- :class:`Journey`: the archive (real rustic sets, burned by the real
  pipeline, unpacked into folders standing in for discs), the lines
  typed, and what must hold afterwards (:class:`Expect`).
- The questions are matched by key phrases per script
  (:data:`PROMPTS`), so the wording may differ while the sequence may
  not.  ``asked`` is that sequence of keys.
- ``differs`` holds, per script, the expectations that differ on
  purpose, each naming the :data:`DIFFERENCES` entry that explains it.
  An entry marked ``deliberate=False`` was found by these journeys and
  is not in #895's list: it is for the owner to decide, not to fix here.

Two drivers return the same :class:`JourneyResult`:

- :func:`run_journey_sh`: ``restore.sh`` from the META disc folder under
  a pseudoterminal, with ``tests/e2e/restore_sh_driver.py``'s
  ``drive_restore``.  The one drive is a folder in ``LCSAS_MOUNT_DIRS``;
  a disc goes in by renaming its folder onto the drive, as
  ``tests/integration/test_interactive_restore.py`` does.
- :func:`run_journey_bat`: ``restore.bat`` under wine, with the prefix
  and the one-answer-per-prompt runner of
  ``tests/recovery_hardening/test_restore_bat_journey_wine.py``.  The
  one drive is D:; a disc goes in by pointing D: at its folder.

Harness limits (not differences between the scripts):

- wine's cmd does not end a batch at ``(goto)``, so restore.bat's
  relaunch from the META disc cannot run here (see ``:relaunch`` in
  restore.bat).  The batch driver lays the tools folder out as the copy
  does and starts the copy in it with ``LCSAS_RELOCATED=D:\\``, as the
  relaunch line does; "tools folder removed" is then the ``.done`` the
  copy leaves for the relaunch line to act on.  For the same reason a
  journey that carries on (:class:`Leg`) runs the Desktop launcher with
  its ``(goto) 2>nul & `` taken out, as test_restore_bat_journey_wine
  does.  Real cmd runs both lines only in CI's windows-e2e job.
- Tier 1's disc questions are answered by the drivers (the disc it
  names goes in), and are not part of ``asked``: they are the same
  program's in both scripts, and how many there are depends on where
  the burn put the packs.  ``disc_prompts`` counts them.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tests.contract.exit_codes import ScriptExit

REPO_ROOT = Path(__file__).resolve().parents[2]
BAT_TRIPLE = "x86_64-pc-windows-gnu"
WIN_PROGRAMS = ("lcsas-restore.exe", "lcsas-shares.exe", "lcsas-ecc.exe")

#: A share word no card has: typed in place of a card's fifth word.
TYPO_WORD = "zqzq"
#: A password no set has.
WRONG_PASSWORD = "not-the-password-1"


# ── What the person types ───────────────────────────────────────────


@dataclass(frozen=True)
class Newest:
    """Put the newest data disc in the drive, then ENTER."""


@dataclass(frozen=True)
class Pick:
    """Choose the backup set called ``name`` at the set menu."""

    name: str


@dataclass(frozen=True)
class Password:
    """The password of the set ``name`` (a wrong one when ``wrong``)."""

    name: str
    wrong: bool = False


@dataclass(frozen=True)
class Card:
    """The words of share card ``n`` of the set ``name``, on one line;
    with ``typo``, its fifth word replaced by :data:`TYPO_WORD`; with
    ``numbered``, each word after its number, as the card prints them
    (#955: "1 change 2 marathon ...")."""

    name: str
    n: int
    typo: bool = False
    numbered: bool = False


@dataclass(frozen=True)
class Rebuilt:
    """The password of the set ``name`` as the person rebuilt it by
    hand, as the share card's fallback says: the META disc's
    ``lcsas-shares`` run on the printed cards ``cards`` (card numbers),
    and what it printed typed (#955)."""

    name: str
    cards: tuple[int, ...] = (1, 2)


@dataclass(frozen=True)
class Folder:
    """The folder typed at a folder question: the ``n``-th restore
    folder of the run (1: the first, 2: another, ...).  Optional: a
    folder question with no :class:`Folder` next in ``typed`` is
    answered with folder 1 by the driver (#953)."""

    n: int = 1


Typed = str | Newest | Pick | Password | Card | Rebuilt | Folder
NEWEST = Newest()


@dataclass(frozen=True)
class Snapshot:
    """On the command line: the start of the id of the set ``name``'s
    snapshot, as ``--list-snapshots`` prints it."""

    name: str


@dataclass(frozen=True)
class PasswordFile:
    """On the command line: the path of a file that holds the set
    ``name``'s password (the driver writes it, in the script's own
    path form)."""

    name: str


Arg = str | Snapshot | PasswordFile


# ── The questions, by key phrase per script ─────────────────────────


@dataclass(frozen=True)
class Phrase:
    """One question: the regex that ends it on each script's screen
    (None: that script never asks it).  ``key`` may name the regex's
    group ``n`` (``"card {n}"``).  ``by_driver``: the driver answers it
    (a disc, the folder restore.bat asks for, a pause), not ``typed``."""

    key: str
    sh: str | None
    bat: str | None
    by_driver: bool = False


#: Tier 1's disc question (the same program under both scripts): its
#: box ends with the abort line, perhaps a note, the frame, then "> ".
TIER1_BOX_END = r"Type 'q' then ENTER to stop for now\.[\s\S]*?\n\+-+\+\r?\n> "

PROMPTS: tuple[Phrase, ...] = (
    Phrase("newest data disc", sh=r"ENTER, S or Q: ", bat=r"Newest data disc: "),
    Phrase("which set",
           sh=r"Choose a backup set \(number or name\): ",
           bat=r"Which one do you want to restore\? \[1\]: "),
    # The second line of sh: the folder for another backup set (#953),
    # asked before that set's menu.  restore.bat asks the same question
    # for every set, after the menu.
    Phrase("folder",
           sh=(r"(?:Restore to which folder\? \(ENTER: [^)\r\n]*; Q then ENTER: stop\): "
               r"|Folder to restore it into \(ENTER: [^)\r\n]*; "
               r"Q then ENTER: no other set\): )"),
           bat=r"Restore to which folder\? \[[^\]\r\n]*\]: ", by_driver=True),
    Phrase("password", sh=r"(?<![a-z])Password: ", bat=r"(?<![a-z])Password: "),
    Phrase("card {n}",
           sh=(r"Share card (?P<n>\d+) of \d+: type all the words on one line, "
               r"then ENTER \(or Q to quit\)\.\r?\n> "),
           bat=r"Words of card (?P<n>\d+): "),
    Phrase("another set",
           sh=r"Restore another backup set from these discs\? \[y/N\] ",
           bat=r"Restore another backup set from these discs\? \[y/N\]: "),
    Phrase("overwrite",
           sh=r"Type YES to overwrite, anything else to stop: ",
           bat=r"Type YES to overwrite, anything else to stop: "),
    Phrase("space", sh=r"Carry on anyway\? \[y/N\] ", bat=r"Carry on anyway\? \[y/N\]: "),
    # Asked when the folder restored into is on tmpfs: a pytest temp
    # folder there is fine, so the driver says y (as
    # test_interactive_restore does), and the journey does not depend on
    # where the host keeps its temp folders.
    Phrase("ram", sh=r"Continue restoring into RAM anyway\? \[y/N\] ", bat=None,
           by_driver=True),
    Phrase("disc", sh=TIER1_BOX_END, bat=TIER1_BOX_END, by_driver=True),
    Phrase("pause", sh=None, bat=r"Press any key to continue[^\r\n]*", by_driver=True),
)

_COMPILED: dict[str, list[tuple[Phrase, re.Pattern[str]]]] = {
    script: [(p, re.compile(getattr(p, script))) for p in PROMPTS
             if getattr(p, script) is not None]
    for script in ("sh", "bat")
}


@dataclass(frozen=True)
class Hit:
    """A question found on the screen."""

    key: str
    start: int
    end: int
    by_driver: bool


def find_prompt(text: str, pos: int, script: str) -> Hit | None:
    """The first question in ``text[pos:]`` (``script`` "sh" or "bat")."""
    best: Hit | None = None
    for phrase, rx in _COMPILED[script]:
        m = rx.search(text, pos)
        if m is None or (best is not None and m.start() >= best.start):
            continue
        key = phrase.key.format(n=m.group("n")) if "{n}" in phrase.key else phrase.key
        start = m.start()
        if phrase.key == "disc":
            # From the frame line above the box's text, as
            # restore_sh_driver.PromptScanner reads it.
            box = text.rfind("\n+--", pos, m.start())
            start = box if box >= 0 else m.start()
        best = Hit(key, start, m.end(), phrase.by_driver)
    return best


_LABEL_LINE = re.compile(r"^\|\s+([A-Za-z0-9_]+)\s+\|$")


def box_labels(box: str) -> list[str]:
    """The labels tier 1's box names under "It is on any disc labelled:"."""
    i = box.rfind("It is on any disc labelled:")
    if i < 0:
        return []
    labels: list[str] = []
    for line in box[i:].splitlines()[1:]:
        m = _LABEL_LINE.match(line.strip())
        if m is None:
            break
        labels.append(m.group(1))
    return labels


# ── Differences between the scripts ─────────────────────────────────


@dataclass(frozen=True)
class Difference:
    """A way the scripts differ that a journey meets.  ``deliberate``:
    in #895's list (or an owner decision it cites); False: found here,
    for the owner to decide (filed as its own issue, not fixed here)."""

    what: str
    why: str
    deliberate: bool


DIFFERENCES: dict[str, Difference] = {
    "menu by number": Difference(
        what=("restore.bat's set menu takes a number only; restore.sh's "
              "takes a number or the set's name"),
        why=("#627 item 1, kept by the owner 2026-11-05 (menu wording and "
             "the choosing are documented per script)"),
        deliberate=True,
    ),
    "folder before the set": Difference(
        what=("for another backup set, restore.sh asks the folder first, "
              "then shows the set menu again; restore.bat shows the menu, "
              "then asks the folder"),
        why=("found by the second-pass journeys (#953): restore.sh starts "
             "itself again with the folder it asked for; the questions and "
             "their answers are the same"),
        deliberate=False,
    ),
    "empty password": Difference(
        what=("ENTER with no password: restore.sh asks again for as long "
              "as it is pressed; restore.bat stops with exit 77 at the "
              "third empty answer in a row"),
        why=("kept (#953): cmd cannot tell an empty answer from the end of "
             "its input, so restore.bat counts them and stops, and a run "
             "whose input has ended does not ask for ever"),
        deliberate=True,
    ),
    "version": Difference(
        what=("restore.sh takes --version (and --help); restore.bat does "
              "not know them and refuses them with exit 2, listing the "
              "arguments it takes"),
        why=("kept (#953): restore.bat is double-clicked, not run with "
             "flags; RESTORE_WINDOWS.txt documents its arguments"),
        deliberate=True,
    ),
    "too big to count": Difference(
        what=("restore.bat cannot compare a size of 100 TB or more with the "
              "free space (set /a counts to 2147483647 MB); it says it "
              "cannot check and asks the same question.  restore.sh "
              "compares any size and asks only when a drive is short"),
        why="cmd's set /a is 32-bit (#927)",
        deliberate=True,
    ),
}


@dataclass(frozen=True)
class Expect:
    """What must hold after a journey; None: not compared.

    ``asked``: the questions in order, by key (tier 1's disc questions
    left out).  ``restored``: the set (or sets) whose every file must be
    in the folders restored into, byte-exact.  ``tools_folder``: "removed" or
    "kept".  ``says``: regexes the output must match.  ``disc_asked``:
    whether tier 1 asked for a disc at least once.
    """

    exit: int
    asked: tuple[str, ...]
    restored: str | tuple[str, ...] | None = None
    tools_folder: str | None = None
    says: tuple[str, ...] = ()
    disc_asked: bool | None = None


@dataclass(frozen=True)
class Differs:
    """What one script does differently, and which :data:`DIFFERENCES`
    explain it.  Fields left None are as in the journey's ``expect``."""

    because: tuple[str, ...]
    asked: tuple[str, ...] | None = None
    exit: int | None = None
    tools_folder: str | None = None
    says: tuple[str, ...] | None = None
    #: The lines typed, when the questions differ.
    typed: tuple[Typed, ...] | None = None


class _PerScript:
    """typed_for and expect_for, for a journey and for its second leg."""

    typed: tuple[Typed, ...]
    expect: Expect
    differs: dict[str, Differs]

    def typed_for(self, script: str) -> tuple[Typed, ...]:
        d = self.differs.get(script)
        return self.typed if d is None or d.typed is None else d.typed

    def expect_for(self, script: str) -> Expect:
        d = self.differs.get(script)
        if d is None:
            return self.expect
        over: dict[str, Any] = {k: getattr(d, k)
                                for k in ("asked", "exit", "tools_folder", "says")
                                if getattr(d, k) is not None}
        return replace(self.expect, **over)


@dataclass(frozen=True)
class Leg(_PerScript):
    """The person carries on a restore that stopped: restore.sh's
    ``<tools>/resume.sh``, restore.bat's "Resume LCSAS restore.bat" on
    the Desktop.  The drive is empty when it starts."""

    typed: tuple[Typed, ...]
    expect: Expect
    differs: dict[str, Differs] = field(default_factory=dict)


@dataclass(frozen=True)
class Journey(_PerScript):
    name: str
    #: A key of :data:`ARCHIVES` or :data:`ALTERED`.
    archive: str
    typed: tuple[Typed, ...]
    expect: Expect
    #: Per script ("sh", "bat"): only for the differences above.
    differs: dict[str, Differs] = field(default_factory=dict)
    #: Extra environment for both scripts.
    env: dict[str, str] = field(default_factory=dict)
    #: On both scripts' command lines.
    args: tuple[Arg, ...] = ()
    #: Type q at tier 1's n-th disc question (1 = the first).
    quit_at_disc: int | None = None
    #: Type s ("I do not have this disc", #949) at tier 1's n-th disc
    #: question (1 = the first).
    skip_at_disc: int | None = None
    #: What happens when the person carries on.
    then: Leg | None = None


# ── The archives: real rustic sets, burned, unpacked ────────────────


@dataclass(frozen=True)
class ArchiveSpec:
    """A fixture archive, built with :func:`tests.e2e.fixture_lib.
    build_windows_fixture` (rustic, the burn pipeline, the META builder).
    The first set is ``set_name``; ``others`` are smaller sets."""

    set_name: str
    file_count: int
    file_bytes: int
    others: tuple[str, ...] = ()
    splits: tuple[tuple[str, int, int], ...] = ()
    #: Sets made after the META disc was burned: on the data discs only,
    #: so a script finds them only by reading the newest data disc (#605).
    not_on_meta: tuple[str, ...] = ()


ARCHIVES: dict[str, ArchiveSpec] = {
    # One set, small: one data disc.
    "one set": ArchiveSpec("Holiday photos", file_count=3, file_bytes=32 * 1024),
    # Two sets, the second split 2-of-3; the first spans two data discs
    # and was made after the META disc (#605).
    "two sets": ArchiveSpec("Holiday photos", file_count=4, file_bytes=384 * 1024,
                            others=("Family documents",),
                            splits=(("Family documents", 2, 3),),
                            not_on_meta=("Holiday photos",)),
}


@dataclass(frozen=True)
class Altered:
    """An archive made from another by changing its data discs: the
    copies are the base's, then ``how``:

    - "damaged pack": a byte in the middle of the largest pack (a data
      pack) flipped, as a scratch the ECC could not repair leaves it;
    - "huge packs": every pack in each disc's catalog said to be 10 TB, so
      no disk has room for what the catalog says the set needs.  Under
      100 TB, so restore.bat's set /a can count it and compares;
    - "petabyte packs": every pack said to be 1 PB, more than set /a can
      count: restore.bat says it cannot check, and asks (#927)."""

    base: str
    how: str


ALTERED: dict[str, Altered] = {
    "one set, a pack damaged": Altered("one set", "damaged pack"),
    "one set, catalog sizes huge": Altered("one set", "huge packs"),
    "one set, catalog sizes in petabytes": Altered("one set", "petabyte packs"),
}


@dataclass(frozen=True)
class ArchiveSet:
    repo_id: str
    name: str
    name_cmd: str
    password: str
    #: {relative path: sha256} of the files backed up.
    manifest: dict[str, str]
    #: Each share card's words, card 1 first (a split set only).
    cards: tuple[str, ...] = ()
    #: Each printed share card, as ``lcsas password split`` wrote it.
    card_texts: tuple[str, ...] = ()
    #: The id of its one snapshot.
    snapshot: str = ""


@dataclass(frozen=True)
class Archive:
    #: The META disc (a read-only folder).
    meta: Path
    #: Each data disc (a read-only folder), by label.
    discs: dict[str, Path]
    sets: dict[str, ArchiveSet]

    @property
    def newest(self) -> str:
        return sorted(self.discs)[-1]


def _read_only(root: Path) -> None:
    for dirpath, _dirs, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(os.path.join(dirpath, name), 0o444)
        os.chmod(dirpath, 0o555)


def writable(root: Path) -> None:
    """Undo :func:`_read_only` (before a folder is deleted or written)."""
    if not root.exists():
        return
    for dirpath, _dirs, files in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in files:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o644)


def alter_archive(base: Archive, how: str, workdir: Path) -> Archive:
    """The archive ``base`` with its data discs changed (:class:`Altered`)."""
    import sqlite3

    discs: dict[str, Path] = {}
    for label, disc in base.discs.items():
        discs[label] = workdir / "discs" / label
        shutil.copytree(disc, discs[label])
        writable(discs[label])
    if how == "damaged pack":
        pack = max((p for d in discs.values() for p in (d / "data").rglob("*")
                    if p.is_file()), key=lambda p: p.stat().st_size)
        data = bytearray(pack.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pack.write_bytes(bytes(data))
    elif how in ("huge packs", "petabyte packs"):
        size = 10**13 if how == "huge packs" else 10**15
        for d in discs.values():
            with contextlib.closing(sqlite3.connect(d / "catalog.db")) as conn:
                conn.execute("UPDATE packs SET size_bytes = ?", (size,))
                conn.commit()
    else:
        raise ValueError(f"no such alteration: {how!r}")
    for d in discs.values():
        _read_only(d)
    return replace(base, discs=discs)


def build_archive(spec: ArchiveSpec, workdir: Path) -> Archive:
    """Build ``spec`` under ``workdir`` (needs rustic and xorriso)."""
    from lcsas.keyshare import extract_mnemonic
    from tests.e2e.fixture_lib import build_windows_fixture

    fx = build_windows_fixture(
        workdir, file_count=spec.file_count, file_bytes=spec.file_bytes,
        set_name=spec.set_name, other_set_names=spec.others,
        split_sets={n: (k, m) for n, k, m in spec.splits}, meta_metadata=True,
    )
    discs_dir = workdir / "discs"
    discs: dict[str, Path] = {}
    for iso in fx.data_isos:
        dest = discs_dir / iso.stem
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso),
                        "-extract", "/", str(dest)],
                       capture_output=True, check=True, timeout=300)
        discs[iso.stem] = dest
        iso.unlink()
    fx.meta_iso.unlink()
    # The META disc is the tree the builder made (and burned as is), in
    # a folder of its own: restore.sh looks for a catalog.db one and two
    # folders above itself (a META disc copied off a folder), and the
    # fixture's working catalog must not be one.
    meta = workdir / "meta" / "LCSAS_META"
    meta.parent.mkdir()
    os.rename(workdir / "meta_stage", meta)
    for p in workdir.glob("catalog.db*"):
        p.unlink()
    for fs in (fx.restore_set, *fx.other_sets):
        if fs.name in spec.not_on_meta:
            shutil.rmtree(meta / "metadata" / fs.repo_id)
    for d in (*discs.values(), meta):
        writable(d)
        _read_only(d)
    pw_files = [fx.password_file] + [workdir / f"other{i}.pw"
                                      for i in range(1, len(fx.other_sets) + 1)]
    sets: dict[str, ArchiveSet] = {}
    for fs, pw in zip((fx.restore_set, *fx.other_sets), pw_files, strict=True):
        card_texts = tuple((workdir / c).read_text(encoding="utf-8") for c in fs.cards)
        cards = tuple(extract_mnemonic(t) for t in card_texts)
        snaps = sorted(p.name for p in (next(iter(discs.values())) / "metadata"
                                        / fs.repo_id / "snapshots").iterdir())
        sets[fs.name] = ArchiveSet(
            repo_id=fs.repo_id, name=fs.name, name_cmd=fs.name_cmd,
            password=pw.read_text().strip(),
            manifest=json.loads((workdir / fs.manifest).read_text()),
            cards=cards, card_texts=card_texts,
            snapshot=snaps[0] if len(snaps) == 1 else "",
        )
    for scrap in ("mirror", "staging", "sources", "meta_stage.iso"):
        shutil.rmtree(workdir / scrap, ignore_errors=True)
    return Archive(meta=meta, discs=discs, sets=sets)


# ── The result, and the comparison ──────────────────────────────────


@dataclass
class JourneyResult:
    script: str
    exit: int
    asked: list[str]
    disc_prompts: int
    #: Every file restored: its sha256.
    restored: set[str]
    tools_folder: str
    #: Typed lines not used (negative: questions asked after the last).
    untyped: int
    seconds: float
    output: str = field(repr=False, default="")
    #: The second leg (:class:`Leg`), when the journey has one.
    then: JourneyResult | None = None


def mismatches(journey: Journey, got: JourneyResult, archive: Archive) -> list[str]:
    """What in ``got`` differs from what ``journey`` expects of its script."""
    out = _leg_mismatches(journey.expect_for(got.script), got, archive)
    if journey.then is not None:
        if got.then is None:
            out.append("then: the restore was not carried on")
        else:
            out += [f"then: {m}" for m in _leg_mismatches(
                journey.then.expect_for(got.script), got.then, archive)]
    return out


def _leg_mismatches(want: Expect, got: JourneyResult, archive: Archive) -> list[str]:
    out: list[str] = []
    if got.exit != want.exit:
        out.append(f"exit {got.exit}, want {want.exit}")
    if tuple(got.asked) != want.asked:
        out.append(f"asked {got.asked}, want {list(want.asked)}")
    if got.untyped:
        out.append(f"{got.untyped} typed line(s) left over" if got.untyped > 0
                   else f"{-got.untyped} question(s) after the last typed line")
    wanted = (want.restored,) if isinstance(want.restored, str) else want.restored or ()
    for name in wanted:
        files = archive.sets[name].manifest
        missing = sorted(p for p, sha in files.items() if sha not in got.restored)
        if missing:
            out.append(f"{name}: not restored byte-exact: {missing}")
    if want.tools_folder is not None and got.tools_folder != want.tools_folder:
        out.append(f"tools folder {got.tools_folder}, want {want.tools_folder}")
    for rx in want.says:
        if not re.search(rx, got.output):
            out.append(f"does not say /{rx}/")
    if want.disc_asked is not None and (got.disc_prompts > 0) != want.disc_asked:
        out.append(f"tier 1 asked for a disc {got.disc_prompts} time(s), "
                   f"want {'some' if want.disc_asked else 'none'}")
    return out


#: How many restore folders a journey may type (:class:`Folder`).
FOLDERS = 3


def _restored_shas(*roots: Path) -> set[str]:
    shas: set[str] = set()
    for root in roots:
        if root.is_dir():
            shas |= _tree_shas(root)
    return shas


def _tree_shas(root: Path) -> set[str]:
    shas: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            h = hashlib.sha256()
            with open(os.path.join(dirpath, name), "rb") as f:
                for chunk in iter(lambda f=f: f.read(1 << 16), b""):
                    h.update(chunk)
            shas.add(h.hexdigest())
    return shas


# ── Typing ──────────────────────────────────────────────────────────


_MENU_HEAD = "This archive contains more than one backup set:"
_BAT_MENU_LINE = re.compile(r"^ {3}\[(\d+)\] (.*?)\r?$", re.MULTILINE)


def _bat_menu_number(screen: str, set_: ArchiveSet) -> str:
    """The number restore.bat's menu shows ``set_`` under (its name_cmd,
    perhaps followed by the split note)."""
    menu = screen[screen.rfind(_MENU_HEAD):]
    for num, text in _BAT_MENU_LINE.findall(menu):
        if text == set_.name_cmd or text.startswith(set_.name_cmd + " (password split"):
            return num
    raise AssertionError(f"{set_.name_cmd!r} is not on restore.bat's menu:\n{menu}")


def arg_text(item: Arg, archive: Archive, pw_file: Any = None) -> str:
    """A command-line argument of a journey, as typed.  ``pw_file(name)``
    writes the set's password to a file and returns its path as the
    script names it (:class:`PasswordFile`)."""
    if isinstance(item, Snapshot):
        snap = archive.sets[item.name].snapshot
        assert snap, f"{item.name} has not exactly one snapshot"
        return snap[:8]
    if isinstance(item, PasswordFile):
        assert pw_file is not None
        return str(pw_file(item.name))
    return item


def type_line(item: Typed, script: str, archive: Archive, screen: str) -> str:
    """What the person types for ``item`` (not :class:`Newest`)."""
    if isinstance(item, str):
        return item
    if isinstance(item, Pick):
        s = archive.sets[item.name]
        # The set menu: restore.bat takes a number only ("menu by number").
        return _bat_menu_number(screen, s) if script == "bat" else s.name
    if isinstance(item, Password):
        return WRONG_PASSWORD if item.wrong else archive.sets[item.name].password
    if isinstance(item, Card):
        words = archive.sets[item.name].cards[item.n - 1].split()
        if item.typo:
            words[4] = TYPO_WORD
        if item.numbered:
            return " ".join(f"{i} {w}" for i, w in enumerate(words, start=1))
        return " ".join(words)
    if isinstance(item, Rebuilt):
        return rebuild_password(archive, item)
    raise TypeError(f"cannot type {item!r}")


#: The META disc's folder of programs for this computer.
_HOST_TRIPLE = "x86_64-unknown-linux-musl"


def rebuild_password(archive: Archive, item: Rebuilt) -> str:
    """Run the META disc's ``lcsas-shares`` on the printed cards, as the
    share card's fallback says, and return what it printed."""
    import tempfile

    on_disc = archive.meta / "recovery" / "bin" / _HOST_TRIPLE / "lcsas-shares"
    texts = archive.sets[item.name].card_texts
    with tempfile.TemporaryDirectory() as tmp:
        # The disc folder here holds its files without the execute bit
        # (_read_only); a copy runs as the program on a mounted disc does.
        program = Path(tmp) / "lcsas-shares"
        shutil.copyfile(on_disc, program)
        program.chmod(0o755)
        files = []
        for n in item.cards:
            f = Path(tmp) / f"card{n}.txt"
            f.write_text(texts[n - 1], encoding="utf-8")
            files.append(str(f))
        proc = subprocess.run([str(program), *files], capture_output=True,
                              timeout=60, check=False)
    assert proc.returncode == 0, (
        f"{program} could not rebuild the password of {item.name}: "
        f"exit {proc.returncode}\n{proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode()


# ── restore.sh under a pseudoterminal ───────────────────────────────


class _ShScanner:
    """``drive_restore``'s scanner, reading :data:`PROMPTS`."""

    def __init__(self) -> None:
        self._text = ""
        self._pos = 0

    def feed(self, chunk: bytes) -> list[Any]:
        from tests.e2e.restore_sh_driver import Prompt

        self._text += chunk.decode(errors="replace")
        found = []
        while (hit := find_prompt(self._text, self._pos, "sh")) is not None:
            labels = box_labels(self._text[hit.start:hit.end]) if hit.key == "disc" else []
            found.append(Prompt(hit.key, labels))
            self._pos = hit.end
        return found


def run_journey_sh(journey: Journey, archive: Archive, work: Path,
                   timeout: float = 300.0) -> JourneyResult:
    """Run ``journey`` through ``restore.sh`` in ``work``."""
    from tests.e2e.restore_sh_driver import (
        ENTER,
        DiscPicker,
        Prompt,
        build_restore_env,
        drive_restore,
    )

    shelf = work / "shelf"
    for label, disc in archive.discs.items():
        # The discs move in and out of the drive by rename, which needs
        # their folders writable; each run gets its own copy.
        shutil.copytree(disc, shelf / label)
        writable(shelf / label)
    mount = work / "mnt"
    drive = mount / "drive"
    drive.mkdir(parents=True)
    for d in ("home", "tmp"):
        (work / d).mkdir()
    tools = work / "tools"
    targets = [work / ("restored" if n == 1 else f"restored-{n}")
               for n in range(1, FOLDERS + 1)]
    in_drive: list[str] = []

    def eject() -> None:
        if in_drive:
            os.rename(drive, shelf / in_drive.pop())
            drive.mkdir()

    def insert(label: str) -> None:
        if in_drive == [label]:
            return
        eject()
        drive.rmdir()
        os.rename(shelf / label, drive)
        in_drive.append(label)

    env = build_restore_env(home=work / "home", tools_dir=tools, mount_dir=mount,
                            meta_disc=archive.meta, tmpdir=work / "tmp")
    env.update(journey.env)

    def leg(cmd: list[str], typed_: tuple[Typed, ...],
            quit_at: int | None, skip_at: int | None = None) -> JourneyResult:
        typed = list(typed_)
        asked: list[str] = []
        discs = [0]
        picker = DiscPicker(list(archive.discs))
        over = [0]

        def answer(prompt: Prompt, _n: int) -> bytes:
            if prompt.kind == "ram":
                return b"y\n"
            if prompt.kind == "folder":
                # Asked as restore.bat asks it (#926): the folder the
                # journey types (#953), else the first.
                asked.append(prompt.kind)
                n = 1
                if typed and isinstance(typed[0], Folder):
                    n = typed.pop(0).n  # type: ignore[union-attr]
                return (str(targets[n - 1]) + "\n").encode()
            if prompt.kind == "disc":
                discs[0] += 1
                if discs[0] == skip_at:
                    return b"s\n"
                label = picker.pick(prompt.labels)
                if label is None or discs[0] == quit_at:
                    return b"q\n"
                insert(label)
                return ENTER
            asked.append(prompt.kind)
            if not typed:
                over[0] += 1
                return b"q\n"
            item = typed.pop(0)
            if isinstance(item, Newest):
                insert(archive.newest)
                return ENTER
            return (type_line(item, "sh", archive, "") + "\n").encode()

        t0 = time.monotonic()
        try:
            rc, out = drive_restore(cmd, env, answer, timeout=timeout,
                                    scanner=_ShScanner())
        finally:
            eject()
        return JourneyResult(
            script="sh", exit=rc, asked=asked, disc_prompts=discs[0],
            restored=_restored_shas(*targets),
            tools_folder="kept" if tools.exists() else "removed",
            untyped=len(typed) - over[0], seconds=time.monotonic() - t0, output=out,
        )

    def pw_file(name: str) -> Path:
        path = work / "pw.txt"
        path.write_text(archive.sets[name].password)
        return path

    # No --restore-folder: restore.sh asks for the folder, as restore.bat
    # does (#926), and the driver answers it.
    result = leg(["sh", str(archive.meta / "restore.sh"),
                  *(arg_text(a, archive, pw_file) for a in journey.args)],
                 journey.typed_for("sh"), journey.quit_at_disc,
                 journey.skip_at_disc)
    if journey.then is not None and (tools / "resume.sh").is_file():
        result.then = leg(["sh", str(tools / "resume.sh")],
                          journey.then.typed_for("sh"), None)
    return result


# ── restore.bat under wine ──────────────────────────────────────────


def _bat_tools_copy(archive: Archive, tools: Path) -> None:
    """The tools folder as restore.bat's copy from the META disc lays it
    out (see ``:relaunch``): .lcsas-tools first, this computer's
    programs, the guide, the sets' metadata, restore.bat last."""
    tools.mkdir(parents=True)
    (tools / ".lcsas-tools").write_text("made by the journey harness\r\n")
    src = archive.meta / "recovery"
    bindir = tools / "recovery" / "bin" / BAT_TRIPLE
    bindir.mkdir(parents=True)
    for prog in WIN_PROGRAMS:
        shutil.copyfile(src / "bin" / BAT_TRIPLE / prog, bindir / prog)
    (tools / "recovery" / "docs").mkdir()
    shutil.copyfile(src / "docs" / "RESTORE_WINDOWS.txt",
                    tools / "recovery" / "docs" / "RESTORE_WINDOWS.txt")
    shutil.copytree(archive.meta / "metadata", tools / "metadata",
                    copy_function=shutil.copyfile)
    (tools / "recovery" / "scripts").mkdir()
    shutil.copyfile(src / "scripts" / "restore.bat",
                    tools / "recovery" / "scripts" / "restore.bat")
    writable(tools)


def run_journey_bat(journey: Journey, archive: Archive, wine: Any,
                    run_no: int) -> JourneyResult:
    """Run ``journey`` through ``restore.bat`` under ``wine`` (the
    ``_Wine`` of test_restore_bat_journey_wine, its drives reset).
    ``run_no`` makes the run's folders on C: its own."""
    from tests.e2e.restore_sh_driver import DiscPicker
    from tests.recovery_hardening.test_restore_bat_journey_wine import (
        _replay_launcher,
        _Run,
    )
    from tests.recovery_hardening.test_restore_bat_wine_smoke import _map_drive

    name = f"lcsas-j{run_no}"
    tools = wine.drive_c / name
    targets_win = [f"C:\\restored-{run_no}" + ("" if n == 1 else f"-{n}")
                   for n in range(1, FOLDERS + 1)]
    targets = [wine.drive_c / t[3:] for t in targets_win]
    _bat_tools_copy(archive, tools)
    _map_drive(wine.prefix, "d", archive.meta)  # the one drive: META in

    def leg(cmd: str | list[str], env: dict[str, str], typed_: tuple[Typed, ...],
            quit_at: int | None, skip_at: int | None = None) -> JourneyResult:
        typed = list(typed_)
        asked: list[str] = []
        discs = 0
        over = 0
        picker = DiscPicker(list(archive.discs))
        t0 = time.monotonic()
        run = _Run(wine, cmd, env)
        try:
            while True:
                text = run.text
                hit = find_prompt(text, run.pos, "bat")
                if hit is None:
                    if run.proc.poll() is not None:
                        run._pump.join(timeout=5)
                        if find_prompt(run.text, run.pos, "bat") is not None:
                            continue
                        break
                    if time.monotonic() > run.deadline:
                        break
                    time.sleep(0.2)
                    continue
                run.pos = hit.end
                if hit.key == "pause":
                    run.send("")
                elif hit.key == "disc":
                    discs += 1
                    if discs == skip_at:
                        run.send("s")
                        continue
                    label = picker.pick(box_labels(text[hit.start:hit.end]))
                    if label is None or discs == quit_at:
                        run.send("q")
                    else:
                        _map_drive(wine.prefix, "d", archive.discs[label])
                        run.send("")
                elif hit.key == "folder":
                    asked.append(hit.key)
                    n = 1
                    if typed and isinstance(typed[0], Folder):
                        n = typed.pop(0).n  # type: ignore[union-attr]
                    run.send(targets_win[n - 1])
                else:
                    asked.append(hit.key)
                    if not typed:
                        over += 1
                        run.send("q")
                        continue
                    item = typed.pop(0)
                    if isinstance(item, Newest):
                        _map_drive(wine.prefix, "d", archive.discs[archive.newest])
                        run.send("")
                    else:
                        run.send(type_line(item, "bat", archive, text[:hit.end]))
        finally:
            rc = run.finish()
        if not tools.exists():
            folder = "removed"
        else:
            # The relaunch line deletes a tools folder holding .done.
            folder = "removed" if (tools / ".done").exists() else "kept"
        return JourneyResult(
            script="bat", exit=rc, asked=asked, disc_prompts=discs,
            restored=_restored_shas(*targets), tools_folder=folder,
            untyped=len(typed) - over, seconds=time.monotonic() - t0,
            output=run.text,
        )

    def pw_file(set_name: str) -> str:
        (wine.drive_c / f"pw-{run_no}.txt").write_text(archive.sets[set_name].password)
        return f"C:\\pw-{run_no}.txt"

    # One argv entry each (see _Run): inside one string, wine would pass
    # a '"' to cmd as \", and restore.bat would see \--backup-set\.
    result = leg([f"C:\\{name}\\recovery\\scripts\\restore.bat",
                  *(arg_text(a, archive, pw_file) for a in journey.args)],
                 {"LCSAS_RELOCATED": "D:\\", **journey.env},
                 journey.typed_for("bat"), journey.quit_at_disc,
                 journey.skip_at_disc)
    launcher = wine.desktop / "Resume LCSAS restore.bat"
    if journey.then is not None and launcher.is_file():
        # The drive is empty; the launcher runs as wine can run it (its
        # (goto) line without the `(goto) 2>nul & `, the harness limit).
        _map_drive(wine.prefix, "d", wine.prefix / "_no_drive")
        result.then = leg(_replay_launcher(wine, launcher), dict(journey.env),
                          journey.then.typed_for("bat"), None)
    for d in (tools, *targets):
        writable(d)
        shutil.rmtree(d, ignore_errors=True)
    (wine.drive_c / f"pw-{run_no}.txt").unlink(missing_ok=True)
    return result


# ── The journeys ────────────────────────────────────────────────────

PHOTOS = "Holiday photos"
DOCS = "Family documents"
#: A set no archive has (not one of its names in other letters: a name
#: matched case-blind is #627 item 8).
UNKNOWN_SET = "Garden photos"

JOURNEYS: tuple[Journey, ...] = (
    Journey(
        name="one set, password typed, one disc",
        archive="one set",
        typed=(NEWEST, Password(PHOTOS)),
        expect=Expect(exit=ScriptExit.OK, asked=("newest data disc", "folder", "password"),
                      restored=PHOTOS, tools_folder="removed", disc_asked=False),
    ),
    Journey(
        name="two sets, pick by name, newest data disc asked first",
        archive="two sets",
        typed=(NEWEST, Pick(PHOTOS), Password(PHOTOS), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "which set", "folder",
                             "password", "another set"),
                      restored=PHOTOS, tools_folder="removed", disc_asked=True),
        differs={"bat": Differs(because=("menu by number",))},
    ),
    Journey(
        name="split password from share cards, one card mistyped then corrected",
        archive="two sets",
        typed=(NEWEST, Pick(DOCS), Card(DOCS, 1, typo=True), Card(DOCS, 1),
               Card(DOCS, 3), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "which set", "folder",
                             "card 1", "card 1", "card 2", "another set"),
                      restored=DOCS, tools_folder="removed",
                      says=(rf"'{TYPO_WORD}' is not a share word",)),
        differs={"bat": Differs(because=("menu by number",))},
    ),
    # #955: the share card as printed: the words typed with their
    # numbers, a card at each question.
    Journey(
        name="split password, the share card's words typed with their numbers",
        archive="two sets",
        typed=(NEWEST, Pick(DOCS), Card(DOCS, 2, numbered=True),
               Card(DOCS, 3, numbered=True), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "which set", "folder",
                             "card 1", "card 2", "another set"),
                      restored=DOCS, tools_folder="removed"),
        differs={"bat": Differs(because=("menu by number",))},
    ),
    # #955: the password rebuilt by hand from the printed cards with the
    # META disc's lcsas-shares (the share card's fallback), then P at
    # the share-card question and the password, as the estate card says.
    Journey(
        name="split password rebuilt by hand, P at the card question, then the password",
        archive="two sets",
        typed=(NEWEST, Pick(DOCS), "P", Rebuilt(DOCS, (1, 3)), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "which set", "folder",
                             "card 1", "password", "another set"),
                      restored=DOCS, tools_folder="removed"),
        differs={"bat": Differs(because=("menu by number",))},
    ),
    Journey(
        name="wrong password, then the right one",
        archive="one set",
        typed=(NEWEST, Password(PHOTOS, wrong=True), Password(PHOTOS)),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "folder", "password",
                             "password"),
                      restored=PHOTOS, tools_folder="removed"),
    ),
    Journey(
        name="q at the disc prompt, then carry on from the tools folder",
        archive="two sets",
        typed=(NEWEST, Pick(PHOTOS), Password(PHOTOS)),
        quit_at_disc=1,
        expect=Expect(exit=ScriptExit.READER_QUIT,
                      asked=("newest data disc", "which set", "folder",
                             "password"),
                      tools_folder="kept", disc_asked=True),
        differs={"bat": Differs(because=("menu by number",))},
        then=Leg(typed=(Password(PHOTOS), "n"),
                 expect=Expect(exit=ScriptExit.OK, asked=("password", "another set"),
                               restored=PHOTOS, tools_folder="removed",
                               disc_asked=True)),
    ),
    # #949: the person does not have the disc tier 1 asks for and types
    # s.  The files on the discs in reach are restored, the rest listed,
    # exit 4, and the end names the skipped disc; the tools folder stays,
    # and carrying on later with the disc in restores the rest.
    Journey(
        name="s at the disc prompt, then carry on with the disc",
        archive="two sets",
        typed=(NEWEST, Pick(PHOTOS), Password(PHOTOS)),
        skip_at_disc=1,
        expect=Expect(exit=ScriptExit.NOT_ALL_RESTORED,
                      asked=("newest data disc", "which set", "folder",
                             "password"),
                      tools_folder="kept", disc_asked=True,
                      says=(r"Skipped discs: LCSAS_\w+",
                            r"type s at its\s+question")),
        differs={"bat": Differs(because=("menu by number",))},
        then=Leg(typed=(Password(PHOTOS), "n"),
                 expect=Expect(exit=ScriptExit.OK, asked=("password", "another set"),
                               restored=PHOTOS, tools_folder="removed",
                               disc_asked=True)),
    ),
    Journey(
        name="not enough space: the question, answered yes",
        archive="one set, catalog sizes huge",
        typed=(NEWEST, "y", Password(PHOTOS)),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "folder", "space",
                             "password"),
                      restored=PHOTOS, tools_folder="removed"),
    ),
    Journey(
        name="a set too big to count: the space question, answered no",
        archive="one set, catalog sizes in petabytes",
        typed=(NEWEST, "n"),
        expect=Expect(exit=ScriptExit.FAILED, asked=("newest data disc", "folder", "space"),
                      says=(r"nothing was restored",)),
        differs={"bat": Differs(because=("too big to count",),
                                says=(r"nothing was restored",
                                      r"cannot check the free space"))},
    ),
    Journey(
        name="some items not restored: exit 4, the failures file named",
        archive="one set, a pack damaged",
        typed=(NEWEST, Password(PHOTOS)),
        expect=Expect(exit=ScriptExit.NOT_ALL_RESTORED,
                      asked=("newest data disc", "folder", "password"),
                      tools_folder="removed",
                      says=(r"could not be restored", r"RESTORE_FAILURES\.txt")),
    ),
    Journey(
        name="--backup-set with an unknown name",
        archive="one set",
        args=("--backup-set", UNKNOWN_SET),
        typed=(NEWEST,),
        expect=Expect(exit=ScriptExit.FAILED, asked=("newest data disc",),
                      says=(r"not among", re.escape(PHOTOS))),
    ),
    Journey(
        name="--backup-set picks a set by name",
        archive="two sets",
        args=("--backup-set", PHOTOS),
        typed=(NEWEST, Password(PHOTOS), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "folder", "password",
                             "another set"),
                      restored=PHOTOS, tools_folder="removed"),
    ),
    Journey(
        name="an unknown argument is a usage error",
        archive="one set",
        args=("--bogus",),
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(), says=(r"--bogus",)),
    ),
    Journey(
        name="--repo was renamed (#934)",
        archive="one set",
        args=("--repo", PHOTOS),
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(),
                      says=(r"--repo was renamed: use --backup-set",)),
    ),
    Journey(
        name="--target was renamed (#934)",
        archive="one set",
        args=("--target", "restored"),
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(),
                      says=(r"--target was renamed: use --restore-folder",)),
    ),
    Journey(
        name="LCSAS_REPO was renamed (#934)",
        archive="one set",
        env={"LCSAS_REPO": PHOTOS},
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(),
                      says=(r"LCSAS_REPO was renamed: use LCSAS_BACKUP_SET",)),
    ),
    Journey(
        name="LCSAS_PWFILE was renamed (#934)",
        archive="one set",
        env={"LCSAS_PWFILE": "pw.txt"},
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(),
                      says=(r"LCSAS_PWFILE was renamed: use LCSAS_PASSWORD_FILE",)),
    ),
    Journey(
        name="LCSAS_BACKUP_SET with an unknown name",
        archive="one set",
        env={"LCSAS_BACKUP_SET": UNKNOWN_SET},
        typed=(NEWEST,),
        expect=Expect(exit=ScriptExit.FAILED, asked=("newest data disc",),
                      says=(r"not among", re.escape(PHOTOS))),
    ),
    # #953: the second pass.  "Restore another backup set?" answered y,
    # a folder of its own for the second set: both sets restored.
    Journey(
        name="another set into a folder of its own",
        archive="two sets",
        typed=(NEWEST, Pick(PHOTOS), Folder(1), Password(PHOTOS), "y",
               Folder(2), Pick(DOCS), Card(DOCS, 1), Card(DOCS, 3), "n"),
        expect=Expect(exit=ScriptExit.OK,
                      asked=("newest data disc", "which set", "folder",
                             "password", "another set", "folder", "which set",
                             "card 1", "card 2", "another set"),
                      restored=(PHOTOS, DOCS), tools_folder="removed"),
        differs={"bat": Differs(
            because=("menu by number", "folder before the set"),
            asked=("newest data disc", "which set", "folder", "password",
                   "another set", "which set", "folder", "card 1", "card 2",
                   "another set"),
            typed=(NEWEST, Pick(PHOTOS), Folder(1), Password(PHOTOS), "y",
                   Pick(DOCS), Folder(2), Card(DOCS, 1), Card(DOCS, 3), "n"))},
    ),
    # #953: the second set typed into the first set's folder: both
    # scripts warn and ask; anything but YES stops with 65, and the
    # first set's files are as they were.
    Journey(
        name="another set into the first set's folder: warned, no stops",
        archive="two sets",
        typed=(NEWEST, Pick(PHOTOS), Folder(1), Password(PHOTOS), "y",
               Folder(1), Pick(DOCS), "n"),
        expect=Expect(exit=ScriptExit.USER_ABORT,
                      asked=("newest data disc", "which set", "folder",
                             "password", "another set", "folder", "which set",
                             "overwrite"),
                      restored=PHOTOS,
                      says=(r"already holds the restore of ANOTHER backup set",
                            r"Aborting; nothing was written")),
        differs={"bat": Differs(
            because=("menu by number", "folder before the set"),
            asked=("newest data disc", "which set", "folder", "password",
                   "another set", "which set", "folder", "overwrite"),
            typed=(NEWEST, Pick(PHOTOS), Folder(1), Password(PHOTOS), "y",
                   Pick(DOCS), Folder(1), "n"))},
    ),
    # #953: ENTER with nothing typed at the password.
    Journey(
        name="ENTER three times at the password",
        archive="one set",
        typed=(NEWEST, "", "", "", "q"),
        expect=Expect(exit=ScriptExit.WRONG_PASSWORD,
                      asked=("newest data disc", "folder", "password",
                             "password", "password", "password")),
        differs={"bat": Differs(
            because=("empty password",),
            asked=("newest data disc", "folder", "password", "password",
                   "password"),
            typed=(NEWEST, "", "", ""))},
    ),
    # #953: restore.sh refused an id that is no hex only after every
    # question; both refuse it now, at once, as restore.bat did.
    Journey(
        name="--snapshot with an id that is not one",
        archive="one set",
        args=("--snapshot", "zzzz"),
        typed=(),
        expect=Expect(exit=ScriptExit.USAGE, asked=(),
                      says=(r"--snapshot needs a snapshot id",)),
    ),
    # #953: restore.bat did not know --password-file.
    Journey(
        name="--password-file names the password file",
        archive="one set",
        args=("--password-file", PasswordFile(PHOTOS)),
        typed=(NEWEST,),
        expect=Expect(exit=ScriptExit.OK, asked=("newest data disc", "folder"),
                      restored=PHOTOS, tools_folder="removed"),
    ),
    Journey(
        name="--version",
        archive="one set",
        args=("--version",),
        typed=(),
        expect=Expect(exit=ScriptExit.OK, asked=(), says=(r"lcsas-restore\.sh ",)),
        differs={"bat": Differs(because=("version",), exit=ScriptExit.USAGE,
                                says=(r'does not know the argument "--version"',))},
    ),
    Journey(
        name="--snapshot names the snapshot",
        archive="one set",
        args=("--snapshot", Snapshot(PHOTOS)),
        typed=(NEWEST, Password(PHOTOS)),
        expect=Expect(exit=ScriptExit.OK, asked=("newest data disc", "folder", "password"),
                      restored=PHOTOS, tools_folder="removed"),
    ),
)
