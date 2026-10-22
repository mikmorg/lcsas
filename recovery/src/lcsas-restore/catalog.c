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

/*
 * Highest schema version whose query surface this binary understands.
 * The frozen v5 surface is enumerated in src/lcsas/db/schema.py
 * ("TIER-1 FROZEN SURFACE") and pinned by
 * tests/unit/test_schema_v5_columns_frozen.py.  A catalog whose version
 * exceeds this may have renamed/dropped a column tier-1 queries; we warn
 * once and surface query errors as errors (not misses) so the heir is
 * told to prefer same-generation tooling.
 */
#define LCSAS_TIER1_SCHEMA_MAX 5

struct lcsas_catalog {
    sqlite3 *db;
    int schema_version;   /* cached at open; -1 if unreadable */
    int skew_warned;      /* emit the newer-schema warning at most once */
};

/*
 * A "file:" URI for `path` with ?immutable=1.  '%', '?' and '#' would
 * escape or end the path inside a URI, so they are percent-encoded.  An
 * absolute path gets an empty authority ("file://" + "/abs"), so a
 * leading "//" can't be read as a host.  NULL when out of memory.
 */
static char *
immutable_uri(const char *path)
{
    static const char hexd[] = "0123456789ABCDEF";
    static const char query[] = "?immutable=1";
    size_t n = strlen(path);
    size_t i;
    size_t j;
    char *u = (char *)malloc(7 + 3 * n + sizeof query);

    if (u) {
        strcpy(u, path[0] == '/' ? "file://" : "file:");
        j = strlen(u);
        for (i = 0; i < n; i++) {
            unsigned char ch = (unsigned char)path[i];
            if (ch == '%' || ch == '?' || ch == '#') {
                u[j++] = '%';
                u[j++] = hexd[ch >> 4];
                u[j++] = hexd[ch & 15];
            } else {
                u[j++] = (char)ch;
            }
        }
        memcpy(u + j, query, sizeof query);
    }
    return u;
}

/*
 * #543: a WAL-mode catalog on read-only media opens, but every query
 * then fails ("unable to open database file"): SQLite has to create
 * the -shm and -wal files beside it, and can't.  A disc never changes,
 * so read it as immutable (no locks, no -shm, no -wal), as the Python
 * readers do.  Only after a plain open's first query has failed that
 * way (see lcsas_catalog_open): immutable would also ignore a live
 * -wal next to a catalog copied off a running system.  Keeps the plain
 * handle, and its error, when this doesn't help.
 */
static void
reopen_immutable(lcsas_catalog *c, const char *path)
{
    char *uri = immutable_uri(path);
    sqlite3 *plain = c->db;
    sqlite3 *db = NULL;
    int version = -1;

    if (uri && sqlite3_open_v2(uri, &db,
                               SQLITE_OPEN_READONLY | SQLITE_OPEN_URI,
                               NULL) == SQLITE_OK) {
        c->db = db;
        version = lcsas_catalog_schema_version(c);
    }
    if (version >= 0) {
        sqlite3_close(plain);
        c->schema_version = version;
    } else {
        sqlite3_close(db);
        c->db = plain;
    }
    free(uri);
}

lcsas_catalog *
lcsas_catalog_open(const char *path)
{
    lcsas_catalog *c;
    int rc;

    c = (lcsas_catalog *)malloc(sizeof(*c));
    if (!c) return NULL;
    c->db = NULL;
    c->schema_version = -1;
    c->skew_warned = 0;
    rc = sqlite3_open_v2(path, &c->db, SQLITE_OPEN_READONLY, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "catalog open failed: %s\n",
                c->db ? sqlite3_errmsg(c->db) : "unknown");
        sqlite3_close(c->db);
        free(c);
        return NULL;
    }
    c->schema_version = lcsas_catalog_schema_version(c);
    /* The -shm SQLite can't create shows up as SQLITE_CANTOPEN on a
     * read-only mount and SQLITE_READONLY in an unwritable directory.
     * Nothing else (not a database, no such table) is helped by
     * reopening, so don't spend a second open on it. */
    rc = sqlite3_errcode(c->db);
    if (c->schema_version < 0
            && (rc == SQLITE_CANTOPEN || rc == SQLITE_READONLY))
        reopen_immutable(c, path);
    return c;
}

/*
 * Emit the newer-schema warning to stderr at most once per catalog.
 * Called from the lookup paths so it fires only when disc-swap hints are
 * actually being consulted, not merely on open, and only when the
 * failure looks like skew: not for a catalog that can't be read (#543).
 */
static void
warn_schema_skew_once(lcsas_catalog *c)
{
    if (c->skew_warned) return;
    if (c->schema_version > LCSAS_TIER1_SCHEMA_MAX
            && lcsas_catalog_error_is_skew(c)) {
        fprintf(stderr,
            "[catalog] schema v%d is newer than this recovery binary "
            "(supports v%d). Disc-swap hints may be missing -- prefer the "
            "restore tooling from the same-generation META disc, or proceed "
            "and insert discs when prompted.\n",
            c->schema_version, LCSAS_TIER1_SCHEMA_MAX);
        c->skew_warned = 1;
    }
}

const char *
lcsas_catalog_errmsg(lcsas_catalog *c)
{
    return sqlite3_errmsg(c->db);
}

