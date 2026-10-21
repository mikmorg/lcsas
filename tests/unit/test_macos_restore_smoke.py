"""The parsing half of tests/e2e/macos_restore_smoke.py, against real prompt text.

macos-e2e.yml only runs on a PR that touches restore.sh, the meta builder or the
harness itself, and then on a macOS runner.  If the swap prompt printed by
recovery/src/lcsas-restore/disc_locator.c changed wording, the harness would
find out there, late.  These pin the strings it keys on, using a prompt box
captured from a macos-14 run, and the manifest comparison that decides pass/fail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "macos_restore_smoke.py"

_spec = importlib.util.spec_from_file_location("macos_restore_smoke", SCRIPT)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules["macos_restore_smoke"] = smoke
_spec.loader.exec_module(smoke)

LABELS = [f"LCSAS_TEST_TINY_2026_000{n}" for n in range(1, 5)]

# Captured from a macos-14 run (paths shortened).  Hint-less first prompt, then
# one that names the volume.
BOX_NO_HINT = """\
+----------------------------------------------------------+
| Pack c3e2247d2646d9f7... is required for the next file.       |
| (catalog has the pack, but no current volume mapping)    |
|                                                          |
| Currently searching:                                     |
|   /Volumes/LCSAS_TEST_TINY_2026_0001                     |
|   /Volumes                                               |
|                                                          |
| Single-drive recovery: if your machine has only ONE      |
| optical drive, eject the RECOVERY disc first, then       |
| insert the disc named above into the SAME drive.         |
|                                                          |
| Insert the right disc and press ENTER to retry.          |
| Type 'q' then ENTER to abort.                            |
| (A disc that mounts by itself is found without ENTER.)   |
+----------------------------------------------------------+
> """

BOX_HINT = BOX_NO_HINT.replace(
    "| (catalog has the pack, but no current volume mapping)    |\n",
    "| It lives on volume(s):                                   |\n"
    "|   LCSAS_TEST_TINY_2026_0003                              |\n",
).replace("c3e2247d2646d9f7", "3895a5111808328e")


def test_prompt_text_checks_accept_the_real_box() -> None:
    smoke.check_prompt_text(smoke.last_box("noise\n" + BOX_NO_HINT))


def test_prompt_text_checks_reject_a_reworded_box() -> None:
    reworded = BOX_NO_HINT.replace("press ENTER to retry", "hit return")
    with pytest.raises(smoke.CheckError):
        smoke.check_prompt_text(reworded)


def test_last_box_starts_at_the_pack_line_and_ends_at_the_prompt() -> None:
    text = "before\n" + BOX_NO_HINT + "\n(still not found -- try again)\n"
    box = smoke.last_box(text)
    assert box.startswith("| Pack c3e2247d2646d9f7...")
    assert box.endswith("+\n> ")  # the closing rule and the "> " tier 1 waits behind


def test_labels_named_reads_only_the_lives_on_section() -> None:
    # The "Currently searching" list names attached volumes; those must not
    # be mistaken for the disc the pack lives on.
    assert smoke.labels_named(BOX_NO_HINT, LABELS) == []
    assert smoke.labels_named(BOX_HINT, LABELS) == [LABELS[2]]


def test_right_label_prefers_the_hint_then_the_pack_map() -> None:
    free = LABELS[1:]
    packmap = {LABELS[3]: ["c3e2247d2646d9f7" + "0" * 48], LABELS[1]: ["ab" * 32]}
    assert smoke.right_label(BOX_HINT, [LABELS[2]], free, packmap) == LABELS[2]
    assert smoke.right_label(BOX_NO_HINT, [], free, packmap) == LABELS[3]
    # A disc that is already attached is never "the right one" again.
    assert smoke.right_label(BOX_NO_HINT, [], LABELS[1:3], packmap) is None
    assert smoke.right_label(BOX_NO_HINT, [], free, {}) is None


def test_at_prompt_needs_a_prompt_printed_after_the_offset() -> None:
    assert smoke.at_prompt(BOX_NO_HINT, 0)
    assert not smoke.at_prompt(BOX_NO_HINT, len(BOX_NO_HINT))  # an answered prompt
    again = BOX_NO_HINT + "\n(still not found -- check the disc label and try again)\n> "
    assert smoke.at_prompt(again, len(BOX_NO_HINT))  # the one-line re-prompt
    assert not smoke.at_prompt(BOX_NO_HINT + "\nprogress: 15/61 blobs", 0)  # not waiting


def test_continuing_marker_matches_what_tier1_prints() -> None:
    assert smoke.CONTINUING_RE.search("[lcsas-restore] found c3e2247d2646d9f7...; continuing.")
    assert not smoke.CONTINUING_RE.search("found c3e2247d...; continuing.")


def test_compare_tree_reports_missing_mismatched_and_extra(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"alpha")
    (tmp_path / "b.bin").write_bytes(b"changed")
    (tmp_path / "extra.bin").write_bytes(b"?")
    manifest = {
        "a.bin": smoke.sha256(tmp_path / "a.bin"),
        "b.bin": "0" * 64,
        "gone.bin": "1" * 64,
    }
    assert smoke.compare_tree(tmp_path, manifest) == [
        "HASH MISMATCH: b.bin",
        "MISSING: gone.bin",
        "EXTRA: extra.bin",
    ]
    (tmp_path / "extra.bin").unlink()
    manifest["b.bin"] = smoke.sha256(tmp_path / "b.bin")
    del manifest["gone.bin"]
    assert smoke.compare_tree(tmp_path, manifest) == []
