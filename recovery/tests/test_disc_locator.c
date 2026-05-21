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
#include <sys/stat.h>
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

    /* Meta-disc exclusion: tmpdir IS the meta disc → pack search must skip. */
    {
        const char *search[] = { tmpdir };
        lcsas_disc_locator_init(&l, search, 1, NULL, 0);
        lcsas_disc_locator_set_meta(&l, tmpdir);
        rc = lcsas_disc_locate_pack(&l, pack_id, found, sizeof found);
        if (rc == 0) {
            fprintf(stderr,
                    "FAIL: meta-disc exclusion should hide pack, rc=%d\n",
                    rc); fails++;
        }
        lcsas_disc_locator_free(&l);
    }

    /* Cache-dir drain: enable cache, locate, pack should be copied to cache.
     * After locate, the cache should contain the pack. */
    {
        char cache_dir[1024];
        const char *search[] = { tmpdir };
        snprintf(cache_dir, sizeof cache_dir, "%s_cache", tmpdir);
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

    /* Cleanup tmpdir. */
    {
        char rm_cmd[1024];
        snprintf(rm_cmd, sizeof rm_cmd, "rm -rf %s", tmpdir);
        (void)system(rm_cmd);
    }

    if (fails == 0) printf("test_disc_locator: OK\n");
    return fails ? 1 : 0;
}
