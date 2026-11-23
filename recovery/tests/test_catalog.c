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
#include <sys/stat.h>
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

/* Create (or replace) the database at `path` from `sql`. */
static int
make_db(const char *path, const char *sql)
{
    sqlite3 *db = NULL;
    int rc;
    unlink(path);
    if (sqlite3_open(path, &db) != SQLITE_OK) {
        sqlite3_close(db);
        return -1;
    }
    rc = exec_sql(db, sql);
    sqlite3_close(db);
    return rc;
}

/*
 * Run lcsas_catalog_print_pending_packs with stdout sent to a temp
 * file; its output goes to `buf`.  Returns the call's return value.
 */
static int
pending_output(lcsas_catalog *c, const char *repo_id, char *buf, size_t cap)
{
    FILE *f = tmpfile();
    int saved;
    int rc;
    size_t n = 0;

    buf[0] = '\0';
    if (!f) return -2;
    fflush(stdout);
    saved = dup(1);
    dup2(fileno(f), 1);
    rc = lcsas_catalog_print_pending_packs(c, repo_id);
    fflush(stdout);
    dup2(saved, 1);
    close(saved);
    rewind(f);
    n = fread(buf, 1, cap - 1, f);
    buf[n] = '\0';
    fclose(f);
    return rc;
}

/* The schema-v10 shape of the tables tier 1 reads (frozen columns, and
 * the others a burn writes, so a query naming one is not caught out by
 * the fixture). */
#define V10_TABLES \
    "CREATE TABLE schema_version (version INTEGER, applied_at DATETIME);" \
    "INSERT INTO schema_version VALUES (10, datetime('now'));" \
    "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT," \
    "  sha256 TEXT UNIQUE NOT NULL, size_bytes INTEGER, repo_id TEXT," \
    "  is_pruned INTEGER DEFAULT 0);" \
    "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT," \
    "  label TEXT UNIQUE, uuid TEXT, media_type TEXT, status TEXT);" \
    "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER," \
    "  PRIMARY KEY (volume_id, pack_id));"

/*
 * #951: the size of a backup set counts each pack once, however many
 * volumes hold it: an archive kept in two places, a lost volume burned
 * again.  A pack on DESTROYED volumes only is not counted.  The last
 * line does not promise that every disc listed is needed.
 */
