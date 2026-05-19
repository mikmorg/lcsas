/*
 * disc_locator.h -- find pack files across multiple mounted discs.
 *
 * Replaces the single-path pack lookup inside repo.c with a search
 * across an arbitrary list of mount points, with an optional
 * interactive prompt + retry loop when a pack is missing.
 *
 * See recovery/docs/MULTI_DISC_DESIGN.txt for the full design.
 */
#ifndef LCSAS_DISC_LOCATOR_H
#define LCSAS_DISC_LOCATOR_H

#include "catalog.h"

#include <stddef.h>
#include <stdio.h>

typedef struct {
    /* Caller-provided list of paths (each is a directory expected to
     * contain a `data/` subdir with pack files, two-level or flat).
     * The locator BORROWS these pointers -- the caller owns the
     * underlying strings and must keep them alive. */
    const char **search_paths;
    size_t       n_paths;

    /* Optional: catalog for volume-label hints in prompts.  May be
     * NULL.  This is the CALLER's catalog handle and is borrowed --
     * the locator may swap in a fresher one (see `owned_catalog`
     * below) and use that for hints instead. */
    lcsas_catalog *catalog;

    /* Interactive behaviour.
     *   0 = fail-fast on missing pack (current behaviour)
     *   1 = print a prompt to prompt_out, read response from
     *       prompt_in, re-scan after Enter, abort on 'q'. */
    int interactive;

    /* Streams used for interactive prompts.  Default to stdin/stderr. */
    FILE *prompt_in;
    FILE *prompt_out;

    /* Optional: the mount point of the recovery / meta disc.  When set,
     * the locator never opens files under this path or returns paths
     * inside it, so the user can eject the meta-disc with a single
     * optical drive and re-use the same drive for data discs.  May be
     * NULL when single-drive recovery is not a concern. */
    const char *meta_disc;

    /* Misses counter for diagnostics. */
    unsigned long misses;

    /* Mount-parent directories (e.g. "/Volumes", "/media/$USER",
     * "/mnt") scanned on every retry to discover newly-inserted discs.
     * Borrowed pointers; caller owns the underlying strings. */
    const char **mount_parents;
    size_t       n_mount_parents;

    /* Discovered subdirectories of mount_parents.  Refreshed on every
     * retry from `refresh_discovered()`.  The locator OWNS these
     * strings and frees them in `lcsas_disc_locator_free()`. */
    char       **discovered_paths;
    size_t       n_discovered;
    size_t       cap_discovered;

    /* Freshest catalog opened by the locator from a discovered mount
     * (NULL when none beats the caller-provided one).  Locator owns
     * this handle and closes it on free / refresh. */
    lcsas_catalog *owned_catalog;
    char          *owned_catalog_path;
    long long      owned_catalog_mtime;

    /* Optional opt-in opportunistic pack cache.  When set (e.g. via
     * the LCSAS_PACK_CACHE_DIR env var, plumbed from main.c), every
     * successful pack hit on a mounted disc triggers a "drain":
     * the rest of that disc's data/ subtree is copied into the cache
     * so future packs from the same disc don't require another swap.
     * Trades disk space for reduced disc thrashing.
     *
     * NULL = feature off (default), no draining, no extra disk use. */
    char *cache_dir;

    /* Maximum number of pack files to copy per drain_disc() call.
     * 0 = unlimited (drain the full disc in one call; default).
     * Set via LCSAS_DRAIN_CHUNK_PACKS env var.  When > 0, drain_disc
     * returns after copying this many packs and the next call resumes
     * where it left off (already-cached packs are skipped via stat).
     * Use 100-500 on slow optical drives to keep the restore
     * interactive between drain calls. */
    int drain_chunk_packs;
} lcsas_disc_locator;

/*
 * Initialise a locator with sensible defaults.
 *   search_paths/n_paths: caller-provided arrays (not copied; must
 *                        outlive the locator).
 *   catalog:             optional; may be NULL.
 *   interactive:         see above.
 */
