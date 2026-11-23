"""Protocol and implementation for the Rustic backup engine wrapper."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Protocol

from lcsas.rustic.types import BackupResult, SnapshotInfo
from lcsas.utils.subprocess import SubprocessRunnerBase

_logger = logging.getLogger(__name__)


class RusticRunner(Protocol):
    """Abstract interface for Rustic operations.

    Implementations may use real subprocess calls or mocks.  A test
    double defines only what ``SubprocessRusticRunner`` has
    (``tests/unit/test_rustic_doubles.py``).  Which packs a snapshot
    needs is not asked of rustic (0.11 has no JSON for a restore dry
    run): see ``lcsas.packs.snapshot_coverage.packs_for_snapshot``.
    """

    def init_repo(
        self,
        repo_path: Path,
        password_file: Path,
        hot_repo_path: Path | None = None,
    ) -> None: ...

    def backup(
        self,
        source_paths: list[Path],
        repo_path: Path,
        password_file: Path,
    ) -> BackupResult: ...

    def snapshots(
        self,
        repo_path: Path,
        password_file: Path,
    ) -> list[SnapshotInfo]: ...

    def restore(
        self,
        snapshot_id: str,
        repo_path: Path,
        password_file: Path,
        target_path: Path,
    ) -> None: ...


class SubprocessRusticRunner(SubprocessRunnerBase):
    """Real Rustic implementation using subprocess calls."""

    def __init__(
        self,
        rustic_binary: str = "rustic",
        tmpdir: Path | None = None,
    ) -> None:
        super().__init__(rustic_binary, tmpdir)

    def _run(
        self,
        args: list[str],
        repo_path: Path,
        password_file: Path,
        check: bool = True,
        timeout: int = 3600,
    ) -> subprocess.CompletedProcess[str]:
        from lcsas.log import mask_password_path

        cmd = [
            self._binary,
            "-r", str(repo_path),
            "--password-file", str(password_file),
            *args,
        ]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
                env=self._env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            operation = args[0] if args else "unknown"
            self._handle_timeout("rustic", operation, exc)
            raise  # unreachable — _handle_timeout always raises
        except subprocess.CalledProcessError as exc:
            self._log_stderr("rustic", exc)
            # Re-create with password path masked so it doesn't leak
            # into higher-level log messages / tracebacks.
            masked_cmd = [
                mask_password_path(c) if c == str(password_file) else c
                for c in exc.cmd
            ]
            masked_stderr = (
                exc.stderr.replace(str(password_file), "***")
                if exc.stderr else exc.stderr
            )
            raise subprocess.CalledProcessError(
                exc.returncode, masked_cmd,
                output=exc.output, stderr=masked_stderr,
            ) from None

    def init_repo(
        self,
        repo_path: Path,
        password_file: Path,
        hot_repo_path: Path | None = None,
    ) -> None:
        args = ["init"]
        if hot_repo_path:
            args.extend(["--repo-hot", str(hot_repo_path)])
        self._run(args, repo_path, password_file)

    def backup(
        self,
        source_paths: list[Path],
        repo_path: Path,
        password_file: Path,
    ) -> BackupResult:
        args = ["backup", "--json", *[str(p) for p in source_paths]]
        result = self._run(args, repo_path, password_file)
        return _parse_backup_result(result.stdout)

    def snapshots(
        self,
        repo_path: Path,
        password_file: Path,
    ) -> list[SnapshotInfo]:
        args = ["snapshots", "--json"]
        result = self._run(args, repo_path, password_file)
        return _parse_snapshots(result.stdout)

    def restore(
        self,
        snapshot_id: str,
        repo_path: Path,
        password_file: Path,
        target_path: Path,
    ) -> None:
        # Rustic 0.11.2 is idempotent by default: it compares size+mtime
        # for each target file and skips files that are already up-to-date.
        # Re-running restore after an interruption therefore resumes from
        # where it left off without wasted I/O (Issue #92).
        # --verify-existing would defeat this by forcing a re-read of every
        # file regardless of mtime — do NOT add it here.
        args = ["restore", snapshot_id, str(target_path)]
        self._run(args, repo_path, password_file, timeout=21600)  # 6 hours


# ---------------------------------------------------------------------------
# Output parsers (separated for independent testing)
# ---------------------------------------------------------------------------

def _parse_backup_result(output: str) -> BackupResult:
    """Parse rustic backup --json output into a BackupResult."""
    from lcsas.rustic.parser import parse_backup_output
    return parse_backup_output(output)


def _parse_snapshots(output: str) -> list[SnapshotInfo]:
    """Parse rustic snapshots --json output."""
    from lcsas.rustic.parser import parse_snapshots_output
    return parse_snapshots_output(output)