static void
test_pending_counts_each_pack_once(void)
{
    const char *p = "/tmp/lcsas_catalog_test_twice.db";
    char out[2048];
    lcsas_catalog *c;
    int rc;

    if (make_db(p, V10_TABLES
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES"
            "  ('p1', 100000, 'sec'), ('p2', 50000, 'sec'),"
            "  ('p3', 50000, 'sec'), ('p4', 32100, 'sec'),"
            "  ('p5', 999999, 'sec'), ('o1', 4096, 'other');"
            "INSERT INTO volumes (label, status) VALUES"
            "  ('HOME_0001', 'VERIFIED'), ('AWAY_0002', 'VERIFIED'),"
            "  ('GONE_0003', 'DESTROYED');"
            /* p1..p4 at home and away; p5 only on the DESTROYED one. */
            "INSERT INTO volume_packs VALUES (1, 1), (1, 2), (1, 3), (1, 4),"
            "  (2, 1), (2, 2), (2, 3), (2, 4), (3, 5), (1, 6);") != 0) {
        fprintf(stderr, "FAIL #951: fixture\n");
        fails++;
        return;
    }
    c = lcsas_catalog_open(p);
    if (!c) { fprintf(stderr, "FAIL #951: open\n"); fails++; return; }
    rc = pending_output(c, "sec", out, sizeof out);
    if (rc != 0 || !strstr(out,
            "Total: 4 packs, 0.2 MB. They are on 2 discs; some hold the "
            "same packs.\n")) {
        fprintf(stderr, "FAIL #951: a pack on two volumes is counted "
                "twice (rc %d):\n%s", rc, out);
        fails++;
    }
    if (!strstr(out, "  HOME_0001: 4 packs (0.2 MB)\n")
            || !strstr(out, "  AWAY_0002: 4 packs (0.2 MB)\n")
            || strstr(out, "GONE_0003")) {
        fprintf(stderr, "FAIL #951: the per-disc list:\n%s", out);
        fails++;
    }
    /* The whole archive: one more pack, on one disc already listed. */
    rc = pending_output(c, NULL, out, sizeof out);
    if (rc != 0 || !strstr(out, "Total: 5 packs, 0.2 MB. They are on 2 "
                                "discs; some hold the same packs.\n")) {
        fprintf(stderr, "FAIL #951: whole archive (rc %d):\n%s", rc, out);
        fails++;
    }
    /* A set on no volume: a total of nothing, still exit 0 (the
     * scripts then size the whole archive). */
    rc = pending_output(c, "nobody", out, sizeof out);
    if (rc != 0 || !strstr(out, "Total: 0 packs, 0.0 MB.\n")) {
        fprintf(stderr, "FAIL #951: a set with no pack (rc %d):\n%s",
                rc, out);
        fails++;
    }
    lcsas_catalog_close(c);

    /* One disc, no pack on two: no word about shared packs. */
    if (make_db(p, V10_TABLES
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES"
            "  ('p1', 1048576, 'one');"
            "INSERT INTO volumes (label, status) VALUES ('ONLY_0001', 'VERIFIED');"
            "INSERT INTO volume_packs VALUES (1, 1);") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #951: one-disc fixture\n");
        fails++;
        return;
    }
    rc = pending_output(c, "one", out, sizeof out);
    if (rc != 0 || !strstr(out, "Total: 1 pack, 1.0 MB. They are on 1 disc.\n")) {
        fprintf(stderr, "FAIL #951: one pack on one disc (rc %d):\n%s",
                rc, out);
        fails++;
    }
    lcsas_catalog_close(c);

    /* A lost volume burned again: the pack is on the DEPRECATED volume
     * and on its replacement; the total is unchanged. */
    if (make_db(p, V10_TABLES
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES"
            "  ('p1', 1048576, 'one'), ('p2', 1048576, 'one');"
            "INSERT INTO volumes (label, status) VALUES"
            "  ('LOST_0001', 'DEPRECATED'), ('KEPT_0002', 'VERIFIED'),"
            "  ('AGAIN_0003', 'VERIFIED');"
            "INSERT INTO volume_packs VALUES (1, 1), (2, 2), (3, 1);") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #951: re-burn fixture\n");
        fails++;
        return;
    }
    rc = pending_output(c, "one", out, sizeof out);
    if (rc != 0 || !strstr(out, "Total: 2 packs, 2.0 MB. They are on 3 "
                                "discs; some hold the same packs.\n")) {
        fprintf(stderr, "FAIL #951: a lost volume burned again (rc %d):\n%s",
                rc, out);
        fails++;
    }
    lcsas_catalog_close(c);
    unlink(p);
}

/*
 * #951: a catalog the size queries cannot read gives no total and an
 * error, so the scripts say they could not work out the size (a "Total:
 * 0 packs" line used to skip the free-space check without a word).
 * A catalog lacking volume_packs fails the first query; one whose
 * volumes lack a label fails the per-disc query.
 */
static void
test_pending_unreadable(void)
{
    const char *p = "/tmp/lcsas_catalog_test_pending_bad.db";
    char out[2048];
    lcsas_catalog *c;
    int rc;
    int pass;
    static const char *const sql[2] = {
        "CREATE TABLE schema_version (version INTEGER);"
        "INSERT INTO schema_version VALUES (10);"
        "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
        "  size_bytes INTEGER, repo_id TEXT);"
        "INSERT INTO packs VALUES (1, 'p1', 10, 'r');",
        "CREATE TABLE schema_version (version INTEGER);"
        "INSERT INTO schema_version VALUES (10);"
        "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
        "  size_bytes INTEGER, repo_id TEXT);"
        "INSERT INTO packs VALUES (1, 'p1', 10, 'r');"
        "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, status TEXT);"
        "INSERT INTO volumes VALUES (1, 'VERIFIED');"
        "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);"
        "INSERT INTO volume_packs VALUES (1, 1);"
    };
    for (pass = 0; pass < 2; pass++) {
        if (make_db(p, sql[pass]) != 0 || !(c = lcsas_catalog_open(p))) {
            fprintf(stderr, "FAIL #951 unreadable (%d): fixture\n", pass);
            fails++;
            continue;
        }
        rc = pending_output(c, "r", out, sizeof out);
        if (rc != -1 || strstr(out, "Total:")) {
            fprintf(stderr, "FAIL #951 unreadable (%d): want -1 and no "
                    "total; got %d:\n%s", pass, rc, out);
            fails++;
        }
        lcsas_catalog_close(c);
    }
    unlink(p);
}

