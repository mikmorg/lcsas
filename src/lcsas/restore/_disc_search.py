"""Tier 3, the Python reader: the disc search (#884).

Finds the bytes of a pack in the pack cache or on any reachable data
disc, and asks for a disc when none has it.  ``PurePythonRestorer``
(``restic_fallback.py``) holds one search and reads packs only through
it.

This file is inlined into the generated ``standalone_restorer.py``
ahead of ``restic_fallback.py`` (see ``standalone_builder.py``), so it
imports nothing but the standard library, compiles under Python 3.10,
and defines no top-level name that another inlined file defines.
"""

from __future__ import annotations
