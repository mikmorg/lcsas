/*
 * catalog.c -- SQLite catalog reader.
 *
 * Wraps the vendored SQLite amalgamation (vendored/sqlite/sqlite3.h).
 * Read-only access: never opens a database for writing.
 *
 * Schema version 5.  See docs/FORMAT.txt for the full schema.
 */
#include "catalog.h"
#include "../../vendored/sqlite/sqlite3.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct lcsas_catalog {
    sqlite3 *db;
};

lcsas_catalog *
lcsas_catalog_open(const char *path)
{
    lcsas_catalog *c;
    int rc;

    c = (lcsas_catalog *)malloc(sizeof(*c));
    if (!c) return NULL;
    c->db = NULL;
    rc = sqlite3_open_v2(path, &c->db, SQLITE_OPEN_READONLY, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "catalog open failed: %s\n",
                c->db ? sqlite3_errmsg(c->db) : "unknown");
        sqlite3_close(c->db);
        free(c);
        return NULL;
    }
    return c;
}

void
lcsas_catalog_close(lcsas_catalog *c)
{
    if (!c) return;
    if (c->db) sqlite3_close(c->db);
    free(c);
}

int
lcsas_catalog_schema_version(lcsas_catalog *c)
{
    sqlite3_stmt *st = NULL;
    int rc;
    int version = -1;

    rc = sqlite3_prepare_v2(c->db,
            "SELECT version FROM schema_version", -1, &st, NULL);
    if (rc != SQLITE_OK) return -1;
    if (sqlite3_step(st) == SQLITE_ROW) {
        version = sqlite3_column_int(st, 0);
    }
    sqlite3_finalize(st);
    return version;
}

static void
copy_text_col(sqlite3_stmt *st, int col, char *dst, size_t cap)
{
    const unsigned char *t = sqlite3_column_text(st, col);
    size_t n;
    if (!t) { dst[0] = '\0'; return; }
    n = strlen((const char *)t);
    if (n >= cap) n = cap - 1;
    memcpy(dst, t, n);
    dst[n] = '\0';
}

int
lcsas_catalog_find_pack(lcsas_catalog *c, const char *sha256_hex,
                        lcsas_catalog_pack *out)
{
    sqlite3_stmt *st = NULL;
    int rc;
    int found = 0;

    rc = sqlite3_prepare_v2(c->db,
            "SELECT pack_id, sha256, size_bytes, repo_id "
            "FROM packs WHERE sha256 = ?", -1, &st, NULL);
    if (rc != SQLITE_OK) return -1;
    sqlite3_bind_text(st, 1, sha256_hex, -1, SQLITE_STATIC);
    if (sqlite3_step(st) == SQLITE_ROW) {
        out->pack_id = sqlite3_column_int64(st, 0);
        copy_text_col(st, 1, out->sha256_hex, sizeof(out->sha256_hex));
        out->size_bytes = sqlite3_column_int64(st, 2);
        copy_text_col(st, 3, out->repo_id, sizeof(out->repo_id));
        found = 1;
    }
    sqlite3_finalize(st);
    return found ? 0 : -1;
}

int
lcsas_catalog_volumes_for_pack(lcsas_catalog *c, long long pack_id,
                               lcsas_catalog_volume *out,
                               size_t max_vols)
{
    sqlite3_stmt *st = NULL;
    int rc;
    size_t count = 0;

    rc = sqlite3_prepare_v2(c->db,
            "SELECT v.volume_id, v.label, v.uuid, v.media_type, v.status "
            "FROM volumes v "
            "JOIN volume_packs vp ON vp.volume_id = v.volume_id "
            "WHERE vp.pack_id = ? AND v.status != 'DESTROYED' "
            "ORDER BY v.volume_id",
            -1, &st, NULL);
    if (rc != SQLITE_OK) return -1;
    sqlite3_bind_int64(st, 1, pack_id);
    while (count < max_vols && sqlite3_step(st) == SQLITE_ROW) {
        out[count].volume_id = sqlite3_column_int64(st, 0);
        copy_text_col(st, 1, out[count].label, sizeof(out[count].label));
        copy_text_col(st, 2, out[count].uuid, sizeof(out[count].uuid));
        copy_text_col(st, 3, out[count].media_type,
                      sizeof(out[count].media_type));
        copy_text_col(st, 4, out[count].status,
                      sizeof(out[count].status));
        count++;
    }
    sqlite3_finalize(st);
    return (int)count;
}

void
lcsas_catalog_describe(lcsas_catalog *c)
{
    sqlite3_stmt *st = NULL;
    int v = lcsas_catalog_schema_version(c);

    fprintf(stderr, "[catalog] schema v%d\n", v);
    if (sqlite3_prepare_v2(c->db,
            "SELECT COUNT(*) FROM packs", -1, &st, NULL) == SQLITE_OK) {
        if (sqlite3_step(st) == SQLITE_ROW) {
            fprintf(stderr, "[catalog] packs: %d\n",
                    sqlite3_column_int(st, 0));
        }
        sqlite3_finalize(st);
    }
    if (sqlite3_prepare_v2(c->db,
            "SELECT COUNT(*) FROM volumes WHERE status != 'DESTROYED'",
            -1, &st, NULL) == SQLITE_OK) {
        if (sqlite3_step(st) == SQLITE_ROW) {
            fprintf(stderr, "[catalog] volumes: %d\n",
                    sqlite3_column_int(st, 0));
        }
        sqlite3_finalize(st);
    }
    if (sqlite3_prepare_v2(c->db,
            "SELECT COUNT(*) FROM snapshots",
            -1, &st, NULL) == SQLITE_OK) {
        if (sqlite3_step(st) == SQLITE_ROW) {
            fprintf(stderr, "[catalog] snapshots: %d\n",
                    sqlite3_column_int(st, 0));
        }
        sqlite3_finalize(st);
    }
}
