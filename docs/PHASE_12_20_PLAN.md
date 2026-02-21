# LCSAS Phase 12–20: Hardening & Completeness Plan

Generated: 2026-02-21
Baseline: commit 349804a (schema v3, 622 tests passing)

This document tracks all planned improvements. Each item references the
audit gap ID (S1, D3, A2, etc.) for traceability.

---

## Phase 12 — Schema v4 + Data Model  [D1, D4, D5, D6, D7]

### 12.1  `volume_events` table  [D1]
Add a new table to track verification history, ECC repairs, and lifecycle events.

```sql
CREATE TABLE IF NOT EXISTS volume_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id   INTEGER NOT NULL,
    event_type  TEXT NOT NULL CHECK(event_type IN (
        'VERIFY_PASS','VERIFY_FAIL','ECC_REPAIR',
        'LOCATION_MOVE','CONDITION_CHECK','NOTE')),
    event_date  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    location    TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (volume_id) REFERENCES volumes (volume_id),
    FOREIGN KEY (location) REFERENCES locations (name)
);
CREATE INDEX idx_volume_events_volume ON volume_events (volume_id);
CREATE INDEX idx_volume_events_type   ON volume_events (event_type);
```

CRUD module: `src/lcsas/db/volume_events.py` — `add_event()`, `get_events_for_volume()`,
`get_latest_event()`, `get_events_by_type()`.

Model: Add `VolumeEvent` frozen dataclass to `models.py`.

### 12.2  `volume_copies` verification metadata  [D5]
ALTER TABLE to add three columns:
- `iso_sha256 TEXT` — SHA-256 of the ISO at this location
- `last_verified_at DATETIME` — when this copy was last verified
- `media_serial TEXT DEFAULT ''` — physical media identifier (disc serial, UPC, etc.)

Update `VolumeCopy` model, `_row_to_copy()`, and `add_volume_copy()` signature.

### 12.3  Volume status transition enforcement  [D4]
Add `VALID_TRANSITIONS` dict in `volumes.py`:
```python
VALID_TRANSITIONS = {
    "STAGING":    {"BURNING", "DEPRECATED", "DESTROYED"},
    "BURNING":    {"BURNED", "STAGING", "DESTROYED"},     # STAGING = retry
    "BURNED":     {"VERIFIED", "STAGING", "DESTROYED"},   # STAGING = re-burn
    "VERIFIED":   {"DEPRECATED", "DESTROYED"},
    "DEPRECATED": {"DESTROYED"},
    "DESTROYED":  set(),
}
```
`update_status()` raises `ValueError` if transition is invalid.
Add `force_status()` escape hatch for admin override with a log warning.

### 12.4  Per-copy pack integrity  [D6]
D6 is addressed via D1 (`volume_events`). When a volume is verified,
a `VERIFY_PASS` or `VERIFY_FAIL` event is recorded. Per-pack corruption tracking
can be added as `detail` JSON (e.g., `{"failed_packs": ["abc123"]}`). No schema
change to `volume_packs` — denormalizing `sha256` there would duplicate `packs.sha256`.

### 12.5  Snapshot JSON helpers  [D7]
Add query helpers in `queries.py` using SQLite's `json_each()`:
- `get_snapshots_by_path(conn, path_pattern) -> list[Snapshot]`
- `get_snapshots_by_tag(conn, tag) -> list[Snapshot]`

These use `WHERE EXISTS (SELECT 1 FROM json_each(s.paths) WHERE value LIKE ?)`.
No schema change — the JSON TEXT storage is fine for SQLite 3.9+ (2015).

### 12.6  Migration v3→v4
In `schema.py`:
- Bump `CURRENT_SCHEMA_VERSION = 4`
- Add `volume_events` CREATE TABLE + indexes
- ALTER `volume_copies` ADD COLUMN `iso_sha256`, `last_verified_at`, `media_serial`
- Column-existence checks as per v2→v3 pattern

Files to modify:
- `src/lcsas/db/schema.py` — DDL, migrate(), version
- `src/lcsas/db/models.py` — VolumeEvent, VolumeCopy fields
- `src/lcsas/db/volumes.py` — VALID_TRANSITIONS in update_status()
- `src/lcsas/db/volume_copies.py` — new fields
- `src/lcsas/db/queries.py` — JSON snapshot helpers
- NEW: `src/lcsas/db/volume_events.py`
- Tests: test_db_volumes.py, test_db_volume_copies.py, test_db_schema.py + new test_db_volume_events.py

---

