"""The docs' volume lifecycle tables match the lifecycle module (#892).

``docs/workflows/consolidate-and-catalog-ops.md`` ("Volume lifecycle state
transitions") shows the status graph as a text block and lists every
``VolumeLifecycle`` event in a table.  This check fails when the graph
block differs from ``_TRANSITIONS`` in ``src/lcsas/db/lifecycle.py``, or
when the table misses an event or names one the module does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.test_volume_lifecycle_table import lifecycle_events

DOC = (Path(__file__).resolve().parents[2] / "docs" / "workflows"
       / "consolidate-and-catalog-ops.md")
_SECTION = "## Volume lifecycle state transitions"


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert _SECTION in text, f"{DOC.name} lost its {_SECTION!r} section"
    body = text.split(_SECTION, 1)[1]
    return body.split("\n## ", 1)[0]


def _graph(section: str) -> dict[str, set[str]]:
    blocks = re.findall(r"```text\n(.*?)```", section, flags=re.S)
    assert len(blocks) == 1, "expected one ```text graph block"
    graph: dict[str, set[str]] = {}
    for line in blocks[0].strip().splitlines():
        src, _, dst = line.partition("→")
        targets = dst.strip()
        graph[src.strip()] = (set() if targets == "(terminal)" else
                              {t.strip() for t in targets.split(",")})
    return graph


def _table_events(section: str) -> list[str]:
    head = "| Event (`VolumeLifecycle.`)"
    assert head in section, "the event table lost its header"
    rows = section.split(head, 1)[1].split("\n\n", 1)[0].splitlines()[2:]
    events = []
    for row in rows:
        m = re.match(r"\|\s*`(\w+)`\s*\|", row)
        assert m, f"unreadable event row: {row}"
        events.append(m.group(1))
    return events


def test_graph_block_is_transitions() -> None:
    from lcsas.db.lifecycle import _TRANSITIONS

    assert _graph(_section()) == _TRANSITIONS


def test_event_table_lists_every_event_once() -> None:
    events = _table_events(_section())
    assert len(events) == len(set(events)), "an event is listed twice"
    assert set(events) == lifecycle_events()
