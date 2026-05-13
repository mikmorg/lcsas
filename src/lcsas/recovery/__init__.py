"""LCSAS recovery toolchain orchestrator.

Wraps the standalone ``recovery/`` tree (C89 + POSIX-sh) for the
Python orchestrator.  Provides a ``RecoveryBuilder`` class that knows
how to invoke ``make`` against the recovery/Makefile, verify the
resulting binaries match a manifest, and cross-compile for additional
architectures.
"""

from lcsas.recovery.build import RecoveryBuilder, RecoveryArtifacts

__all__ = ["RecoveryBuilder", "RecoveryArtifacts"]