/*
 * #950: what tier 1 ranks a catalog by: whether it knows a backup set,
 * its label mark (PRAGMA user_version) and how many volumes it lists;
 * -1 from each when the catalog cannot be asked (no packs or volumes
 * table).
 */
static void
test_rank_inputs(void)
{
    const char *p = "/tmp/lcsas_catalog_test_rank.db";
    lcsas_catalog *c;

    if (make_db(p, "PRAGMA user_version = 42;" V10_TABLES
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES"
            "  ('p1', 10, 'ours');"
            "INSERT INTO volumes (label, status) VALUES ('A_0001', 'VERIFIED'),"
            "  ('B_0002', 'DESTROYED'), ('C_0003', 'STAGING');") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #950: rank fixture\n");
        fails++;
        return;
    }
    if (lcsas_catalog_knows_repo(c, "ours") != 1
            || lcsas_catalog_knows_repo(c, "theirs") != 0
            || lcsas_catalog_label_mark(c) != 42
            || lcsas_catalog_volume_count(c) != 3) {
        fprintf(stderr, "FAIL #950: knows %d/%d, mark %lld, volumes %lld "
                "(want 1/0, 42, 3)\n",
                lcsas_catalog_knows_repo(c, "ours"),
                lcsas_catalog_knows_repo(c, "theirs"),
                lcsas_catalog_label_mark(c),
                lcsas_catalog_volume_count(c));
        fails++;
    }
    lcsas_catalog_close(c);

    if (make_db(p, "CREATE TABLE schema_version (version INTEGER);"
                   "INSERT INTO schema_version VALUES (99);") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #950: rank skew fixture\n");
        fails++;
        return;
    }
    if (lcsas_catalog_knows_repo(c, "ours") != -1
            || lcsas_catalog_volume_count(c) != -1
            || lcsas_catalog_label_mark(c) != 0) {
        fprintf(stderr, "FAIL #950: a catalog with no packs or volumes "
                "table: want -1, -1 and mark 0\n");
        fails++;
    }
    lcsas_catalog_close(c);
    unlink(p);
}

/*
 * #957: the newest volume that is not DESTROYED; none when every one
 * is; -1 when there is no volumes table to ask.
 */
static void
test_newest_label(void)
{
    const char *p = "/tmp/lcsas_catalog_test_newest.db";
    lcsas_catalog *c;
    char label[64];
    int rc;

    if (make_db(p, V10_TABLES
            "INSERT INTO volumes (label, status) VALUES ('A_0001', 'VERIFIED'),"
            "  ('B_0002', 'DEPRECATED'), ('C_0003', 'DESTROYED');") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #957: fixture\n");
        fails++;
        return;
    }
    rc = lcsas_catalog_newest_label(c, label, sizeof label);
    if (rc != 1 || strcmp(label, "B_0002") != 0) {
        fprintf(stderr, "FAIL #957: newest label %d '%s' (want B_0002)\n",
                rc, rc == 1 ? label : "");
        fails++;
    }
    lcsas_catalog_close(c);
    if (make_db(p, V10_TABLES
            "INSERT INTO volumes (label, status) VALUES ('C_0003', 'DESTROYED');")
                != 0
            || !(c = lcsas_catalog_open(p))
            || lcsas_catalog_newest_label(c, label, sizeof label) != 0) {
        fprintf(stderr, "FAIL #957: every volume DESTROYED: want none\n");
        fails++;
    }
    lcsas_catalog_close(c);
    if (make_db(p, "CREATE TABLE schema_version (version INTEGER);"
                   "INSERT INTO schema_version VALUES (10);") != 0
            || !(c = lcsas_catalog_open(p))
            || lcsas_catalog_newest_label(c, label, sizeof label) != -1) {
        fprintf(stderr, "FAIL #957: no volumes table: want -1\n");
        fails++;
    }
    lcsas_catalog_close(c);
    unlink(p);
}

/*
 * #961: the catalog's version is its highest schema_version row.  A
 * master migrated from v5 to v10 keeps a row for each step (5..10); the
 * first row is 5.  An empty table is no version at all.
 */
