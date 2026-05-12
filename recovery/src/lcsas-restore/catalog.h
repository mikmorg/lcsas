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
 * Open a catalog file (read-only).  Returns NULL on error.
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
 * Look up a pack by its SHA-256 hex string.  Returns 0 on success,
 * -1 on miss / error.
 */
int lcsas_catalog_find_pack(lcsas_catalog *c, const char *sha256_hex,
                            lcsas_catalog_pack *out);

/*
 * Given a pack_id, list volumes that contain it.  Writes up to
 * max_vols entries into `out`; returns the number written, or -1.
 */
int lcsas_catalog_volumes_for_pack(lcsas_catalog *c, long long pack_id,
                                   lcsas_catalog_volume *out,
                                   size_t max_vols);

/*
 * Print a one-line summary to stderr for human-readable recovery
 * progress (used by the CLI in verbose mode).
 */
void lcsas_catalog_describe(lcsas_catalog *c);

#endif
