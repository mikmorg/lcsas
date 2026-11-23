"""#956: a repository password changed after it was split.

rustic's ``key add`` + ``key remove`` changes the password of a
repository; its master key stays.  New discs then carry only the new key
file, while their text still said the password "is SPLIT into N share
cards", and those cards rebuild the old password, which the newest disc
rejects.  Nothing warned.

Now ``password split`` records the key file the split password opens
(``repositories.encryption_key_id``), and ``stage`` refuses while a
recorded split's key file is no longer in the mirror, until the new
password is split or the record is cleared (owner decision: new discs do
not carry superseded key files).  With no split recorded, ``stage`` warns
once that the password changed since the repository was registered.

Real rustic, real xorriso, every owner command through ``main()``, and
the heir's side through the real ``restore.sh`` of a META disc.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_REFUSAL = "was changed after it was split"


def _install(root: Path, *, split: bool) -> ScratchInstall:
    """alpha backed up once and registered; split = true when *split*."""
    inst = ScratchInstall(root, repos=("alpha",))
    if split:
        text = inst.config.read_text()
        inst.config.write_text(text.replace(
            "[repos.alpha]\n",
            "[repos.alpha]\nsplit = true\nsplit_threshold = 2\n"
            "split_shares = 3\n",
        ))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def _key_files(inst: ScratchInstall) -> set[str]:
    return {p.name for p in (inst.mirror("alpha") / "keys").iterdir()}


def _change_password(inst: ScratchInstall, new: str) -> str:
    """The owner's password change: key add, then key remove the old key.
    Returns the new key file's name."""
    (old,) = _key_files(inst)
    new_file = inst.root / "tmp" / "new.key"
    new_file.write_text(new + "\n")
    inst.rustic("alpha", "key", "add", "--new-password-file", str(new_file))
    inst.key("alpha").write_text(new + "\n")
    inst.rustic("alpha", "key", "remove", old)
    (fresh,) = _key_files(inst)
    assert fresh != old
    return fresh


def _split(inst: ScratchInstall, out: Path) -> None:
    assert inst.lcsas("password", "split", "--repo", "alpha", "-k", "2",
                      "-n", "3", "--out", str(out)) == 0


def _stage_and_burn(inst: ScratchInstall) -> str:
    assert inst.lcsas("stage") == 0
    session = inst.sessions()[-1]
    inst.mark_burned(session)
    return session


def test_split_records_the_key_file_the_password_opens(tmp_path: Path) -> None:
    inst = _install(tmp_path / "x", split=True)
    (only,) = _key_files(inst)
    # As if `repo add` had recorded nothing (an empty keys/ at the time).
    conn = sqlite3.connect(inst.db)
    conn.execute("UPDATE repositories SET encryption_key_id = ''")
    conn.commit()
    conn.close()
    _split(inst, inst.root / "cards")
    (row,) = inst.query("SELECT encryption_key_id FROM repositories")
    assert row[0] == only


def test_stage_refuses_until_the_new_password_is_split(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x", split=True)
    _split(inst, inst.root / "cards1")
    _stage_and_burn(inst)

    new_key = _change_password(inst, "scratch-password-alpha-changed")
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0

    for argv in (("stage", "--dry-run"), ("stage",),
                 ("stage", "--allow-split-drift")):
        before = inst.snapshot()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert inst.lcsas(*argv) == 1, argv
        assert _REFUSAL in caplog.text, argv
        assert "lcsas password split --repo alpha" in caplog.text
        assert "lcsas password unsplit --repo alpha" in caplog.text
        assert "Unexpected error" not in caplog.text
        assert "--allow-split-drift to override" not in caplog.text
        assert inst.snapshot() == before, "a refused stage changed something"

    # The new password split: staged, and the new cards open the new disc.
    cards = inst.root / "cards2"
    _split(inst, cards)
    (row,) = inst.query("SELECT encryption_key_id FROM repositories")
    assert row[0] == new_key
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    assert _REFUSAL not in caplog.text
    rebuilt = inst.root / "rebuilt.key"
    assert inst.lcsas(
        "password", "combine",
        "--share-file", str(cards / "alpha-share-1-card.txt"),
        "--share-file", str(cards / "alpha-share-3-card.txt"),
        "--out", str(rebuilt),
    ) == 0
    out = inst.root / "restored"
    run = inst.heir_restore("alpha", out, password_file=rebuilt)
    assert run.returncode == 0, run.stdout + run.stderr
    restored = tree_digest(out)
    expected = tree_digest(inst.data("alpha"))
    assert {k.rsplit("/", 1)[-1]: v for k, v in restored.items()
            if k.endswith(".bin")} == expected, (restored, expected)


def test_unsplit_clears_the_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x", split=True)
    _split(inst, inst.root / "cards1")
    _stage_and_burn(inst)
    _change_password(inst, "scratch-password-alpha-changed")
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 1

    assert inst.lcsas("password", "unsplit", "--repo", "alpha", "--yes") == 0
    inst.config.write_text(inst.config.read_text().replace(
        "split = true\n", "split = false\n"))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    assert _REFUSAL not in caplog.text


def test_no_split_warns_once_and_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x", split=False)
    _stage_and_burn(inst)
    new_key = _change_password(inst, "scratch-password-alpha-changed")
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0

    warning = "password of 'alpha' changed since"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 0
    assert warning in caplog.text
    (row,) = inst.query("SELECT encryption_key_id FROM repositories")
    assert row[0] != new_key, "a dry run wrote to the catalog"

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _stage_and_burn(inst)
    assert warning in caplog.text
    (row,) = inst.query("SELECT encryption_key_id FROM repositories")
    assert row[0] == new_key

    inst.backup("alpha", "f3.bin")
    assert inst.lcsas("scan") == 0
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    assert warning not in caplog.text


def test_verify_says_the_password_does_not_open_the_repository(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x", split=False)
    wrong = inst.root / "wrong.key"
    wrong.write_text("not-the-password\n")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("password", "verify", "--repo", "alpha",
                          "--password-file", str(wrong)) == 1
    assert "does NOT unlock repository 'alpha'" in caplog.text
    assert "about to split" not in caplog.text
    assert "--no-verify-repo" not in caplog.text


def test_record_existing_clears_a_refusal_from_an_older_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A split recorded by an earlier build kept the key file of `repo add`.
    When the password had been changed before that split, stage takes the
    split for one made before the change; recording the split again from
    K of its cards, which open the current key file, clears that."""
    inst = _install(tmp_path / "x", split=True)
    (old_key,) = _key_files(inst)
    _change_password(inst, "scratch-password-alpha-changed")
    cards = inst.root / "cards"
    _split(inst, cards)
    # As an earlier build left it: the key file of `repo add`.
    conn = sqlite3.connect(inst.db)
    conn.execute("UPDATE repositories SET encryption_key_id = ?", (old_key,))
    conn.commit()
    conn.close()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 1
    assert _REFUSAL in caplog.text

    assert inst.lcsas(
        "password", "split", "--repo", "alpha", "--record-existing",
        "--share-file", str(cards / "alpha-share-1-card.txt"),
        "--share-file", str(cards / "alpha-share-2-card.txt"),
    ) == 0
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    assert _REFUSAL not in caplog.text