static void
test_schema_version_is_the_highest_row(void)
{
    const char *p = "/tmp/lcsas_catalog_test_migrated.db";
    lcsas_catalog *c;
    int v;

    if (make_db(p,
            "CREATE TABLE schema_version (version INTEGER, applied_at TEXT);"
            "INSERT INTO schema_version VALUES (5, '2026-01-01'),"
            "  (6, '2026-02-01'), (7, '2026-03-01'), (8, '2026-04-01'),"
            "  (9, '2026-05-01'), (10, '2026-06-01');") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #961: fixture\n");
        fails++;
        return;
    }
    v = lcsas_catalog_schema_version(c);
    if (v != 10) {
        fprintf(stderr, "FAIL #961: a migrated catalog reads as v%d "
                "(want v10)\n", v);
        fails++;
    }
    lcsas_catalog_close(c);

    if (make_db(p, "CREATE TABLE schema_version (version INTEGER);") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #961: empty-table fixture\n");
        fails++;
        return;
    }
    v = lcsas_catalog_schema_version(c);
    if (v != -1 || lcsas_catalog_check(c) != -1) {
        fprintf(stderr, "FAIL #961: an empty schema_version reads as v%d "
                "(want -1, and the check fails)\n", v);
        fails++;
    }
    lcsas_catalog_close(c);
    unlink(p);
}

/*
 * #961 / #951: the volume lookup reads only the frozen columns
 * (volume_id, label, status), so a catalog whose volumes table has no
 * uuid or media_type column still names the discs.
 */
static void
test_volumes_read_frozen_columns_only(void)
{
    const char *p = "/tmp/lcsas_catalog_test_frozen.db";
    lcsas_catalog *c;
    lcsas_catalog_volume vols[8];
    int n;

    if (make_db(p,
            "CREATE TABLE schema_version (version INTEGER);"
            "INSERT INTO schema_version VALUES (11);"
            "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
            "  size_bytes INTEGER, repo_id TEXT);"
            "INSERT INTO packs VALUES (1, 'p1', 10, 'r');"
            "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, label TEXT,"
            "  status TEXT);"
            "INSERT INTO volumes VALUES (1, 'ONLY_0001', 'VERIFIED');"
            "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);"
            "INSERT INTO volume_packs VALUES (1, 1);") != 0
            || !(c = lcsas_catalog_open(p))) {
        fprintf(stderr, "FAIL #961 frozen: fixture\n");
        fails++;
        return;
    }
    n = lcsas_catalog_volumes_for_pack(c, 1, vols, 8);
    if (n != 1 || strcmp(vols[0].label, "ONLY_0001") != 0
            || strcmp(vols[0].status, "VERIFIED") != 0) {
        fprintf(stderr, "FAIL #961 frozen: volumes without uuid and "
                "media_type: %d volume(s) (want 1): %s\n", n,
                n < 0 ? lcsas_catalog_errmsg(c) : "");
        fails++;
    }
    lcsas_catalog_close(c);
    unlink(p);
}

/*
 * #967: a catalog in WAL format, in a folder that can be written (a
 * disc copied to a folder): reading it leaves no -shm or -wal file
 * beside it.
 */
