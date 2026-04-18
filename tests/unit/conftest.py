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
