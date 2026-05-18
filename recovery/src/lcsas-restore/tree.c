/*
 * tree.c -- recursive tree-blob walker.
 *
 * Mirrors src/lcsas/restore/restic_fallback.py:_restore_tree.
 *
 * Memory model: a tree blob is loaded, parsed, and the loop walks
 * each child in source order.  Subtree recursion is iterative-by-
 * recursion (C stack); for very deep trees this could overflow, but
 * restic trees are usually shallow.  For pathological depths use
 * `ulimit -s unlimited` before invoking.
 */
#include "tree.h"
#include "path.h"
#include "json_q.h"
#include "lcsas_io.h"
#include "hex.h"
#include "posix_compat.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LCSAS_PROGRESS_DEFAULT_BLOBS_PER_TICK 16ULL
#define LCSAS_PROGRESS_DEFAULT_BYTES_PER_TICK (1024ULL * 1024ULL)

void
lcsas_progress_init(lcsas_progress *p, unsigned long long total_hint)
{
    if (!p) return;
    p->enabled = 1;
    p->total_blob_hint = total_hint;
    p->blobs_done = 0;
    p->bytes_done = 0;
    p->last_tick_blobs = 0;
    p->last_tick_bytes = 0;
    p->blobs_per_tick = LCSAS_PROGRESS_DEFAULT_BLOBS_PER_TICK;
    p->bytes_per_tick = LCSAS_PROGRESS_DEFAULT_BYTES_PER_TICK;
}

static void
emit_progress_line(const lcsas_progress *p)
{
    /* Render bytes as integer MB to keep the format `\d+/\d+` clean
     * for downstream regex matching (no decimal point in the number). */
    unsigned long long mb = p->bytes_done / (1024ULL * 1024ULL);
    fprintf(stderr,
            "[lcsas-restore] progress: %llu/%llu blobs, %llu MB\n",
            p->blobs_done, p->total_blob_hint, mb);
}

void
lcsas_progress_tick(lcsas_progress *p, unsigned long long blob_len)
{
    unsigned long long d_blobs;
    unsigned long long d_bytes;

    if (!p || !p->enabled) return;
    p->blobs_done++;
    p->bytes_done += blob_len;

    d_blobs = p->blobs_done - p->last_tick_blobs;
    d_bytes = p->bytes_done - p->last_tick_bytes;
    if (d_blobs >= p->blobs_per_tick || d_bytes >= p->bytes_per_tick) {
        emit_progress_line(p);
        p->last_tick_blobs = p->blobs_done;
        p->last_tick_bytes = p->bytes_done;
    }
}

void
lcsas_progress_finish(const lcsas_progress *p)
{
    if (!p || !p->enabled) return;
    /* Always emit a final line so the operator sees the closing count
     * even if the last tick already fired exactly at completion. */
    emit_progress_line(p);
}

static int
restore_file_node(const char *repo_path,
                  const lcsas_master_key *mk,
                  const lcsas_blob_index *ix,
                  const char *src,
                  const lcsas_json_tok *toks,
                  long node_idx,
                  const char *target_path,
                  struct lcsas_disc_locator *locator,
                  lcsas_progress *progress)
{
    long content_idx = lcsas_json_obj_get(src, toks, node_idx, "content");
    int fd;
    long t;
    long blob_count;
    long found = 0;
    int rc = 0;

    fd = lcsas_create_file(target_path);
    if (fd < 0) return -1;

    if (content_idx < 0 || toks[content_idx].type != LCSAS_JSON_ARRAY) {
        /* Empty content -> empty file. */
        close(fd);
        return 0;
    }
    blob_count = toks[content_idx].size;

    for (t = content_idx + 1; found < blob_count; t++) {
        if (toks[t].parent == content_idx
                && toks[t].type == LCSAS_JSON_STRING) {
            unsigned char id[32];
            const lcsas_blob_loc *loc;
            unsigned char *blob = NULL;
            size_t blob_len = 0;

            found++;

            if (toks[t].size != 64) { rc = -1; break; }
            if (lcsas_hex_decode(src + toks[t].start, 32, id) != 0) {
                rc = -1; break;
            }
            loc = lcsas_blob_index_find(ix, id);
            if (!loc) {
                fprintf(stderr, "blob not in index: %.64s\n",
                        src + toks[t].start);
                rc = -1; break;
            }
            if (lcsas_repo_read_blob(repo_path, mk, loc, locator,
                                     &blob, &blob_len) != 0) {
                rc = -1; break;
            }
            if (lcsas_write_exact(fd, blob, blob_len) != 0) {
                free(blob); rc = -1; break;
            }
            lcsas_progress_tick(progress, (unsigned long long)blob_len);
            free(blob);
        }
        if (toks[t].start >= toks[content_idx].end) break;
    }

    close(fd);
    return rc;
}

