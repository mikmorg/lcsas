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
    /* NULL-terminated list of paths (each is a directory expected to
     * contain a `data/` subdir with pack files, two-level or flat). */
    const char **search_paths;
    size_t       n_paths;

    /* Optional: catalog for volume-label hints in prompts.
     * May be NULL. */
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