/*
 * Not from the schema version: catalogs written by the current LCSAS
 * are past LCSAS_TIER1_SCHEMA_MAX by design (later versions only add to
 * the frozen v5 surface), so a damaged one would count as skew too.  A
 * query naming a table or column the catalog lacks is what skew looks
 * like.
 */
int
lcsas_catalog_error_is_skew(lcsas_catalog *c)
{
    return strncmp(sqlite3_errmsg(c->db), "no such ", 8) == 0;
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

    rc = sqlite3_prepare_v2(c->db,
            "SELECT pack_id, sha256, size_bytes, repo_id "
            "FROM packs WHERE sha256 = ?", -1, &st, NULL);
    if (rc != SQLITE_OK) {
        warn_schema_skew_once(c);
        return -1;
    }
    sqlite3_bind_text(st, 1, sha256_hex, -1, SQLITE_STATIC);
    rc = sqlite3_step(st);
    if (rc == SQLITE_ROW) {
        out->pack_id = sqlite3_column_int64(st, 0);
        copy_text_col(st, 1, out->sha256_hex, sizeof(out->sha256_hex));
        out->size_bytes = sqlite3_column_int64(st, 2);
        copy_text_col(st, 3, out->repo_id, sizeof(out->repo_id));
    }
    sqlite3_finalize(st);
    /* A failing step (an I/O error, a damaged page) is a read error,
     * not a miss: "no record of this pack" would be a false answer.
     * finalize leaves the step's error on the handle for errmsg. */
    if (rc == SQLITE_ROW) return 0;
    return rc == SQLITE_DONE ? 1 : -1;
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
    if (rc != SQLITE_OK) {
        warn_schema_skew_once(c);
        return -1;
    }
    sqlite3_bind_int64(st, 1, pack_id);
    rc = SQLITE_DONE;
    while (count < max_vols && (rc = sqlite3_step(st)) == SQLITE_ROW) {
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
    /* As in find_pack: a failing step is a read error, not "on no
     * volume".  rc is SQLITE_ROW when the cap stopped the loop. */
    if (rc != SQLITE_ROW && rc != SQLITE_DONE) return -1;
    return (int)count;
}

/* Prints the WHOLE-ARCHIVE disc plan, not a per-snapshot one -- by
 * design (#350, resolved won't-fix).  A snapshot-filtered pick-list needs
 * the snapshot->pack mapping, which the catalog does NOT hold: the catalog
 * maps pack->volume (packs/volume_packs/volumes), while which packs a given
 * snapshot's blobs live in is derivable only from the repo index/trees.
 * (The Python planner is no different -- restore/planner.py takes the pack
 * list as INPUT, sourced from a rustic dry-run, and uses the catalog only
 * for the pack->disc half.)  Giving the durable tier-1 binary a per-snapshot
 * preview would mean either enlarging the holographically-burned catalog
 * with a snapshot_packs table (paid on every disc) or adding a repo
 * index-walk in C89 to the 50-year path -- both taxing the "minimal durable
 * binary" thesis to save disc fetches on a multi-hundred-disc restore.
 * Deliberately declined: correctness is unaffected (the over-broad list
 * still restores the right data) and the per-snapshot optimisation stays a
 * tier-3 (rustic) capability.  See issue #350 for the full analysis. */
int
lcsas_catalog_print_pending_packs(lcsas_catalog *c)
{
    sqlite3_stmt *st = NULL;
    int rc;
    long long total_packs = 0;
    long long total_bytes = 0;
    long long total_discs = 0;
    double mb;

    /* Group all packs by volume label, ordered by pack count desc.
     * Column names verified from lcsas_catalog_find_pack /
     * lcsas_catalog_volumes_for_pack.  Mirror the DESTROYED filter. */
    rc = sqlite3_prepare_v2(c->db,
            "SELECT v.label, COUNT(p.pack_id), SUM(p.size_bytes) "
            "FROM packs p "
            "JOIN volume_packs vp ON p.pack_id = vp.pack_id "
            "JOIN volumes v ON vp.volume_id = v.volume_id "
            "WHERE v.status != 'DESTROYED' "
            "GROUP BY v.label "
            "ORDER BY COUNT(p.pack_id) DESC",
            -1, &st, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "catalog query failed: %s\n", sqlite3_errmsg(c->db));
        return -1;
    }

    printf("Pending packs by disc:\n");
    while (sqlite3_step(st) == SQLITE_ROW) {
        const unsigned char *label = sqlite3_column_text(st, 0);
        long long n_packs = sqlite3_column_int64(st, 1);
        long long n_bytes = sqlite3_column_int64(st, 2);
        mb = (double)n_bytes / (1024.0 * 1024.0);
        printf("  %s: %lld pack%s (%.1f MB)\n",
               label ? (const char *)label : "(unknown)",
               n_packs,
               n_packs == 1 ? "" : "s",
               mb);
        total_packs += n_packs;
        total_bytes += n_bytes;
        total_discs++;
    }
    sqlite3_finalize(st);

    mb = (double)total_bytes / (1024.0 * 1024.0);
    printf("Total: %lld pack%s, %.1f MB across %lld disc%s.\n",
           total_packs, total_packs == 1 ? "" : "s",
           mb,
           total_discs, total_discs == 1 ? "" : "s");
    return 0;
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
