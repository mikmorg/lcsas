/*
 * fuzz_tree_restore.c -- LibFuzzer harness for lcsas_tree_restore (T1C-04).
 *
 * tree.c's recursive node-walk is the most state-rich parser in the
 * tier-1 binary: it consumes attacker-shaped JSON (after decryption)
 * and materialises files/dirs/symlinks.  This harness drives the whole
 * walker over crafted tree blobs without needing a real encrypted repo.
 *
 * Link seam (no source refactor): tree.c's only dependencies on repo.c
 * are lcsas_blob_index_find and lcsas_repo_read_blob.  We compile
 * tree.c + path.c + json_q.c + hex.c + lcsas_io.c + b64.c and provide
 * stub definitions of those two functions here:
 *
 *   - lcsas_blob_index_find: returns a static dummy lcsas_blob_loc so
 *     every hex blob id "resolves".
 *   - lcsas_repo_read_blob: on the FIRST call (the root tree blob)
 *     returns a copy of the fuzz input; on every later call returns a
 *     fixed 4-byte payload.  A per-iteration call budget terminates
 *     self-referencing `subtree` ids so a cycle can't loop forever.
 *
 * Each iteration restores into a fresh per-run dir under $TMPDIR and
 * removes it recursively afterwards.
 *
 * Compile / run:
 *   make -C recovery fuzz-tree-smoke   # 60 seconds
 *   make -C recovery fuzz-tree         # 30 CPU-minutes
 */
#include "tree.h"
#include "repo.h"
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>

/* Per-iteration state for the read_blob stub. */
static const uint8_t *g_input;
static size_t g_input_size;
static int g_call_count;
#define READ_BLOB_BUDGET 64

/* ── repo.c stubs ───────────────────────────────────────────────── */

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
    unsigned char *buf;
    (void)repo_path; (void)mk; (void)loc; (void)extra_locator;

    if (g_call_count >= READ_BLOB_BUDGET) return -1;

    if (g_call_count == 0) {
        /* Root tree blob: hand back a copy of the fuzz input. */
        size_t n = g_input_size;
        buf = (unsigned char *)malloc(n ? n : 1);
        if (!buf) return -1;
        if (n) memcpy(buf, g_input, n);
        *out = buf;
        *out_len = n;
    } else {
        /* Any later blob (subtree / file content): a fixed payload.
         * Kept tiny so a `subtree`-cycle terminates cheaply against the
         * call budget rather than re-parsing the whole input. */
        buf = (unsigned char *)malloc(4);
        if (!buf) return -1;
        memcpy(buf, "{}\0", 4);
        *out = buf;
        *out_len = 2;
    }
    g_call_count++;
    return 0;
}

/* ── recursive rmdir of the per-run target ──────────────────────── */

static void
rmrf(const char *path)
{
    DIR *d = opendir(path);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            char child[4096];
            struct stat st;
            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
                continue;
            if ((size_t)snprintf(child, sizeof child, "%s/%s",
                                 path, e->d_name) >= sizeof child)
                continue;
            if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
                rmrf(child);
            else
                unlink(child);
        }
        closedir(d);
    }
    rmdir(path);
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    lcsas_master_key mk;
    lcsas_blob_index ix;
    char target[4096];
    const char *tmp = getenv("TMPDIR");
    /* A valid 64-hex tree id so lcsas_hex_decode succeeds; the stub
     * index resolves it regardless of value. */
    static const char *tree_id =
        "0000000000000000000000000000000000000000000000000000000000000000";

    if (!tmp || !*tmp) tmp = "/tmp";

    memset(&mk, 0, sizeof mk);
    memset(&ix, 0, sizeof ix);

    g_input = data;
    g_input_size = size;
    g_call_count = 0;

    if ((size_t)snprintf(target, sizeof target,
                         "%s/lcsas_fuzz_tree_%d_%ld",
                         tmp, (int)getpid(), (long)random()) >= sizeof target)
        return 0;
    if (mkdir(target, 0700) != 0) return 0;

    /* Cap depth low so a (broken) deep tree exercises the T1C-04
     * depth-cap path quickly within the call budget. */
    lcsas_tree_max_depth = 32;

    (void)lcsas_tree_restore("/nonexistent-repo", &mk, &ix, tree_id,
                             target, target, NULL, NULL);

    rmrf(target);
    return 0;
}
