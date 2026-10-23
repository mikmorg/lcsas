/*
 * catalog.h -- SQLite catalog reader for the LCSAS holographic catalog.
 *
 * Provides volume-to-pack lookup: given a pack hash, return the volume
 * label (and disc copy locations) where that pack can be found.  This
 * drives the disc-swap prompts during multi-volume recovery.
 *
 * Schema version 5 (see src/lcsas/db/schema.py).  Catalog files are
 * named "catalog.db" and live at the root of every burned volume.
 */
#ifndef LCSAS_CATALOG_H
#define LCSAS_CATALOG_H

#include <stddef.h>

typedef struct lcsas_catalog lcsas_catalog;

typedef struct {
    long long pack_id;
    char sha256_hex[65];
    long long size_bytes;
    char repo_id[128];
} lcsas_catalog_pack;

typedef struct {
    long long volume_id;
    char label[64];
    char uuid[64];
    char media_type[16];
    char status[16];
} lcsas_catalog_volume;

/*
 * Open a catalog file (read-only).  Returns NULL on error.  When the
 * first query on a plain read-only open fails (a WAL-mode catalog on
 * read-only media), it is reopened as immutable (#543).
 */
lcsas_catalog *lcsas_catalog_open(const char *path);

/*
 * Close.
 */
void lcsas_catalog_close(lcsas_catalog *c);

/*
 * Read the schema version from the schema_version table.  Returns -1
 * on error.
 */
int lcsas_catalog_schema_version(lcsas_catalog *c);

/*
 * Look up a pack by its SHA-256 hex string.  Tri-state return:
 *    0  = found (out is populated)
 *    1  = not found (a genuine miss against a readable v5 surface)
 *   -1  = query error: the catalog was written by a newer LCSAS that
 *         renamed/dropped a frozen column (see the schema-skew warning
 *         emitted to stderr), or it could not be read at all.
 *         lcsas_catalog_error_is_skew tells the two apart.  Callers MUST
 *         distinguish -1 from 1: a -1 means "could not ask", not "asked
 *         and the pack isn't cataloged".
 *
 * The queried column set is the TIER-1 FROZEN SURFACE pinned in
 * src/lcsas/db/schema.py / tests/unit/test_schema_v5_columns_frozen.py.
 */
int lcsas_catalog_find_pack(lcsas_catalog *c, const char *sha256_hex,
                            lcsas_catalog_pack *out);

/*
 * Given a pack_id, list volumes that contain it.  Writes up to
 * max_vols entries into `out`; returns the number written (>= 0), or -1
 * on query error (as for lcsas_catalog_find_pack).
 */
int lcsas_catalog_volumes_for_pack(lcsas_catalog *c, long long pack_id,
                                   lcsas_catalog_volume *out,
                                   size_t max_vols);

/*
 * After a -1 from a lookup, before any other catalog call: SQLite's
 * message for the failure ...
 */
const char *lcsas_catalog_errmsg(lcsas_catalog *c);

/*
 * ... and whether it is schema skew: the query named a table or column
 * the catalog lacks, as when a newer LCSAS renamed or dropped one.  0
 * means the catalog could not be read (damaged, or unreadable where it
 * sits), which is no reason to look for a newer meta disc.
 */
int lcsas_catalog_error_is_skew(lcsas_catalog *c);

/*
 * Print a one-line summary to stderr for human-readable recovery
 * progress (used by the CLI in verbose mode).
 */
void lcsas_catalog_describe(lcsas_catalog *c);

/*
 * Print a "pending packs by disc" summary to stdout, then exit.
 * Groups all packs (catalog-wide) by volume label, sorted by pack
 * count descending.  This is the --list-pending-packs path: it gives
 * the operator a preview of which discs they will need to insert
 * before any actual restore begins.
 *
 * Returns 0 on success, -1 on SQL error.
 */
int lcsas_catalog_print_pending_packs(lcsas_catalog *c);

#endif
