#!/usr/bin/env python3
"""Interleave the agent's stream-json transcript with the disc-loader log.

Both files have timestamps. Emit a merged, human-readable timeline so
`verify.sh` and post-mortem reviewers can see in one pass which disc was
in the drive when each tool call happened.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _load_transcript(path: Path) -> list[tuple[datetime | None, str]]:
    out: list[tuple[datetime | None, str]] = []
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(event.get("timestamp", ""))
        kind = event.get("type", "event")
        summary = _summarize(event)
        out.append((ts, f"[{kind}] {summary}"))
    return out


def _summarize(event: dict) -> str:
    msg = event.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {})
                    return f"{name} {json.dumps(inp)[:200]}"
                if block.get("type") == "text":
                    txt = block.get("text", "").strip().replace("\n", " ")
                    return txt[:200]
    return json.dumps(event)[:200]


def _load_disc_log(path: Path) -> list[tuple[datetime | None, str]]:
    out: list[tuple[datetime | None, str]] = []
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        parts = line.split(" ", 1)
        ts = _parse_ts(parts[0]) if parts else None
        body = parts[1] if len(parts) > 1 else line
        out.append((ts, f"[disc-loader] {body}"))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: merge_timeline.py TRANSCRIPT_JSONL DISC_LOADER_LOG",
              file=sys.stderr)
        return 64
    transcript = _load_transcript(Path(sys.argv[1]))
    disc_log = _load_disc_log(Path(sys.argv[2]))
    events = transcript + disc_log
    events.sort(key=lambda e: e[0] or datetime.max)
    for ts, body in events:
        stamp = ts.isoformat() if ts else "----"
        print(f"{stamp}  {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
