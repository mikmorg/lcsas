/*
 * tree.h -- recursive tree-blob walker.
 *
 * Reads a tree blob, iterates its nodes, and materializes files,
 * directories, and symlinks under `target_dir`.
 */
#ifndef LCSAS_TREE_H
#define LCSAS_TREE_H

#include "repo.h"

struct lcsas_disc_locator;

/*
 * Progress counter shared across recursive lcsas_tree_restore calls.
 *
 * Counters are accumulated as blobs flow through restore_file_node.
 * A progress line is emitted to stderr each time *either* threshold
 * (blobs_per_tick or bytes_per_tick) is crossed since the last tick.
 *
 * total_blob_hint is purely informational -- the denominator shown in
 * "N/M" lines.  We use the loaded index size (lcsas_blob_index.count)
 * as the hint; it overstates by including blobs the snapshot does not
 * reference, but a pre-walk would double tree-blob I/O for no real
 * UX gain (this exists to reassure the operator that work is happening,
 * not to be a precise ETA).
 *
 * Zero-init via {0} disables progress reporting.  Field `enabled` must
 * be set to 1 explicitly to turn output on.
 */
typedef struct {
    int enabled;
    unsigned long long total_blob_hint;
    unsigned long long blobs_done;
    unsigned long long bytes_done;
    unsigned long long last_tick_blobs;
    unsigned long long last_tick_bytes;
    unsigned long long blobs_per_tick;  /* default 16 */
    unsigned long long bytes_per_tick;  /* default 1<<20 */
} lcsas_progress;

void lcsas_progress_init(lcsas_progress *p, unsigned long long total_hint);

/*
 * Record one decrypted/decompressed blob (`blob_len` bytes) towards
 * progress and emit a `[lcsas-restore] progress: N/M blobs, X MB`
 * stderr line if a tick threshold has been crossed.  Safe to call with
 * p == NULL (no-op) or p->enabled == 0.
 */
void lcsas_progress_tick(lcsas_progress *p, unsigned long long blob_len);

/* Emit a final summary line.  Safe with NULL / disabled. */
void lcsas_progress_finish(const lcsas_progress *p);

int lcsas_tree_restore(const char *repo_path,
                       const lcsas_master_key *mk,
                       const lcsas_blob_index *ix,
                       const char *tree_id_hex,
                       const char *target_dir,
                       const char *target_root, /* used for symlink safety */
                       struct lcsas_disc_locator *locator,
                       lcsas_progress *progress);

#endif
