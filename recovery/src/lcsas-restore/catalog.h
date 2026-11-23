/*
 * catalog.h -- SQLite catalog reader for the LCSAS disc catalog.
 *
 * Provides volume-to-pack lookup: given a pack ID, return the label of
 * each volume that holds it (and where its discs are kept).  This
 * drives the disc-swap prompts during a multi-disc restore.
 *
 * Queries only the frozen tier-1 surface every schema since version 5
 * keeps (TIER1_FROZEN_SURFACE in src/lcsas/db/schema.py).  Catalog
 * files are named "catalog.db" and live at the root of every data disc.
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

/* A volume, from the frozen columns only (#961). */
typedef struct {
    long long volume_id;
    char label[64];
    char status[16];
} lcsas_catalog_volume;

/*
 * Open a catalog file (read-only).  Returns NULL on error.  When the
 * first query on a plain read-only open fails (a WAL-mode catalog on
 * read-only media), it is reopened as immutable (#543).
 */
lcsas_catalog *lcsas_catalog_open(const char *path);

/*
 * Check that an opened catalog can be read through (#788): it has a
 * schema version and SQLite's PRAGMA quick_check finds every page in
 * order.  Returns 0 when it can, -1 when it is damaged or cut short
 * (lcsas_catalog_open succeeds on such a file: SQLite reads pages only
 * when a query needs them).
 */
int lcsas_catalog_check(lcsas_catalog *c);

/*
 * Close.
 */
void lcsas_catalog_close(lcsas_catalog *c);

/*
 * Read the schema version: the highest row of the schema_version table
 * (#961; a migrated catalog keeps a row per step).  Returns -1 on
 * error or when the table has no row.
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
 * Given a pack_id, list volumes that contain it, newest first.  Writes up to
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
 * Print a one-line summary to stderr for human-readable restore
 * progress (used by the CLI in verbose mode).
 */
void lcsas_catalog_describe(lcsas_catalog *c);

/*
 * Print a "pending packs by disc" summary to stdout.  Groups the packs
 * by volume label, sorted by pack count descending: every pack in the
 * catalog, or with `repo_id` non-NULL only that repository's (#798).
 * This is the --list-pending-packs path: it gives the heir a preview of
 * which discs they will need to insert before any actual restore
 * begins, and the scripts their size estimate, from the last line:
 *     Total: 4 packs, 0.2 MB. They are on 2 discs; some hold the same packs.
 * The total counts each pack once (#951); "They are on ..." is left out
 * when no disc holds one, and "; some hold ..." when no pack is on two.
 *
 * Returns 0 on success, -1 (and prints nothing on stdout) when the
 * catalog cannot be read.
 */
int lcsas_catalog_print_pending_packs(lcsas_catalog *c, const char *repo_id);

#endif