## Phase 13 — Orchestrator Refactoring  [A2, A3, A4, O4]

### 13.1  Extract shared `_stage_single_volume()` helper  [A2]
Factor the duplicated logic from `prepare()` (L107–196) and `stage()` (L314–477)
into a single private method:

```python
def _stage_single_volume(
    self, packs: list[Pack], media_type: MediaType,
    location: str, session_id: str | None,
    skip_ecc: bool, iso_output: Path | None,
) -> tuple[Volume, Path]:
    """Create staging dir, write packs, inject metadata, create ISO.
    Returns (volume, iso_path)."""
```

Both `prepare()` and `stage()` call this helper. `prepare()` returns a
`BurnManifest` wrapping the result; `stage()` loops calling it for N volumes
and wraps results in a `StageResult`.

### 13.2  Use `config.metadata_reserve_bytes` in merger  [A3]
`VolumeMerger.__init__()` must accept `metadata_reserve_bytes: int` parameter.
`plan_consolidation()` passes it to `estimate_volumes_needed(reserved=...)`.
CLI handler `cmd_consolidate` passes `config.metadata_reserve_bytes`.

Files: `consolidate/merger.py` L61, `cli/main.py` consolidate handler.

### 13.3  Atomic ISO creation  [A4]
`SubprocessXorrisoRunner.create_iso()` writes to `output_iso.with_suffix('.iso.tmp')`,
then `os.rename()` to final path on success. On failure, the .tmp file is cleaned up
in a `try/finally` block.

File: `iso/xorriso.py` L47–63

### 13.4  ISO size validation  [O4]
After `create_iso()` (and optional ECC augmentation), validate:
```python
iso_size = iso_path.stat().st_size
if iso_size > media_type.capacity_bytes:
    raise ValueError(
        f"ISO {iso_path.name} is {iso_size:,} bytes, exceeds "
        f"{media_type.name} capacity of {media_type.capacity_bytes:,} bytes"
    )
```
This goes in the new `_stage_single_volume()` helper after ISO creation.

Files: `burn/orchestrator.py` (new helper)

---

## Phase 14 — Verification Pipeline  [S1]

### 14.1  Post-burn read-back verification
In `burn_session()`, after `xorriso.burn_iso()` and before marking VERIFIED:

```python
if not skip_burn:
    verify_ok = self._xorriso.verify_disc(device)
    if not verify_ok:
        update_status(conn, vid, "BURNED")   # stays BURNED, not VERIFIED
        add_event(conn, vid, "VERIFY_FAIL", location, "Post-burn read-back failed")
        receipt.verify_passed = False
        continue  # skip to next volume
    add_event(conn, vid, "VERIFY_PASS", location, "Post-burn read-back")
```

Only mark VERIFIED if verification passes. Failed volumes stay BURNED
so the user can investigate and re-burn.

### 14.2  Manual / remote verification CLI
Extend `lcsas verify` to support:
- `lcsas verify <label>` — existing: verify disc in local drive
- `lcsas verify <label> --mark-verified` — manual marking (for remote verification)
- `lcsas verify <label> --mark-failed --detail "reason"` — record failure

Both record a `volume_event`. `--mark-verified` transitions status to VERIFIED
and records a `VERIFY_PASS` event. This supports workflows where a user burns
on machine A and verifies on machine B, then imports the result.

### 14.3  `verify --all` batch verification
`lcsas verify --all [--location <loc>]` — iterate all BURNED or VERIFIED volumes
(optionally filtered by location) and verify each. Record events. Report summary.

Files:
- `burn/orchestrator.py` — burn_session() verify step
- `cli/main.py` — verify handler extensions
- `db/volume_events.py` — add_event() calls
- Tests: test_burn_orchestrator.py, test_cli_comprehensive.py

---

## Phase 15 — Resilient Restore  [S2, S4]

### 15.1  Pick list with alternate volumes  [S4]
Redesign `get_pick_list()` to return alternate sources:

```python
@dataclass(frozen=True)
class PackSource:
    pack: Pack
    volume_label: str
    volume_id: int
    alternates: list[str]  # other volume labels holding this pack

def get_pick_list_with_alternates(
    conn, pack_sha256_list, preferred_location=""
) -> dict[str, list[PackSource]]:
```

The query joins `packs → volume_packs → volumes → volume_copies` and for each
pack, collects ALL volumes that hold it (preferring volumes at the preferred
location, then by most-recently-verified). The primary assignment goes to the
first volume; remaining volumes populate `alternates`.

`RestorePlanner` returns a new `PickListV2` with `alternates` data.

