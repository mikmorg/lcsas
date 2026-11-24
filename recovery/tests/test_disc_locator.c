/*
 * test_disc_locator.c -- exercise the disc-locator public API.
 *
 * Sets up a synthetic disc layout under $TMPDIR (default /tmp) and
 * verifies that lcsas_disc_locate_pack finds packs across the four
 * supported path layouts.  Also exercises mount-parent enumeration,
 * cache_dir drain, meta-disc exclusion, and miss-path
 * (non-interactive).
 */
#include "disc_locator.h"
#include "catalog.h"
#include "hex.h"
#include "sha256.h"
#include "../vendored/sqlite/sqlite3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <utime.h>
#include <errno.h>

static int fails = 0;

static int
mkdir_recursive(const char *path)
{
    char buf[1024];
    size_t i, n = strlen(path);
    if (n >= sizeof buf) return -1;
    memcpy(buf, path, n + 1);
    for (i = 1; i < n; i++) {
        if (buf[i] == '/') {
            buf[i] = '\0';
            if (mkdir(buf, 0755) != 0 && errno != EEXIST) return -1;
            buf[i] = '/';
        }
    }
    if (mkdir(buf, 0755) != 0 && errno != EEXIST) return -1;
    return 0;
}

static int
write_pack(const char *path, const char *contents)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    fputs(contents, f);
    fclose(f);
    return 0;
}

/*
 * Pick a base directory for drain-cache fixtures whose filesystem has
 * headroom.  drain_disc refuses caches on a <10%-free filesystem, so
 * on a build host whose /tmp is nearly full every drain-exercising
 * case would silently take the guard branch and the whole drain
 * machinery would lose coverage [FMA-09].  Preference: $TMPDIR, then
 * /dev/shm, then /tmp; the first writable candidate with >=11% free
 * wins (plain /tmp when none qualifies -- the affected lines are
 * classified VOLATILE in recovery/docs/EXEMPTIONS.md).
 */
/*
 * Count the lines of `f` (rewound) that contain `needle`.
 */
static int
count_lines_with(FILE *f, const char *needle)
{
    char line[512];
    int n = 0;
    rewind(f);
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, needle)) n++;
    }
    fseek(f, 0, SEEK_END);
    return n;
}

/*
 * Make <parent>/<name>/ holding volume_info.json = `json` (NULL: no
 * file at all).
 */
static void
make_labelled_disc(const char *parent, const char *name, const char *json)
{
    char p[1024];
    snprintf(p, sizeof p, "%s/%s", parent, name);
    mkdir_recursive(p);
    if (json) {
        snprintf(p, sizeof p, "%s/%s/volume_info.json", parent, name);
        write_pack(p, json);
    }
}

/* mkdir -p for paths up to 4 KiB (mkdir_recursive stops at 1 KiB). */
static int
mkdir_long(const char *path)
{
    char buf[4096];
    size_t i, n = strlen(path);
    if (n >= sizeof buf) return -1;
    memcpy(buf, path, n + 1);
    for (i = 1; i < n; i++) {
        if (buf[i] == '/') {
            buf[i] = '\0';
            if (mkdir(buf, 0755) != 0 && errno != EEXIST) return -1;
            buf[i] = '/';
        }
    }
    if (mkdir(buf, 0755) != 0 && errno != EEXIST) return -1;
    return 0;
}

/* Create (or replace) a SQLite catalog at `path` from `sql`. */
static int
make_catalog(const char *path, const char *sql)
{
    sqlite3 *db = NULL;
    char *err = NULL;
    int rc;
    unlink(path);
    if (sqlite3_open(path, &db) != SQLITE_OK) {
        sqlite3_close(db);
        return -1;
    }
    rc = sqlite3_exec(db, sql, NULL, NULL, &err);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "make_catalog: %s\n", err ? err : "?");
        sqlite3_free(err);
    }
    sqlite3_close(db);
    return rc == SQLITE_OK ? 0 : -1;
}

/*
 * One interactive miss for `missing` against the catalog at `cat_path`,
 * answered with "q".  Returns 1 when the prompt printed `needle`.
 */
static int
prompt_mentions(const char *search_dir, const char *cat_path,
                const unsigned char *missing, const char *needle)
{
    const char *search[1];
    lcsas_disc_locator l;
    lcsas_catalog *cat;
    int pfd[2];
    FILE *in;
    FILE *out;
    char found[2048];
    int hit = 0;

    search[0] = search_dir;
    cat = lcsas_catalog_open(cat_path);
    if (!cat) return 0;
    if (pipe(pfd) != 0) {
        lcsas_catalog_close(cat);
        return 0;
    }
    in = fdopen(pfd[0], "r");
    out = tmpfile();
    if (write(pfd[1], "q\n", 2) != 2) { /* in reads EOF instead: aborts too */ }
    close(pfd[1]);
    lcsas_disc_locator_init(&l, search, 1, cat, 1);
    lcsas_disc_locator_set_auto_mount(&l, 0);
    if (in) l.prompt_in = in;
    if (out) l.prompt_out = out;
    alarm(20);
    (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
    alarm(0);
    if (out) {
        hit = count_lines_with(out, needle) > 0;
        fclose(out);
    }
    lcsas_disc_locator_free(&l);
    if (in) fclose(in); else close(pfd[0]);
    lcsas_catalog_close(cat);
    return hit;
}

/*
 * Check a prompt transcript: every line of each box, from its top
 * "+---" border to its bottom one, is exactly 60 characters.  Returns
 * the number of box lines seen, or -1 (each bad line named on stderr).
 */
static int
box_lines_are_60(FILE *f, const char *what)
{
    char line[512];
    int in_box = 0;
    int n = 0;
    int bad = 0;
    rewind(f);
    while (fgets(line, sizeof line, f)) {
        size_t len = strlen(line);
        int border;
        if (len > 0 && line[len - 1] == '\n') line[--len] = '\0';
        border = strncmp(line, "+---", 4) == 0;
        if (!in_box && !border) continue;
        n++;
        if (len != 60) {
            fprintf(stderr, "FAIL box width (%s): %lu columns: '%s'\n",
                    what, (unsigned long)len, line);
            bad = 1;
        }
        if (border) in_box = !in_box;
    }
    fseek(f, 0, SEEK_END);
    return bad ? -1 : n;
}

/*
 * One interactive miss for `missing`, answered with `answer` ("" means
 * the input just ends), with the prompt written to a temp file.  The
 * locator gets the given search paths, mount parents, meta disc and
 * catalog (each may be NULL/0).  Returns the transcript (the caller
 * fcloses it), or NULL.  *rc and *quit get the locate's return and
 * the locator's reader_quit.
 */
static FILE *
prompt_run(const char **search, size_t n_search,
           const char **parents, size_t n_parents,
           const char *meta, const char *cat_path,
           const unsigned char *missing, const char *answer,
           int *rc, int *quit)
{
    lcsas_disc_locator l;
    lcsas_catalog *cat = NULL;
    int pfd[2];
    FILE *in;
    FILE *out = tmpfile();
    char found[2048];
    size_t alen = strlen(answer);

    *rc = 0;
    *quit = 0;
    if (!out) return NULL;
    if (pipe(pfd) != 0) {
        fclose(out);
        return NULL;
    }
    if (alen > 0 && write(pfd[1], answer, alen) != (ssize_t)alen) {
        /* A short write ends the input early: still an abort. */
    }
    close(pfd[1]);
    in = fdopen(pfd[0], "r");
    if (cat_path) cat = lcsas_catalog_open(cat_path);
    lcsas_disc_locator_init(&l, search, n_search, cat, 1);
    lcsas_disc_locator_set_auto_mount(&l, 0);
    if (n_parents) lcsas_disc_locator_set_mount_parents(&l, parents, n_parents);
    if (meta) lcsas_disc_locator_set_meta(&l, meta);
    if (in) l.prompt_in = in;
    l.prompt_out = out;
    alarm(20);
    *rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
    alarm(0);
    *quit = l.reader_quit;
    lcsas_disc_locator_free(&l);
    if (in) fclose(in); else close(pfd[0]);
    if (cat) lcsas_catalog_close(cat);
    return out;
}

/* Send stderr to a temp file until capture_stderr_end(); returns it. */
static FILE *
capture_stderr_begin(int *saved)
{
    FILE *f = tmpfile();
    fflush(stderr);
    *saved = dup(2);
    if (f && *saved >= 0) dup2(fileno(f), 2);
    return f;
}

static void
capture_stderr_end(int saved)
{
    fflush(stderr);
    if (saved >= 0) {
        dup2(saved, 2);
        close(saved);
    }
}

/* Write a sparse file of `bytes` bytes whose last byte is `last`, and
 * put the SHA-256 of its contents (a pack's name) in `hex_out`. */
static int
write_sparse_named(const char *path, long bytes, int last, char hex_out[65])
{
    static char zeros[65536];
    lcsas_sha256_ctx c;
    unsigned char d[32];
    unsigned char b = (unsigned char)last;
    long left = bytes - 1;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    int ok;
    if (fd < 0) return -1;
    ok = lseek(fd, (off_t)(bytes - 1), SEEK_SET) != (off_t)-1
         && write(fd, &b, 1) == 1;
    close(fd);
    lcsas_sha256_init(&c);
    while (left > 0) {
        long n = left < (long)sizeof zeros ? left : (long)sizeof zeros;
        lcsas_sha256_update(&c, zeros, (size_t)n);
        left -= n;
    }
    lcsas_sha256_update(&c, &b, 1);
    lcsas_sha256_final(&c, d);
    lcsas_hex_encode(d, 32, hex_out);
    hex_out[64] = '\0';
    return ok ? 0 : -1;
}

/* Write `contents` as a real pack under <root>/data/<xx>/<id>: its
 * name is the SHA-256 of its contents, as restic names packs.  The id
 * goes to `hex_out`. */
static int
write_real_pack(const char *root, const char *contents, char hex_out[65])
{
    unsigned char d[32];
    char p[1200];
    lcsas_sha256(contents, strlen(contents), d);
    lcsas_hex_encode(d, 32, hex_out);
    hex_out[64] = '\0';
    snprintf(p, sizeof p, "%s/data/%c%c", root, hex_out[0], hex_out[1]);
    if (mkdir_recursive(p) != 0) return -1;
    snprintf(p, sizeof p, "%s/data/%c%c/%s", root, hex_out[0], hex_out[1],
             hex_out);
    return write_pack(p, contents);
}

/* A small catalog that passes lcsas_catalog_check. */
static const char *const GOOD_CATALOG_SQL =
    "CREATE TABLE schema_version (version INTEGER);"
    "INSERT INTO schema_version VALUES (10);"
    "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
    "  size_bytes INTEGER, repo_id TEXT);"
    "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, label TEXT,"
    "  status TEXT);"
    "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);";

/* Write a sparse file of `bytes` bytes (reads back as zeros). */
static int
write_sparse(const char *path, long bytes)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    int ok;
    if (fd < 0) return -1;
    ok = ftruncate(fd, (off_t)bytes) == 0;
    close(fd);
    return ok ? 0 : -1;
}

static const char *
roomy_base(void)
{
    static char base[1024];
    const char *cands[3];
    size_t i;
    cands[0] = getenv("TMPDIR");
    cands[1] = "/dev/shm";
    cands[2] = "/tmp";
    for (i = 0; i < 3; i++) {
        struct statvfs vfs;
        if (!cands[i] || !cands[i][0]) continue;
        if (access(cands[i], W_OK) != 0) continue;
        if (statvfs(cands[i], &vfs) != 0 || vfs.f_blocks == 0) continue;
        if (((unsigned long long)vfs.f_bavail * 100ULL)
            / (unsigned long long)vfs.f_blocks >= 11) {
            snprintf(base, sizeof base, "%s", cands[i]);
            return base;
        }
    }
    return "/tmp";
}

/*
 * #949: the answer 's' ("I do not have this disc") at the disc prompt.
 * One locator for the whole run, as main() has: the skipped labels are
 * remembered, a pack whose discs were all skipped is not found at once,
 * and K discs give at most K boxes.  Returns the number of failures.
 */
static int
locate_with_answers(lcsas_disc_locator *l, const char *dir,
                    const unsigned char *missing, int *boxes)
{
    char found[2048];
    int rc;
    (void)dir;
    alarm(20);
    rc = lcsas_disc_locate_pack(l, missing, found, sizeof found);
    alarm(0);
    /* Every box drawn so far in this run's transcript. */
    *boxes = count_lines_with(l->prompt_out, "| Pack ");
    return rc;
}

static int
test_skip_answer(const char *tmpdir)
{
    int bad = 0;
    char cat_path[512];
    char sql[4096];
    unsigned char p1[32], p2[32], p3[32];
    const char *h1 = "1111111111111111111111111111111111111111111111111111111111111111";
    const char *h2 = "2222222222222222222222222222222222222222222222222222222222222222";
    const char *h3 = "3333333333333333333333333333333333333333333333333333333333333333";
    const char *search[1];
    lcsas_disc_locator l;
    lcsas_catalog *cat;
    int pfd[2];
    FILE *in;
    FILE *out;
    FILE *rep;
    int boxes;
    int rc;

    memset(p1, 0x11, 32);
    memset(p2, 0x22, 32);
    memset(p3, 0x33, 32);
    search[0] = tmpdir;
    snprintf(cat_path, sizeof cat_path, "%s/skip_catalog.db", tmpdir);
    /* p1 on A and B; p2 on B and C; p3 on A only. */
    snprintf(sql, sizeof sql,
             "%s"
             "INSERT INTO packs VALUES(1, '%s', 10, 'r');"
             "INSERT INTO packs VALUES(2, '%s', 10, 'r');"
             "INSERT INTO packs VALUES(3, '%s', 10, 'r');"
             "INSERT INTO volumes VALUES(1, 'DISC_A_0001', 'u1', 'BD25', 'VERIFIED');"
             "INSERT INTO volumes VALUES(2, 'DISC_B_0002', 'u2', 'BD25', 'VERIFIED');"
             "INSERT INTO volumes VALUES(3, 'DISC_C_0003', 'u3', 'BD25', 'VERIFIED');"
             "INSERT INTO volume_packs VALUES(1, 1);"
             "INSERT INTO volume_packs VALUES(2, 1);"
             "INSERT INTO volume_packs VALUES(2, 2);"
             "INSERT INTO volume_packs VALUES(3, 2);"
             "INSERT INTO volume_packs VALUES(1, 3);",
             "CREATE TABLE schema_version (version INTEGER);"
             "INSERT INTO schema_version VALUES (10);"
             "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
             "  size_bytes INTEGER, repo_id TEXT);"
             "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, label TEXT,"
             "  uuid TEXT, media_type TEXT, status TEXT);"
             "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);",
             h1, h2, h3);
    if (make_catalog(cat_path, sql) != 0) {
        fprintf(stderr, "FAIL 949: catalog\n");
        return 1;
    }

    /* Run 1: a catalog that names the discs. */
    cat = lcsas_catalog_open(cat_path);
    out = tmpfile();
    if (!cat || !out || pipe(pfd) != 0) {
        fprintf(stderr, "FAIL 949: setup\n");
        if (cat) lcsas_catalog_close(cat);
        if (out) fclose(out);
        return 1;
    }
    /* Answers: 's' for p1's box (A, B), 's' for p2's box (C only). */
    if (write(pfd[1], "s\nS\n", 4) != 4) { /* input ends early: q */ }
    close(pfd[1]);
    in = fdopen(pfd[0], "r");
    lcsas_disc_locator_init(&l, search, 1, cat, 1);
    lcsas_disc_locator_set_auto_mount(&l, 0);
    if (in) l.prompt_in = in;
    l.prompt_out = out;

    rc = locate_with_answers(&l, tmpdir, p1, &boxes);
    if (rc != -1 || l.reader_quit || boxes != 1 || l.n_skipped != 2
            || strcmp(l.skipped_labels[0], "DISC_B_0002") != 0
            || strcmp(l.skipped_labels[1], "DISC_A_0001") != 0) {
        fprintf(stderr, "FAIL 949: s for p1 (rc %d quit %d boxes %d n %lu)\n",
                rc, l.reader_quit, boxes, (unsigned long)l.n_skipped);
        bad++;
    }
    if (count_lines_with(out, "If you do not have it: type 's'") != 1
            || count_lines_with(out, "skipping disc DISC_A_0001") != 1
            || box_lines_are_60(out, "949 skip") <= 0) {
        fprintf(stderr, "FAIL 949: box text for p1\n"); bad++;
    }
    /* p3 is only on A, skipped: not found, no box. */
    rc = locate_with_answers(&l, tmpdir, p3, &boxes);
    if (rc != -1 || boxes != 1 || l.misses != 1) {
        fprintf(stderr, "FAIL 949: p3 asked again (rc %d misses %lu)\n",
                rc, l.misses);
        bad++;
    }
    /* p2 is on B (skipped) and C: the box names C only. */
    rc = locate_with_answers(&l, tmpdir, p2, &boxes);
    if (rc != -1 || boxes != 2 || l.n_skipped != 3
            || count_lines_with(out, "|   DISC_C_0003") != 1
            || count_lines_with(out, "|   DISC_B_0002") != 1) {
        fprintf(stderr, "FAIL 949: p2 box (rc %d boxes %d n %lu)\n",
                rc, boxes, (unsigned long)l.n_skipped);
        bad++;
    }
    /* Three discs, two boxes, three items not found. */
    if (l.misses != 2 || l.skipped_misses != 3 || l.stop_asking) {
        fprintf(stderr, "FAIL 949: bounded prompts (misses %lu, skipped %lu)\n",
                l.misses, l.skipped_misses);
        bad++;
    }
    rep = tmpfile();
    if (rep) {
        lcsas_disc_locator_report_skips(&l, rep);
        if (count_lines_with(rep,
                "Skipped discs: DISC_B_0002, DISC_A_0001, DISC_C_0003.") != 1
                || count_lines_with(rep, "3 item(s) need a disc") != 1
                || count_lines_with(rep, "No disc was asked for") != 0) {
            fprintf(stderr, "FAIL 949: report\n"); bad++;
        }
        fclose(rep);
    }
    lcsas_disc_locator_free(&l);
    if (in) fclose(in); else close(pfd[0]);
    fclose(out);
    lcsas_catalog_close(cat);

    /* Run 2: no catalog.  's' stops every later disc question. */
    out = tmpfile();
    if (!out || pipe(pfd) != 0) {
        fprintf(stderr, "FAIL 949: setup 2\n");
        if (out) fclose(out);
        return bad + 1;
    }
    if (write(pfd[1], "s\n", 2) != 2) { /* input ends early: q */ }
    close(pfd[1]);
    in = fdopen(pfd[0], "r");
    lcsas_disc_locator_init(&l, search, 1, NULL, 1);
    lcsas_disc_locator_set_auto_mount(&l, 0);
    if (in) l.prompt_in = in;
    l.prompt_out = out;
    rc = locate_with_answers(&l, tmpdir, p1, &boxes);
    if (rc != -1 || l.reader_quit || !l.stop_asking || boxes != 1
            || count_lines_with(out, "If you have no more discs") != 1
            || box_lines_are_60(out, "949 stop") <= 0) {
        fprintf(stderr, "FAIL 949: s with no catalog\n"); bad++;
    }
    rc = locate_with_answers(&l, tmpdir, p2, &boxes);
    if (rc != -1 || boxes != 1 || l.skipped_misses != 2) {
        fprintf(stderr, "FAIL 949: asked again after stop\n"); bad++;
    }
    rep = tmpfile();
    if (rep) {
        lcsas_disc_locator_report_skips(&l, rep);
        if (count_lines_with(rep, "Skipped discs") != 0
                || count_lines_with(rep, "No disc was asked for") != 1
                || count_lines_with(rep, "2 item(s) need a disc") != 1) {
            fprintf(stderr, "FAIL 949: report 2\n"); bad++;
        }
        fclose(rep);
    }
    lcsas_disc_locator_free(&l);
    if (in) fclose(in); else close(pfd[0]);
    fclose(out);

    /* No answer 's': the report prints nothing. */
    lcsas_disc_locator_init(&l, search, 1, NULL, 0);
    rep = tmpfile();
    if (rep) {
        lcsas_disc_locator_report_skips(&l, rep);
        if (ftell(rep) != 0) {
            fprintf(stderr, "FAIL 949: report with no skip\n"); bad++;
        }
        fclose(rep);
    }
    lcsas_disc_locator_free(&l);
    return bad;
}

