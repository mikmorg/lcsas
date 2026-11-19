"""The restore journeys' table (#895), checked without running a script.

``tests/recovery_hardening/test_journeys.py`` runs each journey through
restore.sh and restore.bat; it needs rustic, xorriso and wine.  This
checks, on every run, what it relies on: each difference a journey
records is named and explained, and the prompt table finds each
script's questions in the order they are printed.
"""
from __future__ import annotations

from tests.contract.journeys import (
    DIFFERENCES,
    JOURNEYS,
    PROMPTS,
    box_labels,
    find_prompt,
)


def test_every_difference_is_named_and_explained() -> None:
    """A journey's ``differs`` names only DIFFERENCES entries, each with
    what differs and why; and every entry is used by some journey."""
    used: set[str] = set()
    for j in JOURNEYS:
        assert set(j.differs) <= {"sh", "bat"}, j.name
        for d in j.differs.values():
            assert d.because, f"{j.name}: a difference with no reason"
            unknown = set(d.because) - set(DIFFERENCES)
            assert not unknown, f"{j.name}: {sorted(unknown)} not in DIFFERENCES"
            used |= set(d.because)
    for key, diff in DIFFERENCES.items():
        assert diff.what and diff.why, key
    assert used == set(DIFFERENCES), f"unused: {sorted(set(DIFFERENCES) - used)}"


def test_journey_names_are_unique_and_typed_lines_cover_the_questions() -> None:
    names = [j.name for j in JOURNEYS]
    assert len(names) == len(set(names))
    driver_keys = {p.key for p in PROMPTS if p.by_driver}
    for j in JOURNEYS:
        for script in ("sh", "bat"):
            asked = [k for k in j.expect_for(script).asked if k not in driver_keys]
            # One typed line per question the person answers.
            assert len(asked) == len(j.typed), (j.name, script, asked)


def _keys(text: str, script: str) -> list[str]:
    keys, pos = [], 0
    while (hit := find_prompt(text, pos, script)) is not None:
        keys.append(hit.key)
        pos = hit.end
    return keys


def test_find_prompt_reads_restore_sh_questions_in_order() -> None:
    text = (
        "  Press ENTER when it is in; S then ENTER to skip\r\n"
        "ENTER, S or Q: \r\n"
        "Multiple repositories on this archive:\r\n  1) a\r\n  2) b\r\n"
        "Choose a repository (number or name): a\r\n"
        "Type the password for a, then ENTER (Q then ENTER to quit).\r\n"
        "Password: \r\n"
        "Share card 2 of 2: type all the words on one line, then ENTER "
        "(or Q to quit).\r\n> words\r\n"
        "\r\nRestore another backup set from these discs? [y/N] "
    )
    assert _keys(text, "sh") == ["newest data disc", "which set", "password",
                                 "card 2", "another set"]


def test_find_prompt_reads_restore_bat_questions_in_order() -> None:
    text = (
        "Newest data disc: \r\n"
        "Which one do you want to restore? [1]: \r\n"
        "Restore to which folder? [C:\\Users\\me\\Documents\\restored]: \r\n"
        "Words of card 1: \r\n"
        "Password: \r\n"
        "Restore another backup set from these discs? [y/N]: \r\n"
        "Press any key to continue . . . "
    )
    assert _keys(text, "bat") == ["newest data disc", "which set", "folder",
                                  "card 1", "password", "another set", "pause"]
    # "Password: " inside a word is not the question.
    assert _keys("the Repository password: is wrong", "bat") == []


def _box(labels: list[str], note: bool = False) -> str:
    """Tier 1's disc question, as disc_locator.c prints it."""
    frame = "+" + "-" * 58 + "+"
    lines = ["", frame, "| Pack 0123456789abcdef... is required for the next file.  |"]
    if labels:
        lines.append("| It is on any disc labelled:                              |")
        lines += [f"|   {lab:<54} |" for lab in labels]
    lines += ["|                                                          |",
              "| Insert the right disc and press ENTER to retry.          |",
              "| Type 'q' then ENTER to abort.                            |"]
    if note:
        lines.append("| (A disc that mounts by itself is found without ENTER.)   |")
    lines += [frame, "> "]
    return "\r\n".join(lines)


def test_tier1_box_is_one_question_with_its_labels() -> None:
    for note in (False, True):
        text = "before\r\n" + _box(["LCSAS_TEST_TINY_2026_0001"], note)
        hit = find_prompt(text, 0, "sh")
        assert hit is not None and hit.key == "disc" and hit.by_driver
        assert hit.end == len(text)
        assert box_labels(text[hit.start:hit.end]) == ["LCSAS_TEST_TINY_2026_0001"]
    assert box_labels(_box([])) == []