### 15.2  Executor retry on corruption  [S2]
`RestoreExecutor.ingest_volume()` currently raises `PackCorruptionError` immediately.

New flow:
1. On SHA-256 mismatch, log warning and add pack to `failed_packs` set
2. Return `failed_packs` from `ingest_volume()` instead of raising
3. The CLI handler checks `failed_packs` after each volume
4. For each failed pack, consult `alternates` from the pick list
5. Prompt user to insert the alternate volume (or auto-skip if not interactive)
6. Re-attempt ingestion of failed packs from alternate source
7. If ALL alternates exhausted, raise `PackCorruptionError` with full details

The executor needs a new method:
```python
def ingest_packs_from_volume(
    self, mount_point: Path, packs: list[Pack]
) -> list[Pack]:  # returns failed packs
```

### 15.3  Integration test
Add `test_alternate_volume_restore.py` in `tests/integration/`:
- Create 2 repos, stage to 3 volumes with overlapping packs
- Corrupt one pack on volume 1
- Verify restore succeeds by falling back to volume 2's copy
- Verify VERIFY_FAIL event recorded for volume 1

Files:
- `db/queries.py` — get_pick_list_with_alternates()
- `restore/planner.py` — PickListV2, PackSource
- `restore/executor.py` — ingest return failed, new method
- `cli/main.py` — restore exec handler retry loop
- NEW: tests/integration/test_alternate_volume_restore.py
- Tests: test_restore.py, test_restore_executor.py, test_cli_restore.py

---

## Phase 16 — Prune Sync  [D3]

### 16.1  Detect pruned packs
Add to `DeltaAnalyzer`:
```python
def detect_pruned(self) -> list[Pack]:
    """Find packs in DB for this repo that are no longer on the mirror."""
```
Query: `SELECT * FROM packs WHERE repo_id = ? AND is_pruned = 0`
Filter: pack.sha256 NOT IN scanner_result keys.

### 16.2  Bulk mark pruned
Add `bulk_mark_pruned(conn, pack_ids: list[int])` to `packs.py`.

### 16.3  Integration with `scan` command
In `cmd_scan()`, after registering new packs, call `detect_pruned()`.
If pruned packs found:
- Print count and total bytes
- Auto-mark as pruned (with `--no-prune-sync` flag to disable)
- Record volume_event NOTE on affected volumes

Files:
- `packs/delta.py` — detect_pruned()
- `db/packs.py` — bulk_mark_pruned()
- `cli/main.py` — cmd_scan() prune detection
- Tests: test_scanner_delta.py, test_db_packs.py

---

## Phase 17 — Staging & Pack Layout  [A1, A6]

### 17.1  Two-level pack layout on disc  [A6]
Change `StagingBuilder.stage_packs()` to use two-level layout:
```python
prefix_dir = self._data_dir / pack.sha256[:2]
prefix_dir.mkdir(exist_ok=True)
dst = prefix_dir / pack.sha256
```

This matches what `RestoreExecutor.ingest_volume()` expects as its
preferred source layout. The executor's flat-then-two-level fallback
remains for backward compatibility with v3 discs that used flat layout.

Update `_find_pack_file()` to search two-level first, then flat.

### 17.2  Oversized pack warning  [A1]
In `_multi_bin_pack()`, after the bin-pack loop, check for perpetually
unfit packs:
```python
usable = media_type.usable_bytes - self._config.metadata_reserve_bytes
oversized = [p for p in remaining if p.size_bytes > usable]
if oversized:
    logger.warning(
        "%d pack(s) exceed %s usable capacity (%s) and cannot be archived: %s",
        len(oversized), media_type.name, format_bytes(usable),
        ", ".join(p.sha256[:12] for p in oversized)
    )
```

Also raise `ValueError` if ALL remaining packs are oversized (infinite loop guard).

Files:
- `staging/builder.py` L70 — two-level layout
- `burn/orchestrator.py` — _multi_bin_pack() warning
- `restore/executor.py` — search order: two-level first
- Tests: test_staging.py, test_binpack.py

---

## Phase 18 — Pure-Python Restore Improvements  [A7, A8]

### 18.1  Hard link support  [A7]
In `_restore_tree()`, maintain a `dict[str, Path]` mapping `inode → first_restored_path`.
Restic file nodes include `"inode"` and `"links"` (link count). When `links > 1`:
- First occurrence: restore normally, record `inode → path`
- Subsequent occurrences: `os.link(first_path, current_path)`