void lcsas_disc_locator_init(lcsas_disc_locator *l,
                             const char **search_paths,
                             size_t n_paths,
                             lcsas_catalog *catalog,
                             int interactive);

/*
 * Mark `meta_disc` as the recovery medium so the locator (a) never
 * lists it as a candidate pack source and (b) cd()'s out of it before
 * prompting, so a one-drive user can eject and swap discs.  `meta_disc`
 * is borrowed; the caller must keep it alive for the locator's
 * lifetime.  Pass NULL to clear.
 */
void lcsas_disc_locator_set_meta(lcsas_disc_locator *l,
                                 const char *meta_disc);

/*
 * Attach a list of mount-parent directories to scan on every retry.
 * Each parent is a directory whose immediate children may be newly-
 * inserted optical discs (e.g. "/Volumes", "/media/$USER", "/mnt").
 * On each retry the locator opendir()s every parent, adds new
 * children to its discovered-paths list, and also looks for a
 * fresher catalog.db at each child's root.
 *
 * `mount_parents` and the strings it points to are BORROWED -- caller
 * keeps ownership.  Pass NULL/0 to clear.
 */
void lcsas_disc_locator_set_mount_parents(lcsas_disc_locator *l,
                                          const char **mount_parents,
                                          size_t n_mount_parents);

/*
 * Release any resources owned by the locator (discovered paths,
 * locator-opened catalog).  Safe to call on a locator initialised
 * with lcsas_disc_locator_init().  Borrowed fields (search_paths,
 * caller's catalog, meta_disc, mount_parents) are NOT touched.
 */
void lcsas_disc_locator_free(lcsas_disc_locator *l);

/*
 * Record the mtime of the caller-provided catalog so the locator
 * never swaps in an OLDER catalog discovered on a mounted disc.  The
 * caller passes the SQLite path it opened (typically the value of
 * `--catalog`); the locator stat()s it and uses st_mtime as the
 * floor.  Safe to call with a NULL/missing path -- the floor stays
 * at 0 and any opened catalog wins.
 */
void lcsas_disc_locator_set_catalog_floor(lcsas_disc_locator *l,
                                          const char *catalog_path);

/*
 * Enable the opt-in opportunistic pack cache.  Set `cache_dir` to a
 * writable directory; the locator will mkdir-p it and, on every
 * successful pack hit found on a non-cache path, copy the rest of
 * that disc's data/ subtree into the cache so subsequent packs from
 * the same disc resolve from local storage.  Without this, restoring
 * a tree whose blob references interleave packs from N discs causes
 * O(blobs) disc swaps in the worst case; with this, O(N) swaps.
 *
 * Pass NULL to clear (default).  The locator owns the duplicated
 * string and frees it in `lcsas_disc_locator_free`.
 */
void lcsas_disc_locator_set_cache_dir(lcsas_disc_locator *l,
                                      const char *cache_dir);

/*
 * Locate the pack file containing the given 32-byte pack_id (the
 * SHA-256 of the pack's contents, used as its filename).
 *
 * The function tries each search_path in order, looking for both
 * the two-level layout `data/<XX>/<hex>` and the flat layout
 * `data/<hex>`.  If `<search_path>` itself looks like a restic data
 * dir (i.e. contains pack files directly), it's also probed.
 *
 * On success returns 0 and fills `out_path` with the absolute path.
 * On miss + interactive=0, returns -1.
 * On miss + interactive=1, prompts the user, re-scans, retries.
 *   - Returns 0 once found.
 *   - Returns -1 if the user types 'q' to abort.
 */
int lcsas_disc_locate_pack(lcsas_disc_locator *l,
                           const unsigned char pack_id[32],
                           char *out_path, size_t out_path_cap);

#endif  /* LCSAS_DISC_LOCATOR_H */
