#!/usr/bin/env python3
"""prompt_hygiene_check.py — KEY-04 docs-driven prompt guard.

The docs-driven split-key blind variant (agent_prompt_split_docs.txt) is
the acceptance gate for what the heir actually reads and holds.  Its whole
value is that it spoon-feeds NOTHING: the agent must derive every command
from the on-disc instructions.  If a token like ``restore.sh`` or
``keyshare`` leaks back into the prompt, the gate silently reverts to a
scripted smoke test (the exact failure mode KEY-04 fixes).

This check asserts the docs-driven prompt contains none of the
command-spoon-feed tokens below.  The scripted prompt
(agent_prompt_split.txt) is intentionally exempt — it is the tooling
smoke and is allowed to name commands.

Exit codes:
    0 — prompt is clean
    1 — at least one forbidden token found (printed to stderr)
"""

from __future__ import annotations

import sys

# Command / path / binary tokens that would turn the docs-driven prompt
# back into a command spoon-feed.  Matched case-insensitively as plain
# substrings.
FORBIDDEN_TOKENS = (
    "restore.sh",
    "keyshare",
    "lcsas-",
    "/mnt/recovery/bin",
    "--target",
    "--key",
)


def check_text(text: str) -> list[str]:
    """Return the forbidden tokens present in *text* (empty == clean)."""
    lowered = text.lower()
    return [tok for tok in FORBIDDEN_TOKENS if tok.lower() in lowered]


def check_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return check_text(fh.read())


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: prompt_hygiene_check.py <agent_prompt_split_docs.txt>",
            file=sys.stderr,
        )
        return 64
    hits = check_file(argv[1])
    if hits:
        print(
            f"PROMPT NOT DOCS-DRIVEN: {argv[1]} contains spoon-feed tokens: "
            f"{hits}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