Fallback: if `os.link` fails (cross-device), copy normally and log warning.

### 18.2  Device/fifo/socket warnings  [A7]
Change the silent skip at L622–624 to log a warning:
```python
else:
    logger.warning("Skipping unsupported node type %r: %s", node_type, name)
```

### 18.3  Extended attribute restoration  [A8]
In `_apply_metadata()`, after `os.chmod()` and `os.utime()`:
```python
for xa in node.get("extended_attributes", []):
    try:
        os.setxattr(path, xa["name"], base64.b64decode(xa["value"]),
                     follow_symlinks=False)
    except OSError:
        pass  # best-effort, log warning
```

Restic stores xattrs as `[{"name": "user.foo", "value": "<base64>"}]`.

Files:
- `restore/restic_fallback.py` L600–665
- Tests: test_restic_fallback.py (unit test with mock tree data)

---

## Phase 19 — CLI & Operational Improvements  [O3, U2, U3, U4, U5, U11]

### 19.1  `locked_connection` for ALL write commands  [O3]
Change these handlers from `get_connection` to `locked_connection`:
- `cmd_repo_add` — writes to repositories
- `cmd_location` (add/move/status subcommands) — writes to locations, volume_copies
- `cmd_catalog_import` — writes to volume_copies
- `cmd_verify` — writes to volumes, volume_events
- `cmd_consolidate` — may write to volumes (deprecate_sources)
- `cmd_restore_exec` — reads only (no change needed)

Read-only handlers (`status`, `repo list`, `db export`, `restore plan`) stay
with `get_connection` since WAL mode allows concurrent readers.

### 19.2  `repo remove` command  [U2]
Add `lcsas repo remove <repo_id> [--force]`:
- Without `--force`: refuse if repo has any non-pruned, unarchived packs
- With `--force`: mark all packs as pruned, delete snapshots, delete repo
- Always refuse if repo has packs on active volumes (data would become orphaned)

Add `delete_repo(conn, repo_id)` to `repos.py` and `delete_snapshots_for_repo()`
to `snapshots.py`.

### 19.3  `consolidate --execute`  [U3]
Extend `cmd_consolidate` to accept `--execute` flag:
1. Generate plan (existing code)
2. If `--execute`: call `stage()` with only the active packs from the plan
3. After staging + burning succeeds, call `deprecate_sources()`
4. Print summary of deprecated volumes and new volumes

This reuses the existing stage/burn pipeline — no new burn logic needed.

### 19.4  Deprecation safety check  [U4]
Before `update_status(DEPRECATED)`, verify:
```python
def check_deprecation_safe(conn, volume_id) -> list[str]:
    """Return list of pack sha256s that would become unreplicated."""
```
Query: packs on this volume that exist on NO other ACTIVE/VERIFIED volume.
If non-empty, `update_status()` raises `ValueError` listing the at-risk packs.
Override with `force=True` parameter.

### 19.5  Unknown TOML key validation  [U5]
In `load_config()`, collect all keys from the TOML dict and compare against
known sections/keys. Warning log for unknown keys:
```python
KNOWN_SECTIONS = {"paths", "defaults", "repos", "survivability"}
KNOWN_PATH_KEYS = {"mirror_base", "staging", "database"}
# etc.
unknown = set(raw.keys()) - KNOWN_SECTIONS
if unknown:
    logger.warning("Unknown config sections: %s (typo?)", unknown)
```

### 19.6  XDG-compliant `db_path` default  [U11]
Change `default_config()` to use `~/.local/share/lcsas/archive.db` instead
of `/var/lib/lcsas/archive.db`. The `load_config()` function already accepts
any path from TOML, so this only affects the no-config-file default.
Also update `cmd_init()` to create the XDG directory if needed.

Files:
- `cli/main.py` — multiple handlers
- `db/repos.py` — delete_repo()
- `db/snapshots.py` — delete_snapshots_for_repo()
- `db/volumes.py` — check_deprecation_safe()
- `config/settings.py` — TOML validation, default path
- `consolidate/merger.py` — execute flow
- Tests: test_cli.py, test_config.py, test_db_repos.py, test_db_volumes.py, test_consolidate.py

---

## Phase 20 — Documentation  [D8, U6, U7]

### 20.1  Document catalog encryption tradeoff  [D8]
Add a "Security Considerations" section to `architecture.md`:
- The catalog DB is intentionally unencrypted and embedded on every disc
- This enables self-describing recovery without the encryption key
- File paths, hostnames, and snapshot timestamps are visible in the catalog
- Pack contents remain encrypted — only metadata is exposed
- For sensitive archives, consider: separate metadata-scrubbed catalog variant (future)

