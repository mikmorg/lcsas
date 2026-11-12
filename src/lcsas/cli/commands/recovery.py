"""``lcsas recovery``."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from lcsas.log import get_logger

logger = get_logger()


def cmd_recovery(args: argparse.Namespace) -> int:
    """Dispatcher for `lcsas recovery {build,test,manifest,verify}`."""
    from lcsas.recovery import RecoveryBuilder

    project_root = Path(__file__).resolve().parents[4]  # the checkout, 4 levels up
    recovery_dir = project_root / "recovery"
    if not recovery_dir.is_dir():
        logger.error("recovery/ tree not found at %s", recovery_dir)
        return 1

    rb = RecoveryBuilder(recovery_dir)
    sub = getattr(args, "recovery_command", None)

    if sub == "build":
        verbose = bool(getattr(args, "verbose", False))
        try:
            if args.arch == "host":
                a = rb.build_host(verbose=verbose)
            else:
                a = rb.cross_build(args.arch, cc=args.cc, verbose=verbose)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.error("recovery build failed: %s", exc)
            return 1
        print(f"built {a.arch}: {a.lcsas_restore}")
        if a.lcsas_iso9660:
            print(f"  + {a.lcsas_iso9660}")
        return 0

    if sub == "test":
        verbose = bool(getattr(args, "verbose", False))
        ok = rb.run_tests(verbose=verbose)
        if not ok:
            logger.error("recovery tests FAILED")
            return 1
        print("recovery tests: OK")
        return 0

    if sub == "manifest":
        path = rb.write_manifest(getattr(args, "output", None))
        line_count = sum(1 for _ in path.open())
        print(f"wrote {path} ({line_count} files)")
        return 0

    if sub == "verify":
        out = subprocess.run(
            ["make", "-C", str(recovery_dir), "repro-check"],
            check=False,
        )
        return out.returncode

    logger.error("Usage: lcsas recovery {build,test,manifest,verify}")
    return 1
