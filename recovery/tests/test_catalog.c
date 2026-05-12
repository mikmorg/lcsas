/*
 * test_catalog.c -- catalog reader against a synthesized schema-v5 DB.
 *
 * Builds a temporary SQLite DB with the LCSAS schema (subset:
 * schema_version, repositories, packs, volumes, volume_packs) and
 * verifies lcsas_catalog_* lookups return the expected rows.
 */
#include "catalog.h"
#include "../vendored/sqlite/sqlite3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int fails = 0;

static int
exec_sql(sqlite3 *db, const char *sql)
{
    char *err = NULL;
    int rc = sqlite3_exec(db, sql, NULL, NULL, &err);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "SQL error: %s\n", err ? err : "?");
        sqlite3_free(err);
        return -1;
    }
    return 0;
}

int main(void)
{
    const char *db_path = "/tmp/lcsas_catalog_test.db";
    sqlite3 *db = NULL;
    lcsas_catalog *c;
    lcsas_catalog_pack pk;
    lcsas_catalog_volume vols[8];
    int n;

    unlink(db_path);

    if (sqlite3_open(db_path, &db) != SQLITE_OK) {
        fprintf(stderr, "FAIL: sqlite3_open\n");
        return 1;
    }

    if (exec_sql(db,
        "CREATE TABLE schema_version (version INTEGER, applied_at DATETIME);"
        "INSERT INTO schema_version VALUES (5, datetime('now'));"
        "CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT,"
        "  mirror_path TEXT NOT NULL, encryption_key_id TEXT DEFAULT '',"
        "  created_at DATETIME);"
        "INSERT INTO repositories VALUES "
        "  ('repo-abc', 'main', '/srv/repo', '', datetime('now'));"
        "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  sha256 TEXT UNIQUE NOT NULL, size_bytes INTEGER, repo_id TEXT,"
        "  is_pruned INTEGER DEFAULT 0);"
        "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES"
        "  ('aa11', 1024, 'repo-abc'),"
        "  ('bb22', 2048, 'repo-abc'),"
        "  ('cc33', 4096, 'repo-abc');"
        "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  label TEXT UNIQUE, uuid TEXT UNIQUE, media_type TEXT,"
        "  capacity_bytes INTEGER, used_bytes INTEGER DEFAULT 0,"
        "  location TEXT DEFAULT 'Home_Shelf', status TEXT DEFAULT 'STAGING',"
        "  created_at DATETIME, closed_at DATETIME, verified_at DATETIME);"
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) VALUES"
        "  ('vol-001', 'uuid-aaa', 'BD25', 26843545600, 'VERIFIED'),"
        "  ('vol-002', 'uuid-bbb', 'BD25', 26843545600, 'VERIFIED'),"
        "  ('vol-003', 'uuid-ccc', 'BD25', 26843545600, 'DESTROYED');"
        "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER,"
        "  PRIMARY KEY (volume_id, pack_id));"
        "INSERT INTO volume_packs VALUES"
        "  (1, 1), (1, 2),"
        "  (2, 2),"
        "  (3, 2);"  /* vol-003 is DESTROYED -> should be filtered out */
        ) < 0) {
        fprintf(stderr, "FAIL: schema setup\n");
        sqlite3_close(db);
        return 1;
    }
    sqlite3_close(db);

    c = lcsas_catalog_open(db_path);
    if (!c) { fprintf(stderr, "FAIL: open\n"); return 1; }

    if (lcsas_catalog_schema_version(c) != 5) {
        fprintf(stderr, "FAIL: schema_version\n");
        fails++;
    }

    if (lcsas_catalog_find_pack(c, "bb22", &pk) != 0) {
        fprintf(stderr, "FAIL: find_pack(bb22)\n");
        fails++;
    } else {
        if (pk.size_bytes != 2048) { fprintf(stderr, "FAIL: pk size\n"); fails++; }
        if (strcmp(pk.repo_id, "repo-abc") != 0) {
            fprintf(stderr, "FAIL: pk repo_id\n"); fails++;
        }
    }

    if (lcsas_catalog_find_pack(c, "deadbeef", &pk) != -1) {
        fprintf(stderr, "FAIL: find_pack should miss\n");
        fails++;
    }

    /* Pack 2 lives in vols 1 and 2 (vol-003 is DESTROYED, filtered out). */
    n = lcsas_catalog_volumes_for_pack(c, 2, vols, 8);
    if (n != 2) {
        fprintf(stderr, "FAIL: volumes_for_pack(2) returned %d (want 2)\n", n);
        fails++;
    }
    if (n >= 1 && strcmp(vols[0].label, "vol-001") != 0) {
        fprintf(stderr, "FAIL: first volume label %s\n", vols[0].label);
        fails++;
    }
    if (n >= 2 && strcmp(vols[1].label, "vol-002") != 0) {
        fprintf(stderr, "FAIL: second volume label %s\n", vols[1].label);
        fails++;
    }

    /* Pack 1 lives only in vol-001. */
    n = lcsas_catalog_volumes_for_pack(c, 1, vols, 8);
    if (n != 1) {
        fprintf(stderr, "FAIL: volumes_for_pack(1) returned %d (want 1)\n", n);
        fails++;
    }

    lcsas_catalog_close(c);
    unlink(db_path);

    if (fails == 0) printf("test_catalog: OK\n");
    return fails ? 1 : 0;
}
