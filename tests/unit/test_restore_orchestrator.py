"""The restore orchestration API, driven with plain arguments (#762).

The ``lcsas restore`` handlers are thin wrappers over
:mod:`lcsas.restore.orchestrator`; these tests call it directly, with no
argparse namespace, and answer its questions through the ``prompt`` and
``stdin_is_tty`` callbacks instead of patching ``input`` and ``sys.stdin``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lcsas.db.models import Pack
from lcsas.restore.executor import IngestionResult
from lcsas.restore.orchestrator import (
    PlannedRestore,
    plan_restore,
    restore_from_disc,
    run_restore,
)
from lcsas.restore.planner import PackSource, PickListV2
from lcsas.rustic.types import RestorePlan

_SHA = "ab" * 32


def _planned(tmp_path: Path, *, total_bytes: int = 1) -> PlannedRestore:
    pack = Pack(
        pack_id=1, sha256=_SHA, size_bytes=1, repo_id="r",
        is_pruned=False, created_at="2026-11-16",
    )
    pick_list = PickListV2(
        volumes={"VOL_A": [PackSource(pack, "VOL_A", 1, alternates=[])]},
        missing_packs=[],
        total_packs=1,
        total_bytes=total_bytes,
    )
    return PlannedRestore(
        plan=RestorePlan(snapshot_id="snap1", required_pack_hashes=[_SHA]),
        pick_list=pick_list,
        runner=MagicMock(),
        mirror_path=tmp_path / "mirror",
    )


def _run(
    tmp_path: Path, planned: PlannedRestore, **kwargs: Any,
) -> tuple[int, MagicMock]:
    """run_restore with the executor class replaced; returns (rc, instance)."""
    with (
        patch("lcsas.restore.executor.RestoreExecutor") as exec_cls,
        patch("lcsas.ecc.dvdisaster.select_ecc_runner", return_value=None),
    ):
        exec_cls.verify_cache_completeness.return_value = []
        executor = exec_cls.return_value
        executor.ingest_volume.return_value = IngestionResult(1, [])
        rc = run_restore(
            planned,
            staging_path=tmp_path / "staging",
            snapshot_id="snap1",
            target_path=tmp_path / "restored",
            password_file=tmp_path / "key",
            cache_dir=tmp_path / "cache",
            volume_dir=None,
            iso_dir=None,
            skip_verify=True,
            **kwargs,
        )
    return rc, executor


def test_run_restore_asks_for_each_disc_through_the_prompt(
    tmp_path: Path,
) -> None:
    mount = tmp_path / "mnt"
    mount.mkdir()
    asked: list[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return str(mount)

    rc, executor = _run(
        tmp_path, _planned(tmp_path), prompt=prompt, stdin_is_tty=lambda: True,
    )

    assert rc == 0
    assert asked == [
        "\nMount disc 'VOL_A' and enter mount path (or 'skip' to skip): ",
    ]
    assert executor.ingest_volume.call_args.args[1] == mount
    executor.execute_restore.assert_called_once()


def test_run_restore_without_a_terminal_refuses_to_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    def prompt(text: str) -> str:
        raise AssertionError(f"asked {text!r} with no terminal")

    with caplog.at_level(logging.ERROR, logger="lcsas"):
        rc, executor = _run(
            tmp_path, _planned(tmp_path),
            prompt=prompt, stdin_is_tty=lambda: False,
        )

    assert rc == 1
    assert "Interactive restore requires a TTY" in caplog.text
    executor.ingest_volume.assert_not_called()


def test_run_restore_space_shortfall_declined_through_the_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    asked: list[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return "n"

    with caplog.at_level(logging.ERROR, logger="lcsas"):
        rc, executor = _run(
            tmp_path, _planned(tmp_path, total_bytes=10**18),
            prompt=prompt, stdin_is_tty=lambda: True,
        )

    assert rc == 1
    assert asked == ["Continue anyway? [y/N] "]
    assert "Aborted: free up space and re-run." in caplog.text
    executor.prepare_cache.assert_not_called()


def test_plan_restore_unknown_repository_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    config = SimpleNamespace(
        repositories={"family": object()}, staging_path=tmp_path,
    )
    with caplog.at_level(logging.ERROR, logger="lcsas"):
        planned = plan_restore(
            config,  # type: ignore[arg-type]
            MagicMock(), repo_name="work", snapshot_id="snap1",
        )

    assert planned is None
    assert "repository 'work' not found in config." in caplog.text
    assert "Available: family" in caplog.text


def test_restore_from_disc_rejects_a_missing_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="lcsas"):
        rc = restore_from_disc(
            tmp_path / "no-disc", tmp_path / "restored", tmp_path / "key",
        )

    assert rc == 1
    assert "is not a directory" in caplog.text
