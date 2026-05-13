/*
 * disc_locator.c -- multi-disc pack search + interactive prompt.
 *
 * See disc_locator.h and recovery/docs/MULTI_DISC_DESIGN.txt.
 */
#include "disc_locator.h"
#include "hex.h"
#include "posix_compat.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifndef _WIN32
#  include <unistd.h>      /* chdir */
#else
#  include <direct.h>
#  define chdir _chdir
#endif

void
lcsas_disc_locator_init(lcsas_disc_locator *l,
                        const char **search_paths,
                        size_t n_paths,
                        lcsas_catalog *catalog,
                        int interactive)
{
    l->search_paths = search_paths;
    l->n_paths      = n_paths;
    l->catalog      = catalog;
    l->interactive  = interactive;
    l->prompt_in    = stdin;
    l->prompt_out   = stderr;
    l->meta_disc    = NULL;
    l->misses       = 0;
}

void
lcsas_disc_locator_set_meta(lcsas_disc_locator *l, const char *meta_disc)
{
    l->meta_disc = meta_disc;
}

/*
 * Return non-zero iff `path` is identical to, or a child of, `meta`.
 * Both are treated as POSIX-style paths; trailing slashes ignored.
 */
static int
path_under(const char *path, const char *meta)
{
    size_t pl, ml;
    if (!path || !meta || !*meta) return 0;
    pl = strlen(path);
    ml = strlen(meta);
    while (ml > 1 && meta[ml - 1] == '/') ml--;
    if (pl < ml) return 0;
    if (memcmp(path, meta, ml) != 0) return 0;
    if (pl == ml) return 1;
    return path[ml] == '/' || path[ml] == '\0';
}

/*
 * Probe one candidate path.  Tries (in order):
 *   <root>/data/<XX>/<hex>     two-level layout
 *   <root>/data/<hex>          flat layout
 *   <root>/<XX>/<hex>          two-level relative to data dir directly
 *   <root>/<hex>               flat relative to data dir directly
 *
 * Returns 1 if found (and writes path to out_path), 0 if not found.
 */
static int
try_one_path(const char *root, const char *hex,
             char *out_path, size_t cap)
{
    struct stat st;
    int rc;

    rc = snprintf(out_path, cap, "%s/data/%c%c/%s", root, hex[0], hex[1], hex);
    if (rc > 0 && (size_t)rc < cap && stat(out_path, &st) == 0) return 1;

    rc = snprintf(out_path, cap, "%s/data/%s", root, hex);
    if (rc > 0 && (size_t)rc < cap && stat(out_path, &st) == 0) return 1;

    rc = snprintf(out_path, cap, "%s/%c%c/%s", root, hex[0], hex[1], hex);
    if (rc > 0 && (size_t)rc < cap && stat(out_path, &st) == 0) return 1;

    rc = snprintf(out_path, cap, "%s/%s", root, hex);
    if (rc > 0 && (size_t)rc < cap && stat(out_path, &st) == 0) return 1;

    return 0;
}

static int
scan_paths(lcsas_disc_locator *l, const char *hex,
           char *out_path, size_t cap)
{
    size_t i;
    for (i = 0; i < l->n_paths; i++) {
        const char *p = l->search_paths[i];
        /* Skip any search path that is the meta-disc (or lives under
         * it) -- otherwise a single-drive user can never eject. */
        if (l->meta_disc && path_under(p, l->meta_disc)) continue;
        if (try_one_path(p, hex, out_path, cap)) {
            /* Also refuse to return a path inside the meta-disc, even
             * if it somehow matched (defence in depth). */
            if (l->meta_disc && path_under(out_path, l->meta_disc))
                continue;
            return 1;
        }
    }
    return 0;
}

/*
 * Print an interactive prompt naming the missing pack, using the
 * catalog (if any) to suggest a volume label.
 */
