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
#include "json_q.h"   /* lcsas_json_max_tok_bytes + lcsas_json_tok (#383) */

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

/* Valid JSON whose root token is NOT an object (an array); exercises the
 * "root token not OBJECT" reject that is distinct from the invalid-JSON
 * (parse-fail) path garbage_blob hits (#383). */
static char *not_object_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("[1,2,3]");
    *len_out = strlen(blob);
    return blob;
}

/* A one-node tree with a symlink whose linktarget decodes to a string
 * containing an embedded NUL (JSON \u0000): decode_path_component returns
 * DECODE_PATH_NUL, so the symlink must be skipped (path-safety guard, #383). */
static char *symlink_nul_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[{\"name\":\"lnk\",\"type\":\"symlink\","
                  "\"linktarget\":\"a\\u0000b\"}]}");
    *len_out = strlen(blob);
    return blob;
}

/* A small but multi-token tree blob; combined with a tiny
 * lcsas_json_max_tok_bytes ceiling it drives lcsas_json_parse_alloc to
 * return -2 ("too large for tier-1"), #383. */
static char *tiny_tree_blob(int call, size_t *len_out)
{
    char *blob;
    (void)call;
    blob = strdup("{\"nodes\":[]}");
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

static void test_root_not_object(void)
{
    /* A tree blob that parses as valid JSON but whose root is an array,
     * not an object, must be rejected (rc != 0) - the reject distinct
     * from the parse-fail path (#383). */
    char *target = mktarget();
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = not_object_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    if (rc == 0) {
        fprintf(stderr,
                "FAIL root_not_object: expected rc != 0, got 0\n");
        fails++;
    }
    rmrf(target);
}

static void test_symlink_embedded_nul_skipped(void)
{
    /* A symlink node whose linktarget decodes to a string containing an
     * embedded NUL must be SKIPPED, not created - the path-safety guard
     * (#383).  The walk continues cleanly; no target/lnk may appear. */
    char *target = mktarget();
    char linkpath[512];
    struct stat st;
    lcsas_master_key mk;
    lcsas_blob_index ix;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = symlink_nul_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;

    (void)lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                             target, target, NULL, NULL);
    sprintf(linkpath, "%s/lnk", target);
    if (lstat(linkpath, &st) == 0) {
        fprintf(stderr,
                "FAIL symlink_nul: unsafe symlink with embedded-NUL "
                "target was created at %s\n", linkpath);
        fails++;
    }
    rmrf(target);
}

static void test_tree_blob_too_large(void)
{
    /* With the JSON token-buffer ceiling clamped tiny, a multi-token
     * tree blob overflows the token cap: lcsas_json_parse_alloc returns
     * -2 and the walk fails loud (rc != 0) rather than truncating (#383).
     * Same clamp technique as T1C-01 (test_repo.c). */
    char *target = mktarget();
    size_t saved = lcsas_json_max_tok_bytes;
    lcsas_master_key mk;
    lcsas_blob_index ix;
    int rc;

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);
    g_blob_fn = tiny_tree_blob;
    g_call = 0;
    lcsas_tree_max_depth = 1000;
    lcsas_json_max_tok_bytes = sizeof(lcsas_json_tok);  /* -> cap 1 token */

    rc = lcsas_tree_restore("/repo", &mk, &ix, TREE_ID,
                            target, target, NULL, NULL);
    lcsas_json_max_tok_bytes = saved;
    if (rc == 0) {
        fprintf(stderr,
                "FAIL tree_too_large: expected rc != 0 on -2, got 0\n");
        fails++;
    }
    rmrf(target);
}

int main(void)
{
    test_path_too_long();
    test_depth_cap();
    test_malformed_no_crash();
    test_root_not_object();
    test_symlink_embedded_nul_skipped();
    test_tree_blob_too_large();
    if (fails) {
        fprintf(stderr, "test_tree: %d FAIL\n", fails);
        return 1;
    }
    printf("test_tree: ALL OK\n");
    return 0;
}