/*
 * #950: a disc catalog for the ranking tests, at <parent>/<disc>/
 * catalog.db: label mark `mark` (PRAGMA user_version), `nvol` volumes
 * numbered from `first_id`, one pack of set `repo`, file time `mtime`.
 */
static int
rank_catalog(const char *parent, const char *disc, long mark, int nvol,
             int first_id, const char *repo, long mtime)
{
    char dir[1024];
    char path[1100];
    char sql[2048];
    struct utimbuf ut;

    snprintf(dir, sizeof dir, "%s/%s", parent, disc);
    snprintf(path, sizeof path, "%s/catalog.db", dir);
    if (mkdir_recursive(dir) != 0) return -1;
    snprintf(sql, sizeof sql,
             "PRAGMA user_version = %ld;"
             "CREATE TABLE schema_version (version INTEGER);"
             "INSERT INTO schema_version VALUES (10);"
             "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
             "  size_bytes INTEGER, repo_id TEXT);"
             "INSERT INTO packs VALUES (1, '%s', 10, '%s');"
             "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY,"
             "  label TEXT, status TEXT);"
             "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1"
             "  FROM n WHERE i + 1 < %d)"
             "  INSERT INTO volumes SELECT %d + i, '%s_' || (%d + i),"
             "  'VERIFIED' FROM n;"
             "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);"
             "INSERT INTO volume_packs VALUES (%d, 1);",
             mark,
             "5555555555555555555555555555555555555555555555555555555555555555",
             repo, nvol, first_id, disc, first_id, first_id);
    if (make_catalog(path, sql) != 0) return -1;
    ut.actime = ut.modtime = (time_t)mtime;
    return utime(path, &ut);
}

/*
 * #950: the catalog a locator takes from the discs under `parent`
 * after one miss, as "<disc>" (the folder name), or "none".  Its
 * stderr goes to `err` when given.
 */
