"""Integration test for lcsas-iso9660.

Builds a small ISO 9660 image with pycdlib containing a few files and
directories, then runs the C binary against it to verify cat / ls /
extract all work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BINARY = Path(__file__).resolve().parents[1] / "build" / "lcsas-iso9660"


def main() -> int:
    if not BINARY.exists():
        print(f"FAIL: {BINARY} not built", file=sys.stderr)
        return 1

    try:
        import pycdlib  # noqa: F401
    except ImportError:
        print("SKIP: pycdlib not installed", file=sys.stderr)
        return 0

    import pycdlib

    tmp = Path(tempfile.mkdtemp(prefix="lcsas_iso_"))
    fails = 0
    try:
        iso_path = tmp / "test.iso"

        # Build ISO with ISO 9660 level 2 names.
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=2)
        hello_data = b"Hello from ISO 9660!\n"
        iso.add_fp(__import__("io").BytesIO(hello_data),
                   len(hello_data), "/HELLO.TXT;1")
        big_data = bytes(range(256)) * 64  # 16 KiB
        iso.add_fp(__import__("io").BytesIO(big_data),
                   len(big_data), "/DATA.BIN;1")
        iso.add_directory("/SUB")
        sub_data = b"nested\n"
        iso.add_fp(__import__("io").BytesIO(sub_data),
                   len(sub_data), "/SUB/F.TXT;1")
        iso.write(str(iso_path))
        iso.close()

        # ── ls / ──
        result = subprocess.run([str(BINARY), "ls", str(iso_path), "/"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"FAIL ls /: rc={result.returncode}\n{result.stderr}",
                  file=sys.stderr)
            fails += 1
        else:
            names = sorted(l.split()[-1] for l in result.stdout.strip().splitlines())
            if "HELLO.TXT" not in names or "DATA.BIN" not in names or "SUB" not in names:
                print(f"FAIL ls: missing entries, got {names}", file=sys.stderr)
                fails += 1

        # ── ls /SUB ──
        result = subprocess.run([str(BINARY), "ls", str(iso_path), "/SUB"],
                                capture_output=True, text=True)
        if "F.TXT" not in result.stdout:
            print(f"FAIL ls /SUB: got {result.stdout!r}", file=sys.stderr)
            fails += 1

        # ── cat /HELLO.TXT ──
        result = subprocess.run([str(BINARY), "cat", str(iso_path),
                                 "/HELLO.TXT"],
                                capture_output=True)
        if result.stdout != hello_data:
            print(f"FAIL cat: got {result.stdout!r}", file=sys.stderr)
            fails += 1

        # ── extract large file ──
        dst = tmp / "out.bin"
        result = subprocess.run([str(BINARY), "extract", str(iso_path),
                                 "/DATA.BIN", str(dst)],
                                capture_output=True, text=True)
        if result.returncode != 0 or dst.read_bytes() != big_data:
            print(f"FAIL extract: rc={result.returncode}", file=sys.stderr)
            fails += 1

        # ── cat /SUB/F.TXT (case-insensitive lookup test) ──
        result = subprocess.run([str(BINARY), "cat", str(iso_path),
                                 "/sub/f.txt"],
                                capture_output=True)
        if result.stdout != sub_data:
            print(f"FAIL case-insensitive cat: got {result.stdout!r}",
                  file=sys.stderr)
            fails += 1

        if fails == 0:
            print("test_iso9660: OK")
            return 0
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
