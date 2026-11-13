"""Shared pytest fixtures for unit tests."""

from __future__ import annotations

import pytest

from lcsas.db.connection import get_memory_connection
from lcsas.db.schema import create_all


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema initialized."""
    c = get_memory_connection()
    create_all(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_tool_version_check(monkeypatch):
    """Unit tests need no xorriso or dvdisaster on PATH (#731).

    ``BurnOrchestrator.stage()`` checks the real tools' versions before
    staging.  Several unit tests stage with a real
    ``SubprocessXorrisoRunner`` whose ``create_iso`` is patched, so the
    check is stubbed here; ``test_stage_tool_versions.py`` puts the real
    one back.
    """
    from lcsas.burn.orchestrator import BurnOrchestrator

    monkeypatch.setattr(
        BurnOrchestrator, "_tool_version_preflight",
        lambda self, *, apply_ecc: None,
    )