static void
test_wal_catalog_leaves_nothing_behind(void)
{
    char dir[256];
    char path[300];
    char side[320];
    sqlite3 *w = NULL;
    lcsas_catalog *c;
    lcsas_catalog_pack pk;
    struct stat st;
    int left = 0;

    snprintf(dir, sizeof dir, "/tmp/lcsas_catalog_test_wal_%ld",
             (long)getpid());
    snprintf(path, sizeof path, "%s/catalog.db", dir);
    mkdir(dir, 0755);
    if (sqlite3_open(path, &w) != SQLITE_OK
            || exec_sql(w,
                "PRAGMA journal_mode=WAL;"
                "CREATE TABLE schema_version (version INTEGER);"
                "INSERT INTO schema_version VALUES (10);"
                "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY,"
                "  sha256 TEXT, size_bytes INTEGER, repo_id TEXT);"
                "INSERT INTO packs VALUES (1, 'aa11', 1024, 'r');") < 0) {
        fprintf(stderr, "FAIL #967: WAL fixture\n");
        fails++;
    }
    sqlite3_close(w);   /* the last close checkpoints and removes both */
    c = lcsas_catalog_open(path);
    if (!c || lcsas_catalog_find_pack(c, "aa11", &pk) != 0) {
        fprintf(stderr, "FAIL #967: WAL catalog not read\n");
        fails++;
    }
    snprintf(side, sizeof side, "%s-shm", path);
    if (stat(side, &st) == 0) left++;
    snprintf(side, sizeof side, "%s-wal", path);
    if (stat(side, &st) == 0) left++;
    lcsas_catalog_close(c);
    snprintf(side, sizeof side, "%s-shm", path);
    if (stat(side, &st) == 0) left++;
    unlink(side);
    snprintf(side, sizeof side, "%s-wal", path);
    if (stat(side, &st) == 0) left++;
    unlink(side);
    if (left) {
        fprintf(stderr, "FAIL #967: reading a WAL catalog left %d -shm/-wal "
                "file(s) beside it\n", left);
        fails++;
    }
    unlink(path);
    rmdir(dir);
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

    /* A genuine miss against a readable v5 surface returns 1 (not -1). */
    if (lcsas_catalog_find_pack(c, "deadbeef", &pk) != 1) {
        fprintf(stderr, "FAIL: find_pack miss should return 1\n");
        fails++;
    }

    /* Pack 2 lives in vols 1 and 2 (vol-003 is DESTROYED, filtered out),
     * newest volume first (#896: the heir starts from the newest disc). */
    n = lcsas_catalog_volumes_for_pack(c, 2, vols, 8);
    if (n != 2) {
        fprintf(stderr, "FAIL: volumes_for_pack(2) returned %d (want 2)\n", n);
        fails++;
    }
    if (n >= 1 && strcmp(vols[0].label, "vol-002") != 0) {
        fprintf(stderr, "FAIL: first volume label %s\n", vols[0].label);
        fails++;
    }
    if (n >= 2 && strcmp(vols[1].label, "vol-001") != 0) {
        fprintf(stderr, "FAIL: second volume label %s\n", vols[1].label);
        fails++;
    }

    /* Pack 1 lives only in vol-001. */
    n = lcsas_catalog_volumes_for_pack(c, 1, vols, 8);
    if (n != 1) {
        fprintf(stderr, "FAIL: volumes_for_pack(1) returned %d (want 1)\n", n);
        fails++;
    }

    /* lcsas_catalog_describe(): smoke-test the printer (no return value
     * to assert, just verify it doesn't crash and the function runs). */
    lcsas_catalog_describe(c);

    /* lcsas_catalog_print_pending_packs(): full SELECT/JOIN/GROUP path. */
    if (lcsas_catalog_print_pending_packs(c, NULL) != 0) {
        fprintf(stderr, "FAIL: print_pending_packs returned non-zero\n");
        fails++;
    }
    /* #798: the same, for one repository. */
    if (lcsas_catalog_print_pending_packs(c, "repo-abc") != 0) {
        fprintf(stderr, "FAIL: print_pending_packs(repo) returned non-zero\n");
        fails++;
    }

    /* lcsas_catalog_volumes_for_pack with non-existent pack_id: empty result. */
    n = lcsas_catalog_volumes_for_pack(c, 9999, vols, 8);
    if (n != 0) {
        fprintf(stderr, "FAIL: volumes_for_pack(missing) returned %d (want 0)\n", n);
        fails++;
    }

    /* lcsas_catalog_volumes_for_pack with a cap smaller than the result.
     * Pack 2 has 2 volumes; ask for max=1 and verify we get exactly 1. */
    n = lcsas_catalog_volumes_for_pack(c, 2, vols, 1);
    if (n != 1) {
        fprintf(stderr, "FAIL: volumes_for_pack(2, max=1) returned %d (want 1)\n", n);
        fails++;
    }
    /* The cap keeps the newest. */
    if (n == 1 && strcmp(vols[0].label, "vol-002") != 0) {
        fprintf(stderr, "FAIL: volumes_for_pack(2, max=1) kept %s (want "
                        "vol-002)\n", vols[0].label);
        fails++;
    }

    lcsas_catalog_close(c);

    /* Error path: open a non-existent DB path.  Verifies the error
     * branch in lcsas_catalog_open that frees the allocated catalog
     * and prints a diagnostic on sqlite3_open_v2 failure. */
    {
        lcsas_catalog *missing = lcsas_catalog_open("/tmp/does-not-exist-lcsas.db");
        if (missing != NULL) {
            fprintf(stderr, "FAIL: open on missing path should return NULL\n");
            lcsas_catalog_close(missing);
            fails++;
        }
    }

    unlink(db_path);

    /* ── FMT-05: schema forward-compat (version skew) ──────────────────
     * Build a v6 catalog whose `packs.sha256` column was renamed to
     * `content_hash` (a hypothetical future breaking change tier-1 does
     * NOT understand).  Assert:
     *   - open succeeds (the file format is fine; only the column moved);
     *   - the newer-schema warning is emitted exactly once on stderr,
     *     and only after a lookup is attempted;
     *   - find_pack returns -1 (query error), distinguishable from the
     *     1 (genuine miss) we proved above on the v5 fixture.
     */
    {
        const char *v6_path = "/tmp/lcsas_catalog_test_v6.db";
        const char *cap_path = "/tmp/lcsas_catalog_test_v6.stderr";
        sqlite3 *v6 = NULL;
        lcsas_catalog *cv6;
        FILE *cap;
        char line[512];
        int warn_count = 0;
        int saved_fd;
        fpos_t saved_pos;
        int rc6;
        int skew6 = 0;

        unlink(v6_path);
        unlink(cap_path);

        if (sqlite3_open(v6_path, &v6) != SQLITE_OK) {
            fprintf(stderr, "FAIL: sqlite3_open v6\n");
            return 1;
        }
        /* schema_version = 6; packs.sha256 -> packs.content_hash. */
        if (exec_sql(v6,
            "CREATE TABLE schema_version (version INTEGER, applied_at DATETIME);"
            "INSERT INTO schema_version VALUES (6, datetime('now'));"
            "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  content_hash TEXT UNIQUE NOT NULL, size_bytes INTEGER,"
            "  repo_id TEXT);"
            "INSERT INTO packs (content_hash, size_bytes, repo_id) VALUES"
            "  ('aa11', 1024, 'repo-abc');"
            ) < 0) {
            fprintf(stderr, "FAIL: v6 schema setup\n");
            sqlite3_close(v6);
            return 1;
        }
        sqlite3_close(v6);

        /* Redirect stderr to a file so we can count the warning lines.
         * fflush + dup the fd, freopen, then restore afterwards. */
        fflush(stderr);
        fgetpos(stderr, &saved_pos);
        saved_fd = dup(fileno(stderr));
        if (freopen(cap_path, "w", stderr) == NULL) {
            fprintf(stdout, "FAIL: freopen stderr\n");
            return 1;
        }

        cv6 = lcsas_catalog_open(v6_path);

        /* Two lookups: the warning must fire at most once across both. */
        rc6 = -2;
        if (cv6) {
            rc6 = lcsas_catalog_find_pack(cv6, "aa11", &pk);
            skew6 = lcsas_catalog_error_is_skew(cv6);
            (void)lcsas_catalog_find_pack(cv6, "bb22", &pk);
            lcsas_catalog_close(cv6);
        }

        /* Restore stderr. */
        fflush(stderr);
        dup2(saved_fd, fileno(stderr));
        close(saved_fd);
        clearerr(stderr);
        fsetpos(stderr, &saved_pos);

        if (!cv6) {
            fprintf(stderr, "FAIL: v6 catalog open returned NULL\n");
            fails++;
        }
        if (rc6 != -1) {
            fprintf(stderr,
                "FAIL: find_pack on v6/renamed column returned %d (want -1)\n",
                rc6);
            fails++;
        }
        if (!skew6) {
            fprintf(stderr, "FAIL: a renamed column is not reported as skew\n");
            fails++;
        }

        cap = fopen(cap_path, "r");
        if (!cap) {
            fprintf(stderr, "FAIL: cannot reopen captured stderr\n");
            fails++;
        } else {
            while (fgets(line, sizeof line, cap)) {
                if (strstr(line, "is newer than this recovery binary")) {
                    warn_count++;
                }
            }
            fclose(cap);
        }
        if (warn_count != 1) {
            fprintf(stderr,
                "FAIL: newer-schema warning emitted %d times (want 1)\n",
                warn_count);
            fails++;
        }

        unlink(v6_path);
        unlink(cap_path);
    }

    /* ── #543: a catalog that can't be read is not schema skew ───────
     *
     * A WAL-mode catalog in a directory SQLite can't write to, as on a
     * read-only disc: the plain read-only open succeeds, but the first
     * query fails because the -shm file can't be created.  Reopened as
     * immutable, it reads.  The '%' and '#' in the path must survive the
     * URI.  (As root the directory stays writable and the plain open
     * works; the outcome is the same.)
     *
     * A second WAL database there has no schema_version table.  Since
     * #967 every catalog is opened immutable, so it reads (it was kept
     * on a plain handle that could not read, and its lookups failed
     * with that read error); its missing tables then rightly count as
     * skew, as they did as root.
     */
    {
        char ro_dir[256];
        char ro_path[300];
        char nv_path[300];
        sqlite3 *w = NULL;
        lcsas_catalog *cw;

        snprintf(ro_dir, sizeof ro_dir, "/tmp/lcsas_catalog_test_ro %%#%ld",
                 (long)getpid());
        snprintf(ro_path, sizeof ro_path, "%s/catalog.db", ro_dir);
        snprintf(nv_path, sizeof nv_path, "%s/noversion.db", ro_dir);
        mkdir(ro_dir, 0755);
        if (sqlite3_open(ro_path, &w) != SQLITE_OK
                || exec_sql(w,
                    "PRAGMA journal_mode=WAL;"
                    "CREATE TABLE schema_version (version INTEGER);"
                    "INSERT INTO schema_version VALUES (10);"
                    "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY,"
                    "  sha256 TEXT, size_bytes INTEGER, repo_id TEXT);"
                    "INSERT INTO packs VALUES (1, 'aa11', 1024, 'repo-abc');")
                   < 0) {
            fprintf(stderr, "FAIL #543: WAL catalog setup\n");
            fails++;
        }
        sqlite3_close(w);
        w = NULL;
        if (sqlite3_open(nv_path, &w) != SQLITE_OK
                || exec_sql(w, "PRAGMA journal_mode=WAL;"
                               "CREATE TABLE t (x INTEGER);") < 0) {
            fprintf(stderr, "FAIL #543: no-version WAL setup\n");
            fails++;
        }
        sqlite3_close(w);
        chmod(ro_dir, 0555);
        cw = lcsas_catalog_open(ro_path);
        if (!cw || lcsas_catalog_schema_version(cw) != 10
                || lcsas_catalog_find_pack(cw, "aa11", &pk) != 0
                || pk.pack_id != 1) {
            fprintf(stderr,
                    "FAIL #543: WAL catalog in a read-only directory unread\n");
            fails++;
        }
        lcsas_catalog_close(cw);
        cw = lcsas_catalog_open(nv_path);
        if (!cw || lcsas_catalog_schema_version(cw) != -1
                || lcsas_catalog_find_pack(cw, "aa11", &pk) != -1
                || !lcsas_catalog_error_is_skew(cw)) {
            fprintf(stderr,
                    "FAIL #543: no-version WAL catalog: want an open handle"
                    " whose lookups fail as skew (got: %s)\n",
                    cw ? lcsas_catalog_errmsg(cw) : "NULL");
            fails++;
        }
        lcsas_catalog_close(cw);
        chmod(ro_dir, 0755);
        unlink(ro_path);
        unlink(nv_path);
        rmdir(ro_dir);
    }

    /* A file that isn't a database: the open succeeds (SQLite reads
     * the header lazily), there is no reopen (immutable can't help),
     * and lookups fail with SQLite's own message, which is not skew. */
    {
        const char *bad_path = "/tmp/lcsas_catalog_test_bad.db";
        FILE *bf = fopen(bad_path, "wb");
        lcsas_catalog *cb;

        if (bf) {
            fputs("not an SQLite database, only text long enough to fill"
                  " the 100-byte header that SQLite checks first.......\n",
                  bf);
            fclose(bf);
        }
        cb = lcsas_catalog_open(bad_path);
        if (!cb) {
            fprintf(stderr, "FAIL #543: open of a non-database returned NULL\n");
            fails++;
        } else {
            if (lcsas_catalog_find_pack(cb, "aa11", &pk) != -1
                    || lcsas_catalog_error_is_skew(cb)
                    || strcmp(lcsas_catalog_errmsg(cb),
                              "file is not a database") != 0) {
                fprintf(stderr,
                        "FAIL #543: non-database: want -1, not skew, "
                        "\"file is not a database\"; got \"%s\"\n",
                        lcsas_catalog_errmsg(cb));
                fails++;
            }
            lcsas_catalog_close(cb);
        }
        unlink(bad_path);
    }

    /* A damaged page: both lookups prepare, then fail on their first
     * step.  That is a read error (-1), not "no record" (1) or "on no
     * volume" (0), and not skew.  The packs and volume_packs root pages
     * are overwritten; schema_version's is left intact, so the open's
     * probe still reads. */
    {
        const char *cor_path = "/tmp/lcsas_catalog_test_damaged.db";
        sqlite3 *d = NULL;
        sqlite3_stmt *st = NULL;
        lcsas_catalog *cd;
        unsigned char junk[512];
        FILE *df;
        int nroot = 0;
        long roots[2] = { 0, 0 };

        unlink(cor_path);
        memset(junk, 0xFF, sizeof junk);
        if (sqlite3_open(cor_path, &d) != SQLITE_OK
                || exec_sql(d,
                    "PRAGMA page_size=512;"
                    "CREATE TABLE schema_version (version INTEGER);"
                    "INSERT INTO schema_version VALUES (10);"
                    "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY,"
                    "  sha256 TEXT, size_bytes INTEGER, repo_id TEXT);"
                    "INSERT INTO packs VALUES (1, 'aa11', 1024, 'repo-abc');") < 0
                || exec_sql(d,
                    "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY,"
                    "  label TEXT, uuid TEXT, media_type TEXT, status TEXT);"
                    "INSERT INTO volumes VALUES"
                    "  (1, 'vol-001', 'uuid-aaa', 'BD25', 'VERIFIED');"
                    "CREATE TABLE volume_packs (volume_id INTEGER,"
                    "  pack_id INTEGER);"
                    "INSERT INTO volume_packs VALUES (1, 1);") < 0) {
            fprintf(stderr, "FAIL #543: damaged-catalog setup\n");
            fails++;
        }
        df = NULL;
        if (sqlite3_prepare_v2(d,
                "SELECT rootpage FROM sqlite_master"
                " WHERE name IN ('packs', 'volume_packs')",
                -1, &st, NULL) == SQLITE_OK) {
            df = fopen(cor_path, "r+b");
            while (df && sqlite3_step(st) == SQLITE_ROW) {
                long root = (long)sqlite3_column_int(st, 0);
                fseek(df, (root - 1) * 512L, SEEK_SET);
                if (root > 1 && nroot < 2
                        && fwrite(junk, 1, sizeof junk, df) == sizeof junk)
                    roots[nroot++] = root;
            }
        }
        sqlite3_finalize(st);
        sqlite3_close(d);
        if (df) fclose(df);
        if (nroot != 2) {
            fprintf(stderr, "FAIL #543: damaged %d root pages (want 2):"
                    " %ld %ld\n", nroot, roots[0], roots[1]);
            fails++;
        }
        cd = lcsas_catalog_open(cor_path);
        if (!cd) {
            fprintf(stderr, "FAIL #543: open of a damaged catalog returned NULL\n");
            fails++;
        } else {
            int fr = lcsas_catalog_find_pack(cd, "aa11", &pk);
            int fskew = lcsas_catalog_error_is_skew(cd);
            int nv = lcsas_catalog_volumes_for_pack(cd, 1, vols, 8);
            int vskew = lcsas_catalog_error_is_skew(cd);
            if (fr != -1 || fskew || nv != -1 || vskew) {
                fprintf(stderr,
                        "FAIL #543: damaged page: find_pack %d (skew %d),"
                        " volumes_for_pack %d (skew %d); want -1, -1, no"
                        " skew\n", fr, fskew, nv, vskew);
                fails++;
            }
            /* #951: no total from a damaged catalog. */
            {
                char out[1024];
                int prc = pending_output(cd, "repo-abc", out, sizeof out);
                if (prc != -1 || strstr(out, "Total:")) {
                    fprintf(stderr, "FAIL #951: damaged page: want -1 and "
                            "no total; got %d:\n%s", prc, out);
                    fails++;
                }
            }
            lcsas_catalog_close(cd);
        }
        unlink(cor_path);
    }

    test_pending_counts_each_pack_once();
    test_pending_unreadable();
    test_schema_version_is_the_highest_row();
    test_rank_inputs();
    test_newest_label();
    test_volumes_read_frozen_columns_only();
    test_wal_catalog_leaves_nothing_behind();

    /* A path longer than tier 1 ever builds (4 KiB) is refused. */
    {
        static char longp[4200];
        memset(longp, 'a', sizeof longp - 1);
        longp[0] = '/';
        longp[sizeof longp - 1] = '\0';
        c = lcsas_catalog_open(longp);
        if (c) {
            fprintf(stderr, "FAIL #967: a 4 KiB+ path opened\n");
            lcsas_catalog_close(c);
            fails++;
        }
    }

    if (fails == 0) printf("test_catalog: OK\n");
    return fails ? 1 : 0;
}
