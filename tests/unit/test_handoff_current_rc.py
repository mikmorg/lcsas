"""#691: .claude/HANDOFF.md must not name a superseded rc as current.

The field session reads HANDOFF.md first.  It once said rc5 was the
drill build after rc7 was tagged.  The file now names no rc; this test
fails if it names one older than the newest known rc (the package
version, or the newest ``v1.0.0-rc*`` tag when tags are present).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / ".claude" / "HANDOFF.md"


def _newest_rc() -> int:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version = "\d+\.\d+\.\d+rc(\d+)"', text, re.M)
    newest = int(m.group(1)) if m else 0
    try:
        tags = subprocess.run(
            ["git", "-C", str(ROOT), "tag", "-l", "v1.0.0-rc*"],
            capture_output=True, text=True, check=False, timeout=30,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        tags = []
    for tag in tags:
        t = re.fullmatch(r"v1\.0\.0-rc(\d+)", tag)
        if t:
            newest = max(newest, int(t.group(1)))
    return newest


def test_handoff_names_no_superseded_rc() -> None:
    newest = _newest_rc()
    assert newest > 0, "could not work out the newest rc"
    mentioned = {int(n) for n in re.findall(r"\brc(\d+)\b", HANDOFF.read_text())}
    stale = sorted(n for n in mentioned if n < newest)
    assert not stale, (
        f".claude/HANDOFF.md names rc{stale} but the newest rc is rc{newest}; "
        "point at tracker #478 / the newest v1.0.0-rc* tag instead"
    )


def test_handoff_says_where_the_current_build_is() -> None:
    text = HANDOFF.read_text()
    assert "#478" in text
    assert "v1.0.0-rc*" in text
