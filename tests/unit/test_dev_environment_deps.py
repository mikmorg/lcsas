"""Regression: ``pip install -e ".[dev]"`` must pull in ``zstandard``.

The tier-3 pure-Python restorer (``standalone_restorer.py``, derived from
``lcsas.restore.restic_fallback``) needs ``zstandard`` to decompress modern
rustic v2 blobs.  It is intentionally an OPTIONAL runtime dep (the
"zero runtime dependencies" rule in CLAUDE.md), but the dev/CI environment
must always have it — otherwise the integration suite crashes in
``test_pure_python_restore.py`` and ``test_meta_volume_restore.py`` with::

    RuntimeError: This repository uses zstd compression but the
    'zstandard' Python package is not installed.

This is the gate that prevents that regression.  Closes #141.

NOTE: do NOT use ``pytest.importorskip`` here — a skip would silently
hide the very regression this test exists to catch.
"""


def test_zstandard_importable_in_dev_environment() -> None:
    import zstandard  # noqa: F401
