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
    l->search_paths        = search_paths;
    l->n_paths             = n_paths;
    l->catalog             = catalog;
    l->interactive         = interactive;
    l->prompt_in           = stdin;
    l->prompt_out          = stderr;
    l->meta_disc           = NULL;
    l->misses              = 0;
    l->mount_parents       = NULL;
    l->n_mount_parents     = 0;
    l->discovered_paths    = NULL;
    l->n_discovered        = 0;
    l->cap_discovered      = 0;
    l->owned_catalog       = NULL;
    l->owned_catalog_path  = NULL;
    l->owned_catalog_mtime = 0;
}

void
lcsas_disc_locator_set_meta(lcsas_disc_locator *l, const char *meta_disc)
{
    l->meta_disc = meta_disc;
}

void
lcsas_disc_locator_set_mount_parents(lcsas_disc_locator *l,
                                     const char **mount_parents,
                                     size_t n_mount_parents)
{
    l->mount_parents   = mount_parents;
    l->n_mount_parents = n_mount_parents;
}

void
lcsas_disc_locator_set_catalog_floor(lcsas_disc_locator *l,
                                     const char *catalog_path)
{
    struct stat st;
    if (!catalog_path || !*catalog_path) return;
    if (stat(catalog_path, &st) != 0) return;
    l->owned_catalog_mtime = (long long)st.st_mtime;
}

/*
 * Free discovered-paths storage (locator-owned strings).
 */
static void
free_discovered(lcsas_disc_locator *l)
{
    size_t i;
    if (!l->discovered_paths) return;
    for (i = 0; i < l->n_discovered; i++) {
        free(l->discovered_paths[i]);
    }
    free(l->discovered_paths);
    l->discovered_paths = NULL;
    l->n_discovered     = 0;
    l->cap_discovered   = 0;
}

