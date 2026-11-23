"""#967 item 4: the passwords an heir can type at restore.sh's prompt.

One real rustic set holds a key for each password below (``rustic key
add``, before the stage, so every disc's copy of its keys has them all).
Each password is typed at the real ``restore.sh``'s hidden prompt under a
pseudoterminal, as a person types it, and tier 1 must restore the set.

The table records what restore.sh does with each.  Two are not accepted,
and that is deliberate:

- **empty**: an empty line is no password.  restore.sh says "No password
  typed." and asks again.
- **ending in CR**: a terminal turns the CR typed into ENTER (``ICRNL``),
  so the CR never reaches the password: what arrives is the password
  without it, which the key rejects ("That password was not accepted").
  (Not tested here: whether such a set can be restored another way; a
  password file's trailing CR is stripped too, #641.)

restore.bat's half of the table is windows-e2e's typed-password leg
(``!``, ``%`` and ``^`` on real cmd; wine's cmd is not the verdict).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.contract.journeys import _ShScanner
from tests.e2e.restore_sh_driver import Prompt, build_restore_env, drive_restore
from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

RESTORED = "restored"
EMPTY = "asked again: no password typed"
NOT_ACCEPTED = "asked again: not accepted"

#: (name, the password typed, what restore.sh does).
VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("plain", "correct-horse-battery-staple", RESTORED),
    ("spaces inside", "two words  three", RESTORED),
    ("leading space", " leading", RESTORED),
    ("trailing space", "trailing ", RESTORED),
    ("shell characters", "a$HOME`id`$(id);|&<>", RESTORED),
    ("quotes and backslashes", "q\"u'o\\t\\\\e", RESTORED),
    ("cmd characters", "a!b!TEMP!%PATH%^c^^", RESTORED),
    ("glob characters", "*?[ab]~", RESTORED),
    ("dash first", "-n -e", RESTORED),
    ("not ASCII", "pässwörd-日本", RESTORED),
    ("empty", "", EMPTY),
    ("ending in CR", "carriage\r", NOT_ACCEPTED),
)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> tuple[ScratchInstall, Path]:
    """The set ``alpha``, a key per password, staged and burned; the
    discs and a META disc built from this checkout in a mount folder."""
    from lcsas.meta.builder import MetaVolumeBuilder

    root = tmp_path_factory.mktemp("typed")
    inst = ScratchInstall(root / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    for _name, password, _what in VARIANTS:
        if password:
            inst.rustic("alpha", "key", "add", f"--new-password={password}")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    inst.mark_burned(session)
    mnt = root / "mnt"
    mnt.mkdir()
    for label, tree in sorted(inst.disc_trees().items()):
        shutil.copytree(tree, mnt / label)
    MetaVolumeBuilder(mnt / "LCSAS_META", catalog_db_path=None,
                      allow_no_dvdisaster_source=True).build()
    return inst, mnt


def _typed(archive: tuple[ScratchInstall, Path], work: Path,
           password: str) -> tuple[int, str, int, Path]:
    """Type *password* at the prompt (then Q at any second prompt).
    Returns the exit code, the output, the prompts and the folder."""
    _inst, mnt = archive
    for d in ("home", "tmp"):
        (work / d).mkdir(parents=True)
    out = work / "restored"
    env = build_restore_env(home=work / "home", tools_dir=work / "tools",
                            mount_dir=mnt, meta_disc=mnt / "LCSAS_META",
                            tmpdir=work / "tmp")
    env.update({"LCSAS_BACKUP_SET": "alpha", "LCSAS_NEWEST_DISC_PROMPT": "0",
                "LCSAS_ANOTHER_SET": "0"})
    asked = [0]

    def answer(prompt: Prompt, _n: int) -> bytes:
        if prompt.kind == "password":
            asked[0] += 1
            return (password.encode() + b"\n") if asked[0] == 1 else b"q\n"
        if prompt.kind == "ram":
            return b"y\n"
        return b"q\n"

    rc, output = drive_restore(
        ["sh", str(mnt / "LCSAS_META" / "restore.sh"), "--restore-folder", str(out)],
        env, answer, timeout=300, scanner=_ShScanner())
    return rc, output, asked[0], out


@pytest.mark.parametrize(("password", "what"),
                         [pytest.param(p, w, id=n) for n, p, w in VARIANTS])
def test_a_typed_password(archive: tuple[ScratchInstall, Path], tmp_path: Path,
                          password: str, what: str) -> None:
    inst, _mnt = archive
    rc, output, asked, out = _typed(archive, tmp_path, password)
    if what == RESTORED:
        assert rc == 0, output
        assert asked == 1, output
        assert tree_digest(out / "alpha") == tree_digest(inst.data("alpha")), output
        return
    # Asked again, then Q: exit 77, nothing restored.
    assert rc == 77, output
    assert asked >= 2, output
    assert not (out / "alpha").exists(), output
    if what == EMPTY:
        assert "No password typed." in output, output
    else:
        assert "That password was not accepted for alpha." in output, output


def test_every_password_has_its_key(archive: tuple[ScratchInstall, Path]) -> None:
    """The table is not vacuous: each non-empty password opens the set
    by itself (rustic, on the owner's mirror)."""
    inst, _mnt = archive
    for _name, password, _what in VARIANTS:
        if not password:
            continue
        res = subprocess.run(
            ["rustic", "-r", str(inst.mirror("alpha")), "--no-cache",
             f"--password={password}", "snapshots"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, (password, res.stderr)
