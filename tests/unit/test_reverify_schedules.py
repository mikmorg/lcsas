"""The re-verification schedules have one home (Q21).

``docs/workflows/verify-and-audit.md`` ("Re-verification schedules") is
the one place that says how often to read discs back and when to
re-burn.  Every disc carries the long-term schedule in DISC_CARE.txt
(``HolographicInjector.write_disc_care``), so the two texts must say the
same thing.  The READINESS checklist follows the owner schedule.

These tests fail if DISC_CARE.txt and the doc drift apart, if the
checklist goes back to a schedule of its own, or if a link to one of
the schedule sections stops matching a heading (the link-integrity test
strips ``#anchors``, so it would not notice).
"""

from __future__ import annotations

import re
from pathlib import Path

from lcsas.staging.metadata import HolographicInjector

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_DOC = REPO_ROOT / "docs" / "workflows" / "verify-and-audit.md"
CHECKLIST = REPO_ROOT / "recovery" / "docs" / "READINESS_CHECKLIST.txt"


def _squash(text: str) -> str:
    """Collapse whitespace runs so line wrapping and spacing do not matter."""
    return " ".join(text.split())


def _doc_long_term_bullets() -> list[str]:
    """The bullet list right under '### Long-term schedule' in the doc."""
    text = VERIFY_DOC.read_text(encoding="utf-8")
    start = text.index("\n### Long-term schedule\n")
    bullets: list[str] = []
    for line in text[start:].splitlines()[2:]:
        if line.startswith("- "):
            bullets.append(line[2:])
        elif bullets:
            break  # first non-bullet line after the list ends it
    return [_squash(b.replace("**", "")) for b in bullets]


def _disc_care_bullets(tmp_path: Path) -> list[str]:
    """The schedule bullets under PERIODIC VERIFICATION in DISC_CARE.txt."""
    root = tmp_path / "disc"
    root.mkdir()
    HolographicInjector(root).write_disc_care(None)
    text = (root / "DISC_CARE.txt").read_text(encoding="utf-8")
    # The section header, not the "(see PERIODIC VERIFICATION)" mention.
    section = text[text.index("PERIODIC VERIFICATION\n---"):]
    bullets: list[str] = []
    for line in section.splitlines():
        if line.startswith("  - "):
            bullets.append(line[4:])
        elif bullets and line.startswith("    ") and line.strip():
            bullets[-1] += " " + line.strip()  # wrapped continuation
        elif bullets:
            break  # the blank line after the list ends it
    return [_squash(b) for b in bullets]


def test_disc_care_carries_the_long_term_schedule(tmp_path: Path) -> None:
    doc = _doc_long_term_bullets()
    disc = _disc_care_bullets(tmp_path)
    assert len(doc) == 3, f"expected 3 long-term schedule bullets, got {doc}"
    assert disc == doc, (
        "DISC_CARE.txt's PERIODIC VERIFICATION lines must say exactly what "
        "the 'Long-term schedule' in docs/workflows/verify-and-audit.md "
        f"says.\n  doc:  {doc}\n  disc: {disc}"
    )


def test_checklist_follows_the_owner_schedule() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    assert "docs/workflows/verify-and-audit.md" in text
    item = text[text.index("[ ] DISC READABILITY SCAN"):]
    item = item[: item.index("[ ] OFFSITE COPY CHECK")]
    how_often = _squash(item[item.index("HOW OFTEN:"):])
    assert how_often == (
        "HOW OFTEN: monthly, a twelfth of the discs, so that every disc "
        "is read once a year. Re-burn at once after any read error."
    ), f"the disc readability scan must follow the owner schedule: {how_often!r}"
    assert "Disc readability scan           Monthly, a twelfth of the discs" in text


def _github_slug(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\- ]", "", slug)
    return slug.replace(" ", "-")


def test_links_to_the_schedule_sections_resolve() -> None:
    headings = re.findall(
        r"^#{1,6} (.+)$", VERIFY_DOC.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    slugs = {_github_slug(h) for h in headings}
    for name in ("re-verification-schedules", "owner-schedule",
                 "long-term-schedule"):
        assert name in slugs, f"verify-and-audit.md lost the #{name} heading"

    sources = [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").rglob("*.md")]
    found = 0
    for src in sources:
        for anchor in re.findall(
            r"verify-and-audit\.md#([\w-]+)", src.read_text(encoding="utf-8"),
        ):
            found += 1
            assert anchor in slugs, (
                f"{src.relative_to(REPO_ROOT)} links to "
                f"verify-and-audit.md#{anchor}, which matches no heading"
            )
    assert found > 0, "no links to verify-and-audit.md sections were found"