void
lcsas_disc_locator_free(lcsas_disc_locator *l)
{
    free_discovered(l);
    if (l->owned_catalog) {
        lcsas_catalog_close(l->owned_catalog);
        l->owned_catalog = NULL;
    }
    free(l->owned_catalog_path);
    l->owned_catalog_path  = NULL;
    l->owned_catalog_mtime = 0;
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
 * Append `path` to the discovered list if it isn't already in either
 * the caller-provided search paths or the discovered list.  Returns
 * 1 if appended, 0 if duplicate or on allocation failure.
 */
static int
push_discovered(lcsas_disc_locator *l, const char *path)
{
    size_t i;
    char *dup;
    char **grown;
    size_t newcap;

    if (!path || !*path) return 0;

    for (i = 0; i < l->n_paths; i++) {
        if (l->search_paths[i] && strcmp(l->search_paths[i], path) == 0)
            return 0;
    }
    for (i = 0; i < l->n_discovered; i++) {
        if (strcmp(l->discovered_paths[i], path) == 0) return 0;
    }

    if (l->n_discovered == l->cap_discovered) {
        newcap = l->cap_discovered ? l->cap_discovered * 2 : 8;
        grown  = (char **)realloc(l->discovered_paths,
                                  newcap * sizeof(char *));
        if (!grown) return 0;
        l->discovered_paths = grown;
        l->cap_discovered   = newcap;
    }
    dup = (char *)malloc(strlen(path) + 1);
    if (!dup) return 0;
    memcpy(dup, path, strlen(path) + 1);
    l->discovered_paths[l->n_discovered++] = dup;
    return 1;
}

/*
 * Consider `path` as a potentially-fresher catalog.db.  If its mtime
 * beats whatever the locator has previously opened, swap it in.
 */
static void
consider_catalog(lcsas_disc_locator *l, const char *path)
{
    struct stat st;
    lcsas_catalog *cat;
    char *path_dup;

    if (!path || !*path) return;
    if (stat(path, &st) != 0) return;
    if ((long long)st.st_mtime <= l->owned_catalog_mtime) return;

    cat = lcsas_catalog_open(path);
    if (!cat) return;

    /* Successfully opened a newer catalog -- swap. */
    if (l->owned_catalog) lcsas_catalog_close(l->owned_catalog);
    path_dup = (char *)malloc(strlen(path) + 1);
    if (!path_dup) {
        lcsas_catalog_close(cat);
        return;
    }
    memcpy(path_dup, path, strlen(path) + 1);
    free(l->owned_catalog_path);
    l->owned_catalog       = cat;
    l->owned_catalog_path  = path_dup;
    l->owned_catalog_mtime = (long long)st.st_mtime;
}

/*
 * Pick whichever catalog (caller's vs locator-owned) to use for hints.
 */
static lcsas_catalog *
effective_catalog(lcsas_disc_locator *l)
{
    if (l->owned_catalog) return l->owned_catalog;
    return l->catalog;
}

/*
 * Walk each mount_parent directory, append every direct subdirectory
 * to discovered_paths, and check each for a fresher catalog.db.
 * Discards prior discoveries first so the list is always current.
 */
static void
refresh_discovered(lcsas_disc_locator *l)
{
    size_t i;

    free_discovered(l);

    /* Also try the parent itself as a candidate source (some setups
     * mount a single disc directly at /mnt rather than /mnt/<label>).
     * That's handled by also stat-ing the parent itself. */
    for (i = 0; i < l->n_mount_parents; i++) {
        const char *parent = l->mount_parents[i];
        DIR *d;
        struct dirent *e;
        char child[4096];
        int rc;

        if (!parent || !*parent) continue;
        if (l->meta_disc && path_under(parent, l->meta_disc)) continue;

        /* The parent itself may be the mount point of a single
         * inserted disc -- probe its catalog and add it as a search
         * path so two-level / flat layout discovery picks up packs
         * directly under it. */
        rc = snprintf(child, sizeof child, "%s/catalog.db", parent);
        if (rc > 0 && (size_t)rc < sizeof child) {
            consider_catalog(l, child);
        }
        push_discovered(l, parent);

        d = opendir(parent);
        if (!d) continue;
        while ((e = readdir(d)) != NULL) {
            struct stat st;
            const char *name = e->d_name;
            if (name[0] == '.' &&
                (name[1] == '\0' || (name[1] == '.' && name[2] == '\0')))
                continue;
            rc = snprintf(child, sizeof child, "%s/%s", parent, name);
            if (rc <= 0 || (size_t)rc >= sizeof child) continue;
            if (stat(child, &st) != 0) continue;
            if (!S_ISDIR(st.st_mode)) continue;
            if (l->meta_disc && path_under(child, l->meta_disc))
                continue;
            push_discovered(l, child);

            /* Look for a catalog.db at the disc root (LCSAS holographic
             * layout) -- pick the freshest one we can open. */
            rc = snprintf(child, sizeof child, "%s/%s/catalog.db",
                          parent, name);
            if (rc > 0 && (size_t)rc < sizeof child) {
                consider_catalog(l, child);
            }
        }
        closedir(d);
    }
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

/*
 * Try a single search candidate, honouring the meta-disc exclusion.
 * Returns 1 on hit (with out_path populated), 0 otherwise.
 */
static int
try_with_meta(lcsas_disc_locator *l, const char *p, const char *hex,
              char *out_path, size_t cap)
{
    if (!p) return 0;
    if (l->meta_disc && path_under(p, l->meta_disc)) return 0;
    if (!try_one_path(p, hex, out_path, cap)) return 0;
    if (l->meta_disc && path_under(out_path, l->meta_disc)) return 0;
    return 1;
}

static int
scan_paths(lcsas_disc_locator *l, const char *hex,
           char *out_path, size_t cap)
{
    size_t i;
    for (i = 0; i < l->n_paths; i++) {
        if (try_with_meta(l, l->search_paths[i], hex, out_path, cap))
            return 1;
    }
    for (i = 0; i < l->n_discovered; i++) {
        if (try_with_meta(l, l->discovered_paths[i], hex, out_path, cap))
            return 1;
    }
    return 0;
}

/*
 * Render one search-path candidate into the prompt's "Currently
 * searching:" block, truncating with a "..." prefix when too long.
 */
static void
print_search_path(FILE *o, const char *p)
{
    char buf[64];
    size_t len = strlen(p);
    if (len > 54) {
        snprintf(buf, sizeof buf, "...%s", p + len - 51);
    } else {
        snprintf(buf, sizeof buf, "%s", p);
    }
    fprintf(o, "|   %-54s |\n", buf);
}

/*
 * Print an interactive prompt naming the missing pack, using the
 * catalog (if any) to suggest a volume label.
 */
static void
print_prompt(lcsas_disc_locator *l, const char *hex)
{
    FILE *o = l->prompt_out;
    lcsas_catalog *cat = effective_catalog(l);
    size_t i;

    fputc('\n', o);
    fputs("+----------------------------------------------------------+\n", o);
    fprintf(o, "| Pack %.16s... is required for the next file.       |\n",
            hex);

    if (cat) {
        lcsas_catalog_pack pk;
        if (lcsas_catalog_find_pack(cat, hex, &pk) == 0) {
            lcsas_catalog_volume vols[8];
            int n = lcsas_catalog_volumes_for_pack(
                        cat, pk.pack_id, vols, 8);
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
        print_search_path(o, l->search_paths[i]);
    }
    for (i = 0; i < l->n_discovered; i++) {
        print_search_path(o, l->discovered_paths[i]);
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

    /* Fast path: scan all known search paths once (with whatever
     * discoveries are already cached from a prior call). */
    if (scan_paths(l, hex, out_path, out_path_cap)) return 0;

    /* Discover any newly-mounted discs before giving up.  Also
     * re-picks the freshest catalog -- if the user inserted the
     * right disc just before the first read, we may find the pack
     * without ever prompting. */
    if (l->n_mount_parents > 0) {
        refresh_discovered(l);
        if (scan_paths(l, hex, out_path, out_path_cap)) return 0;
    }

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

    /* Interactive: prompt-and-retry loop.  Prompt with whatever
     * discoveries are already current; after Enter, refresh to catch
     * a disc the user mounted between prompt and keypress.  The
     * freshest catalog is re-picked at each refresh so hash->label
     * hints stay accurate after a disc swap. */
    for (;;) {
        l->misses++;
        print_prompt(l, hex);
        if (read_response(l) < 0) {
            fprintf(stderr, "[lcsas-restore] aborted by user\n");
            return -1;
        }
        refresh_discovered(l);
        if (scan_paths(l, hex, out_path, out_path_cap)) {
            fprintf(l->prompt_out,
                    "[lcsas-restore] found %.16s...; continuing.\n", hex);
            return 0;
        }
        fprintf(l->prompt_out,
                "(still not found -- check the disc label and try again)\n");
    }
}