### 20.2  Architecture doc refresh  [U6]
Update `docs/architecture.md` to reflect current state:
- Add missing tables: `locations`, `volume_copies`, `burn_sessions`,
  `session_volumes`, `volume_events` (new in v4)
- Fix column names (`capacity` → `capacity_bytes`, `display_name` → `name`)
- Add `BURNING` and `DESTROYED` to volume lifecycle
- Fix staging layout: flat → two-level (after Phase 17)
- Fix ECC overhead: 20% → 15%
- Add sections on: multi-location tracking, session-based burns,
  resilient restore, prune sync

### 20.3  DEVELOPMENT_PLAN refresh  [U7]
Update `docs/DEVELOPMENT_PLAN.md`:
- Mark Phases 8 (prune sync), 9 (verification tracking) as done
  (after implementing them in Phases 14, 16)
- Mark Phase 11 (survivability) as done
- Remove stale §2.1 (duplicate queries), §3.1 (unparsed commands), §3.2 (missing commands)
- Update test count from 561 to current
- Add Phase 12–20 summary with completion status

Files:
- `docs/architecture.md`
- `docs/DEVELOPMENT_PLAN.md`

---

## Dependency Graph

```
Phase 12 (Schema v4) ──┬──> Phase 14 (Verification) ──> Phase 15 (Resilient Restore)
                        │
Phase 13 (Refactor)  ───┘

Phase 16 (Prune Sync)          — independent
Phase 17 (Layout)              — independent, before Phase 20
Phase 18 (Pure-Python)         — independent
Phase 19 (CLI/Ops)             — after Phase 12 (needs volume_events, transitions)
Phase 20 (Docs)                — last (documents everything above)
```

Parallelizable: 16, 17, 18 can proceed independently alongside 14/15.

---

## Verification Strategy

After each phase:
1. `make test` — all existing tests pass
2. New tests cover every changed function
3. Integration tests verify end-to-end flows
4. `ruff check src/ tests/` — no lint regressions

Final acceptance:
- Full test suite green (target: ~700+ tests)
- e2e_test.py passes
- Manual burn + verify + restore on real BD-R media
- Architecture doc matches code

---

## Item Cross-Reference

| Audit ID | Phase | Section | Status |
|----------|-------|---------|--------|
| S1       | 14    | 14.1–14.3 | Planned |
| S2       | 15    | 15.2    | Planned |
| S4       | 15    | 15.1    | Planned |
| O3       | 19    | 19.1    | Planned |
| O4       | 13    | 13.4    | Planned |
| D1       | 12    | 12.1    | Planned |
| D3       | 16    | 16.1–16.3 | Planned |
| D4       | 12    | 12.3    | Planned |
| D5       | 12    | 12.2    | Planned |
| D6       | 12    | 12.4    | Addressed via D1 |
| D7       | 12    | 12.5    | Planned |
| D8       | 20    | 20.1    | Planned |
| A1       | 17    | 17.2    | Planned |
| A2       | 13    | 13.1    | Planned |
| A3       | 13    | 13.2    | Planned |
| A4       | 13    | 13.3    | Planned |
| A6       | 17    | 17.1    | Planned |
| A7       | 18    | 18.1–18.2 | Planned |
| A8       | 18    | 18.3    | Planned |
| U2       | 19    | 19.2    | Planned |
| U3       | 19    | 19.3    | Planned |
| U4       | 19    | 19.4    | Planned |
| U5       | 19    | 19.5    | Planned |
| U6       | 20    | 20.2    | Planned |
| U7       | 20    | 20.3    | Planned |
| U11      | 19    | 19.6    | Planned |

---

## Decisions

- **D6 (per-volume pack hash):** Addressed via `volume_events` rather than
  denormalizing `sha256` into `volume_packs`. Verification events in D1 track
  per-volume integrity; the `detail` field can record failed pack lists.
- **S3 (catalog merge):** Skipped — the holographic design (latest disc has
  cumulative catalog) makes this unnecessary in practice.
- **O1 (subprocess timeouts):** Ignored per request.
- **O2 (LTO tape I/O):** Future work, not addressed in this plan.
- **S1 remote verification:** Implemented via `--mark-verified` / `--mark-failed`
  CLI flags (Phase 14.2), allowing verification on a different machine and manual
  status import.
- **A6 backward compatibility:** Executor retains flat-then-two-level source search
  so v3-era discs remain readable.
