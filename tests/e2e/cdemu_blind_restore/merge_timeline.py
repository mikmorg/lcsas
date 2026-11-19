#!/usr/bin/env python3
"""Interleave the agent's stream-json transcript with the disc-loader log.

Both files have timestamps. Emit a merged, human-readable timeline so
`verify.sh` and post-mortem reviewers can see in one pass which disc was
in the drive when each tool call happened.

The transcript stamps its events in UTC with a ``Z`` suffix; the
disc-loader log uses ``date -Iseconds`` (``+00:00``).  Every timestamp is
made timezone-aware (a naive one is taken as UTC) so the two sort together.
Comparing an aware stamp with a naive one raised TypeError on every run
until #835, which left each run's timeline.txt empty.  Events with no
timestamp (the transcript's system and result events) keep their place
after the stamped event before them.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Import transcript.py from this folder, leaving no __pycache__ behind.
sys.dont_write_bytecode = True
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript  # noqa: E402

Event = tuple[datetime | None, str]

# Stream-json bookkeeping events that say nothing about what happened.
_NOISE_SUBTYPES = frozenset({"thinking_tokens", "commands_changed"})


def _parse_ts(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    text = ts.strip()
    # datetime.fromisoformat() reads a trailing "Z" only from Python 3.11.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _cannot_read(path: Path, exc: OSError) -> None:
    print(f"merge_timeline.py: cannot read {path}: {exc.strerror}",
          file=sys.stderr)


def _read_lines(path: Path) -> list[str]:
    """The file's lines; [] (with a message) when it cannot be read.  The
    disc-loader log is root-owned 0640, so run.sh reads it with sudo."""
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError as exc:
        _cannot_read(path, exc)
        return []


def _load_transcript(path: Path) -> list[Event]:
    try:
        events = transcript.load(path).events
    except OSError as exc:
        _cannot_read(path, exc)
        return []
    out: list[Event] = []
    for event in events:
        if event.get("subtype") in _NOISE_SUBTYPES:
            continue
        ts = _parse_ts(event.get("timestamp"))
        kind = event.get("type", "event")
        summary = _summarize(event)
        out.append((ts, f"[{kind}] {summary}"))
    return out


def _block_text(content: object) -> str:
    """The text of a tool_result's content (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    return ""


def _summarize(event: dict) -> str:
    msg = event.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {})
                    return f"{name} {json.dumps(inp)[:200]}"
                if kind == "text":
                    txt = str(block.get("text", "")).strip().replace("\n", " ")
                    return txt[:200]
                if kind == "thinking":
                    return "(thinking)"
                if kind == "tool_result":
                    txt = _block_text(block.get("content"))
                    return "-> " + txt.strip().replace("\n", " | ")[:200]
    if event.get("type") == "result":
        return (f"turns={event.get('num_turns')} "
                f"cost={event.get('total_cost_usd')} "
                f"result={str(event.get('result', ''))[:120]!r}")
    return json.dumps(event)[:200]


def _load_disc_log(path: Path) -> list[Event]:
    out: list[Event] = []
    for line in _read_lines(path):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        ts = _parse_ts(parts[0])
        body = parts[1] if len(parts) > 1 and ts is not None else line
        out.append((ts, f"[disc-loader] {body}"))
    return out


def merge(transcript: list[Event], disc_log: list[Event]) -> list[Event]:
    """Merge two event lists in time order.

    A stamped event sorts by its time; an unstamped one inherits the time
    of the stamped event before it in its own file (the file's start if
    none), so it stays next to the events it came with.  Ties keep the
    transcript first and each file's own order.
    """
    keyed: list[tuple[datetime, int, int, Event]] = []
    floor = datetime.min.replace(tzinfo=UTC)
    for source, events in enumerate((transcript, disc_log)):
        last = floor
        for index, event in enumerate(events):
            if event[0] is not None:
                last = event[0]
            keyed.append((last, source, index, event))
    keyed.sort(key=lambda k: (k[0], k[1], k[2]))
    return [k[3] for k in keyed]


def render(events: list[Event]) -> str:
    lines = []
    for ts, body in events:
        stamp = ts.isoformat() if ts else "-" * 25
        lines.append(f"{stamp}  {body}")
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: merge_timeline.py TRANSCRIPT_JSONL DISC_LOADER_LOG",
              file=sys.stderr)
        return 64
    transcript_path, disc_path = Path(args[0]), Path(args[1])
    events = _load_transcript(transcript_path)
    disc_log = _load_disc_log(disc_path)
    if not events and not disc_log:
        print("merge_timeline.py: nothing to merge (both inputs empty or "
              "unreadable)", file=sys.stderr)
        return 1
    sys.stdout.write(render(merge(events, disc_log)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
