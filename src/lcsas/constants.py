"""LCSAS constants — volume statuses, media types, and other literals."""

from __future__ import annotations

# Volume lifecycle statuses
STATUS_STAGING = "STAGING"
STATUS_BURNING = "BURNING"
STATUS_BURNED = "BURNED"
STATUS_VERIFIED = "VERIFIED"
STATUS_CONSOLIDATING = "CONSOLIDATING"
STATUS_DEPRECATED = "DEPRECATED"
STATUS_DESTROYED = "DESTROYED"

# Display-only status for a lost volume: a burned volume with no disc left
# in service.  It is never stored: the catalog records a lost volume as
# DEPRECATED plus a NOTE event that starts with AUTO_DEPRECATED_NOTE_PREFIX
# (Q33, docs/adr/0003-lost-volumes-are-not-a-schema-state.md).  The one
# predicate that decides it is lcsas.db.queries.get_lost_volume_ids.
DISPLAY_STATUS_LOST = "LOST"

# Start of the NOTE event detail written when a volume's last in-service
# disc is recorded deprecated, destroyed or lost.  Older catalogs carry
# "auto-deprecated: last physical copy ..." so only this prefix is matched.
AUTO_DEPRECATED_NOTE_PREFIX = "auto-deprecated:"

# Burn session statuses
SESSION_STAGED = "STAGED"
SESSION_PARTIAL = "PARTIAL"
SESSION_COMPLETE = "COMPLETE"
SESSION_CLEANED = "CLEANED"

# Event types
EVENT_NOTE = "NOTE"
EVENT_VERIFY_PASS = "VERIFY_PASS"
EVENT_VERIFY_FAIL = "VERIFY_FAIL"
EVENT_VERIFY_FAIL_REBURN = "VERIFY_FAIL_REBURN"

# Common thresholds
HASH_VERIFICATION_SIZE_THRESHOLD = 500_000_000  # 500 MB
METADATA_RESERVE_DEFAULT_MB = 104_857_600  # 100 MB (in bytes)
SQLITE_BATCH_SIZE = 900  # Conservative batch size (999 - buffer)

# Default configuration
DEFAULT_MEDIA_TYPE = "MDISC100"
DEFAULT_ECC_REDUNDANCY_PCT = 20