int
lcsas_tree_restore(const char *repo_path,
                   const lcsas_master_key *mk,
                   const lcsas_blob_index *ix,
                   const char *tree_id_hex,
                   const char *target_dir,
                   const char *target_root,
                   struct lcsas_disc_locator *locator,
                   lcsas_progress *progress)
{
    unsigned char tree_id[32];
    const lcsas_blob_loc *loc;
    unsigned char *blob = NULL;
    size_t blob_len = 0;
    lcsas_json_tok *toks = NULL;
    long ntoks;
    long nodes_arr;
    long node_count, found = 0;
    long t;
    int rc = -1;

    if (lcsas_hex_decode(tree_id_hex, 32, tree_id) != 0) return -1;
    loc = lcsas_blob_index_find(ix, tree_id);
    if (!loc) {
        fprintf(stderr, "tree blob not found: %s\n", tree_id_hex);
        return -1;
    }
    if (lcsas_repo_read_blob(repo_path, mk, loc, locator,
                             &blob, &blob_len) != 0) {
        return -1;
    }

    /* Trees can be large; allocate a generous token buffer. */
    toks = (lcsas_json_tok *)malloc(sizeof(lcsas_json_tok) * 65536);
    if (!toks) { free(blob); return -1; }

    ntoks = lcsas_json_parse((const char *)blob, blob_len, toks, 65536);
    if (ntoks <= 0 || toks[0].type != LCSAS_JSON_OBJECT) goto out;

    nodes_arr = lcsas_json_obj_get((char *)blob, toks, 0, "nodes");
    if (nodes_arr < 0 || toks[nodes_arr].type != LCSAS_JSON_ARRAY) {
        rc = 0; goto out;
    }
    node_count = toks[nodes_arr].size;

    /* mkdir target_dir if needed. */
    lcsas_mkdir_p(target_dir);

    for (t = nodes_arr + 1; found < node_count; t++) {
        if (!(toks[t].parent == nodes_arr
                  && toks[t].type == LCSAS_JSON_OBJECT)) {
            if (toks[t].start >= toks[nodes_arr].end) break;
            continue;
        }
        found++;

        {
            long name_i = lcsas_json_obj_get((char *)blob, toks, t, "name");
            long type_i = lcsas_json_obj_get((char *)blob, toks, t, "type");
            long subtree_i = lcsas_json_obj_get((char *)blob, toks, t, "subtree");
            long lt_i = lcsas_json_obj_get((char *)blob, toks, t, "linktarget");
            char name_buf[1024];
            char type_buf[32];
            char node_path[4096];

            if (name_i < 0 || type_i < 0) continue;
            if (lcsas_json_decode_string((char *)blob, &toks[name_i], name_buf) < 0)
                continue;
            if (lcsas_json_decode_string((char *)blob, &toks[type_i], type_buf) < 0)
                continue;

            /* Path traversal safety: name must be a plain basename. */
            if (lcsas_path_safe_name(name_buf) != 0) {
                fprintf(stderr, "skip unsafe name: %s\n", name_buf);
                continue;
            }
            {
                size_t i;
                int has_slash = 0;
                for (i = 0; name_buf[i]; i++) {
                    if (name_buf[i] == '/') { has_slash = 1; break; }
                }
                if (has_slash) {
                    fprintf(stderr, "skip name with slash: %s\n", name_buf);
                    continue;
                }
            }

            snprintf(node_path, sizeof node_path, "%s/%s", target_dir, name_buf);

            if (strcmp(type_buf, "file") == 0) {
                if (restore_file_node(repo_path, mk, ix,
                                      (char *)blob, toks, t, node_path,
                                      locator, progress) != 0) {
                    fprintf(stderr, "file restore failed: %s\n", node_path);
                    goto out;
                }
            } else if (strcmp(type_buf, "dir") == 0) {
                lcsas_mkdir_p(node_path);
                if (subtree_i >= 0
                        && toks[subtree_i].type == LCSAS_JSON_STRING
                        && toks[subtree_i].size == 64) {
                    char sub_hex[65];
                    memcpy(sub_hex, (char *)blob + toks[subtree_i].start, 64);
                    sub_hex[64] = '\0';
                    if (lcsas_tree_restore(repo_path, mk, ix, sub_hex,
                                           node_path, target_root,
                                           locator, progress) != 0) {
                        goto out;
                    }
                }
            } else if (strcmp(type_buf, "symlink") == 0) {
                if (lt_i >= 0) {
                    char tgt[1024];
                    if (lcsas_json_decode_string((char *)blob, &toks[lt_i],
                                                 tgt) < 0) continue;
                    if (lcsas_path_safe_symlink(target_root,
                                                target_dir, tgt) != 0) {
                        fprintf(stderr, "skip unsafe symlink %s -> %s\n",
                                node_path, tgt);
                        continue;
                    }
                    /* Atomic create: unlink first if exists. */
                    unlink(node_path);
                    if (symlink(tgt, node_path) != 0) {
                        fprintf(stderr, "symlink failed: %s -> %s\n",
                                node_path, tgt);
                    }
                }
            } else {
                fprintf(stderr, "skip unsupported node type %s: %s\n",
                        type_buf, name_buf);
            }
        }
        if (toks[t].start >= toks[nodes_arr].end) break;
    }
    rc = 0;

out:
    free(blob);
    free(toks);
    return rc;
}
