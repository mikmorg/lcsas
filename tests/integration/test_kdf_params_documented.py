"""The stolen-disc doc states the KDF parameters rustic really writes (#489).

docs/DISC_CONFIDENTIALITY.md §3/§7 quotes the scrypt N, r, p that the live
rustic writer records in a fresh key file, and the brute-force table is
derived from them.  The doc said N=2^15 for months while rustic wrote
N=2^17.  This initialises a real repo and fails if the doc no longer names
what rustic records, so a rustic upgrade that changes them gets noticed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic]

DOC = Path(__file__).resolve().parents[2] / "docs" / "DISC_CONFIDENTIALITY.md"


def test_doc_names_the_params_rustic_writes(tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text("kdf-params\n")
    repo = tmp_path / "repo"
    subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), "init"],
        check=True, capture_output=True, timeout=300,
    )
    keys = sorted((repo / "keys").iterdir())
    assert keys, "rustic init wrote no key file"
    doc = json.loads(keys[0].read_text())
    n, r, p = doc["N"], doc["r"], doc["p"]

    text = DOC.read_text(encoding="utf-8")
    row = f"| rustic 0.11 (LCSAS) | {n} | {r} | {p} |"
    assert row in text, (
        f"rustic now writes N={n}, r={r}, p={p}; update the §7 table and the "
        "§3 summary in docs/DISC_CONFIDENTIALITY.md (re-run tools/kdf_bench.py)"
    )
    assert "username" not in doc and "hostname" not in doc, (
        "rustic now writes username/hostname into key files; §6 says it doesn't"
    )