static void
print_prompt(lcsas_disc_locator *l, const char *hex)
{
    FILE *o = l->prompt_out;
    size_t i;

    fputc('\n', o);
    fputs("+----------------------------------------------------------+\n", o);
    fprintf(o, "| Pack %.16s... is required for the next file.       |\n",
            hex);

    if (l->catalog) {
        lcsas_catalog_pack pk;
        if (lcsas_catalog_find_pack(l->catalog, hex, &pk) == 0) {
            lcsas_catalog_volume vols[8];
            int n = lcsas_catalog_volumes_for_pack(
                        l->catalog, pk.pack_id, vols, 8);
            if (n > 0) {
                fputs("| It lives on volume(s):                                   |\n", o);
                {
                    int j;
                    for (j = 0; j < n; j++) {
                        fprintf(o, "|   %-54s |\n", vols[j].label);
                    }
                }
            } else {
                fputs("| (catalog has the pack, but no current volume mapping)    |\n", o);
            }
        } else {
            fputs("| (catalog has no record of this pack hash)                |\n", o);
        }
    } else {
        fputs("| (no --catalog supplied; cannot suggest a volume)         |\n", o);
    }

    fputs("|                                                          |\n", o);
    fputs("| Currently searching:                                     |\n", o);
    for (i = 0; i < l->n_paths; i++) {
        char buf[64];
        const char *p = l->search_paths[i];
        size_t len = strlen(p);
        if (len > 54) {
            snprintf(buf, sizeof buf, "...%s", p + len - 51);
        } else {
            snprintf(buf, sizeof buf, "%s", p);
        }
        fprintf(o, "|   %-54s |\n", buf);
    }

    fputs("|                                                          |\n", o);
    if (l->meta_disc) {
        fputs("| Single-drive recovery: if your machine has only ONE      |\n", o);
        fputs("| optical drive, eject the RECOVERY disc first, then       |\n", o);
        fputs("| insert the disc named above into the SAME drive.         |\n", o);
        fputs("|                                                          |\n", o);
    }
    fputs("| Insert the right disc and press ENTER to retry.          |\n", o);
    fputs("| Type 'q' then ENTER to abort.                            |\n", o);
    fputs("+----------------------------------------------------------+\n", o);
    fputs("> ", o);
    fflush(o);
}

/*
 * Read one line from prompt_in.  Returns 0 if user pressed Enter,
 * -1 if user typed 'q'/'Q' to abort, or -1 on EOF.
 */
static int
read_response(lcsas_disc_locator *l)
{
    int c;
    int abort_req = 0;
    if (l->prompt_in == NULL) return -1;
    c = fgetc(l->prompt_in);
    if (c == EOF) return -1;
    if (c == 'q' || c == 'Q') abort_req = 1;
    /* Consume the rest of the line. */
    while (c != EOF && c != '\n') {
        c = fgetc(l->prompt_in);
    }
    return abort_req ? -1 : 0;
}

int
lcsas_disc_locate_pack(lcsas_disc_locator *l,
                       const unsigned char pack_id[32],
                       char *out_path, size_t out_path_cap)
{
    char hex[65];

    lcsas_hex_encode(pack_id, 32, hex);
    hex[64] = '\0';

    /* Fast path: scan all known search paths once. */
    if (scan_paths(l, hex, out_path, out_path_cap)) return 0;

    if (!l->interactive) {
        fprintf(stderr, "pack not found: %s\n", hex);
        return -1;
    }

    /* Before we block on user input, make sure the process is not
     * holding the meta-disc captive via its current working directory.
     * If cwd is anywhere under meta_disc, drop to "/" so the user can
     * eject. */
    if (l->meta_disc) {
        char cwd[4096];
        const char *gcwd =
#ifndef _WIN32
            getcwd(cwd, sizeof cwd);
#else
            _getcwd(cwd, sizeof cwd);
#endif
        if (gcwd && path_under(cwd, l->meta_disc)) {
            if (chdir("/") != 0) {
                /* Best effort -- fall through and prompt anyway. */
            }
        }
    }

    /* Interactive: prompt-and-retry loop. */
    for (;;) {
        l->misses++;
        print_prompt(l, hex);
        if (read_response(l) < 0) {
            fprintf(stderr, "[lcsas-restore] aborted by user\n");
            return -1;
        }
        if (scan_paths(l, hex, out_path, out_path_cap)) {
            fprintf(l->prompt_out,
                    "[lcsas-restore] found %.16s...; continuing.\n", hex);
            return 0;
        }
        fprintf(l->prompt_out,
                "(still not found -- check the disc label and try again)\n");
    }
}
