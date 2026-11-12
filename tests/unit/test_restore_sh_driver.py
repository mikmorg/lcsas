"""#756: the root-only restore.sh drivers stay in step with restore.sh.

``tests/integration/test_interactive_restore.py`` and
``scripts/smoke_single_drive.py`` need root (mounts, cdemu), so they
self-skip in CI, and they rotted once: they passed ``--drive`` and
``--work-dir`` after #689 retired them, and waited for prompts nothing
prints any more.  Both now build their command line and read the
prompts with ``tests/e2e/restore_sh_driver.py``.  This checks that
module against ``restore.sh`` and tier 1's source, and runs its
pseudoterminal loop against a fake restore.sh, with no root.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

from tests.e2e import restore_sh_driver as drv

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVERS = (
    REPO_ROOT / "tests" / "integration" / "test_interactive_restore.py",
    REPO_ROOT / "scripts" / "smoke_single_drive.py",
)
TIER1_MAIN = REPO_ROOT / "recovery" / "src" / "lcsas-restore" / "main.c"


# ---------------------------------------------------------------------------
# The interface, read from the source
# ---------------------------------------------------------------------------


def test_valid_flags_line_is_read():
    flags = drv.restore_sh_valid_flags()
    assert {"--repo", "--target", "--key"} <= flags
    assert "--drive" not in flags and "--work-dir" not in flags


def test_command_passes_only_valid_flags(tmp_path):
    cmd = drv.build_restore_cmd(tmp_path, key=tmp_path / "k",
                                target=tmp_path / "t", repo="photos")
    passed = {a for a in cmd if a.startswith("--")}
    assert passed <= drv.restore_sh_valid_flags()
    assert cmd[:2] == ["sh", str(tmp_path / "restore.sh")]


def test_env_names_what_restore_sh_reads(tmp_path):
    env = drv.build_restore_env(home=tmp_path, tools_dir=tmp_path / "t",
                                mount_dir=tmp_path / "m",
                                meta_disc=tmp_path / "meta",
                                tmpdir=tmp_path / "tmp")
    text = drv.RESTORE_SH_SOURCE.read_text(encoding="utf-8")
    for name in env:
        if name.startswith("LCSAS_"):
            assert f"${{{name}" in text or f"${name}" in text, name


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda p: p.name)
def test_drivers_use_the_shared_command(driver):
    """No driver builds its own restore.sh command line, and none passes
    a flag restore.sh rejects."""
    text = driver.read_text(encoding="utf-8")
    assert "build_restore_cmd(" in text
    assert "drive_restore(" in text
    valid = drv.restore_sh_valid_flags()
    for retired in ("--drive", "--work-dir"):
        assert retired not in valid
        assert f'"{retired}"' not in text
    assert "INSERT DISC" not in text  # the prompt nothing prints now


def _box_line_in_c(path: Path, text: str) -> bool:
    """True when *path* prints *text* as a line of tier 1's framed box:
    a C string starting ``"| <text>``."""
    src = path.read_text(encoding="utf-8")
    return re.search(r'"\|\s+' + re.escape(text), src) is not None


@pytest.mark.parametrize("needle", [
    drv.TIER1_LABELS_LINE, drv.TIER1_NO_RECORD_LINE,
    drv.TIER1_RETRY_LINE, drv.TIER1_ABORT_LINE,
])
def test_tier1_prompt_text_is_real(needle):
    assert _box_line_in_c(drv.DISC_LOCATOR_SOURCE, needle.decode())


def test_tier1_done_and_quit_code_are_real():
    main_c = TIER1_MAIN.read_text(encoding="utf-8")
    assert drv.TIER1_DONE.decode() in main_c
    assert f"#define EXIT_READER_QUIT {drv.EXIT_DISC_PROMPT_QUIT}" in main_c


@pytest.mark.parametrize("needle", [
    drv.NEWEST_DISC_PROMPT, drv.ANOTHER_SET_PROMPT, drv.RAM_TARGET_PROMPT,
])
def test_restore_sh_prompt_text_is_real(needle):
    text = drv.RESTORE_SH_SOURCE.read_text(encoding="utf-8")
    # Printed with printf, so the text sits in the source as it is shown.
    assert needle.decode() + "'" in text


def test_quit_exit_code_is_documented():
    text = drv.RESTORE_SH_SOURCE.read_text(encoding="utf-8")
    assert re.search(rf"#\s+{drv.EXIT_DISC_PROMPT_QUIT}\s+the reader typed q "
                     r"at the disc prompt of tier 1", text)
    assert f"$tier1_rc -eq {drv.EXIT_DISC_PROMPT_QUIT}" in text


# ---------------------------------------------------------------------------
# Reading the prompts
# ---------------------------------------------------------------------------


