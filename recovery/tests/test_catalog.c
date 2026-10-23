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

    /* lcsas_catalog_describe(): smoke-test the printer (no return value
     * to assert, just verify it doesn't crash and the function runs). */
    lcsas_catalog_describe(c);

    /* lcsas_catalog_print_pending_packs(): full SELECT/JOIN/GROUP path. */
    if (lcsas_catalog_print_pending_packs(c) != 0) {
        fprintf(stderr, "FAIL: print_pending_packs returned non-zero\n");
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
     * A second WAL database there has no schema_version table, so the
     * immutable reopen doesn't help either: the catalog keeps the plain
     * handle, and lookups fail with its read error, which is not skew.
     * Not as root: there the plain handle reads, and the missing tables
     * rightly count as skew.
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
        cw = geteuid() != 0 ? lcsas_catalog_open(nv_path) : NULL;
        if (geteuid() != 0
                && (!cw || lcsas_catalog_find_pack(cw, "aa11", &pk) != -1
                    || lcsas_catalog_error_is_skew(cw))) {
            fprintf(stderr,
                    "FAIL #543: no-version WAL catalog: want an open handle"
                    " whose lookups fail, not as skew (got: %s)\n",
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
            lcsas_catalog_close(cd);
        }
        unlink(cor_path);
    }

    if (fails == 0) printf("test_catalog: OK\n");
    return fails ? 1 : 0;
}
