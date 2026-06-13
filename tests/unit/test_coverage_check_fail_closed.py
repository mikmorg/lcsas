"""Unit: coverage_check.py fails closed on an empty report (GATE-07).

The tier-1 coverage gate is the one CI check guarding the C binary that
authenticates and restores the heir's data. Its checker used to return
exit 0 with only a stderr WARNING when zero src/lcsas-restore/*.c entries
appeared in coverage.json — so any gcovr filter/path/flag drift that
emptied the report silently disabled the whole threshold gate (a failure
mode project history already records once). These tests pin the
fail-closed behavior: an empty report, or any file below threshold, must
exit non-zero.

Stdlib-only; runs the real script as a subprocess against crafted JSON
fixtures so it exercises the exact code path CI runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "recovery" / "scripts" / "coverage_check.py"


def _run(json_path: Path, threshold: float = 88.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--threshold", str(threshold), "--json", str(json_path)],
        capture_output=True,
        text=True,
    )


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps(data))
    return p


def test_no_lcsas_restore_entries_fails_closed(tmp_path: Path) -> None:
    """Report with entries but none under src/lcsas-restore/ → exit 1."""
    data = {
        "line_percent": 99.0,
        "files": [
            {"filename": "recovery/vendored/sqlite3.c", "line_percent": 99.0},
            {"filename": "recovery/src/keyshare/keyshare.c", "line_percent": 99.0},
        ],
    }
    result = _run(_write(tmp_path, data))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no src/lcsas-restore/*.c files found" in result.stderr
    assert "Failing closed" in result.stderr


def test_file_below_threshold_fails_and_names_it(tmp_path: Path) -> None:
    """One file under threshold → exit 1, the file named in stderr."""
    data = {
        "line_percent": 90.0,
        "files": [
            {"filename": "recovery/src/lcsas-restore/aes.c", "line_percent": 95.0},
            {"filename": "recovery/src/lcsas-restore/repo.c", "line_percent": 60.0},
        ],
    }
    result = _run(_write(tmp_path, data))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "repo.c" in result.stderr
    assert "below 88% threshold" in result.stderr


def test_all_above_threshold_passes(tmp_path: Path) -> None:
    """All matching files at/above threshold → exit 0."""
    data = {
        "line_percent": 96.0,
        "files": [
            {"filename": "recovery/src/lcsas-restore/aes.c", "line_percent": 95.0},
            {"filename": "recovery/src/lcsas-restore/repo.c", "line_percent": 88.0},
        ],
    }
    result = _run(_write(tmp_path, data))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
