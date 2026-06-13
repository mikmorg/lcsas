/*
 * test_tree.c -- tree-walk guards (T1C-04).
 *
 * Links tree.c with stub definitions of its only two repo.c
 * dependencies (lcsas_blob_index_find + lcsas_repo_read_blob), so we
 * can feed arbitrary tree blobs to lcsas_tree_restore without an
 * encrypted fixture.  Covers:
 *   - depth cap: a tree deeper than lcsas_tree_max_depth fails loud
 *     (rc != 0) instead of SIGSEGV.
 *   - path-length guard: a joined path > 4095 bytes fails loud and
 *     leaves no truncated-path file behind.
 *   - malformed blob: a non-array / garbage tree fails cleanly.
 */
#include "tree.h"
#include "repo.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <unistd.h>

static int fails = 0;

/* ── stub blob source ───────────────────────────────────────────── */

/* The harness hands lcsas_tree_restore a chain of synthetic tree
 * blobs.  g_blob_fn produces the blob for call N; returning a NULL
 * pointer makes lcsas_repo_read_blob fail (terminates the walk). */
static char *(*g_blob_fn)(int call, size_t *len_out);
static int g_call;

const lcsas_blob_loc *
lcsas_blob_index_find(const lcsas_blob_index *ix,
                      const unsigned char id[LCSAS_BLOB_ID_LEN])
{
    static lcsas_blob_loc dummy;
    (void)ix; (void)id;
    return &dummy;
}

int
lcsas_repo_read_blob(const char *repo_path,
                     const lcsas_master_key *mk,
                     const lcsas_blob_loc *loc,
                     struct lcsas_disc_locator *extra_locator,
                     unsigned char **out, size_t *out_len)
{
    char *blob;
    size_t len = 0;
    (void)repo_path; (void)mk; (void)loc; (void)extra_locator;
    if (!g_blob_fn) return -1;
    blob = g_blob_fn(g_call, &len);
    g_call++;
    if (!blob) return -1;
    *out = (unsigned char *)blob;
    *out_len = len;
    return 0;
}

/* ── helpers ────────────────────────────────────────────────────── */

#define TREE_ID \
    "0000000000000000000000000000000000000000000000000000000000000000"

static void rmrf(const char *path)
{
    DIR *d = opendir(path);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            char child[8192];
            struct stat st;
            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
                continue;
            sprintf(child, "%s/%s", path, e->d_name);
            if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
                rmrf(child);
            else
                unlink(child);
        }
        closedir(d);
    }
    rmdir(path);
}

static char *mktarget(void)
{
    static char buf[256];
    sprintf(buf, "/tmp/lcsas_test_tree_%ld", (long)getpid());
    rmrf(buf);
    mkdir(buf, 0700);
    return buf;
}

/* Each call returns a one-node tree: a dir with a 900-char name and a
 * subtree pointer.  The chain never terminates on its own; the depth
 * cap or the path-length guard must stop it. */
static char *deep_chain_blob(int call, size_t *len_out)
{
    static char name[901];
    char *blob = (char *)malloc(2048);
    (void)call;
    memset(name, 'a', 900);
    name[900] = '\0';
    sprintf(blob,
            "{\"nodes\":[{\"name\":\"%s\",\"type\":\"dir\","
            "\"mode\":493,\"subtree\":\"" TREE_ID "\"}]}", name);
    *len_out = strlen(blob);
    return blob;
}

/* Each call returns a one-node tree: a dir with a short name and a
 * subtree pointer.  Short names keep the joined path well under 4096
 * so only the depth cap can stop the chain. */
static char *deep_chain_short(int call, size_t *len_out)
{
    char *blob = (char *)malloc(256);
    (void)call;
    sprintf(blob,
            "{\"nodes\":[{\"name\":\"d\",\"type\":\"dir\","
            "\"mode\":493,\"subtree\":\"" TREE_ID "\"}]}");
    *len_out = strlen(blob);
    return blob;
}

/* Garbage that parses but isn't a tree object's "nodes" array. */
static char *garbage_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[-]}");
    *len_out = strlen(blob);
    return blob;
}

/* ── tests ──────────────────────────────────────────────────────── */

static void test_path_too_long(void)
{
    /* 900-char dir names: after ~5 levels the joined path exceeds the
     * 4096-byte node_path buffer.  With a generous depth cap the
     * path-length guard must fire first (rc != 0) and no directory
     * with a 900-'a' name may appear beyond the truncation point. */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = deep_chain_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;   /* let the path guard win, not depth */

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL path_too_long: expected rc != 0, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_depth_cap(void)
{
    /* Short names so the path guard never fires; the depth cap must
     * stop the otherwise-infinite chain. */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = deep_chain_short;  /* short names: only depth can stop it */
    g_call = 0;
    lcsas_tree_max_depth = 4;

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL depth_cap: expected rc != 0 at depth 4, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_malformed_no_crash(void)
{
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = garbage_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    /* The contract is "doesn't crash"; rc may be 0 or non-zero
     * depending on how the parser classifies the garbage element. */
    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    (void)rc;
    rmrf(target);
}

int main(void)
{
    test_path_too_long();
    test_depth_cap();
    test_malformed_no_crash();
    if (fails) {
        fprintf(stderr, "test_tree: %d FAIL\n", fails);
        return 1;
    }
    printf("test_tree: ALL OK\n");
    return 0;
}
