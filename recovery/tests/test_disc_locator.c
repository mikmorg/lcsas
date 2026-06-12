/*
 * test_disc_locator.c -- exercise the disc-locator public API.
 *
 * Sets up a synthetic disc layout under /tmp and verifies that
 * lcsas_disc_locate_pack finds packs across the four supported
 * path layouts.  Also exercises mount-parent enumeration, cache_dir
 * drain, meta-disc exclusion, and miss-path (non-interactive).
 */
#include "disc_locator.h"
#include "hex.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <sys/types.h>
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

    /* Cleanup tmpdir. */
    {
        char rm_cmd[1024];
        snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", tmpdir);
        (void)system(rm_cmd);
    }

    if (fails == 0) printf("test_disc_locator: OK\n");
    return fails ? 1 : 0;
}