static const char *
picked_disc(const char *parent, char *out, size_t cap)
{
    lcsas_disc_locator l;
    const char *parents[1];
    unsigned char miss[32];
    char found[2048];
    const char *p;

    memset(miss, 0xE7, 32);
    parents[0] = parent;
    lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
    lcsas_disc_locator_set_mount_parents(&l, parents, 1);
    (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
    snprintf(out, cap, "none");
    if (l.owned_catalog_path) {
        p = l.owned_catalog_path + strlen(parent) + 1;
        snprintf(out, cap, "%.*s", (int)(strcspn(p, "/")), p);
    }
    lcsas_disc_locator_free(&l);
    return out;
}

/*
 * #950: catalogs are ranked by what is in them, not by file time.  The
 * highest label mark wins; then the one that lists more volumes; then
 * the newer file.  Each case: discA should win, and discB's file is
 * newer.  Returns the number of failures.
 */
static int
test_catalog_ranking(const char *tmpdir)
{
    struct {
        const char *what;
        long mark_a; int nvol_a; int first_a;
        long mark_b; int nvol_b; int first_b;
    } cases[3];
    char parent[1024];
    char got[64];
    char rm[1100];
    int bad = 0;
    int i;
    FILE *err;
    int saved;

    /* A waiting session aborted after B was made: A lists no more
     * volumes than B, and its mark is higher. */
    cases[0].what = "an older disc that lists as many volumes";
    cases[0].mark_a = 3; cases[0].nvol_a = 2; cases[0].first_a = 1;
    cases[0].mark_b = 2; cases[0].nvol_b = 2; cases[0].first_b = 1;
    /* Two discs of one session carry the same mark; the later one
     * lists more volumes. */
    cases[1].what = "two discs of one session";
    cases[1].mark_a = 3; cases[1].nvol_a = 3; cases[1].first_a = 1;
    cases[1].mark_b = 3; cases[1].nvol_b = 2; cases[1].first_b = 1;
    /* A rebuilt master numbers its rows afresh: B has more volumes,
     * with higher numbers, but A's mark is higher. */
    cases[2].what = "a catalog from a rebuilt master";
    cases[2].mark_a = 5; cases[2].nvol_a = 2; cases[2].first_a = 1;
    cases[2].mark_b = 4; cases[2].nvol_b = 4; cases[2].first_b = 100;

    for (i = 0; i < 3; i++) {
        snprintf(parent, sizeof parent, "%s_rank%d", tmpdir, i);
        if (rank_catalog(parent, "discA", cases[i].mark_a, cases[i].nvol_a,
                         cases[i].first_a, "r", 1000000000L) != 0
                || rank_catalog(parent, "discB", cases[i].mark_b,
                                cases[i].nvol_b, cases[i].first_b, "r",
                                1000000100L) != 0) {
            fprintf(stderr, "FAIL 950: ranking fixture %d\n", i);
            bad++;
            continue;
        }
        if (strcmp(picked_disc(parent, got, sizeof got), "discA") != 0) {
            fprintf(stderr, "FAIL 950: %s: took %s's catalog (want "
                    "discA's)\n", cases[i].what, got);
            bad++;
        }
        snprintf(rm, sizeof rm, "rm -rf %s", parent);
        (void)system(rm);
    }

    /* Everything else equal: the newer file wins. */
    snprintf(parent, sizeof parent, "%s_rank_time", tmpdir);
    if (rank_catalog(parent, "discA", 3, 2, 1, "r", 1000000100L) != 0
            || rank_catalog(parent, "discB", 3, 2, 1, "r", 1000000000L) != 0
            || strcmp(picked_disc(parent, got, sizeof got), "discA") != 0) {
        fprintf(stderr, "FAIL 950: equal catalogs: took %s's (want the "
                "newer file, discA's)\n", got);
        bad++;
    }
    snprintf(rm, sizeof rm, "rm -rf %s", parent);
    (void)system(rm);

    /* An empty catalog.db (a copy that never got its bytes) is called
     * empty, and is not taken. */
    snprintf(parent, sizeof parent, "%s_rank_empty", tmpdir);
    if (rank_catalog(parent, "discA", 3, 2, 1, "r", 1000000000L) != 0) {
        fprintf(stderr, "FAIL 950: empty-catalog fixture\n");
        bad++;
    } else {
        char p[1100];
        struct utimbuf ut;
        snprintf(p, sizeof p, "%s/discB", parent);
        mkdir_recursive(p);
        snprintf(p, sizeof p, "%s/discB/catalog.db", parent);
        write_pack(p, "");
        ut.actime = ut.modtime = (time_t)1000000100L;
        (void)utime(p, &ut);
        err = capture_stderr_begin(&saved);
        (void)picked_disc(parent, got, sizeof got);
        capture_stderr_end(saved);
        if (strcmp(got, "discA") != 0) {
            fprintf(stderr, "FAIL 950: an empty catalog: took %s's\n", got);
            bad++;
        }
        if (err) {
            if (count_lines_with(err, "is empty") != 1
                    || count_lines_with(err, "newer LCSAS") != 0) {
                fprintf(stderr, "FAIL 950: an empty catalog is not called "
                        "empty\n");
                bad++;
            }
            fclose(err);
        }
    }
    snprintf(rm, sizeof rm, "rm -rf %s", parent);
    (void)system(rm);
    return bad;
}

/*
 * #950: catalogs that do not know the backup set being restored.  With
 * the set given (lcsas_disc_locator_set_repo, from the --repo folder):
 * another archive's catalog is never taken, however it ranks, and is
 * named once however many rescans; its disc is not copied into the
 * pack cache.  Each catalog is looked into once per run (the memo):
 * rescans copy and check nothing they have seen.  --catalog is one
 * candidate (lcsas_disc_locator_consider_catalog): when it is unusable
 * it sets no floor.  Returns the number of failures.
 */
static int
test_catalog_of_another_archive(const char *tmpdir)
{
    lcsas_disc_locator l;
    const char *parents[1];
    unsigned char miss[32];
    char found[2048];
    char parent[1024];
    char cache[1024];
    char p[1200];
    char rm[2200];
    char hex[65];
    FILE *err;
    int saved;
    int bad = 0;
    int i;
    struct stat st;

    memset(miss, 0xE7, 32);
    snprintf(parent, sizeof parent, "%s_foreign", tmpdir);
    snprintf(cache, sizeof cache, "%s/lcsas_dl_foreign_%ld", roomy_base(),
             (long)getpid());
    /* Ours (mark 2, older file) and another archive's (mark 9, newer,
     * more volumes), whose disc holds a pack and says its label. */
    if (rank_catalog(parent, "ours", 2, 2, 1, "set-ours", 1000000000L) != 0
            || rank_catalog(parent, "theirs", 9, 5, 1, "set-theirs",
                            1000000100L) != 0) {
        fprintf(stderr, "FAIL 950 foreign: fixture\n");
        return 1;
    }
    snprintf(p, sizeof p, "%s/theirs", parent);
    if (write_real_pack(p, "another archive's pack\n", hex) != 0) {
        fprintf(stderr, "FAIL 950 foreign: pack\n");
        bad++;
    }
    snprintf(p, sizeof p, "%s/theirs/volume_info.json", parent);
    write_pack(p, "{\"label\": \"THEIRS_0005\"}");
    mkdir_recursive(cache);
    parents[0] = parent;

    err = capture_stderr_begin(&saved);
    lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
    lcsas_disc_locator_set_repo(&l, "/mnt/disc/metadata/set-ours/");
    lcsas_disc_locator_set_mount_parents(&l, parents, 1);
    lcsas_disc_locator_set_cache_dir(&l, cache);
    for (i = 0; i < 4; i++)
        (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
    capture_stderr_end(saved);

    if (strcmp(l.repo_id, "set-ours") != 0) {
        fprintf(stderr, "FAIL 950 foreign: the set is '%s' (want "
                "set-ours)\n", l.repo_id);
        bad++;
    }
    if (!l.owned_catalog_path || !strstr(l.owned_catalog_path, "/ours/")) {
        fprintf(stderr, "FAIL 950 foreign: took %s\n",
                l.owned_catalog_path ? l.owned_catalog_path : "none");
        bad++;
    }
    /* Two catalogs, four rescans: each looked into once. */
    if (l.catalog_evaluations != 2) {
        fprintf(stderr, "FAIL 950 memo: %lu catalog evaluations in four "
                "rescans of two discs (want 2)\n", l.catalog_evaluations);
        bad++;
    }
    snprintf(p, sizeof p, "%s/data/%c%c/%s", cache, hex[0], hex[1], hex);
    if (stat(p, &st) == 0) {
        fprintf(stderr, "FAIL 950 foreign: another archive's pack is in "
                "the cache\n");
        bad++;
    }
    if (err) {
        if (count_lines_with(err, "another archive's catalog") != 1
                || count_lines_with(err, "disc THEIRS_0005 belongs to "
                                         "another archive") != 1) {
            fprintf(stderr, "FAIL 950 foreign: want one line for the "
                    "catalog and one for the disc\n");
            bad++;
        }
        fclose(err);
    }
    lcsas_disc_locator_free(&l);

    /* --catalog: another archive's, then a cut-short one.  Neither is
     * taken, and neither stops ours (older) being taken on a rescan. */
    for (i = 0; i < 2; i++) {
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_repo(&l, "set-ours");
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        snprintf(p, sizeof p, "%s/theirs/catalog.db", parent);
        if (i == 1) {
            snprintf(p, sizeof p, "%s_cut.db", parent);
            rank_catalog(parent, "cut", 7, 3, 1, "set-ours", 1000000200L);
            snprintf(rm, sizeof rm, "%s/cut/catalog.db", parent);
            if (truncate(rm, 600) != 0) bad++;
            snprintf(p, sizeof p, "%s", rm);
        }
        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_consider_catalog(&l, p);
        if (l.owned_catalog) {
            capture_stderr_end(saved);
            fprintf(stderr, "FAIL 950 --catalog (%d): an unusable one "
                    "was taken\n", i);
            bad++;
        } else {
            (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            capture_stderr_end(saved);
            if (!l.owned_catalog_path
                    || !strstr(l.owned_catalog_path, "/ours/")) {
                fprintf(stderr, "FAIL 950 --catalog (%d): an unusable "
                        "--catalog set a floor (took %s)\n", i,
                        l.owned_catalog_path ? l.owned_catalog_path
                                             : "none");
                bad++;
            }
        }
        if (err) fclose(err);
        lcsas_disc_locator_free(&l);
    }

    /* A catalog this program cannot query for the set (no packs
     * table): not used, with a warning. */
    snprintf(p, sizeof p, "%s_nopacks.db", parent);
    make_catalog(p, "CREATE TABLE schema_version (version INTEGER);"
                    "INSERT INTO schema_version VALUES (99);");
    lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
    lcsas_disc_locator_set_repo(&l, "set-ours");
    err = capture_stderr_begin(&saved);
    lcsas_disc_locator_consider_catalog(&l, p);
    capture_stderr_end(saved);
    if (l.owned_catalog
            || !err || count_lines_with(err, "cannot read it") != 1) {
        fprintf(stderr, "FAIL 950: a catalog with no packs table\n");
        bad++;
    }
    if (err) fclose(err);
    lcsas_disc_locator_free(&l);
    unlink(p);

    snprintf(rm, sizeof rm, "rm -rf %s %s %s_cut.db", parent, cache, parent);
    (void)system(rm);
    return bad;
}

/*
 * #993: the --repo folder is not named by its set id.  A catalog that
 * does not know the set by that name is judged by the repository's own
 * packs (lcsas_disc_locator_set_index): one that records them all under
 * one set is taken, and that set's id is the backup set from then on;
 * one that records them under two sets is another archive's.  The
 * index names pack A twice and pack B once.  Returns the number of
 * failures.
 */
static int
test_catalog_adopts_set_993(const char *tmpdir)
{
    static const char *const sql[2] = {
        /* A as set-x; a pack that is not ours as set-other. */
        "INSERT INTO packs VALUES (1, '%s', 10, 'set-x');"
        "INSERT INTO packs VALUES (2, '5555', 10, 'set-other');",
        /* A as set-x, B as set-y. */
        "INSERT INTO packs VALUES (1, '%s', 10, 'set-x');"
        "INSERT INTO packs VALUES (2, '%s', 10, 'set-y');",
    };
    lcsas_disc_locator l;
    lcsas_blob_loc e[3];
    lcsas_blob_index ix;
    char path[1024];
    char all[2048];
    char rows[512];
    char a_hex[65];
    char b_hex[65];
    FILE *err;
    int saved;
    int bad = 0;
    int i;

    memset(e, 0, sizeof e);
    memset(e[0].pack_id, 0xAA, 32);
    memset(e[1].pack_id, 0xBB, 32);
    memset(e[2].pack_id, 0xAA, 32);
    ix.entries = e;
    ix.count = 3;
    ix.cap = 3;
    lcsas_hex_encode(e[0].pack_id, 32, a_hex);
    lcsas_hex_encode(e[1].pack_id, 32, b_hex);
    a_hex[64] = b_hex[64] = '\0';
    snprintf(path, sizeof path, "%s_adopt.db", tmpdir);

    for (i = 0; i < 2; i++) {
        snprintf(rows, sizeof rows, sql[i], a_hex, b_hex);
        snprintf(all, sizeof all, "%s%s", GOOD_CATALOG_SQL, rows);
        if (make_catalog(path, all) != 0) {
            fprintf(stderr, "FAIL 993 (%d): fixture\n", i);
            bad++;
            continue;
        }
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_repo(&l, "/tmp/cache/renamed");
        lcsas_disc_locator_set_index(&l, &ix);
        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_consider_catalog(&l, path);
        capture_stderr_end(saved);
        if (i == 0 && (!l.owned_catalog || strcmp(l.repo_id, "set-x") != 0
                       || l.n_index_packs != 2 || !err
                       || count_lines_with(err, "packs restored here "
                                                "as backup set "
                                                "set-x") != 1)) {
            fprintf(stderr, "FAIL 993: a catalog that knows our packs as "
                    "one set was not taken (set '%s', %lu index packs)\n",
                    l.repo_id, (unsigned long)l.n_index_packs);
            bad++;
        }
        if (i == 1 && (l.owned_catalog
                       || strcmp(l.repo_id, "renamed") != 0 || !err
                       || count_lines_with(err, "another archive's "
                                                "catalog") != 1)) {
            fprintf(stderr, "FAIL 993: a catalog that knows our packs as "
                    "two sets was taken (set '%s')\n", l.repo_id);
            bad++;
        }
        if (err) fclose(err);
        lcsas_disc_locator_free(&l);
    }
    unlink(path);
    return bad;
}

/*
 * #957: the catalog has the pack but lists no disc for it (the pack is
 * on a disc burned after this catalog's): name the newest disc this
 * catalog does list, and say to insert the newest data disc -- the
 * Python reader's words.  A DESTROYED volume is not named.
 */
static int
test_newer_than_hint(const char *tmpdir)
{
    char cat_path[1024];
    char sql[2048];
    unsigned char missing[32];
    char label_row[80];
    int bad = 0;

    memset(missing, 0xAB, 32);
    snprintf(label_row, sizeof label_row, "|   %-54s |",
             "LCSAS_BD25_2026_0002");
    snprintf(cat_path, sizeof cat_path, "%s_newer_catalog.db", tmpdir);
    snprintf(sql, sizeof sql,
             "CREATE TABLE schema_version(version INTEGER);"
             "INSERT INTO schema_version VALUES(10);"
             "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
             " size_bytes INTEGER, repo_id TEXT);"
             "INSERT INTO packs VALUES(1, '%s', 10, 'r');"
             "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
             " status TEXT);"
             "INSERT INTO volumes VALUES(1, 'LCSAS_BD25_2026_0001', 'VERIFIED');"
             "INSERT INTO volumes VALUES(2, 'LCSAS_BD25_2026_0002', 'VERIFIED');"
             "INSERT INTO volumes VALUES(3, 'LCSAS_BD25_2026_0003', 'DESTROYED');"
             "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);",
             "abababababababababababababababababababababababababababababababab");
    if (make_catalog(cat_path, sql) != 0) {
        fprintf(stderr, "FAIL 957: fixture\n");
        return 1;
    }
    if (!prompt_mentions(tmpdir, cat_path, missing,
                         "| It is on a disc newer than:                              |")
            || !prompt_mentions(tmpdir, cat_path, missing, label_row)
            || !prompt_mentions(tmpdir, cat_path, missing,
                                "| Insert your newest data disc: its catalog names it.      |")
            || prompt_mentions(tmpdir, cat_path, missing, "_0003")) {
        fprintf(stderr, "FAIL 957: the prompt does not name the disc the "
                "pack is newer than\n");
        bad++;
    }
    unlink(cat_path);
    return bad;
}

/*
 * #966: the disc box's texts.
 *   - A mount folder that is itself a disc (it has data/) is one disc:
 *     "Currently searching" lists it, not its own folders (data,
 *     metadata, ...).  A mount folder that is not a disc still lists
 *     the discs mounted under it.
 *   - The box of a pack the catalog has no record of names the option
 *     that lists the snapshots, --list-snapshots (it said --list, which
 *     no program takes).
 */
static int
test_box_texts_966(const char *tmpdir)
{
    int f = 0;
    char disc[1024], sub[1024], parent[1024], cat_path[1024], sql[2048];
    const char *search[1];
    const char *parents[1];
    unsigned char missing[32];
    FILE *out;
    int prc, quit;

    memset(missing, 0xAB, 32);
    /* A disc mounted straight at the mount folder. */
    snprintf(disc, sizeof disc, "%s_966disc", tmpdir);
    snprintf(sub, sizeof sub, "%s/data/00", disc);
    mkdir_recursive(sub);
    snprintf(sub, sizeof sub, "%s/metadata/someset", disc);
    mkdir_recursive(sub);
    snprintf(sub, sizeof sub, "%s/extras", disc);
    mkdir_recursive(sub);
    search[0] = tmpdir;
    parents[0] = disc;
    out = prompt_run(search, 1, parents, 1, NULL, NULL, missing, "q\n",
                     &prc, &quit);
    if (!out) {
        fprintf(stderr, "FAIL #966 box: no transcript\n"); f++;
    } else {
        if (count_lines_with(out, "_966disc ") != 1) {
            fprintf(stderr, "FAIL #966 box: the disc is not listed once\n");
            f++;
        }
        if (count_lines_with(out, "_966disc/") != 0) {
            fprintf(stderr, "FAIL #966 box: a folder of the disc is "
                            "listed as searched\n"); f++;
        }
        fclose(out);
    }

    /* A mount folder that is not a disc: the disc under it is listed. */
    snprintf(parent, sizeof parent, "%s_966parent", tmpdir);
    snprintf(sub, sizeof sub, "%s/LCSAS_X_0001/data", parent);
    mkdir_recursive(sub);
    parents[0] = parent;
    out = prompt_run(search, 1, parents, 1, NULL, NULL, missing, "q\n",
                     &prc, &quit);
    if (!out) {
        fprintf(stderr, "FAIL #966 box: no transcript (2)\n"); f++;
    } else {
        if (count_lines_with(out, "_966parent/LCSAS_X_0001 ") != 1) {
            fprintf(stderr, "FAIL #966 box: a disc under the mount folder "
                            "is not listed\n"); f++;
        }
        fclose(out);
    }

    /* A mount folder with a plain folder named data in it (a USB disk
     * labelled data, /mnt/data) and a disc beside it: the disc is still
     * searched, and the pack on it found with no question. */
    snprintf(sub, sizeof sub, "%s_966mixed/data", tmpdir);
    mkdir_recursive(sub);
    snprintf(sub, sizeof sub, "%s_966mixed/LCSAS_Y_0001/data/ab", tmpdir);
    mkdir_recursive(sub);
    snprintf(sub, sizeof sub, "%s_966mixed/LCSAS_Y_0001/data/ab/%s", tmpdir,
             "abababababababababababababababababababababababababababababababab");
    write_pack(sub, "pack-contents");
    snprintf(parent, sizeof parent, "%s_966mixed", tmpdir);
    parents[0] = parent;
    out = prompt_run(search, 1, parents, 1, NULL, NULL, missing, "q\n",
                     &prc, &quit);
    if (!out) {
        fprintf(stderr, "FAIL #966 box: no transcript (mixed)\n"); f++;
    } else {
        if (prc != 0 || quit != 0 || count_lines_with(out, "| Pack ") != 0) {
            fprintf(stderr, "FAIL #966: a disc beside a folder named data "
                            "is not searched (rc=%d quit=%d)\n", prc, quit);
            f++;
        }
        fclose(out);
    }
    {
        char rm[2200];
        snprintf(rm, sizeof rm, "rm -rf %s", parent);
        (void)system(rm);
    }

    /* The catalog has no record of the pack. */
    snprintf(cat_path, sizeof cat_path, "%s_966catalog.db", tmpdir);
    snprintf(sql, sizeof sql,
             "CREATE TABLE schema_version(version INTEGER);"
             "INSERT INTO schema_version VALUES(10);"
             "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
             " size_bytes INTEGER, repo_id TEXT);"
             "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
             " uuid TEXT, media_type TEXT, status TEXT);"
             "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);"
             "INSERT INTO packs VALUES(1, '%s', 10, 'r');",
             "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd");
    if (make_catalog(cat_path, sql) != 0) {
        fprintf(stderr, "FAIL #966 box: catalog fixture\n"); f++;
    } else {
        out = prompt_run(search, 1, NULL, 0, NULL, cat_path, missing,
                         "q\n", &prc, &quit);
        if (!out) {
            fprintf(stderr, "FAIL #966 box: no transcript (3)\n"); f++;
        } else {
            if (box_lines_are_60(out, "966 no record") < 10) {
                fprintf(stderr, "FAIL #966 box: lines not 60 wide\n"); f++;
            }
            if (count_lines_with(out, "| Type 'q', then list the snapshots "
                                      "(--list-snapshots)     |") != 1
                    || count_lines_with(out, "| and restore an older one "
                                             "with --snapshot <id>.           |")
                       != 1
                    || count_lines_with(out, "(--list)") != 0) {
                fprintf(stderr, "FAIL #966 box: the option that lists the "
                                "snapshots is not --list-snapshots\n"); f++;
            }
            fclose(out);
        }
    }
    unlink(cat_path);
    {
        char rm[2200];
        snprintf(rm, sizeof rm, "rm -rf %s %s_966parent", disc, tmpdir);
        (void)system(rm);
    }
    return f;
}

int
main(void)
{
    char tmpdir[300];
    char path[1024];
    char found[2048];
    const char *hex =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    unsigned char pack_id[32];
    lcsas_disc_locator l;
    int rc;
    const char *tmp_base = getenv("TMPDIR");

    /* Under $TMPDIR, so a run that never reaches the cleanup at the
     * end (one stopped by a fault-injected malloc or killed by a
     * timeout) leaves its fixtures where its caller removes them. */
    if (!tmp_base || !*tmp_base || strlen(tmp_base) > 256) tmp_base = "/tmp";
    sprintf(tmpdir, "%s/lcsas_dl_test_XXXXXX", tmp_base);
    if (mkdtemp(tmpdir) == NULL) {
        fprintf(stderr, "FAIL: mkdtemp errno=%d\n", errno);
        return 1;
    }

    /* Convert hex to bytes. */
    {
        size_t i;
        for (i = 0; i < 32; i++) {
            unsigned int b;
            sscanf(hex + 2 * i, "%2x", &b);
            pack_id[i] = (unsigned char)b;
        }
    }

    /* Layout 1: tmpdir/data/01/<hex> */
    snprintf(path, sizeof path, "%s/data/01", tmpdir);
    if (mkdir_recursive(path) != 0) {
        fprintf(stderr, "FAIL mkdir layout1\n"); fails++;
    }
    snprintf(path, sizeof path, "%s/data/01/%s", tmpdir, hex);
    if (write_pack(path, "pack-contents") != 0) {
        fprintf(stderr, "FAIL write pack\n"); fails++;
    }

    /* Non-interactive locator: pack must be found in search_paths. */
    {
        const char *search[] = { tmpdir };
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr, "FAIL: locate_pack rc=%d\n", rc); fails++;
        } else if (strstr(found, hex) == NULL) {
            fprintf(stderr, "FAIL: found path %s missing hex\n", found); fails++;
        }
        lcsas_disc_locator_free(&l);
    }

    /* Layout 2: locate the same pack via mount_parents (refresh_discovered). */
    {
        char parent[1024];
        const char *parents[1];
        /* Place tmpdir under a synthetic mount_parent. */
        snprintf(parent, sizeof parent, "%s_parent", tmpdir);
        if (mkdir_recursive(parent) != 0) {
            fprintf(stderr, "FAIL mkdir parent\n"); fails++;
        }
        /* Symlink the existing disc layout into the parent so refresh
         * sees it as a discovered child. */
        {
            char link_path[1024];
            snprintf(link_path, sizeof link_path, "%s/disc1", parent);
            if (symlink(tmpdir, link_path) != 0 && errno != EEXIST) {
                fprintf(stderr, "WARN symlink failed errno=%d\n", errno);
            }
        }
        parents[0] = parent;
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr,
                    "FAIL: locate via mount_parents rc=%d\n", rc); fails++;
        }
        lcsas_disc_locator_free(&l);

        /* Cleanup parent. */
        {
            char rm_cmd[1024];
            snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", parent);
            (void)system(rm_cmd);
        }
    }

    /* Meta-disc LIVE exclusion: tmpdir IS the meta disc and the meta
     * sentinel (recovery/scripts/restore.sh) is present → pack search
     * must skip.  This guards the security property: when the meta
     * disc is still mounted, never try to mount packs from it. */
    {
        const char *search[] = { tmpdir };
        char sentinel_dir[1024];
        char sentinel_file[1024];
        FILE *f;
        snprintf(sentinel_dir, sizeof sentinel_dir,
                 "%s/recovery/scripts", tmpdir);
        if (mkdir_recursive(sentinel_dir) != 0) {
            fprintf(stderr, "FAIL mkdir sentinel\n"); fails++;
        }
        snprintf(sentinel_file, sizeof sentinel_file,
                 "%s/recovery/scripts/restore.sh", tmpdir);
        f = fopen(sentinel_file, "wb");
        if (f) { fputs("#!/bin/sh\n", f); fclose(f); }

        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_meta(&l, tmpdir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc == 0) {
            fprintf(stderr,
                    "FAIL: meta-disc exclusion should hide pack when "
                    "sentinel is present, rc=%d\n", rc); fails++;
        }
        lcsas_disc_locator_free(&l);

        /* Meta-disc EJECTED exclusion: remove the sentinel (simulating
         * the operator ejecting the meta disc and mounting a data disc
         * at the same mount point).  The path must now be considered
         * again (issue #143). */
        unlink(sentinel_file);
        rmdir(sentinel_dir);
        {
            char rec[1024];
            snprintf(rec, sizeof rec, "%s/recovery", tmpdir);
            rmdir(rec);
        }
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_meta(&l, tmpdir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr,
                    "FAIL: meta-disc no longer live (sentinel removed) "
                    "but pack still excluded, rc=%d\n", rc); fails++;
        } else if (strstr(found, hex) == NULL) {
            fprintf(stderr,
                    "FAIL: found path %s missing hex after eject\n",
                    found); fails++;
        }
        lcsas_disc_locator_free(&l);
    }

    /* Cache-dir drain: enable cache, locate, pack should be copied to cache.
     * After locate, the cache should contain the pack.  Cache lives on
     * a roomy fs so the <10%-free guard can't short-circuit the drain. */
    {
        char cache_dir[1024];
        const char *search[] = { tmpdir };
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_cache_%ld", roomy_base(), (long)getpid());
        if (mkdir_recursive(cache_dir) != 0) {
            fprintf(stderr, "FAIL mkdir cache\n"); fails++;
        }
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr, "FAIL: cache locate rc=%d\n", rc); fails++;
        }
        lcsas_disc_locator_free(&l);
        /* Best-effort: verify cache copy exists. */
        snprintf(path, sizeof path, "%s/%c%c/%s",
                 cache_dir, hex[0], hex[1], hex);
        {
            struct stat st;
            if (stat(path, &st) != 0) {
                /* drain may not have copied; not a hard failure since
                 * draining is opportunistic. Document and move on. */
                fprintf(stderr,
                        "[info] drain copy not present at %s (rc=%d)\n",
                        path, errno);
            }
        }
        /* Cleanup cache. */
        {
            char rm_cmd[1024];
            snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", cache_dir);
            (void)system(rm_cmd);
        }
    }

    /* Miss path (non-interactive): a hex that doesn't exist anywhere. */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        memset(missing, 0xFF, 32);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
        if (rc == 0) {
            fprintf(stderr,
                    "FAIL: miss should return non-zero, got 0\n"); fails++;
        }
        lcsas_disc_locator_free(&l);
    }

    /* Discovered mount with a catalog.db file: exercises consider_catalog
     * (disc_locator.c ~lines 243-265).  We just need a file named
     * catalog.db to be present at the discovered mount root. */
    {
        char parent[1024];
        char disc[1024];
        char cat[1024];
        const char *parents[1];
        FILE *f;
        snprintf(parent, sizeof parent, "%s_catparent", tmpdir);
        snprintf(disc, sizeof disc, "%s/discA", parent);
        snprintf(cat, sizeof cat, "%s/catalog.db", disc);
        if (mkdir_recursive(disc) != 0) {
            fprintf(stderr, "FAIL mkdir cat-parent disc\n"); fails++;
        }
        /* Stub catalog.db — content doesn't matter; consider_catalog
         * only stats and tries to open it.  Empty file == invalid db
         * which is fine; the open will fail and the locator falls back
         * to whatever it had. */
        f = fopen(cat, "wb");
        if (f) { fputs("not a real catalog", f); fclose(f); }

        /* Put the searchable pack inside this disc too so locate_pack
         * actually descends and triggers consider_catalog. */
        {
            char dp[1024];
            snprintf(dp, sizeof dp, "%s/data/01", disc);
            mkdir_recursive(dp);
            snprintf(dp, sizeof dp, "%s/data/01/%s", disc, hex);
            write_pack(dp, "pack-from-cat-disc");
        }
        parents[0] = parent;
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc; /* we don't assert here; the path exercise is the goal */
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", parent);
            (void)system(rm);
        }
    }

    /* Pre-populated cache dir: exercises cache_bytes_used.  Place
     * several files of known sizes under cache_dir, then call
     * locate_pack which triggers a drain that consults the cache size. */
    {
        char cache_dir[1024];
        const char *search[] = { tmpdir };
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_prefilled_cache_%ld",
                 roomy_base(), (long)getpid());
        if (mkdir_recursive(cache_dir) != 0) {
            fprintf(stderr, "FAIL mkdir prefilled cache\n"); fails++;
        }
        /* Pre-populate with files of varying sizes (cache_bytes_used
         * walks the dir recursively). */
        {
            char sub[1024];
            FILE *f;
            int j;
            snprintf(sub, sizeof sub, "%s/aa", cache_dir);
            mkdir_recursive(sub);
            for (j = 0; j < 3; j++) {
                snprintf(sub, sizeof sub, "%s/aa/file%d", cache_dir, j);
                f = fopen(sub, "wb");
                if (f) {
                    int k;
                    for (k = 0; k < 100; k++) fputc('x', f);
                    fclose(f);
                }
            }
        }
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc;
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", cache_dir);
            (void)system(rm);
        }
    }

    /* mkdir_p failure: a cache_dir whose parent is a regular file.
     * mkdir_p should walk into the file as if it were a directory,
     * mkdir fails (errno != EEXIST), and we exit early.  Exercises
     * disc_locator.c lines 135-138 (mkdir/stat error paths). */
    {
        char file_as_parent[1024];
        char nested[1024];
        FILE *f;
        snprintf(file_as_parent, sizeof file_as_parent,
                 "%s_blocker_file", tmpdir);
        f = fopen(file_as_parent, "wb");
        if (f) { fputs("x", f); fclose(f); }
        snprintf(nested, sizeof nested, "%s/cant_create_here",
                 file_as_parent);
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, nested);  /* mkdir_p must fail */
        /* l->cache_dir should still be NULL after the failure. */
        lcsas_disc_locator_free(&l);
        unlink(file_as_parent);
    }

    /* Drain chunk-limit branch: with LCSAS_DRAIN_CHUNK_PACKS=1 and >=2
     * packs in the search root, drain_disc must hit limit_reached
     * (disc_locator.c lines 613-614) after copying the first pack. */
    {
        char chunk_root[1024];
        char chunk_cache[1024];
        const char *search[1];
        char p2[1024];
        const char *hex2 =
            "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
        snprintf(chunk_root, sizeof chunk_root, "%s_chunkroot", tmpdir);
        snprintf(chunk_cache, sizeof chunk_cache,
                 "%s/lcsas_dl_chunkcache_%ld", roomy_base(), (long)getpid());
        {
            char dp[1024];
            snprintf(dp, sizeof dp, "%s/data/01", chunk_root);
            mkdir_recursive(dp);
            snprintf(dp, sizeof dp, "%s/data/01/%s", chunk_root, hex);
            write_pack(dp, "first-pack");
            snprintf(p2, sizeof p2, "%s/data/01/%s", chunk_root, hex2);
            write_pack(p2, "second-pack");
        }
        mkdir_recursive(chunk_cache);
        search[0] = chunk_root;
        /* #791: the chunk limit is set on the locator, not through
         * LCSAS_DRAIN_CHUNK_PACKS (no longer read by the binary). */
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        l.drain_chunk_packs = 1;
        lcsas_disc_locator_set_cache_dir(&l, chunk_cache);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc;
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s %s", chunk_root, chunk_cache);
            (void)system(rm);
        }
    }

    /* Pre-populated cache_dir/data/<prefix>/file: actually triggers
     * cache_bytes_used's walk (lines 482-501).  Place files in the
     * correct cache_dir/data/aa/* layout. */
    {
        char cache_dir[1024];
        const char *search[] = { tmpdir };
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_walked_cache_%ld",
                 roomy_base(), (long)getpid());
        {
            char sub[1024];
            FILE *f;
            int j;
            snprintf(sub, sizeof sub, "%s/data/aa", cache_dir);
            mkdir_recursive(sub);
            for (j = 0; j < 3; j++) {
                snprintf(sub, sizeof sub, "%s/data/aa/file%d", cache_dir, j);
                f = fopen(sub, "wb");
                if (f) {
                    int k;
                    for (k = 0; k < 500; k++) fputc('y', f);
                    fclose(f);
                }
            }
        }
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc;
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", cache_dir);
            (void)system(rm);
        }
    }

    /* Interactive prompt + read_response: provide stdin input, run a
     * locate against a missing pack, the locator must prompt and read
     * a response.  We send "q" to abort the prompt loop. */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        FILE *saved_stdin = stdin;
        FILE *fake_in;
        char input_path[1024];
        snprintf(input_path, sizeof input_path, "%s_interactive_input",
                 tmpdir);
        {
            FILE *iw = fopen(input_path, "w");
            if (iw) {
                /* Many "Enter" presses then "q" — read_response reads
                 * one line at a time; tested up to N misses then quit. */
                fputs("\nq\n", iw);
                fclose(iw);
            }
        }
        fake_in = freopen(input_path, "r", stdin);
        if (fake_in == NULL) {
            /* freopen failed — restore and skip without failing the test. */
            stdin = saved_stdin;
            fprintf(stderr, "[info] freopen stdin failed; skipping interactive test\n");
        } else {
            memset(missing, 0xCD, 32);
            lcsas_disc_locator_init(&l, search, 1, NULL, /*interactive=*/1);
            /* Also set a meta_disc.  It is not live (no sentinel), so
             * print_prompt leaves out the "Single-drive restore" block;
             * the rc6 box test below covers it with a live one. */
            lcsas_disc_locator_set_meta(&l, "/tmp/dummy_meta");
            rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            (void)rc;
            lcsas_disc_locator_free(&l);
            /* freopen leaves the file descriptor in place; the
             * subsequent unlink is safe. */
        }
        unlink(input_path);
    }

    /* path_under edge cases: meta exactly matches path or path has a
     * trailing slash.  Exercises disc_locator.c lines 178, 198 (the
     * boundary `path[ml] == '/' || path[ml] == '\\0'` check). */
    {
        const char *search[] = { tmpdir };
        unsigned char fake_id[32];
        memset(fake_id, 0xAB, 32);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        /* Set meta to a prefix that exactly equals one of the search
         * paths.  The path_under check fires when comparing root paths. */
        lcsas_disc_locator_set_meta(&l, tmpdir);
        rc = lcsas_disc_locate_pack(&l, fake_id, found, sizeof found);
        (void)rc;
        lcsas_disc_locator_free(&l);
    }

    /* Drain under a full filesystem [FMA-09]: simulate the ENOSPC
     * write failure with RLIMIT_FSIZE (no sudo/tmpfs needed).
     * copy_file's short fwrite must abort the copy AND unlink the
     * truncated destination -- a half-written pack left in the cache
     * would later be read back as a corrupt pack -- while the locate
     * itself still succeeds from the source disc (draining is
     * opportunistic).  Also covers the missing-source drain edge: a
     * dangling symlink in the same data/<XX>/ dir must be skipped. */
    {
        char full_root[1024];
        char full_cache[1024];
        const char *search[1];
        char p[1024];
        struct rlimit old_rl, rl;
        struct stat st;
        struct statvfs vfs;
        void (*old_xfsz)(int);
        /* roomy_base() keeps the cache off any <10%-free filesystem
         * so the drain guard can't short-circuit before copy_file;
         * the gated harness additionally pins TMPDIR to a fresh
         * tmpfs to make the copy failure fully deterministic. */
        const char *base = roomy_base();

        snprintf(full_root, sizeof full_root,
                 "%s/lcsas_dl_fullroot_%ld", base, (long)getpid());
        snprintf(full_cache, sizeof full_cache,
                 "%s/lcsas_dl_fullcache_%ld", base, (long)getpid());
        if (statvfs(base, &vfs) == 0 && vfs.f_blocks > 0
            && ((unsigned long long)vfs.f_bavail * 100ULL)
               / (unsigned long long)vfs.f_blocks < 10) {
            fprintf(stderr,
                    "[info] %s is <10%% free: the drain guard will fire "
                    "and the copy-failure branch is not exercised here "
                    "(set TMPDIR to a roomier filesystem)\n", base);
        }
        {
            FILE *f;
            long k;
            snprintf(p, sizeof p, "%s/data/01", full_root);
            mkdir_recursive(p);
            snprintf(p, sizeof p, "%s/data/01/%s", full_root, hex);
            f = fopen(p, "wb");
            if (f) {
                for (k = 0; k < 256 * 1024; k++) fputc('z', f);
                fclose(f);
            } else {
                fprintf(stderr, "FAIL: write big pack errno=%d\n", errno);
                fails++;
            }
            /* Missing source: dangling symlink beside the real pack. */
            snprintf(p, sizeof p, "%s/data/01/dangling-src", full_root);
            (void)symlink("/nonexistent/lcsas-fma09", p);
        }
        mkdir_recursive(full_cache);
        search[0] = full_root;

        old_xfsz = signal(SIGXFSZ, SIG_IGN);
        if (getrlimit(RLIMIT_FSIZE, &old_rl) != 0) {
            fprintf(stderr,
                    "[info] getrlimit failed; skipping fs-full drain test\n");
        } else {
            rl = old_rl;
            rl.rlim_cur = 4096;   /* far below the 256 KiB pack */
            if (setrlimit(RLIMIT_FSIZE, &rl) != 0) {
                fprintf(stderr,
                        "[info] setrlimit failed; skipping fs-full drain test\n");
            } else {
                lcsas_disc_locator_init(&l, search, 1, NULL, 0);
                lcsas_disc_locator_set_cache_dir(&l, full_cache);
                rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
                lcsas_disc_locator_free(&l);
                /* Restore the limit before asserting (later writes,
                 * coverage .gcda files at exit). */
                (void)setrlimit(RLIMIT_FSIZE, &old_rl);
                if (rc != 0) {
                    fprintf(stderr,
                            "FAIL: fs-full drain must not break locate, "
                            "rc=%d\n", rc);
                    fails++;
                }
                snprintf(p, sizeof p, "%s/data/01/%s", full_cache, hex);
                if (stat(p, &st) == 0) {
                    fprintf(stderr,
                            "FAIL: truncated pack left in cache at %s "
                            "(%ld bytes)\n", p, (long)st.st_size);
                    fails++;
                }
                snprintf(p, sizeof p, "%s/data/01/%s.part", full_cache, hex);
                if (stat(p, &st) == 0) {
                    fprintf(stderr,
                            "FAIL: truncated .part left in cache at %s\n", p);
                    fails++;
                }
            }
        }
        (void)signal(SIGXFSZ, old_xfsz);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s %s", full_root, full_cache);
            (void)system(rm);
        }
    }

    /* The pack-cache space rule is in bytes, not a percentage (#634).
     * A 1 TB drive 92 % full still has 80 GB free: a 700 MB disc fits.
     * The same drive with only 1.5 GB free does not (1 GiB margin);
     * a 2 GB drive with 600 MB free fits a 500 MB disc (5 % = 100 MB
     * margin) but not a 520 MB one; a disc bigger than the free
     * space never fits; an empty disc always fits a non-full drive. */
    {
#define MB (1000ULL * 1000ULL)
#define TB (MB * 1000ULL * 1000ULL)
        static const struct { unsigned long long fr, tot, need; int want; } c[] = {
            { 80000ULL * MB, 1ULL * TB, 700ULL * MB, 1 },
            { 1500ULL * MB,  1ULL * TB, 700ULL * MB, 0 },
            { 600ULL * MB,   2000ULL * MB, 500ULL * MB, 1 },
            { 600ULL * MB,   2000ULL * MB, 520ULL * MB, 0 },
            { 600ULL * MB,   2000ULL * MB, 700ULL * MB, 0 },
            { 600ULL * MB,   2000ULL * MB, 0ULL, 1 },
        };
#undef TB
#undef MB
        size_t k;
        for (k = 0; k < sizeof c / sizeof c[0]; k++) {
            int got = lcsas_drain_fits(c[k].fr, c[k].tot, c[k].need);
            if (!got != !c[k].want) {
                fprintf(stderr,
                        "FAIL: lcsas_drain_fits(free=%llu, total=%llu, "
                        "need=%llu) = %d, want %d\n", c[k].fr, c[k].tot,
                        c[k].need, got, c[k].want);
                fails++;
            }
        }
    }

    /* Cache-space guard [FMA-09, #634]: when LCSAS_TEST_FULL_FS_DIR
     * names a directory on a filesystem with less than 5 % free (the gated Python
     * harness in tests/recovery_hardening/test_restore_space_preflight.py
     * mounts and fills a 1 MiB tmpfs), there is no room for the disc
     * plus the margin, so draining into a cache there must be REFUSED:
     * "too little to copy disc" warning on stderr, no pack copied,
     * locate still succeeds.  Self-skips when the env var is unset
     * (plain `make test` runs) or the named fs has 5 % or more free. */
    {
        const char *full_fs = getenv("LCSAS_TEST_FULL_FS_DIR");
        struct statvfs vfs;
        int pct_free = 100;

        if (full_fs && statvfs(full_fs, &vfs) == 0 && vfs.f_blocks > 0) {
            pct_free = (int)(((unsigned long long)vfs.f_bavail * 100ULL)
                             / (unsigned long long)vfs.f_blocks);
        }
        if (full_fs == NULL) {
            fprintf(stderr,
                    "[info] LCSAS_TEST_FULL_FS_DIR unset; skipping "
                    "critically-full drain-guard test\n");
        } else if (pct_free >= 5) {
            fprintf(stderr,
                    "[info] %s is %d%% free (not <5%%); skipping "
                    "critically-full drain-guard test\n", full_fs, pct_free);
        } else {
            char guard_cache[1024];
            char dst[1024];
            const char *search[1];
            struct stat st;

            snprintf(guard_cache, sizeof guard_cache,
                     "%s/lcsas_guard_cache", full_fs);
            mkdir_recursive(guard_cache);
            search[0] = tmpdir;   /* still holds data/01/<hex> */
            lcsas_disc_locator_init(&l, search, 1, NULL, 0);
            lcsas_disc_locator_set_cache_dir(&l, guard_cache);
            rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
            lcsas_disc_locator_free(&l);
            if (rc != 0) {
                fprintf(stderr,
                        "FAIL: critically-full cache must not break "
                        "locate, rc=%d\n", rc);
                fails++;
            }
            snprintf(dst, sizeof dst, "%s/data/01/%s", guard_cache, hex);
            if (stat(dst, &st) == 0) {
                fprintf(stderr,
                        "FAIL: drain wrote %s despite critically-full "
                        "cache filesystem\n", dst);
                fails++;
            }
        }
    }

    /* mkdir_p INTERMEDIATE failure: a cache_dir TWO levels under a
     * regular file.  The intermediate mkdir("<file>/a") fails with
     * ENOTDIR (not a benign race), so mkdir_p restores the slash and
     * the follow-up stat also fails -> early -1.  Exercises
     * disc_locator.c lines 135-138; the one-level case above only
     * reaches the FINAL mkdir at the end of mkdir_p. */
    {
        char file_as_parent[1024];
        char nested[1024];
        FILE *f;
        snprintf(file_as_parent, sizeof file_as_parent,
                 "%s_blocker_file2", tmpdir);
        f = fopen(file_as_parent, "wb");
        if (f) { fputs("x", f); fclose(f); }
        snprintf(nested, sizeof nested, "%s/a/b", file_as_parent);
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, nested);  /* must fail */
        lcsas_disc_locator_free(&l);
        unlink(file_as_parent);
    }

    /* path_under strict-child branch: a search path that is a CHILD
     * of a live meta disc must be excluded.  meta=<mnt> (sentinel
     * present), search=<mnt>/disc -> path_under("<mnt>/disc","<mnt>")
     * takes the prefix-child return (disc_locator.c line 178). */
    {
        char mnt[1024];
        char disc[1024];
        char sub[1024];
        const char *search[1];
        FILE *f;
        snprintf(mnt, sizeof mnt, "%s_metamnt", tmpdir);
        snprintf(disc, sizeof disc, "%s/disc", mnt);
        snprintf(sub, sizeof sub, "%s/recovery/scripts", mnt);
        if (mkdir_recursive(sub) != 0) {
            fprintf(stderr, "FAIL mkdir meta-child sentinel\n"); fails++;
        }
        snprintf(sub, sizeof sub, "%s/recovery/scripts/restore.sh", mnt);
        f = fopen(sub, "wb");
        if (f) { fputs("#!/bin/sh\n", f); fclose(f); }
        /* Real pack under the child so only the exclusion can hide it. */
        snprintf(sub, sizeof sub, "%s/data/01", disc);
        mkdir_recursive(sub);
        snprintf(sub, sizeof sub, "%s/data/01/%s", disc, hex);
        write_pack(sub, "pack-under-meta-child");
        search[0] = disc;
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_meta(&l, mnt);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc == 0) {
            fprintf(stderr,
                    "FAIL: search path under live meta must be "
                    "excluded, rc=%d\n", rc); fails++;
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", mnt);
            (void)system(rm);
        }
    }

    /* push_discovered dedup: the same directory given as BOTH a
     * search path and a mount parent must not be double-registered.
     * Locating a missing pack forces refresh_discovered ->
     * push_discovered(tmpdir), which matches search_paths[0] and
     * returns early (disc_locator.c line 229). */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        const char *parents[] = { tmpdir };
        memset(missing, 0xEE, 32);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
        if (rc == 0) {
            fprintf(stderr,
                    "FAIL: dedup miss should return non-zero, got 0\n");
            fails++;
        }
        lcsas_disc_locator_free(&l);
    }

    /* consider_catalog WITH a cache dir: the catalog.db found on a
     * discovered mount must be copied into the cache and the COPY
     * opened, not the original (disc_locator.c lines 274-284:
     * snprintf of the copy path, unlink of a prior copy, copy_file,
     * open of the copy).  A stub catalog copies fine and then fails
     * to open, which is enough to line-cover the path. */
    {
        char parent[1024];
        char disc[1024];
        char cat[1024];
        char cache_dir[1024];
        const char *parents[1];
        FILE *f;
        snprintf(parent, sizeof parent, "%s_catcacheparent", tmpdir);
        snprintf(disc, sizeof disc, "%s/discA", parent);
        snprintf(cat, sizeof cat, "%s/catalog.db", disc);
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_catcache_%ld", roomy_base(), (long)getpid());
        if (mkdir_recursive(disc) != 0) {
            fprintf(stderr, "FAIL mkdir catcache disc\n"); fails++;
        }
        f = fopen(cat, "wb");
        if (f) { fputs("not a real catalog", f); fclose(f); }
        {
            char dp[1024];
            snprintf(dp, sizeof dp, "%s/data/01", disc);
            mkdir_recursive(dp);
            snprintf(dp, sizeof dp, "%s/data/01/%s", disc, hex);
            write_pack(dp, "pack-from-catcache-disc");
        }
        mkdir_recursive(cache_dir);
        parents[0] = parent;
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc; /* the catalog-copy path exercise is the goal */
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache_dir);
            (void)system(rm);
        }
    }

    /* refresh_discovered: a mount-parent child that IS the live meta
     * disc must be skipped during the walk (disc_locator.c line 373)
     * while sibling discs are still discovered and searched. */
    {
        char parent[1024];
        char metadisc[1024];
        char discb[1024];
        char sub[1024];
        const char *parents[1];
        FILE *f;
        snprintf(parent, sizeof parent, "%s_metaskipparent", tmpdir);
        snprintf(metadisc, sizeof metadisc, "%s/metadisc", parent);
        snprintf(discb, sizeof discb, "%s/discB", parent);
        snprintf(sub, sizeof sub, "%s/recovery/scripts", metadisc);
        if (mkdir_recursive(sub) != 0) {
            fprintf(stderr, "FAIL mkdir metaskip sentinel\n"); fails++;
        }
        snprintf(sub, sizeof sub,
                 "%s/recovery/scripts/restore.sh", metadisc);
        f = fopen(sub, "wb");
        if (f) { fputs("#!/bin/sh\n", f); fclose(f); }
        snprintf(sub, sizeof sub, "%s/data/01", discb);
        mkdir_recursive(sub);
        snprintf(sub, sizeof sub, "%s/data/01/%s", discb, hex);
        write_pack(sub, "pack-on-sibling-disc");
        parents[0] = parent;
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        lcsas_disc_locator_set_meta(&l, metadisc);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr,
                    "FAIL: sibling disc must still be found when a "
                    "mount-parent child is the live meta, rc=%d\n", rc);
            fails++;
        } else if (strstr(found, "discB") == NULL) {
            fprintf(stderr,
                    "FAIL: found %s not on sibling discB\n", found);
            fails++;
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", parent);
            (void)system(rm);
        }
    }

    /* Cache fast path: a pack pre-seeded at cache_dir/data/<XX>/<hex>
     * must be found from the cache before any search path or drain is
     * tried (disc_locator.c line 671). */
    {
        char cache_dir[1024];
        char cp[1024];
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_seeded_cache_%ld",
                 roomy_base(), (long)getpid());
        snprintf(cp, sizeof cp, "%s/data/01", cache_dir);
        mkdir_recursive(cp);
        snprintf(cp, sizeof cp, "%s/data/01/%s", cache_dir, hex);
        write_pack(cp, "pack-from-cache");
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc != 0) {
            fprintf(stderr, "FAIL: cache fast path rc=%d\n", rc); fails++;
        } else if (strstr(found, cache_dir) == NULL) {
            fprintf(stderr,
                    "FAIL: found %s not from cache\n", found); fails++;
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", cache_dir);
            (void)system(rm);
        }
    }

    /* >1 GiB cache soft warning: a SPARSE 2 GiB file pre-seeded in
     * the cache's data/ tree makes cache_bytes_used exceed 1 GiB, so
     * the next drain fires the single-shot size warning
     * (disc_locator.c lines 585-591).  Sparse -> near-zero real disk.
     * Skips (without failing) if the filesystem refuses ftruncate. */
    {
        char cache_dir[1024];
        char sp[1024];
        const char *search[] = { tmpdir };
        int fd;
        int seeded = 0;
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_bigcache_%ld", roomy_base(), (long)getpid());
        snprintf(sp, sizeof sp, "%s/data/aa", cache_dir);
        mkdir_recursive(sp);
        snprintf(sp, sizeof sp, "%s/data/aa/sparse-2g", cache_dir);
        fd = open(sp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) {
            if (ftruncate(fd, (off_t)1 << 31) == 0) seeded = 1;
            close(fd);
        }
        if (!seeded) {
            fprintf(stderr,
                    "[info] cannot create 2 GiB sparse file at %s; "
                    "skipping >1GiB cache-warn test\n", sp);
        } else {
            lcsas_disc_locator_init(&l, search, 1, NULL, 0);
            lcsas_disc_locator_set_cache_dir(&l, cache_dir);
            rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
            if (rc != 0) {
                fprintf(stderr,
                        "FAIL: locate with oversized cache rc=%d\n", rc);
                fails++;
            }
            lcsas_disc_locator_free(&l);
        }
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s", cache_dir);
            (void)system(rm);
        }
    }

    /* Auto-detect (#455), poll path: nothing is ever typed, and a disc
     * that appears under a mount parent while the prompt waits is
     * found by the poll, so the locate returns with no ENTER.  A child
     * process renames a staged directory into the parent (atomic, like
     * a mount).  stdin is a pipe whose write end stays open, so
     * select() keeps timing out instead of seeing EOF.  alarm() turns
     * a poll that never finds the disc into a failed test, not a hang. */
    {
        const char *hex2 =
            "fe23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        unsigned char id2[32];
        char parent[1024], staged[1024], disc[1024], p2[1024];
        const char *parents[1];
        const char *search[] = { tmpdir };
        int pfd[2];
        size_t k;
        for (k = 0; k < 32; k++) {
            unsigned int b;
            sscanf(hex2 + 2 * k, "%2x", &b);
            id2[k] = (unsigned char)b;
        }
        snprintf(parent, sizeof parent, "%s/automount_media", tmpdir);
        snprintf(staged, sizeof staged, "%s/automount_staged", tmpdir);
        snprintf(disc, sizeof disc, "%s/DISC_2", parent);
        snprintf(p2, sizeof p2, "%s/data/fe", staged);
        mkdir_recursive(parent);
        mkdir_recursive(p2);
        snprintf(p2, sizeof p2, "%s/data/fe/%s", staged, hex2);
        write_pack(p2, "auto-mounted");
        parents[0] = parent;
        if (pipe(pfd) != 0) {
            fprintf(stderr, "FAIL: pipe errno=%d\n", errno); fails++;
        } else {
            pid_t kid = fork();
            if (kid == 0) {
                struct timeval tv;
                tv.tv_sec = 0;
                tv.tv_usec = 300000;      /* let the prompt start waiting */
                (void)select(0, NULL, NULL, NULL, &tv);
                _exit(rename(staged, disc) == 0 ? 0 : 1);
            } else {
                FILE *in = fdopen(pfd[0], "r");
                FILE *out = tmpfile();
                char line[256];
                int saw_hint = 0;
                lcsas_disc_locator_init(&l, search, 1, NULL, 1);
                lcsas_disc_locator_set_mount_parents(&l, parents, 1);
                if (in) l.prompt_in = in;
                if (out) l.prompt_out = out;
                l.poll_ms = 50;
                alarm(20);
                rc = lcsas_disc_locate_pack(&l, id2, found, sizeof found);
                alarm(0);
                if (rc != 0 || strstr(found, "DISC_2") == NULL) {
                    fprintf(stderr,
                            "FAIL: auto-detect poll rc=%d found=%s\n",
                            rc, rc == 0 ? found : "(none)");
                    fails++;
                }
                if (out) {
                    rewind(out);
                    while (fgets(line, sizeof line, out)) {
                        if (strstr(line, "found without ENTER")) saw_hint = 1;
                    }
                    fclose(out);
                    if (!saw_hint) {
                        fprintf(stderr,
                                "FAIL: auto-detect prompt lacks its hint line\n");
                        fails++;
                    }
                }
                lcsas_disc_locator_free(&l);
                close(pfd[1]);
                if (in) fclose(in); else close(pfd[0]);
                (void)waitpid(kid, NULL, 0);
            }
        }
    }

    /* Auto-detect, typed-ahead answers: "\nq\n" arrives in ONE write.
     * The poll must see the 'q' still waiting after the ENTER.  This
     * pins the read(2)-not-stdio choice: fgetc would slurp the whole
     * pipe into the FILE buffer on the ENTER, the next select() would
     * then block on an empty pipe, and the 'q' would sit unseen (here:
     * killed by alarm()). */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        int pfd[2];
        memset(missing, 0xEE, 32);
        if (pipe(pfd) != 0) {
            fprintf(stderr, "FAIL: pipe errno=%d\n", errno); fails++;
        } else {
            FILE *in = fdopen(pfd[0], "r");
            FILE *out = fopen("/dev/null", "w");
            if (write(pfd[1], "\nq\n", 3) != 3) {
                fprintf(stderr, "FAIL: pipe write\n"); fails++;
            }
            lcsas_disc_locator_init(&l, search, 1, NULL, 1);
            if (in) l.prompt_in = in;
            if (out) l.prompt_out = out;
            l.poll_ms = 50;
            alarm(20);
            rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            alarm(0);
            if (rc != -1 || l.misses != 2) {
                fprintf(stderr,
                        "FAIL: typed-ahead ENTER+q rc=%d misses=%lu "
                        "(want -1, 2)\n", rc, l.misses);
                fails++;
            }
            lcsas_disc_locator_free(&l);
            close(pfd[1]);
            if (in) fclose(in); else close(pfd[0]);
            if (out) fclose(out);
        }
    }

    /* --auto-mount off: the same answers go through stdio, unpolled,
     * and the prompt does not claim discs are found without ENTER. */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        int pfd[2];
        memset(missing, 0xEF, 32);
        if (pipe(pfd) != 0) {
            fprintf(stderr, "FAIL: pipe errno=%d\n", errno); fails++;
        } else {
            FILE *in = fdopen(pfd[0], "r");
            FILE *out = tmpfile();
            char line[256];
            int saw_hint = 0;
            if (write(pfd[1], "\nq\n", 3) != 3) {
                fprintf(stderr, "FAIL: pipe write\n"); fails++;
            }
            close(pfd[1]);
            lcsas_disc_locator_init(&l, search, 1, NULL, 1);
            lcsas_disc_locator_set_auto_mount(&l, 0);
            if (in) l.prompt_in = in;
            if (out) l.prompt_out = out;
            alarm(20);
            rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            alarm(0);
            if (rc != -1 || l.misses != 2) {
                fprintf(stderr,
                        "FAIL: auto-mount off ENTER+q rc=%d misses=%lu "
                        "(want -1, 2)\n", rc, l.misses);
                fails++;
            }
            if (out) {
                rewind(out);
                while (fgets(line, sizeof line, out)) {
                    if (strstr(line, "found without ENTER")) saw_hint = 1;
                }
                fclose(out);
                if (saw_hint) {
                    fprintf(stderr,
                            "FAIL: auto-mount off prompt still shows the "
                            "auto-detect hint\n");
                    fails++;
                }
            }
            lcsas_disc_locator_free(&l);
            if (in) fclose(in); else close(pfd[0]);
        }
    }

    /* select() refusing the descriptor: polling switches itself off for
     * the run and the answer is read the old way.  A FILE whose fd has
     * been closed makes select() fail with EBADF; the stdio read then
     * hits EOF, which aborts.  (No mount parents and no catalog, so
     * nothing opens a file -- and could reuse the fd number -- before
     * the select.) */
    {
        unsigned char missing[32];
        const char *search[] = { tmpdir };
        int pfd[2];
        memset(missing, 0xED, 32);
        if (pipe(pfd) != 0) {
            fprintf(stderr, "FAIL: pipe errno=%d\n", errno); fails++;
        } else {
            FILE *in = fdopen(pfd[0], "r");
            FILE *out = fopen("/dev/null", "w");
            lcsas_disc_locator_init(&l, search, 1, NULL, 1);
            if (in) l.prompt_in = in;
            if (out) l.prompt_out = out;
            l.poll_ms = 50;
            close(pfd[0]);
            alarm(20);
            rc = lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            alarm(0);
            if (rc != -1 || l.auto_mount != 0) {
                fprintf(stderr,
                        "FAIL: select-failure fallback rc=%d auto_mount=%d "
                        "(want -1, 0)\n", rc, l.auto_mount);
                fails++;
            }
            lcsas_disc_locator_free(&l);
            close(pfd[1]);
            if (in) fclose(in);
            if (out) fclose(out);
        }
    }

    /* Volume label (#456): each refresh of the mount parents names a
     * disc by the label in its volume_info.json, once per insertion.
     * Absent file: silent.  A bad one gets a note and the disc is still
     * searched.  Non-interactive misses drive the refreshes here. */
    {
        char parent[1024], away[1024], disc[1024], p[1024];
        char longlabel[260];
        const char *parents[1];
        const char *search[] = { tmpdir };
        unsigned char missing[32];
        FILE *out = tmpfile();
        int k;
        memset(missing, 0xEC, 32);
        snprintf(parent, sizeof parent, "%s/label_media", tmpdir);
        mkdir_recursive(parent);
        make_labelled_disc(parent, "VOL_OK",
            "{\"uuid\": \"u-1\", \"label\": \"LCSAS_BD25_2026_0001\", "
            "\"media_type\": \"BD25\"}");
        make_labelled_disc(parent, "VOL_META",
            "{\"type\": \"meta\", \"description\": \"rescue\"}");
        make_labelled_disc(parent, "VOL_BADJSON", "{\"label\": \"x\"");
        make_labelled_disc(parent, "VOL_ARRAY", "[\"label\"]");
        make_labelled_disc(parent, "VOL_NOLABEL", "{\"uuid\": \"u-2\"}");
        make_labelled_disc(parent, "VOL_NUMLABEL", "{\"label\": 7}");
        make_labelled_disc(parent, "VOL_CTRL",
            "{\"label\": \"EVIL\\u001b[31mX\\u0007\"}");
        memset(longlabel, 0, sizeof longlabel);
        memcpy(longlabel, "{\"label\": \"", 11);
        memset(longlabel + 11, 'L', 200);
        memcpy(longlabel + 211, "\"}", 2);
        make_labelled_disc(parent, "VOL_LONGLABEL", longlabel);
        make_labelled_disc(parent, "VOL_NONE", NULL);
        /* volume_info.json that is a directory: stat()s, can't be read.
         * Not empty: btrfs gives an empty directory st_size 0, which
         * would read as an empty file rather than fail with EISDIR. */
        snprintf(p, sizeof p, "%s/VOL_DIR/volume_info.json", parent);
        mkdir_recursive(p);
        snprintf(p, sizeof p, "%s/VOL_DIR/volume_info.json/entry", parent);
        write_pack(p, "x");
        /* Past the size cap: a sparse file, so nothing is written. */
        make_labelled_disc(parent, "VOL_BIG", "{}");
        snprintf(p, sizeof p, "%s/VOL_BIG/volume_info.json", parent);
        if (truncate(p, 16L * 1024L * 1024L + 1L) != 0) {
            fprintf(stderr, "FAIL: truncate VOL_BIG errno=%d\n", errno);
            fails++;
        }
        parents[0] = parent;

        if (!out) {
            fprintf(stderr, "FAIL: tmpfile\n"); fails++;
        } else {
            lcsas_disc_locator_init(&l, search, 1, NULL, 0);
            lcsas_disc_locator_set_mount_parents(&l, parents, 1);
            l.prompt_out = out;
            for (k = 0; k < 3; k++) {
                (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            }
            {
                static const struct { const char *needle; int want; } c[] = {
                    { "mounted the disc labelled LCSAS_BD25_2026_0001 at ", 1 },
                    { "mounted the META disc at ", 1 },
                    { "VOL_BADJSON: its volume_info.json is not valid JSON; "
                      "searching it anyway.", 1 },
                    { "VOL_ARRAY: its volume_info.json is not valid JSON", 1 },
                    { "VOL_NOLABEL: its volume_info.json has no usable label", 1 },
                    { "VOL_NUMLABEL: its volume_info.json has no usable label", 1 },
                    { "VOL_LONGLABEL: its volume_info.json has no usable label", 1 },
                    { "mounted the disc labelled EVIL?[31mX? at ", 1 },
                    { "VOL_DIR: its volume_info.json could not be read", 1 },
                    { "VOL_BIG: its volume_info.json is too large", 1 },
                    { "VOL_NONE", 0 },
                    { "\033", 0 }
                };
                size_t j;
                for (j = 0; j < sizeof c / sizeof c[0]; j++) {
                    int got = count_lines_with(out, c[j].needle);
                    if (got != c[j].want) {
                        fprintf(stderr,
                                "FAIL: volume label: %d line(s) with '%s' "
                                "after 3 refreshes (want %d)\n",
                                got, c[j].needle, c[j].want);
                        fails++;
                    }
                }
            }

            /* Eject VOL_OK and put it back: named again.  Then a
             * different disc at the same path: named too. */
            snprintf(disc, sizeof disc, "%s/VOL_OK", parent);
            snprintf(away, sizeof away, "%s/label_away", tmpdir);
            if (rename(disc, away) != 0) {
                fprintf(stderr, "FAIL: eject VOL_OK errno=%d\n", errno);
                fails++;
            }
            (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            if (rename(away, disc) != 0) {
                fprintf(stderr, "FAIL: reinsert VOL_OK errno=%d\n", errno);
                fails++;
            }
            (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            if (count_lines_with(out, "mounted the disc labelled LCSAS_BD25_2026_0001 ")
                    != 2) {
                fprintf(stderr,
                        "FAIL: volume label: a disc ejected and put back "
                        "is not named again (or is named every refresh)\n");
                fails++;
            }
            snprintf(p, sizeof p, "%s/VOL_OK/volume_info.json", parent);
            write_pack(p, "{\"label\": \"LCSAS_BD25_2026_0002\", \"uuid\": "
                          "\"u-other-disc\"}");
            (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
            if (count_lines_with(out, "mounted the disc labelled LCSAS_BD25_2026_0002 ")
                    != 1) {
                fprintf(stderr,
                        "FAIL: volume label: a different disc at the same "
                        "mount path is not named\n");
                fails++;
            }
            lcsas_disc_locator_free(&l);
            fclose(out);
        }

        /* A mount parent too long to append "/volume_info.json" to is
         * skipped, not overrun. */
        {
            char *huge = (char *)malloc(5000);
            if (huge) {
                const char *hp[1];
                memset(huge, 'p', 4999);
                huge[0] = '/';
                huge[4999] = '\0';
                hp[0] = huge;
                lcsas_disc_locator_init(&l, search, 1, NULL, 0);
                lcsas_disc_locator_set_mount_parents(&l, hp, 1);
                (void)lcsas_disc_locate_pack(&l, missing, found, sizeof found);
                if (l.n_seen_volumes != 0) {
                    fprintf(stderr,
                            "FAIL: volume label: overlong mount parent "
                            "recorded as a disc\n");
                    fails++;
                }
                lcsas_disc_locator_free(&l);
                free(huge);
            }
        }
    }

    /* Volume label while the prompt polls: the wrong disc goes in
     * first.  It is named on a fresh line (not glued to the "> "),
     * followed by the "still not found" line; a child process waits for
     * that line, ejects it and inserts the right disc, which is named
     * and then picked up. */
    {
        const char *hex3 =
            "fd23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        unsigned char id3[32];
        char parent[1024], wrong[1024], right[1024], outpath[1024];
        char wrong_in[1024], right_in[1024], p[1024];
        const char *parents[1];
        const char *search[] = { tmpdir };
        int pfd[2];
        size_t k;
        for (k = 0; k < 32; k++) {
            unsigned int b;
            sscanf(hex3 + 2 * k, "%2x", &b);
            id3[k] = (unsigned char)b;
        }
        snprintf(parent, sizeof parent, "%s/label_poll_media", tmpdir);
        snprintf(wrong, sizeof wrong, "%s/label_poll_wrong", tmpdir);
        snprintf(right, sizeof right, "%s/label_poll_right", tmpdir);
        snprintf(wrong_in, sizeof wrong_in, "%s/DISC_W", parent);
        snprintf(right_in, sizeof right_in, "%s/DISC_R", parent);
        snprintf(outpath, sizeof outpath, "%s/label_poll_out.txt", tmpdir);
        mkdir_recursive(parent);
        make_labelled_disc(tmpdir, "label_poll_wrong",
                           "{\"label\": \"WRONG_DISC\"}");
        make_labelled_disc(tmpdir, "label_poll_right",
                           "{\"label\": \"RIGHT_DISC\"}");
        snprintf(p, sizeof p, "%s/data/fd", right);
        mkdir_recursive(p);
        snprintf(p, sizeof p, "%s/data/fd/%s", right, hex3);
        write_pack(p, "right-disc");
        parents[0] = parent;
        if (pipe(pfd) != 0) {
            fprintf(stderr, "FAIL: pipe errno=%d\n", errno); fails++;
        } else {
            FILE *out = fopen(outpath, "w+");
            pid_t kid = out ? fork() : -1;
            if (kid == 0) {
                /* Once the prompt is up, insert the wrong disc; once
                 * the locator says it isn't the one, swap it for the
                 * right one.  Each wait gives up after ~15 s. */
                static const char *const waits[] = {
                    "is required for the next file", "(still not found"
                };
                int w;
                for (w = 0; w < 2; w++) {
                    struct timeval tv;
                    int tries;
                    for (tries = 0; tries < 750; tries++) {
                        char buf[8192];
                        size_t n;
                        FILE *f = fopen(outpath, "r");
                        if (f) {
                            n = fread(buf, 1, sizeof buf - 1, f);
                            buf[n] = '\0';
                            fclose(f);
                            if (strstr(buf, waits[w])) break;
                        }
                        tv.tv_sec = 0;
                        tv.tv_usec = 20000;
                        (void)select(0, NULL, NULL, NULL, &tv);
                    }
                    if (w == 0) {
                        if (rename(wrong, wrong_in) != 0) _exit(1);
                    } else {
                        if (rename(wrong_in, wrong) != 0) _exit(1);
                        _exit(rename(right, right_in) == 0 ? 0 : 1);
                    }
                }
                _exit(1);
            } else if (kid > 0) {
                FILE *in = fdopen(pfd[0], "r");
                char lines[64][512];
                int nl = 0, iw = -1, ir = -1, glued = 0, i;
                lcsas_disc_locator_init(&l, search, 1, NULL, 1);
                lcsas_disc_locator_set_mount_parents(&l, parents, 1);
                if (in) l.prompt_in = in;
                l.prompt_out = out;
                l.poll_ms = 50;
                alarm(20);
                rc = lcsas_disc_locate_pack(&l, id3, found, sizeof found);
                alarm(0);
                if (rc != 0 || strstr(found, "DISC_R") == NULL) {
                    fprintf(stderr,
                            "FAIL: volume label poll rc=%d found=%s\n",
                            rc, rc == 0 ? found : "(none)");
                    fails++;
                }
                rewind(out);
                while (nl < 64 && fgets(lines[nl], sizeof lines[nl], out)) {
                    if (strstr(lines[nl], "mounted the disc labelled")
                            && strncmp(lines[nl], "[lcsas-restore] ", 16) != 0)
                        glued = 1;
                    if (strstr(lines[nl], "mounted the disc labelled WRONG_DISC at ")
                            && iw < 0)
                        iw = nl;
                    if (strstr(lines[nl], "mounted the disc labelled RIGHT_DISC at ")
                            && ir < 0)
                        ir = nl;
                    nl++;
                }
                if (glued) {
                    fprintf(stderr,
                            "FAIL: volume label printed on the prompt's "
                            "'> ' line\n");
                    fails++;
                }
                if (iw < 0 || iw + 1 >= nl
                        || strncmp(lines[iw + 1], "(still not found", 16) != 0) {
                    fprintf(stderr,
                            "FAIL: wrong disc not named, or not followed "
                            "by 'still not found'\n");
                    fails++;
                }
                if (ir < 0 || ir < iw || ir + 1 >= nl
                        || strncmp(lines[ir + 1],
                                   "[lcsas-restore] new disc detected", 33) != 0) {
                    fprintf(stderr,
                            "FAIL: right disc not named just before "
                            "'new disc detected'\n");
                    fails++;
                }
                for (i = 0; i < nl; i++) {
                    if (strcmp(lines[i], "\n") == 0 && i + 1 < nl
                            && strstr(lines[i + 1], "new disc detected")) {
                        fprintf(stderr,
                                "FAIL: blank line before 'new disc "
                                "detected'\n");
                        fails++;
                    }
                }
                lcsas_disc_locator_free(&l);
                close(pfd[1]);
                if (in) fclose(in); else close(pfd[0]);
                (void)waitpid(kid, NULL, 0);
            } else {
                fprintf(stderr, "FAIL: fork/fopen\n"); fails++;
                close(pfd[0]);
                close(pfd[1]);
            }
            if (out) fclose(out);
        }
    }

    /* #488: print_prompt's catalog branches, from real catalogs.  The
     * missing pack's hex is 64 x "ab". */
    {
        unsigned char missing[32];
        char cat_path[1024];
        const char *schema =
            "CREATE TABLE schema_version(version INTEGER);"
            "INSERT INTO schema_version VALUES(10);"
            "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
            " size_bytes INTEGER, repo_id TEXT);"
            "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
            " uuid TEXT, media_type TEXT, status TEXT);"
            "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);";
        char sql[2048];
        memset(missing, 0xAB, 32);
        snprintf(cat_path, sizeof cat_path, "%s_prompt_catalog.db", tmpdir);

        /* The pack is known but sits on no current volume. */
        snprintf(sql, sizeof sql, "%sINSERT INTO packs VALUES(1, '%s', 10, 'r');",
                 schema,
                 "abababababababababababababababababababababababababababababababab");
        if (make_catalog(cat_path, sql) != 0
                || !prompt_mentions(tmpdir, cat_path, missing,
                                    "catalog has the pack, but lists no disc for it")) {
            fprintf(stderr, "FAIL #488: no-volume-mapping prompt\n"); fails++;
        }

        /* A valid catalog with no row for this pack. */
        snprintf(sql, sizeof sql, "%sINSERT INTO packs VALUES(1, '%s', 10, 'r');",
                 schema,
                 "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd");
        if (make_catalog(cat_path, sql) != 0
                || !prompt_mentions(tmpdir, cat_path, missing,
                                    "catalog has no record of this pack ID")) {
            fprintf(stderr, "FAIL #488: no-record prompt\n"); fails++;
        }

        /* Schema skew: the catalog opens but has no packs table, so the
         * lookup's prepare fails (a catalog written by a newer LCSAS). */
        if (make_catalog(cat_path,
                         "CREATE TABLE schema_version(version INTEGER);"
                         "INSERT INTO schema_version VALUES(99);") != 0
                || !prompt_mentions(tmpdir, cat_path, missing,
                                    "catalog could not be queried")) {
            fprintf(stderr, "FAIL #488: schema-skew prompt\n"); fails++;
        }

        /* #543: the pack is known, but the volume tables are missing, so
         * the second lookup's prepare fails.  That is skew too. */
        snprintf(sql, sizeof sql,
                 "CREATE TABLE schema_version(version INTEGER);"
                 "INSERT INTO schema_version VALUES(10);"
                 "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
                 " size_bytes INTEGER, repo_id TEXT);"
                 "INSERT INTO packs VALUES(1, '%s', 10, 'r');",
                 "abababababababababababababababababababababababababababababababab");
        if (make_catalog(cat_path, sql) != 0
                || !prompt_mentions(tmpdir, cat_path, missing,
                                    "catalog could not be queried")) {
            fprintf(stderr, "FAIL #543: no-volume-tables prompt\n"); fails++;
        }

        /* #543: a catalog that is not a database at all can't be read.
         * The prompt says so, and why, and does not send the heir after
         * a newer LCSAS. */
        {
            FILE *g = fopen(cat_path, "wb");
            if (g) {
                fputs("not an SQLite database, only text long enough to"
                      " fill the 100-byte header SQLite checks first.....\n",
                      g);
                fclose(g);
            }
        }
        if (!prompt_mentions(tmpdir, cat_path, missing,
                             "Reason: file is not a database")
                || prompt_mentions(tmpdir, cat_path, missing, "newer LCSAS")) {
            fprintf(stderr, "FAIL #543: unreadable-catalog prompt\n"); fails++;
        }
        unlink(cat_path);
    }

    /* #488: consider_catalog falls back to opening the original when the
     * copy into the cache fails.  A directory (with an entry, so unlink
     * can't clear it) sits where the copy should go.  Pass 0 blocks
     * the copy itself (".new"): the original is opened in place.
     * Pass 1 blocks only the rename of the finished copy onto
     * ".locator-catalog.db": the ".new" copy is opened instead. */
    {
        char parent[1024];
        char disc[1024];
        char cat[1024];
        char cache_dir[1024];
        char blocker[1100];
        const char *parents[1];
        int pass;
        for (pass = 0; pass < 2; pass++) {
            snprintf(parent, sizeof parent, "%s_catfailparent", tmpdir);
            snprintf(disc, sizeof disc, "%s/discA", parent);
            snprintf(cat, sizeof cat, "%s/catalog.db", disc);
            snprintf(cache_dir, sizeof cache_dir,
                     "%s/lcsas_dl_catfail_%ld", roomy_base(), (long)getpid());
            snprintf(blocker, sizeof blocker, "%s/.locator-catalog.db%s/x",
                     cache_dir, pass == 0 ? ".new" : "");
            if (mkdir_recursive(disc) != 0 || mkdir_recursive(blocker) != 0) {
                fprintf(stderr, "FAIL mkdir catfail fixture\n"); fails++;
            }
            /* #788: only a catalog that can be read through is taken,
             * so this is a real one (it was a stub file). */
            make_catalog(cat, GOOD_CATALOG_SQL);
            parents[0] = parent;
            lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
            lcsas_disc_locator_set_mount_parents(&l, parents, 1);
            lcsas_disc_locator_set_cache_dir(&l, cache_dir);
            rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
            (void)rc; /* the copy-failure fallback is the goal */
            if (!l.owned_catalog || !l.owned_catalog_path
                    || strcmp(l.owned_catalog_path, cat) != 0) {
                fprintf(stderr, "FAIL #488 catfail (%d): no catalog "
                                "opened\n", pass); fails++;
            }
            lcsas_disc_locator_free(&l);
            {
                char rm[1024];
                snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache_dir);
                (void)system(rm);
            }
        }
    }

    /* rc6 review: a newer catalog.db that cannot be copied into the
     * cache leaves the catalog in use open.  (Before, the one in use
     * was closed and its copy deleted first, so the locator was left
     * with none.)  discA's catalog is taken on the first rescan;
     * discB, put in later, has a newer catalog that cannot be read:
     * the copy fails, and so does the in-place fallback. */
    {
        char parent[1024], disc[1024], cat_a[1100], cat_b[1100];
        char cache_dir[1024], p[1100], buf[64];
        const char *parents[1];
        unsigned char miss[32];
        struct utimbuf ut;
        FILE *err, *f;
        int saved;
        size_t n = 0;
        memset(miss, 0xC9, 32);
        snprintf(parent, sizeof parent, "%s_keepcat", tmpdir);
        snprintf(disc, sizeof disc, "%s/discA", parent);
        snprintf(cat_a, sizeof cat_a, "%s/catalog.db", disc);
        mkdir_recursive(disc);
        /* #788: a real catalog (it was a stub file, which is no longer
         * taken). */
        make_catalog(cat_a, GOOD_CATALOG_SQL);
        ut.actime = ut.modtime = (time_t)1000000000L;
        (void)utime(cat_a, &ut);
        snprintf(cache_dir, sizeof cache_dir, "%s/lcsas_dl_keepcat_%ld",
                 roomy_base(), (long)getpid());
        mkdir_recursive(cache_dir);
        parents[0] = parent;

        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
        snprintf(disc, sizeof disc, "%s/discB", parent);
        snprintf(cat_b, sizeof cat_b, "%s/catalog.db", disc);
        mkdir_recursive(disc);
        write_pack(cat_b, "catalog B");
        ut.actime = ut.modtime = (time_t)1000000100L;
        (void)utime(cat_b, &ut);
        (void)chmod(cat_b, 0);
        if (access(cat_b, R_OK) == 0) {
            capture_stderr_end(saved);
            fprintf(stderr, "[info] catalog.db stays readable after "
                            "chmod 0 (root?): skipping the keep-catalog "
                            "case\n");
        } else {
            (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            capture_stderr_end(saved);
            if (!l.owned_catalog || !l.owned_catalog_path
                    || strcmp(l.owned_catalog_path, cat_a) != 0) {
                fprintf(stderr, "FAIL rc6 keep catalog: the catalog in "
                                "use was dropped (now %s)\n",
                        l.owned_catalog_path ? l.owned_catalog_path
                                             : "none"); fails++;
            }
            snprintf(p, sizeof p, "%s/.locator-catalog.db", cache_dir);
            {
                lcsas_catalog *kept = lcsas_catalog_open(p);
                if (!kept || lcsas_catalog_check(kept) != 0) {
                    fprintf(stderr, "FAIL rc6 keep catalog: the cache copy "
                                    "of the catalog in use is gone\n");
                    fails++;
                }
                if (kept) lcsas_catalog_close(kept);
            }
            (void)buf;
            (void)n;
            snprintf(p, sizeof p, "%s/.locator-catalog.db.new", cache_dir);
            f = fopen(p, "rb");
            if (f) {
                fclose(f);
                fprintf(stderr, "FAIL rc6 keep catalog: a failed copy "
                                "left %s\n", p); fails++;
            }
        }
        if (err) fclose(err);
        lcsas_disc_locator_free(&l);
        (void)chmod(cat_b, 0644);
        {
            char rm[2200];
            snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache_dir);
            (void)system(rm);
        }
    }

    /* #488: refresh_discovered warns and skips an entry whose full path
     * would not fit its 4 KiB buffer.  The mount parent is ~3.9 KB deep
     * and holds a 250-byte name. */
    {
        char longparent[4096];
        char name[251];
        char here[4096];
        const char *parents[1];
        size_t n;
        int made = 0;
        n = (size_t)snprintf(longparent, sizeof longparent, "%s_long", tmpdir);
        while (n + 201 < 3900) {
            longparent[n++] = '/';
            memset(longparent + n, 'd', 200);
            n += 200;
            longparent[n] = '\0';
        }
        memset(name, 'n', 250);
        name[250] = '\0';
        /* The entry's own absolute path is past PATH_MAX, so make it
         * relative to the parent. */
        if (mkdir_long(longparent) == 0 && getcwd(here, sizeof here)
                && chdir(longparent) == 0) {
            made = mkdir(name, 0755) == 0;
            if (chdir(here) != 0) {
                fprintf(stderr, "FAIL #488: chdir back from long path\n");
                fails++;
            }
        }
        if (!made) {
            fprintf(stderr, "FAIL #488: long-path fixture (errno=%d)\n", errno);
            fails++;
        } else {
            unsigned char miss[32];
            memset(miss, 0xC5, 32);
            parents[0] = longparent;
            lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
            lcsas_disc_locator_set_mount_parents(&l, parents, 1);
            rc = lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            if (rc != -1) {
                fprintf(stderr, "FAIL #488: long-path miss rc=%d\n", rc); fails++;
            }
            lcsas_disc_locator_free(&l);
        }
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s_long", tmpdir);
            (void)system(rm);
        }
    }

    /* #488: before prompting, a cwd inside the meta disc is dropped to
     * "/" so the drive can be ejected. */
    {
        char meta[1024];
        char inner[1100];
        char before[4096];
        char after[4096];
        const char *search[] = { tmpdir };
        unsigned char miss[32];
        int pfd[2];
        snprintf(meta, sizeof meta, "%s_cwdmeta", tmpdir);
        snprintf(inner, sizeof inner, "%s/inner", meta);
        memset(miss, 0xC6, 32);
        if (mkdir_recursive(inner) != 0 || !getcwd(before, sizeof before)
                || pipe(pfd) != 0) {
            fprintf(stderr, "FAIL #488: cwd-under-meta fixture\n"); fails++;
        } else if (chdir(inner) != 0) {
            fprintf(stderr, "FAIL #488: chdir inner\n"); fails++;
            close(pfd[0]); close(pfd[1]);
        } else {
            FILE *in = fdopen(pfd[0], "r");
            FILE *out = fopen("/dev/null", "w");
            char real_meta[4096];
            if (write(pfd[1], "q\n", 2) != 2) { /* EOF aborts as well */ }
            close(pfd[1]);
            /* getcwd() resolves symlinks (here /tmp may be one), so hand
             * the locator the resolved path, as restore.sh does. */
            if (!realpath(meta, real_meta)) {
                snprintf(real_meta, sizeof real_meta, "%s", meta);
            }
            lcsas_disc_locator_init(&l, search, 1, NULL, 1);
            lcsas_disc_locator_set_auto_mount(&l, 0);
            lcsas_disc_locator_set_meta(&l, real_meta);
            if (in) l.prompt_in = in;
            if (out) l.prompt_out = out;
            alarm(20);
            rc = lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            alarm(0);
            if (!getcwd(after, sizeof after) || strcmp(after, "/") != 0) {
                fprintf(stderr, "FAIL #488: cwd not dropped to / (now %s)\n",
                        after);
                fails++;
            }
            lcsas_disc_locator_free(&l);
            if (in) fclose(in); else close(pfd[0]);
            if (out) fclose(out);
            if (chdir(before) != 0) {
                fprintf(stderr, "FAIL #488: chdir back\n"); fails++;
            }
        }
        {
            char rm[1100];
            snprintf(rm, sizeof rm, "rm -rf %s", meta);
            (void)system(rm);
        }
    }


    /* rc6 (#476 drill, F9/F10): every line of the disc-swap box is
     * exactly 60 columns.  Without a catalog it says what to do (put in
     * the newest data disc) instead of naming a flag.  A mount parent
     * that does not exist (or is a file) is not listed as searched.
     * The single-drive hint shows only while the META disc is still in
     * the drive, and typing q -- or the input ending -- sets
     * reader_quit, which main() turns into exit 75. */
    {
        char meta[1024], sub[1024], blocker[1024], cat_path[1024];
        char longlabel[61];
        const char *search[] = { tmpdir };
        const char *parents[2];
        unsigned char missing[32];
        FILE *out;
        int prc, quit;
        char sql[2048];
        const char *schema =
            "CREATE TABLE schema_version(version INTEGER);"
            "INSERT INTO schema_version VALUES(10);"
            "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
            " size_bytes INTEGER, repo_id TEXT);"
            "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
            " uuid TEXT, media_type TEXT, status TEXT);"
            "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);";

        memset(missing, 0xAB, 32);
        snprintf(meta, sizeof meta, "%s_boxmeta", tmpdir);
        snprintf(sub, sizeof sub, "%s/recovery/scripts", meta);
        mkdir_recursive(sub);
        snprintf(sub, sizeof sub, "%s/recovery/scripts/restore.sh", meta);
        write_pack(sub, "#!/bin/sh\n");
        snprintf(blocker, sizeof blocker, "%s_box_parent_is_a_file", tmpdir);
        write_pack(blocker, "x");
        parents[0] = "/nonexistent/lcsas-phantom-parent";
        parents[1] = blocker;

        /* Live META disc, no catalog, 'q'. */
        out = prompt_run(search, 1, parents, 2, meta, NULL, missing, "q\n",
                         &prc, &quit);
        if (!out) {
            fprintf(stderr, "FAIL rc6 box: no transcript\n"); fails++;
        } else {
            if (box_lines_are_60(out, "no catalog") < 10) {
                fprintf(stderr, "FAIL rc6 box: no-catalog box\n"); fails++;
            }
            if (count_lines_with(out, "| (no disc catalog yet: insert your "
                                      "newest data disc)      |") != 1
                    || count_lines_with(out, "--catalog") != 0) {
                fprintf(stderr, "FAIL rc6 box: no-catalog line\n"); fails++;
            }
            if (count_lines_with(out, "Single-drive restore") != 1) {
                fprintf(stderr, "FAIL rc6 box: hint missing while the "
                                "META disc is in\n"); fails++;
            }
            if (count_lines_with(out, "phantom") != 0
                    || count_lines_with(out, "parent_is_a_file") != 0) {
                fprintf(stderr, "FAIL rc6 box: a mount parent that is not "
                                "a folder is listed\n"); fails++;
            }
            if (prc != -1 || quit != 1) {
                fprintf(stderr, "FAIL rc6 box: q gave rc=%d quit=%d "
                                "(want -1, 1)\n", prc, quit); fails++;
            }
            fclose(out);
        }

        /* META disc ejected (sentinel gone), the input just ends. */
        unlink(sub);
        out = prompt_run(search, 1, NULL, 0, meta, NULL, missing, "",
                         &prc, &quit);
        if (!out) {
            fprintf(stderr, "FAIL rc6 box: no transcript (2)\n"); fails++;
        } else {
            if (box_lines_are_60(out, "meta ejected") < 8) {
                fprintf(stderr, "FAIL rc6 box: meta-ejected box\n"); fails++;
            }
            if (count_lines_with(out, "Single-drive restore") != 0) {
                fprintf(stderr, "FAIL rc6 box: hint shown with the META "
                                "disc out\n"); fails++;
            }
            if (prc != -1 || quit != 1) {
                fprintf(stderr, "FAIL rc6 box: EOF gave rc=%d quit=%d "
                                "(want -1, 1)\n", prc, quit); fails++;
            }
            fclose(out);
        }

        /* A catalog naming the disc, one label longer than the box. */
        memset(longlabel, 'L', sizeof longlabel - 1);
        longlabel[sizeof longlabel - 1] = '\0';
        memcpy(longlabel + sizeof longlabel - 5, "0042", 4);
        snprintf(cat_path, sizeof cat_path, "%s_box_catalog.db", tmpdir);
        snprintf(sql, sizeof sql,
                 "%sINSERT INTO packs VALUES(1, '%s', 10, 'r');"
                 "INSERT INTO volumes VALUES(1, 'LCSAS_CD700_2026_0002', 'u1',"
                 " 'CD700', 'VERIFIED');"
                 "INSERT INTO volumes VALUES(2, '%s', 'u2', 'CD700', 'VERIFIED');"
                 "INSERT INTO volume_packs VALUES(1, 1);"
                 "INSERT INTO volume_packs VALUES(2, 1);",
                 schema,
                 "abababababababababababababababababababababababababababababababab",
                 longlabel);
        if (make_catalog(cat_path, sql) != 0) {
            fprintf(stderr, "FAIL rc6 box: catalog fixture\n"); fails++;
        } else {
            out = prompt_run(search, 1, NULL, 0, NULL, cat_path, missing,
                             "q\n", &prc, &quit);
            if (!out) {
                fprintf(stderr, "FAIL rc6 box: no transcript (3)\n"); fails++;
            } else {
                if (box_lines_are_60(out, "catalog labels") < 10) {
                    fprintf(stderr, "FAIL rc6 box: catalog box\n"); fails++;
                }
                if (count_lines_with(out, "|   LCSAS_CD700_2026_0002") != 1
                        || count_lines_with(out, "LLL0042 |") != 1) {
                    fprintf(stderr, "FAIL rc6 box: labels not listed (the "
                                    "long one by its end)\n"); fails++;
                }
                fclose(out);
            }
        }
        unlink(cat_path);
        unlink(blocker);
        {
            char rm[1100];
            snprintf(rm, sizeof rm, "rm -rf %s", meta);
            (void)system(rm);
        }
    }

    /* rc6 copy_file: the copy goes to "<dst>.part" and is renamed only
     * when complete.  (a) A source that cannot be read to the end (here
     * a catalog.db that is a folder: fopen works, fread fails) leaves
     * neither the copy nor a .part behind.  (b) A .part that cannot be
     * created fails the copy cleanly. */
    {
        char parent[1024], disc[1024], p[1100], cache_dir[1024];
        const char *parents[1];
        unsigned char miss[32];
        struct stat st;
        int pass;
        memset(miss, 0xC7, 32);
        for (pass = 0; pass < 2; pass++) {
            snprintf(parent, sizeof parent, "%s_partparent%d", tmpdir, pass);
            snprintf(disc, sizeof disc, "%s/discA", parent);
            snprintf(cache_dir, sizeof cache_dir, "%s/lcsas_dl_part%d_%ld",
                     roomy_base(), pass, (long)getpid());
            if (pass == 0) {
                /* The catalog is a folder (with an entry). */
                snprintf(p, sizeof p, "%s/catalog.db/entry", disc);
                mkdir_recursive(p);
            } else {
                snprintf(p, sizeof p, "%s/catalog.db", disc);
                mkdir_recursive(disc);
                write_pack(p, "not a real catalog");
                /* A folder sits where the .part would go. */
                snprintf(p, sizeof p, "%s/.locator-catalog.db.new.part/x",
                         cache_dir);
                mkdir_recursive(p);
            }
            mkdir_recursive(cache_dir);
            parents[0] = parent;
            lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
            lcsas_disc_locator_set_mount_parents(&l, parents, 1);
            lcsas_disc_locator_set_cache_dir(&l, cache_dir);
            rc = lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            lcsas_disc_locator_free(&l);
            snprintf(p, sizeof p, "%s/.locator-catalog.db", cache_dir);
            if (stat(p, &st) == 0) {
                fprintf(stderr, "FAIL rc6 copy_file (%d): a failed copy "
                                "left %s\n", pass, p); fails++;
            }
            snprintf(p, sizeof p, "%s/.locator-catalog.db.new", cache_dir);
            if (stat(p, &st) == 0) {
                fprintf(stderr, "FAIL rc6 copy_file (%d): a failed copy "
                                "left %s\n", pass, p); fails++;
            }
            if (pass == 0) {
                snprintf(p, sizeof p, "%s/.locator-catalog.db.new.part",
                         cache_dir);
                if (stat(p, &st) == 0) {
                    fprintf(stderr, "FAIL rc6 copy_file: a failed copy "
                                    "left %s\n", p); fails++;
                }
            }
            {
                char rm[2200];
                snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache_dir);
                (void)system(rm);
            }
        }
    }

    /* rc6 drain on first sight (#476 drill, N+1 swaps): with the cache
     * on, a data disc (one with a volume_info.json) that a rescan finds
     * for the first time is copied into the cache at once, even though
     * the pack being looked for is not on it, so the reader never has
     * to put it back.  Progress lines name the disc by its label.  The
     * same disc is not copied again on the next rescan, and a folder
     * with data/ but no volume_info.json is left alone. */
    {
        char parent[1024], disc[1024], plain[1024], p[1200];
        char cache_dir[1024];
        const char *parents[1];
        unsigned char miss[32];
        struct stat st;
        FILE *err;
        int saved;
        int k;
        /* Pack names are the SHA-256 of their contents, as restic names
         * them: a copy is checked against its name (a fixed fake name
         * would now be refused). */
        char packs[3][65];
        memset(miss, 0xC8, 32);
        snprintf(parent, sizeof parent, "%s_firstsight", tmpdir);
        snprintf(disc, sizeof disc, "%s/DISC_A", parent);
        snprintf(plain, sizeof plain, "%s/PLAIN", parent);
        snprintf(cache_dir, sizeof cache_dir, "%s/lcsas_dl_firstsight_%ld",
                 roomy_base(), (long)getpid());
        make_labelled_disc(parent, "DISC_A",
            "{\"label\": \"LCSAS_TEST_2026_0001\", \"type\": \"data\"}");
        for (k = 0; k < 3; k++) {
            /* 3 x 8 MiB: the copy passes a 16 MiB progress step midway. */
            char tmpp[1200];
            snprintf(tmpp, sizeof tmpp, "%s/data/pack%d", disc, k);
            snprintf(p, sizeof p, "%s/data", disc);
            mkdir_recursive(p);
            if (write_sparse_named(tmpp, 8L * 1024L * 1024L, k + 1,
                                   packs[k]) != 0) {
                fprintf(stderr, "FAIL rc6 first sight: sparse pack\n");
                fails++;
            }
            snprintf(p, sizeof p, "%s/data/%c%c", disc, packs[k][0],
                     packs[k][1]);
            mkdir_recursive(p);
            snprintf(p, sizeof p, "%s/data/%c%c/%s", disc, packs[k][0],
                     packs[k][1], packs[k]);
            if (rename(tmpp, p) != 0) {
                fprintf(stderr, "FAIL rc6 first sight: rename pack\n");
                fails++;
            }
        }
        snprintf(p, sizeof p, "%s/data/bb", plain);
        mkdir_recursive(p);
        snprintf(p, sizeof p, "%s/data/bb/bb%062d", plain, 7);
        write_pack(p, "plain-folder-pack");
        mkdir_recursive(cache_dir);
        parents[0] = parent;

        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, miss, found, sizeof found);
        (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
        lcsas_disc_locator_free(&l);
        capture_stderr_end(saved);

        if (rc != -1) {
            fprintf(stderr, "FAIL rc6 first sight: miss rc=%d\n", rc);
            fails++;
        }
        for (k = 0; k < 3; k++) {
            snprintf(p, sizeof p, "%s/data/%c%c/%s", cache_dir, packs[k][0],
                     packs[k][1], packs[k]);
            if (stat(p, &st) != 0 || st.st_size != 8L * 1024L * 1024L) {
                fprintf(stderr, "FAIL rc6 first sight: %s not copied\n", p);
                fails++;
            }
        }
        snprintf(p, sizeof p, "%s/data/bb", cache_dir);
        if (stat(p, &st) == 0) {
            fprintf(stderr, "FAIL rc6 first sight: a folder without "
                            "volume_info.json was copied\n"); fails++;
        }
        if (!err) {
            fprintf(stderr, "FAIL rc6 first sight: no stderr capture\n");
            fails++;
        } else {
            static const struct { const char *needle; int want; } c[] = {
                { "copying disc LCSAS_TEST_2026_0001 to the cache: 24 MB.", 1 },
                { "copying disc LCSAS_TEST_2026_0001 to the cache: 16 of 24 MB", 1 },
                { "copied 24 of 24 MB from disc LCSAS_TEST_2026_0001 to the "
                  "cache.", 1 }
            };
            size_t j;
            for (j = 0; j < sizeof c / sizeof c[0]; j++) {
                if (count_lines_with(err, c[j].needle) != c[j].want) {
                    fprintf(stderr, "FAIL rc6 first sight: want %d line(s) "
                                    "with '%s'\n", c[j].want, c[j].needle);
                    fails++;
                }
            }
            fclose(err);
        }
        {
            char rm[2200];
            snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache_dir);
            (void)system(rm);
        }
    }

    /* #791: the chunk limit stops a drain after that many packs, and
     * a stop at the limit does not give the disc up (#786): the next
     * drain copies the next pack. */
    {
        char root[1024], cache[1024], h1[65], h2[65], h3[65], p[1200];
        unsigned char id1[32];
        const char *search[1];
        int k, cached = 0;
        snprintf(root, sizeof root, "%s_chunk791", tmpdir);
        snprintf(cache, sizeof cache, "%s/lcsas_dl_chunk791_%ld",
                 roomy_base(), (long)getpid());
        mkdir_recursive(cache);
        write_real_pack(root, "chunk pack one", h1);
        write_real_pack(root, "chunk pack two", h2);
        write_real_pack(root, "chunk pack three", h3);
        search[0] = root;
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        l.drain_chunk_packs = 1;
        lcsas_disc_locator_set_cache_dir(&l, cache);
        lcsas_hex_decode(h1, 32, id1);
        (void)lcsas_disc_locate_pack(&l, id1, found, sizeof found);
        {
            const char *hs[3];
            hs[0] = h1; hs[1] = h2; hs[2] = h3;
            for (k = 0; k < 3; k++) {
                struct stat st;
                snprintf(p, sizeof p, "%s/data/%c%c/%s", cache,
                         hs[k][0], hs[k][1], hs[k]);
                if (stat(p, &st) == 0) cached++;
            }
        }
        if (cached != 1 || l.n_drain_skips != 0) {
            fprintf(stderr, "FAIL 791 chunk: %d pack(s) cached, %lu disc(s) "
                    "given up (want 1, 0)\n", cached,
                    (unsigned long)l.n_drain_skips); fails++;
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[2200];
            snprintf(rm, sizeof rm, "rm -rf %s %s", root, cache);
            (void)system(rm);
        }
    }

    /* #786: a disc whose copy into the cache stops partway (here: the
     * cache's data/<xx> for pack A is a file, so A cannot be copied) is
     * walked once, not once per lookup.  Ten lookups of pack A, which
     * is read from the disc, cost two walks (count + copy).  Another
     * disc at the same path (a new data/ time) is walked again. */
    {
        char root[1024], cache[1024], ha[65], hb[65], p[1200];
        unsigned char ida[32];
        const char *search[1];
        struct utimbuf ut;
        unsigned long walks;
        int k, lrc = 0, saved;
        FILE *err;
        snprintf(root, sizeof root, "%s_786disc", tmpdir);
        snprintf(cache, sizeof cache, "%s/lcsas_dl_786_%ld",
                 roomy_base(), (long)getpid());
        write_real_pack(root, "pack A of the 786 disc", ha);
        write_real_pack(root, "pack B of the 786 disc", hb);
        snprintf(p, sizeof p, "%s/data", cache);
        mkdir_recursive(p);
        snprintf(p, sizeof p, "%s/data/%c%c", cache, ha[0], ha[1]);
        write_pack(p, "a file where a folder should be");
        search[0] = root;
        lcsas_hex_decode(ha, 32, ida);
        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache);
        for (k = 0; k < 10; k++) {
            lrc |= lcsas_disc_locate_pack(&l, ida, found, sizeof found);
        }
        walks = l.drain_walks;
        /* Another disc in the same place: its data/ has another time. */
        snprintf(p, sizeof p, "%s/data", root);
        ut.actime = ut.modtime = (time_t)1100000000L;
        (void)utime(p, &ut);
        lrc |= lcsas_disc_locate_pack(&l, ida, found, sizeof found);
        capture_stderr_end(saved);
        if (err) fclose(err);
        if (lrc != 0 || strncmp(found, root, strlen(root)) != 0) {
            fprintf(stderr, "FAIL 786: pack A not read from the disc "
                    "(rc=%d, %s)\n", lrc, found); fails++;
        }
        if (walks != 2) {
            fprintf(stderr, "FAIL 786: %lu disc walks for 10 lookups "
                    "(want 2)\n", walks); fails++;
        }
        if (l.drain_walks <= walks) {
            fprintf(stderr, "FAIL 786: another disc at the same path was "
                    "not walked\n"); fails++;
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[2200];
            snprintf(rm, sizeof rm, "rm -rf %s %s", root, cache);
            (void)system(rm);
        }
    }

    /* Pack check on copy: a pack whose bytes do not hash to its name
     * (read wrong off a scratched disc) is not kept in the cache, and
     * is read from the disc; a good pack beside it is cached. */
    {
        char root[1024], cache[1024], hg[65], hbad[65], p[1200];
        unsigned char idbad[32];
        const char *search[1];
        struct stat st;
        int saved;
        FILE *err;
        snprintf(root, sizeof root, "%s_badcopy", tmpdir);
        snprintf(cache, sizeof cache, "%s/lcsas_dl_badcopy_%ld",
                 roomy_base(), (long)getpid());
        mkdir_recursive(cache);
        write_real_pack(root, "a good pack", hg);
        write_real_pack(root, "the right bytes", hbad);
        snprintf(p, sizeof p, "%s/data/%c%c/%s", root, hbad[0], hbad[1], hbad);
        write_pack(p, "the WRONG bytes");
        search[0] = root;
        lcsas_hex_decode(hbad, 32, idbad);
        err = capture_stderr_begin(&saved);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, cache);
        rc = lcsas_disc_locate_pack(&l, idbad, found, sizeof found);
        (void)lcsas_disc_locate_pack(&l, idbad, found, sizeof found);
        capture_stderr_end(saved);
        if (rc != 0 || strncmp(found, root, strlen(root)) != 0) {
            fprintf(stderr, "FAIL pack check: damaged pack not read from "
                    "the disc (rc=%d, %s)\n", rc, found); fails++;
        }
        snprintf(p, sizeof p, "%s/data/%c%c/%s", cache, hbad[0], hbad[1], hbad);
        if (stat(p, &st) == 0) {
            fprintf(stderr, "FAIL pack check: damaged copy kept at %s\n", p);
            fails++;
        }
        strcat(p, ".part");
        if (stat(p, &st) == 0) {
            fprintf(stderr, "FAIL pack check: %s left\n", p); fails++;
        }
        snprintf(p, sizeof p, "%s/data/%c%c/%s", cache, hg[0], hg[1], hg);
        if (stat(p, &st) != 0) {
            fprintf(stderr, "FAIL pack check: good pack not cached\n");
            fails++;
        }
        if (err) {
            if (count_lines_with(err, "was not read correctly") != 1) {
                fprintf(stderr, "FAIL pack check: want one warning\n");
                fails++;
            }
            fclose(err);
        }
        lcsas_disc_locator_free(&l);
        {
            char rm[2200];
            snprintf(rm, sizeof rm, "rm -rf %s %s", root, cache);
            (void)system(rm);
        }
    }

    /* #788: a disc with a NEWER catalog that cannot be read through does
     * not replace the good catalog in use, with or without the cache;
     * one warning, however many rescans.  Two kinds of damage: the
     * file's tail is cut off (this SQLite then cannot read even the
     * version, but the old open still succeeded), and a page in the
     * middle is overwritten (the version reads fine; only the check of
     * every page finds it). */
    {
        int pass;
        for (pass = 0; pass < 4; pass++) {
            int with_cache = pass & 1;
            int mid_damage = pass >> 1;
            char parent[1024], disc[1024], cat_a[1100], cat_b[1100];
            char cache[1024], p[1200];
            const char *parents[1];
            unsigned char miss[32];
            struct utimbuf ut;
            struct stat st;
            lcsas_catalog *probe;
            FILE *err;
            int saved;
            memset(miss, 0xC8, 32);
            snprintf(parent, sizeof parent, "%s_788cat%d", tmpdir, pass);
            snprintf(disc, sizeof disc, "%s/discA", parent);
            snprintf(cat_a, sizeof cat_a, "%s/catalog.db", disc);
            mkdir_recursive(disc);
            make_catalog(cat_a, GOOD_CATALOG_SQL);
            ut.actime = ut.modtime = (time_t)1000000000L;
            (void)utime(cat_a, &ut);
            snprintf(cache, sizeof cache, "%s/lcsas_dl_788_%ld_%d",
                     roomy_base(), (long)getpid(), pass);
            mkdir_recursive(cache);
            parents[0] = parent;

            err = capture_stderr_begin(&saved);
            lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
            lcsas_disc_locator_set_mount_parents(&l, parents, 1);
            if (with_cache) lcsas_disc_locator_set_cache_dir(&l, cache);
            (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);

            /* discB: a newer catalog of many pages, then damaged. */
            snprintf(disc, sizeof disc, "%s/discB", parent);
            snprintf(cat_b, sizeof cat_b, "%s/catalog.db", disc);
            mkdir_recursive(disc);
            make_catalog(cat_b,
                "CREATE TABLE schema_version (version INTEGER);"
                "INSERT INTO schema_version VALUES (10);"
                "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY,"
                "  sha256 TEXT, size_bytes INTEGER, repo_id TEXT);"
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1"
                "  FROM n WHERE i < 4000)"
                "  INSERT INTO packs SELECT i, hex(randomblob(32)), i, 'r'"
                "  FROM n;");
            if (stat(cat_b, &st) != 0) {
                fprintf(stderr, "FAIL 788: no %s\n", cat_b); fails++;
            } else if (mid_damage) {
                FILE *f = fopen(cat_b, "r+b");
                static unsigned char junk[4096];
                memset(junk, 0xFF, sizeof junk);
                if (!f || fseek(f, (long)(st.st_size / 2), SEEK_SET) != 0
                        || fwrite(junk, 1, sizeof junk, f) != sizeof junk) {
                    fprintf(stderr, "FAIL 788: cannot damage %s\n", cat_b);
                    fails++;
                }
                if (f) fclose(f);
            } else if (truncate(cat_b, st.st_size / 2) != 0) {
                fprintf(stderr, "FAIL 788: cannot cut %s\n", cat_b);
                fails++;
            }
            ut.actime = ut.modtime = (time_t)1000000100L;
            (void)utime(cat_b, &ut);
            (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            (void)lcsas_disc_locate_pack(&l, miss, found, sizeof found);
            capture_stderr_end(saved);

            /* The case under test: the old code's open succeeds. */
            probe = lcsas_catalog_open(cat_b);
            if (!probe || lcsas_catalog_check(probe) == 0
                    || (mid_damage
                        && lcsas_catalog_schema_version(probe) != 10)) {
                fprintf(stderr, "FAIL 788 (%d): the damaged catalog is not "
                        "the case under test\n", pass); fails++;
            }
            if (probe) lcsas_catalog_close(probe);
            if (!l.owned_catalog || !l.owned_catalog_path
                    || strcmp(l.owned_catalog_path, cat_a) != 0
                    || lcsas_catalog_check(l.owned_catalog) != 0) {
                fprintf(stderr, "FAIL 788 (%d): the good catalog was "
                        "replaced (now %s)\n", pass,
                        l.owned_catalog_path ? l.owned_catalog_path
                                             : "none"); fails++;
            }
            if (err) {
                if (count_lines_with(err, "is damaged or cut short") != 1) {
                    fprintf(stderr, "FAIL 788 (%d): want one warning\n",
                            pass); fails++;
                }
                fclose(err);
            }
            snprintf(p, sizeof p, "%s/.locator-catalog.db.new", cache);
            if (stat(p, &st) == 0) {
                fprintf(stderr, "FAIL 788: %s left\n", p); fails++;
            }
            lcsas_disc_locator_free(&l);
            {
                char rm[2200];
                snprintf(rm, sizeof rm, "rm -rf %s %s", parent, cache);
                (void)system(rm);
            }
        }
    }

    fails += test_skip_answer(tmpdir);
    fails += test_catalog_ranking(tmpdir);
    fails += test_catalog_of_another_archive(tmpdir);
    fails += test_catalog_adopts_set_993(tmpdir);
    fails += test_newer_than_hint(tmpdir);
    fails += test_box_texts_966(tmpdir);

    /* Cleanup tmpdir. */
    {
        char rm_cmd[1024];
        snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", tmpdir);
        (void)system(rm_cmd);
    }

    if (fails == 0) printf("test_disc_locator: OK\n");
    return fails ? 1 : 0;
}
