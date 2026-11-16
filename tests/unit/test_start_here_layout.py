"""#854: a data disc's START_HERE.txt has a clean left margin and fits 78
columns.

It was an indented template run through ``textwrap.dedent``.  With a
repository list, or a hint or contact of several lines, the inserted
lines started at column 0, dedent removed nothing, and the file showed
10 columns in with some lines at column 0.  It is now built line by
line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.staging.metadata import START_HERE_WIDTH, HolographicInjector

HINT = (
    "Paper copy in the fire safe, top shelf, in the envelope marked 'B'.\n"
    "A second copy is with the family lawyer, filed under the archive's "
    "name, together with the share cards and a printed copy of this disc.\n"
    "\n"
    "  The safe's combination is in the will."
)
CONTACT = (
    "A family friend who works in IT\n"
    "or any computer repair shop that knows Linux and can read the "
    "RESTORE_INSTRUCTIONS.txt file on this disc for you."
)


def _render(tmp_path: Path, **kw) -> list[str]:
    root = tmp_path / "staging"
    root.mkdir()
    config = LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=root,
        db_path=tmp_path / "db.db",
        archive_owner="A. N. Owner",
        archive_description=(
            "Family photos, scanned letters, tax records and the home "
            "videos of three decades, kept as two backup sets"
        ),
        key_storage_hints=HINT,
        technical_contact=CONTACT,
        repositories={
            name: RepositoryConfig(name=name,
                                   mirror_path=tmp_path / "mirror" / name)
            for name in ("family", "work")
        },
        **kw,
    )
    HolographicInjector(root).write_start_here(config)
    return (root / "START_HERE.txt").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("split", [False, True])
def test_margin_and_width(tmp_path, split):
    kw = {"key_split": True, "key_threshold": 2, "key_shares": 3} if split \
        else {}
    lines = _render(tmp_path, **kw)
    text = "\n".join(lines)

    assert max(len(line) for line in lines) <= START_HERE_WIDTH
    # Headings at column 0, the box first.
    assert lines[0].startswith("╔")
    for heading in ("WHAT ARE THESE DISCS?", "HOW TO GET YOUR FILES BACK",
                    "WHAT TO DO NEXT", "DISC CARE"):
        assert heading in lines
    # The inserts sit at the indent of their section.
    assert "  Repositories on these discs: family, work" in lines
    assert any(line.startswith("  Contents: Family photos") for line in lines)
    hint_lines = [line for line in lines if "fire safe" in line
                  or "family lawyer" in line or "combination" in line]
    assert hint_lines and all(line.startswith("     ") for line in hint_lines)
    # The hint's own indent is kept, under the section's.
    assert "       The safe's combination is in the will." in lines
    contact_lines = [line for line in lines if "IT" in line.split()
                     or "repair shop" in line]
    assert contact_lines and all(line.startswith("  ") for line in contact_lines)
    # Only the fixed text (headings, rules, box, the IMPORTANT and
    # WARNING paragraphs) starts at column 0, never an insert.
    plain = tmp_path / "plain"
    plain.mkdir()
    root = plain / "staging"
    root.mkdir()
    HolographicInjector(root).write_start_here(LCSASConfig(
        mirror_base_path=plain, staging_path=root, db_path=plain / "db",
        **kw,
    ))
    fixed = {line for line in
             (root / "START_HERE.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith(" ")}
    assert {line for line in lines if line and not line.startswith(" ")} \
        == fixed
    # The words survive the wrapping.
    flat = " ".join(text.split())
    assert "A second copy is with the family lawyer, filed under the" in flat
    assert ("or any computer repair shop that knows Linux and can read the "
            "RESTORE_INSTRUCTIONS.txt file") in flat
    if split:
        assert "YOUR PASSWORD IS SPLIT INTO 3 SHARE CARDS" in lines


def test_box_is_square(tmp_path):
    lines = _render(tmp_path)
    assert len({len(line) for line in lines[:3]}) == 1
    assert lines[1].strip("║ ") == "START HERE"
