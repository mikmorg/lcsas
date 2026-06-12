# RUNBOOK — Recovering a catalog wedged by an interrupted schema migration

## When you need this

An LCSAS command refused to run with an error like:

> Catalog contains leftover table(s) volumes_old from an interrupted
> schema migration. Do NOT continue. Restore the catalog from backup,
> or recover manually: the original data is intact in volumes_old.
> See docs/RUNBOOK_migration_recovery.md.

**Your data is NOT lost.** Read on.

## What happened

Older LCSAS builds ran the table-recreating schema migrations
(v4→v5 rebuilds `volumes`, v5→v6 rebuilds `volume_events`) as a
`RENAME → CREATE → INSERT…SELECT → DROP` sequence **without a
transaction**. If the process died mid-sequence, the catalog was left
with the original table renamed to `volumes_old` (or
`volume_events_old`) and no live table in its place — and, if an
old build was run again afterwards, possibly an **empty** shadow table
recreated over it, making the catalog look empty.

Current LCSAS builds run every migration step inside one transaction
(crash anywhere rolls the whole step back), cannot produce this state,
and refuse to touch a catalog that has it — pointing here instead.

All of the original rows are intact inside the `*_old` table.

## Option A — restore from backup (preferred)

If you have a copy of the catalog from before the interrupted
migration, use it and re-run any LCSAS command; the migration re-runs
atomically and completes.

Remember the holographic design: **every burned disc carries a complete
catalog snapshot** (`catalog.db` at the disc root). If the NAS catalog
is the only damaged copy, you can rebuild from disc copies instead of
repairing by hand:

```bash
lcsas catalog rebuild /mnt/disc1 /mnt/disc2 --output rebuilt.db
```

## Option B — manual recovery (two statements per table)

Open the catalog with the sqlite3 shell:

```bash
sqlite3 /path/to/archive.db
```

### If the leftover is `volumes_old` (interrupted v4→v5)

First check whether an old build already recreated an empty shadow
`volumes` table:

```sql
SELECT COUNT(*) FROM volumes;
```

- Error `no such table: volumes` → no shadow; **skip the DROP** below.
- `0` → empty shadow; safe to drop.
- Anything else → **STOP.** Both tables hold rows; do not guess.
  Restore from backup or rebuild from disc copies (Option A).

Then run the recovery:

```sql
DROP TABLE volumes;
ALTER TABLE volumes_old RENAME TO volumes;
```

### If the leftover is `volume_events_old` (interrupted v5→v6)

Same pattern:

```sql
SELECT COUNT(*) FROM volume_events;
```

(`no such table` → skip the DROP; `0` → safe; anything else → STOP.)

```sql
DROP TABLE volume_events;
ALTER TABLE volume_events_old RENAME TO volume_events;
```

### Finish

Exit sqlite3 (`.quit`) and re-run any LCSAS command, e.g.:

```bash
lcsas status
```

The pending migration re-runs — atomically this time — and the catalog
comes back at the current schema version with all rows intact.

## If anything looks wrong

Stop and fall back to Option A. The on-disc holographic catalog copies
are the designed mitigation for exactly this failure: losing the
writable NAS catalog never loses the archive.
