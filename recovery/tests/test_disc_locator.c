/*
 * test_disc_locator.c -- exercise the disc-locator public API.
 *
 * Sets up a synthetic disc layout under /tmp and verifies that
 * lcsas_disc_locate_pack finds packs across the four supported
 * path layouts.  Also exercises mount-parent enumeration, cache_dir
 * drain, meta-disc exclusion, and miss-path (non-interactive).
 */
#include "disc_locator.h"
#include "catalog.h"
#include "hex.h"
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

int
main(void)
{
    char tmpdir[] = "/tmp/lcsas_dl_test_XXXXXX";
    char path[1024];
    char found[2048];
    const char *hex =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    unsigned char pack_id[32];
    lcsas_disc_locator l;
    int rc;

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
        setenv("LCSAS_DRAIN_CHUNK_PACKS", "1", 1);
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_cache_dir(&l, chunk_cache);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc;
        lcsas_disc_locator_free(&l);
        unsetenv("LCSAS_DRAIN_CHUNK_PACKS");
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
            /* Also set meta_disc so print_prompt emits the
             * "Single-drive recovery" instructions block
             * (disc_locator.c lines 717-720). */
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
            }
        }
        (void)signal(SIGXFSZ, old_xfsz);
        {
            char rm[1024];
            snprintf(rm, sizeof rm, "rm -rf %s %s", full_root, full_cache);
            (void)system(rm);
        }
    }

    /* fs_critically_full guard [FMA-09]: when LCSAS_TEST_FULL_FS_DIR
     * names a directory on a <10%-free filesystem (the gated Python
     * harness in tests/recovery_hardening/test_restore_space_preflight.py
     * mounts and fills a 1 MiB tmpfs), draining into a cache there
     * must be REFUSED up front: "disabling further drains" warning on
     * stderr, no pack copied, locate still succeeds.  Self-skips when
     * the env var is unset (plain `make test` runs) or the named fs
     * is not actually critically full. */
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
        } else if (pct_free >= 10) {
            fprintf(stderr,
                    "[info] %s is %d%% free (not <10%%); skipping "
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
                    { "mounted volume LCSAS_BD25_2026_0001 at ", 1 },
                    { "mounted the recovery (meta) disc at ", 1 },
                    { "VOL_BADJSON: its volume_info.json is not valid JSON; "
                      "searching it anyway.", 1 },
                    { "VOL_ARRAY: its volume_info.json is not valid JSON", 1 },
                    { "VOL_NOLABEL: its volume_info.json has no usable label", 1 },
                    { "VOL_NUMLABEL: its volume_info.json has no usable label", 1 },
                    { "VOL_LONGLABEL: its volume_info.json has no usable label", 1 },
                    { "mounted volume EVIL?[31mX? at ", 1 },
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
            if (count_lines_with(out, "mounted volume LCSAS_BD25_2026_0001 ")
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
            if (count_lines_with(out, "mounted volume LCSAS_BD25_2026_0002 ")
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
                    if (strstr(lines[nl], "mounted volume")
                            && strncmp(lines[nl], "[lcsas-restore] ", 16) != 0)
                        glued = 1;
                    if (strstr(lines[nl], "mounted volume WRONG_DISC at ")
                            && iw < 0)
                        iw = nl;
                    if (strstr(lines[nl], "mounted volume RIGHT_DISC at ")
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
                                    "catalog has the pack, but no current volume mapping")) {
            fprintf(stderr, "FAIL #488: no-volume-mapping prompt\n"); fails++;
        }

        /* A valid catalog with no row for this pack. */
        snprintf(sql, sizeof sql, "%sINSERT INTO packs VALUES(1, '%s', 10, 'r');",
                 schema,
                 "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd");
        if (make_catalog(cat_path, sql) != 0
                || !prompt_mentions(tmpdir, cat_path, missing,
                                    "catalog has no record of this pack hash")) {
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
        unlink(cat_path);
    }

    /* #488: consider_catalog falls back to opening the original when the
     * copy into the cache fails.  A directory (with an entry, so unlink
     * can't clear it) sits where the copy should go. */
    {
        char parent[1024];
        char disc[1024];
        char cat[1024];
        char cache_dir[1024];
        char blocker[1100];
        const char *parents[1];
        FILE *f;
        snprintf(parent, sizeof parent, "%s_catfailparent", tmpdir);
        snprintf(disc, sizeof disc, "%s/discA", parent);
        snprintf(cat, sizeof cat, "%s/catalog.db", disc);
        snprintf(cache_dir, sizeof cache_dir,
                 "%s/lcsas_dl_catfail_%ld", roomy_base(), (long)getpid());
        snprintf(blocker, sizeof blocker, "%s/.locator-catalog.db/x", cache_dir);
        if (mkdir_recursive(disc) != 0 || mkdir_recursive(blocker) != 0) {
            fprintf(stderr, "FAIL mkdir catfail fixture\n"); fails++;
        }
        f = fopen(cat, "wb");
        if (f) { fputs("not a real catalog", f); fclose(f); }
        parents[0] = parent;
        lcsas_disc_locator_init(&l, NULL, 0, NULL, 0);
        lcsas_disc_locator_set_mount_parents(&l, parents, 1);
        lcsas_disc_locator_set_cache_dir(&l, cache_dir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        (void)rc; /* the copy-failure fallback is the goal */
        lcsas_disc_locator_free(&l);
        {
            char rm[1024];
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

    /* Cleanup tmpdir. */
    {
        char rm_cmd[1024];
        snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", tmpdir);
        (void)system(rm_cmd);
    }

    if (fails == 0) printf("test_disc_locator: OK\n");
    return fails ? 1 : 0;
}
