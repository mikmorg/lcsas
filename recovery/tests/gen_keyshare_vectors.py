#!/usr/bin/env python3
"""Generate recovery/tests/keyshare_vectors.h from the official vectors.

Emits a self-contained C header so the C SLIP-0039 test runner needs no
JSON parser and stays sanitizer-clean (no file I/O during the test).

Usage:
    python3 recovery/tests/gen_keyshare_vectors.py \
        [tests/fixtures/keyshare/vectors.json] \
        [recovery/tests/keyshare_vectors.h]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_SRC = "tests/fixtures/keyshare/vectors.json"
DEFAULT_DST = "recovery/tests/keyshare_vectors.h"


def cesc(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return "".join(out)


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_SRC)
    dst = Path(argv[2]) if len(argv) > 2 else Path(DEFAULT_DST)
    vectors = json.loads(src.read_text())

    max_m = max(len(entry[1]) for entry in vectors)
    cap = max_m + 1

    lines: list[str] = []
    lines.append("/* Auto-generated from tests/fixtures/keyshare/vectors.json by")
    lines.append(" * recovery/tests/gen_keyshare_vectors.py -- do not hand-edit. */")
    lines.append("#ifndef LCSAS_KEYSHARE_VECTORS_H")
    lines.append("#define LCSAS_KEYSHARE_VECTORS_H")
    lines.append("")
    lines.append("typedef struct {")
    lines.append("    const char *desc;")
    lines.append(f"    const char *mnemonics[{cap}];")
    lines.append("    int nmnemonics;")
    lines.append("    const char *secret_hex; /* empty string => INVALID */")
    lines.append("} keyshare_vector;")
    lines.append("")
    lines.append("static const keyshare_vector KEYSHARE_VECTORS[] = {")
    for entry in vectors:
        desc, mnemonics, secret = entry[0], entry[1], entry[2]
        lines.append("    {")
        lines.append(f'        "{cesc(desc)}",')
        lines.append("        {")
        for m in mnemonics:
            lines.append(f'            "{cesc(m)}",')
        lines.append("        },")
        lines.append(f"        {len(mnemonics)},")
        lines.append(f'        "{secret}"')
        lines.append("    },")
    lines.append("};")
    lines.append("")
    lines.append(
        "#define KEYSHARE_VECTOR_COUNT "
        "((int)(sizeof(KEYSHARE_VECTORS)/sizeof(KEYSHARE_VECTORS[0])))"
    )
    lines.append("")
    lines.append("#endif")

    dst.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(vectors)} vectors -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