def _box(*labels: str, auto: bool = True, no_record: bool = False) -> bytes:
    """A tier 1 disc question as print_prompt writes it, through a pty
    (\\r\\n line ends)."""
    rows = ["", "+" + "-" * 58 + "+",
            "| Pack 0123456789abcdef... is required for the next file.  |"]
    if labels:
        rows.append("| It is on any disc labelled:                              |")
        rows += [f"|   {lab:<54} |" for lab in labels]
    elif no_record:
        rows.append("| (catalog has no record of this pack ID)                  |")
    rows += ["|" + " " * 58 + "|",
             "| Insert the right disc and press ENTER to retry.          |",
             "| Type 'q' then ENTER to abort.                            |"]
    if auto:
        rows.append("| (A disc that mounts by itself is found without ENTER.)   |")
    rows.append("+" + "-" * 58 + "+")
    return ("\r\n".join(rows) + "\r\n> ").encode()


def test_scanner_reads_a_disc_question():
    sc = drv.PromptScanner()
    prompts = sc.feed(_box("LCSAS_TEST_TINY_2026_0002", "LCSAS_TEST_TINY_2026_0003"))
    assert [(p.kind, p.labels) for p in prompts] == [
        ("disc", ["LCSAS_TEST_TINY_2026_0002", "LCSAS_TEST_TINY_2026_0003"]),
    ]


def test_scanner_waits_for_the_whole_box():
    sc = drv.PromptScanner()
    box = _box("VOL_A", auto=False)
    assert sc.feed(box[:-30]) == []
    prompts = sc.feed(box[-30:])
    assert [(p.kind, p.labels) for p in prompts] == [("disc", ["VOL_A"])]


def test_scanner_disc_question_without_labels():
    prompts = drv.PromptScanner().feed(_box(no_record=True))
    assert [(p.kind, p.labels) for p in prompts] == [("disc", [])]


def test_scanner_reads_restore_sh_questions_in_order():
    out = (b"...\r\nENTER, S or Q: " + _box("VOL_B")
           + b"\r\nRestore another backup set from these discs? [y/N] ")
    prompts = drv.PromptScanner().feed(out)
    assert [p.kind for p in prompts] == ["newest", "disc", "another"]


# ---------------------------------------------------------------------------
# The pseudoterminal loop, against a fake restore.sh
# ---------------------------------------------------------------------------


def _fake_restore_sh(path: Path, box_vol_b: bytes) -> None:
    """Asks step 0's question, then tier 1's for VOL_B until the answer
    file says VOL_B is in, then exits 0; q exits 75."""
    box = box_vol_b.decode().replace("\r\n", "\n")
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf 'ENTER, S or Q: ' >&2
        read -r ans
        while :; do
            cat >&2 <<'BOX'
        {textwrap.indent(box, "        ").lstrip()}
        BOX
            read -r ans
            [ "$ans" = q ] && exit 75
            [ "$(cat "$DRIVE")" = VOL_B ] && break
        done
        echo '[lcsas-restore] restore complete' >&2
        exit 0
        """))
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform != "linux", reason="pty driver is POSIX")
def test_drive_restore_answers_each_question(tmp_path):
    fake = tmp_path / "restore.sh"
    _fake_restore_sh(fake, _box("VOL_B", auto=False))
    drive = tmp_path / "drive"
    drive.write_text("")
    seen: list[tuple[str, list[str]]] = []

    def answer(prompt, n):
        seen.append((prompt.kind, prompt.labels))
        if prompt.kind == "disc":
            # The first time, the wrong disc; then the one asked for.
            drive.write_text("VOL_A" if n == 2 else prompt.labels[0])
        return None

    rc, out = drv.drive_restore(["sh", str(fake)],
                                {"PATH": "/usr/bin:/bin", "DRIVE": str(drive)},
                                answer, timeout=30)
    assert rc == 0, out
    assert seen == [("newest", []), ("disc", ["VOL_B"]), ("disc", ["VOL_B"])]
    assert drv.disc_prompts(out) == 2
    assert drv.TIER1_DONE.decode() in out


@pytest.mark.skipif(sys.platform != "linux", reason="pty driver is POSIX")
def test_drive_restore_quit_gives_75(tmp_path):
    fake = tmp_path / "restore.sh"
    _fake_restore_sh(fake, _box("VOL_B", auto=False))
    drive = tmp_path / "drive"
    drive.write_text("")

    def answer(prompt, n):
        return drv.QUIT if prompt.kind == "disc" else None

    rc, out = drv.drive_restore(["sh", str(fake)],
                                {"PATH": "/usr/bin:/bin", "DRIVE": str(drive)},
                                answer, timeout=30)
    assert rc == drv.EXIT_DISC_PROMPT_QUIT, out
