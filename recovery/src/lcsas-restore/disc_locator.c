/*
 * disc_locator.c -- multi-disc pack search + interactive prompt.
 *
 * See disc_locator.h and recovery/docs/MULTI_DISC_DESIGN.txt.
 */
#include "disc_locator.h"
#include "hex.h"
#include "json_q.h"
#include "lcsas_io.h"
#include "posix_compat.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#ifndef _WIN32
#  include <unistd.h>      /* chdir, read */
#  include <dirent.h>
#  include <errno.h>
#  include <sys/select.h>  /* auto-detect poll (#455) */
#  include <sys/statvfs.h>
#  include <sys/time.h>
#else
#  include <direct.h>
#  define chdir _chdir
#endif

/* Forward declarations for helpers defined further below. */
static int copy_file(const char *src, const char *dst);

/*
 * A disc root whose volume_info.json has been announced (#456).  The
 * file's stat identity tells a different disc mounted at the same path
 * apart; Windows reports st_ino 0, so size + mtime carry it there.
 */
struct lcsas_seen_volume {
    char              *root;
    unsigned long long dev;
    unsigned long long ino;
    long long          size;
    long long          mtime;
    int                seen;    /* found again by the refresh in progress */
};

/*
 * volume_info.json is a few hundred bytes plus ~70 per pack in its
 * sha256_manifest: ~7 MB for a BDXL100 of 1 MiB packs.  A bigger file
 * is not one LCSAS wrote, so it is skipped rather than read.
 */
#define VOLUME_INFO_MAX_BYTES (16L * 1024L * 1024L)

void
lcsas_disc_locator_init(lcsas_disc_locator *l,
                        const char **search_paths,
                        size_t n_paths,
                        lcsas_catalog *catalog,
                        int interactive)
{
    const char *chunk_env;
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
    l->cache_dir           = NULL;
    chunk_env = getenv("LCSAS_DRAIN_CHUNK_PACKS");
    l->drain_chunk_packs   = chunk_env ? atoi(chunk_env) : 0;
    l->seen_volumes        = NULL;
    l->n_seen_volumes      = 0;
    l->cap_seen_volumes    = 0;
    l->mid_prompt_line     = 0;
    l->auto_mount          = 1;
    /* 2 s: a desktop auto-mount takes a second or two, so a faster
     * poll gains nothing, and each tick costs one readdir per mount
     * parent plus a few stat()s per candidate disc. */
    l->poll_ms             = 2000;
}

void
lcsas_disc_locator_set_auto_mount(lcsas_disc_locator *l, int on)
{
    l->auto_mount = on ? 1 : 0;
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
    size_t i;
    free_discovered(l);
    for (i = 0; i < l->n_seen_volumes; i++) {
        free(l->seen_volumes[i].root);
    }
    free(l->seen_volumes);
    l->seen_volumes     = NULL;
    l->n_seen_volumes   = 0;
    l->cap_seen_volumes = 0;
    if (l->owned_catalog) {
        lcsas_catalog_close(l->owned_catalog);
        l->owned_catalog = NULL;
    }
    free(l->owned_catalog_path);
    l->owned_catalog_path  = NULL;
    l->owned_catalog_mtime = 0;
    free(l->cache_dir);
    l->cache_dir = NULL;
}

/*
 * Best-effort mkdir -p.  Returns 0 on success or if the directory
 * already exists.  Used both for the cache root and per-prefix
 * subdirs (data/<XX>/).
 */
static int
mkdir_p(const char *path)
{
    char buf[4096];
    size_t i, n;
    struct stat st;

    if (!path || !*path) return -1;
    n = strlen(path);
    if (n >= sizeof buf) return -1;
    memcpy(buf, path, n + 1);

    for (i = 1; i < n; i++) {
        if (buf[i] != '/') continue;
        buf[i] = '\0';
        if (stat(buf, &st) != 0) {
            if (mkdir(buf, 0755) != 0) {
                buf[i] = '/';
                /* If a race created it, that's fine; bail only on
                 * real errors. */
                if (stat(buf, &st) != 0) return -1;
            }
        }
        buf[i] = '/';
    }
    if (stat(buf, &st) == 0) return 0;
    return mkdir(buf, 0755);
}

void
lcsas_disc_locator_set_cache_dir(lcsas_disc_locator *l,
                                 const char *cache_dir)
{
    free(l->cache_dir);
    l->cache_dir = NULL;
    if (!cache_dir || !*cache_dir) return;
    if (mkdir_p(cache_dir) != 0) {
        fprintf(stderr,
                "[lcsas-restore] cannot create cache dir %s; "
                "auto-cache disabled\n", cache_dir);
        return;
    }
    l->cache_dir = strdup(cache_dir);
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
 * Return non-zero iff the meta disc is *still mounted* at `meta`.
 *
 * The meta disc exclusion (`path_under(p, meta)` at three call sites
 * below) must NOT persist after the heir ejects the meta disc and
 * mounts a data disc at the same mount point.  Otherwise the binary
 * blacklists `/mnt` for the rest of the run and the swap prompt loops
 * forever (issue #143).
 *
 * Cheap liveness test: probe a sentinel file that exists on the meta
 * disc but never on a data disc.  `recovery/scripts/restore.sh` is
 * written by `_bundle_recovery_toolchain_artifacts` (meta builder) and
 * `HolographicInjector` (which fills data discs) does NOT
 * write a `recovery/` directory.  `access(F_OK)` is the minimum-cost
 * stat-like check (no fd, no open).
 *
 * NULL / empty `meta` => not live (defensive).
 */
static int
meta_disc_is_live(const char *meta)
{
    char sentinel[4096];
    int rc;

    if (!meta || !*meta) return 0;
    rc = snprintf(sentinel, sizeof sentinel,
                  "%s/recovery/scripts/restore.sh", meta);
    if (rc <= 0 || (size_t)rc >= sizeof sentinel) return 0;
    return access(sentinel, F_OK) == 0;
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
 *
 * To avoid pinning the underlying mount via SQLite's persistent file
 * handle (which would block `umount /mnt` between disc swaps), we
 * **copy the catalog into the cache dir** and open the copy.  The
 * original is touched only during the copy.  Fallback when no cache
 * dir is configured: open the original in-place (legacy behaviour;
 * the heir gets the mount-busy friction).
 */
static void
consider_catalog(lcsas_disc_locator *l, const char *path)
{
    struct stat st;
    lcsas_catalog *cat;
    char *path_dup;
    char open_path[4096];

    if (!path || !*path) return;
    if (stat(path, &st) != 0) return;
    if ((long long)st.st_mtime <= l->owned_catalog_mtime) return;

    if (l->cache_dir) {
        int rc = snprintf(open_path, sizeof open_path,
                          "%s/.locator-catalog.db", l->cache_dir);
        if (rc <= 0 || (size_t)rc >= sizeof open_path) return;
        /* Drop any previous copy so the file size doesn't accumulate. */
        unlink(open_path);
        if (copy_file(path, open_path) != 0) {
            /* Best-effort fallback: open the original.  Will pin the
             * mount, but better than no catalog. */
            cat = lcsas_catalog_open(path);
        } else {
            cat = lcsas_catalog_open(open_path);
        }
    } else {
        cat = lcsas_catalog_open(path);
    }
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
 * Start a line of output on the prompt stream.  While the prompt
 * waits, the cursor sits after its "> ", so a line printed then (a poll
 * tick's find) needs a newline first.
 */
static void
begin_line(lcsas_disc_locator *l)
{
    if (l->mid_prompt_line) {
        fputc('\n', l->prompt_out);
        l->mid_prompt_line = 0;
    }
}

/*
 * Decode the top-level string field `key` of the object at toks[0]
 * into `out`.  Returns 1 for a non-empty string that fits, else 0.
 */
static int
json_string_field(const char *src, const lcsas_json_tok *toks,
                  const char *key, char *out, size_t cap)
{
    long idx = lcsas_json_obj_get(src, toks, 0, key);
    return idx >= 0
        && toks[idx].type == LCSAS_JSON_STRING
        && lcsas_json_decode_string(src, &toks[idx], out, cap) > 0;
}

/*
 * Print the one line that names the disc at `root` from its
 * volume_info.json: the label LCSAS gave it, the meta disc,
 * or why the file could not be used.  A bad file only costs the label;
 * the disc is searched for packs either way.
 */
static void
describe_volume(lcsas_disc_locator *l, const char *root,
                const char *info_path, long long size)
{
    FILE *o = l->prompt_out;
    unsigned char *buf = NULL;
    size_t len = 0;
    lcsas_json_tok *toks = NULL;
    const char *why = NULL;
    char label[128];
    char kind[8];

    begin_line(l);
    if (size > VOLUME_INFO_MAX_BYTES) {
        why = "is too large";
    } else if (lcsas_read_file(info_path, &buf, &len) != 0) {
        why = "could not be read";
    } else if (lcsas_json_parse_alloc((const char *)buf, len, &toks, 64) < 1
               || toks[0].type != LCSAS_JSON_OBJECT) {
        why = "is not valid JSON";
    } else if (json_string_field((const char *)buf, toks, "label",
                                 label, sizeof label)) {
        /* The label is read off the disc: show printable ASCII only, so
         * a stray byte can't drive the terminal. */
        char *c;
        for (c = label; *c; c++) {
            if ((unsigned char)*c < 0x20 || (unsigned char)*c > 0x7e)
                *c = '?';
        }
        fprintf(o, "[lcsas-restore] mounted the disc labelled %s at %s\n",
                label, root);
    } else if (json_string_field((const char *)buf, toks, "type",
                                 kind, sizeof kind)
               && strcmp(kind, "meta") == 0) {
        /* The meta disc's volume_info.json has no label. */
        fprintf(o, "[lcsas-restore] mounted the META disc "
                   "at %s\n", root);
    } else {
        why = "has no usable label";
    }
    if (why) {
        fprintf(o, "[lcsas-restore] disc at %s: its volume_info.json %s; "
                   "searching it anyway.\n", root, why);
    }
    fflush(o);
    free(toks);
    free(buf);
}

/*
 * Name the disc at `root` if it carries a volume_info.json not already
 * named for this insertion (#456).  Absent file: silent, as for older
 * discs and plain directories.  Returns 1 if it printed a line.
 */
static int
announce_volume(lcsas_disc_locator *l, const char *root)
{
    char info_path[4096];
    struct stat st;
    struct lcsas_seen_volume *sv = NULL;
    size_t i;
    int rc;

    rc = snprintf(info_path, sizeof info_path, "%s/volume_info.json", root);
    if (rc <= 0 || (size_t)rc >= sizeof info_path) return 0;
    if (stat(info_path, &st) != 0) return 0;

    for (i = 0; i < l->n_seen_volumes; i++) {
        if (strcmp(l->seen_volumes[i].root, root) == 0) {
            sv = &l->seen_volumes[i];
            break;
        }
    }
    if (sv) {
        sv->seen = 1;
        if (sv->dev == (unsigned long long)st.st_dev
                && sv->ino == (unsigned long long)st.st_ino
                && sv->size == (long long)st.st_size
                && sv->mtime == (long long)st.st_mtime)
            return 0;
    } else {
        if (l->n_seen_volumes == l->cap_seen_volumes) {
            size_t newcap = l->cap_seen_volumes
                              ? l->cap_seen_volumes * 2 : 8;
            struct lcsas_seen_volume *grown =
                (struct lcsas_seen_volume *)realloc(
                    l->seen_volumes, newcap * sizeof *grown);
            /* Out of memory: stay quiet rather than repeat the line on
             * every poll tick. */
            if (!grown) return 0;
            l->seen_volumes     = grown;
            l->cap_seen_volumes = newcap;
        }
        sv = &l->seen_volumes[l->n_seen_volumes];
        sv->root = (char *)malloc(strlen(root) + 1);
        if (!sv->root) return 0;
        memcpy(sv->root, root, strlen(root) + 1);
        sv->seen = 1;
        l->n_seen_volumes++;
    }
    sv->dev   = (unsigned long long)st.st_dev;
    sv->ino   = (unsigned long long)st.st_ino;
    sv->size  = (long long)st.st_size;
    sv->mtime = (long long)st.st_mtime;
    describe_volume(l, root, info_path, (long long)st.st_size);
    return 1;
}

/*
 * Walk each mount_parent directory, append every direct subdirectory
 * to discovered_paths, and check each for a fresher catalog.db.
 * Discards prior discoveries first so the list is always current.
 */
static int
refresh_discovered(lcsas_disc_locator *l)
{
    size_t i;
    int meta_live;
    int announced = 0;

    free_discovered(l);
    for (i = 0; i < l->n_seen_volumes; i++) {
        l->seen_volumes[i].seen = 0;
    }

    /* Liveness check once per refresh -- if the heir ejects the
     * meta disc partway through, the worst case is a single stale
     * decision; the next refresh re-evaluates. */
    meta_live = l->meta_disc && meta_disc_is_live(l->meta_disc);

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
        if (meta_live && path_under(parent, l->meta_disc)) continue;

        /* The parent itself may be the mount point of a single
         * inserted disc -- probe its catalog and add it as a search
         * path so two-level / flat layout discovery picks up packs
         * directly under it. */
        rc = snprintf(child, sizeof child, "%s/catalog.db", parent);
        if (rc > 0 && (size_t)rc < sizeof child) {
            consider_catalog(l, child);
        }
        push_discovered(l, parent);
        announced += announce_volume(l, parent);

        d = opendir(parent);
        if (!d) continue;
        while ((e = readdir(d)) != NULL) {
            struct stat st;
            const char *name = e->d_name;
            if (name[0] == '.' &&
                (name[1] == '\0' || (name[1] == '.' && name[2] == '\0')))
                continue;
            rc = snprintf(child, sizeof child, "%s/%s", parent, name);
            if (rc <= 0 || (size_t)rc >= sizeof child) {
                fprintf(stderr, "[lcsas-restore] path too long (>%lu bytes), skipping\n",
                        (unsigned long)sizeof child);
                continue;
            }
            if (stat(child, &st) != 0) continue;
            if (!S_ISDIR(st.st_mode)) continue;
            if (meta_live && path_under(child, l->meta_disc))
                continue;
            push_discovered(l, child);
            announced += announce_volume(l, child);

            /* Look for a catalog.db (an LCSAS disc catalog) at the disc
             * root -- pick the freshest one we can open. */
            rc = snprintf(child, sizeof child, "%s/%s/catalog.db",
                          parent, name);
            if (rc > 0 && (size_t)rc < sizeof child) {
                consider_catalog(l, child);
            }
        }
        closedir(d);
    }

    /* Forget discs that are gone, so one put back is named again. */
    i = 0;
    while (i < l->n_seen_volumes) {
        if (l->seen_volumes[i].seen) {
            i++;
            continue;
        }
        free(l->seen_volumes[i].root);
        l->seen_volumes[i] = l->seen_volumes[--l->n_seen_volumes];
    }
    return announced;
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
 * Try a single search candidate, skipping anything on the meta disc.
 * Returns 1 on hit (with out_path populated), 0 otherwise.
 */
static int
try_with_meta(lcsas_disc_locator *l, const char *p, const char *hex,
              char *out_path, size_t cap)
{
    int meta_live;
    if (!p) return 0;
    meta_live = l->meta_disc && meta_disc_is_live(l->meta_disc);
    if (meta_live && path_under(p, l->meta_disc)) return 0;
    if (!try_one_path(p, hex, out_path, cap)) return 0;
    if (meta_live && path_under(out_path, l->meta_disc)) return 0;
    return 1;
}

/*
 * Copy one pack file (src → dst) using buffered I/O.  Returns 0 on
 * success, -1 on any error.  Best-effort: callers ignore the rc
 * because draining is opportunistic.
 *
 * FD LIFETIME: all file handles opened in this function are closed
 * before it returns.  Do not hold fds across a disc unmount.
 */
static int
copy_file(const char *src, const char *dst)
{
    FILE *in = fopen(src, "rb");
    FILE *out;
    char buf[64 * 1024];
    size_t n;

    if (!in) return -1;
    out = fopen(dst, "wb");
    if (!out) { fclose(in); return -1; }
    while ((n = fread(buf, 1, sizeof buf, in)) > 0) {
        if (fwrite(buf, 1, n, out) != n) {
            fclose(in); fclose(out); unlink(dst);
            return -1;
        }
    }
    fclose(in);
    fclose(out);
    return 0;
}

/*
 * Drain (a copy of) every pack file under `<root>/data/` into the
 * locator's cache_dir, mirroring the two-level layout
 * `data/<XX>/<hex>`.  Skips files that already exist in the cache
 * (so re-drains are cheap).
 *
 * This is opportunistic: errors mid-drain stop the drain for that
 * disc but don't fail the locate.  Callers gate on cache_dir being
 * non-NULL.
 */
/*
 * Return non-zero if the filesystem hosting `path` is below
 * `min_free_pct` percent free.  Conservative on stat failure
 * (returns 0 — assume OK).  Posts a one-line stderr warning on
 * threshold crossing so the heir sees it once.
 */
static int
fs_critically_full(const char *path, int min_free_pct)
{
#ifndef _WIN32
    struct statvfs vfs;
    unsigned long long free_bytes, total_bytes;
    int pct_free;
    if (!path || statvfs(path, &vfs) != 0) return 0;
    free_bytes  = (unsigned long long)vfs.f_bavail * vfs.f_frsize;
    total_bytes = (unsigned long long)vfs.f_blocks * vfs.f_frsize;
    if (total_bytes == 0) return 0;
    pct_free = (int)((free_bytes * 100ULL) / total_bytes);
    return pct_free < min_free_pct;
#else
    (void)path; (void)min_free_pct;
    return 0;   /* not implemented on Windows; cache off there anyway */
#endif
}

/*
 * Return the total bytes currently stored under the cache dir's
 * data/ subtree.  Walks the same two-level layout drain_disc
 * writes.  Returns 0 on stat failures.
 */
static unsigned long long
cache_bytes_used(const char *cache_dir)
{
    char data_dir[4096], prefix_dir[4096], file_path[4096];
    DIR *d_root, *d_pref;
    struct dirent *e_root, *e_pref;
    struct stat st;
    unsigned long long total = 0;
    int rc;

    if (!cache_dir) return 0;
    rc = snprintf(data_dir, sizeof data_dir, "%s/data", cache_dir);
    if (rc <= 0 || (size_t)rc >= sizeof data_dir) return 0;
    d_root = opendir(data_dir);
    if (!d_root) return 0;
    while ((e_root = readdir(d_root)) != NULL) {
        if (e_root->d_name[0] == '.') continue;
        rc = snprintf(prefix_dir, sizeof prefix_dir, "%s/%s",
                      data_dir, e_root->d_name);
        if (rc <= 0 || (size_t)rc >= sizeof prefix_dir) continue;
        d_pref = opendir(prefix_dir);
        if (!d_pref) continue;
        while ((e_pref = readdir(d_pref)) != NULL) {
            if (e_pref->d_name[0] == '.') continue;
            rc = snprintf(file_path, sizeof file_path, "%s/%s",
                          prefix_dir, e_pref->d_name);
            if (rc <= 0 || (size_t)rc >= sizeof file_path) continue;
            if (stat(file_path, &st) == 0 && S_ISREG(st.st_mode)) {
                total += (unsigned long long)st.st_size;
            }
        }
        closedir(d_pref);
    }
    closedir(d_root);
    return total;
}

/* FD LIFETIME: all file handles opened in this function are closed
 * before it returns.  Do not hold fds across a disc unmount. */
static void
drain_disc(lcsas_disc_locator *l, const char *root)
{
    char data_dir[4096], prefix_dir[4096];
    char src[4096], dst[4096], cache_prefix[4096];
    DIR *d_root, *d_pref;
    struct dirent *e_root, *e_pref;
    struct stat st;
    int rc;
    int packs_copied = 0;
    int chunk_limit;
    int limit_reached = 0;

    if (!l->cache_dir || !root) return;
    /* If `root` IS the cache (or under it), nothing to drain. */
    if (path_under(root, l->cache_dir)) return;

    /* Refuse to drain when the cache's filesystem is critically
     * full (<10% free).  Prevents the silent-ENOSPC failure mode
     * that would abort the restore mid-flight.  One-line warning. */
    if (fs_critically_full(l->cache_dir, 10)) {
        static int warned = 0;
        if (!warned) {
            fprintf(stderr,
                    "[lcsas-restore] WARNING: cache filesystem at %s "
                    "is <10%% free; disabling further drains.  Set "
                    "LCSAS_PACK_CACHE_DIR= (empty) for the rest of "
                    "this run if disk pressure is critical.\n",
                    l->cache_dir);
            warned = 1;
        }
        return;
    }

    /* Soft warning at 1 GB total cache size.  Single-shot. */
    {
        static int big_warned = 0;
        if (!big_warned) {
            unsigned long long used = cache_bytes_used(l->cache_dir);
            if (used > (1ULL << 30)) {   /* 1 GiB */
                fprintf(stderr,
                        "[lcsas-restore] WARNING: pack cache at %s "
                        "is %.1f GB; set LCSAS_PACK_CACHE_DIR= (empty) "
                        "to disable further drains if disk is tight.\n",
                        l->cache_dir,
                        (double)used / (double)(1ULL << 30));
                big_warned = 1;
            }
        }
    }

    rc = snprintf(data_dir, sizeof data_dir, "%s/data", root);
    if (rc <= 0 || (size_t)rc >= sizeof data_dir) return;
    if (stat(data_dir, &st) != 0) return;

    fprintf(stderr,
            "[lcsas-restore] draining disc %s to cache "
            "(may take several minutes on optical media)...\n", root);

    chunk_limit = l->drain_chunk_packs;

    d_root = opendir(data_dir);
    if (!d_root) return;
    while (!limit_reached && (e_root = readdir(d_root)) != NULL) {
        if (e_root->d_name[0] == '.') continue;
        rc = snprintf(prefix_dir, sizeof prefix_dir,
                      "%s/%s", data_dir, e_root->d_name);
        if (rc <= 0 || (size_t)rc >= sizeof prefix_dir) {
            fprintf(stderr, "[lcsas-restore] path too long (>%lu bytes), skipping\n",
                    (unsigned long)sizeof prefix_dir);
            continue;
        }
        if (stat(prefix_dir, &st) != 0 || !S_ISDIR(st.st_mode)) continue;

        rc = snprintf(cache_prefix, sizeof cache_prefix,
                      "%s/data/%s", l->cache_dir, e_root->d_name);
        if (rc <= 0 || (size_t)rc >= sizeof cache_prefix) {
            fprintf(stderr, "[lcsas-restore] path too long (>%lu bytes), skipping\n",
                    (unsigned long)sizeof cache_prefix);
            continue;
        }
        if (mkdir_p(cache_prefix) != 0) continue;

        d_pref = opendir(prefix_dir);
        if (!d_pref) continue;
        while ((e_pref = readdir(d_pref)) != NULL) {
            if (e_pref->d_name[0] == '.') continue;
            rc = snprintf(src, sizeof src, "%s/%s",
                          prefix_dir, e_pref->d_name);
            if (rc <= 0 || (size_t)rc >= sizeof src) {
                fprintf(stderr, "[lcsas-restore] path too long (>%lu bytes), skipping\n",
                        (unsigned long)sizeof src);
                continue;
            }
            rc = snprintf(dst, sizeof dst, "%s/%s",
                          cache_prefix, e_pref->d_name);
            if (rc <= 0 || (size_t)rc >= sizeof dst) {
                fprintf(stderr, "[lcsas-restore] path too long (>%lu bytes), skipping\n",
                        (unsigned long)sizeof dst);
                continue;
            }
            /* Skip if already cached. */
            if (stat(dst, &st) == 0) continue;
            if (stat(src, &st) != 0 || !S_ISREG(st.st_mode)) continue;
            if (copy_file(src, dst) == 0) {
                packs_copied++;
                if (chunk_limit > 0 && packs_copied >= chunk_limit) {
                    limit_reached = 1;
                    break;
                }
            }
        }
        closedir(d_pref);
    }
    closedir(d_root);
    fprintf(stderr, "[lcsas-restore] disc drain complete.\n");
}

static int
scan_paths(lcsas_disc_locator *l, const char *hex,
           char *out_path, size_t cap)
{
    size_t i;
    /* Try the local cache first (fast path; never triggers a drain). */
    if (l->cache_dir
            && try_with_meta(l, l->cache_dir, hex, out_path, cap)) {
        return 1;
    }
    for (i = 0; i < l->n_paths; i++) {
        if (try_with_meta(l, l->search_paths[i], hex, out_path, cap)) {
            drain_disc(l, l->search_paths[i]);
            return 1;
        }
    }
    for (i = 0; i < l->n_discovered; i++) {
        if (try_with_meta(l, l->discovered_paths[i], hex, out_path, cap)) {
            drain_disc(l, l->discovered_paths[i]);
            return 1;
        }
    }
    return 0;
}

/*
 * Is the prompt polling for new discs while it waits (#455)?  Needs
 * auto-detect on, a positive interval, and a prompt stream backed by a
 * descriptor select() can take.  Never on Windows (see disc_locator.h).
 */
static int
auto_detect_active(const lcsas_disc_locator *l)
{
#ifndef _WIN32
    int fd;
    if (!l->auto_mount || l->poll_ms <= 0 || l->prompt_in == NULL)
        return 0;
    fd = fileno(l->prompt_in);
    return fd >= 0 && fd < FD_SETSIZE;
#else
    (void)l;
    return 0;
#endif
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
 * The prompt's lines for a catalog lookup that failed.  Blame a newer
 * LCSAS only for schema skew (#543).  A WAL catalog on read-only media,
 * or a damaged copy, used to get that wording too, sending the heir
 * after a newer meta disc when theirs was the right one.
 */
static void
print_catalog_error(FILE *o, lcsas_catalog *cat)
{
    if (lcsas_catalog_error_is_skew(cat)) {
        fputs("| (catalog could not be queried -- likely written by a     |\n", o);
        fputs("|  newer LCSAS; disc-swap hints unavailable. Insert discs  |\n", o);
        fputs("|  one at a time when prompted, or use the restore tools   |\n", o);
        fputs("|  from the NEWEST META disc you have.)                    |\n", o);
    } else {
        fputs("| (catalog could not be read, so no disc-swap hints.       |\n", o);
        fprintf(o, "|  Reason: %-47.47s |\n", lcsas_catalog_errmsg(cat));
        fputs("|  Insert discs one at a time when prompted.)              |\n", o);
    }
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
        int fr = lcsas_catalog_find_pack(cat, hex, &pk);
        if (fr == 0) {
            lcsas_catalog_volume vols[8];
            int n = lcsas_catalog_volumes_for_pack(
                        cat, pk.pack_id, vols, 8);
            if (n > 0) {
                fputs("| It is on any disc labelled:                              |\n", o);
                {
                    int j;
                    for (j = 0; j < n; j++) {
                        fprintf(o, "|   %-54s |\n", vols[j].label);
                    }
                }
            } else if (n == 0) {
                fputs("| (catalog has the pack, but lists no disc for it)         |\n", o);
            } else {
                fr = -1;
            }
        } else if (fr > 0) {
            fputs("| (catalog has no record of this pack ID)                  |\n", o);
        }
        /* A query error.  Do NOT claim "no record": we never asked. */
        if (fr < 0) print_catalog_error(o, cat);
    } else {
        fputs("| (no --catalog supplied; cannot suggest a disc)           |\n", o);
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
        fputs("| Single-drive restore: if your machine has only ONE       |\n", o);
        fputs("| optical drive, eject the META disc first, then           |\n", o);
        fputs("| insert the disc named above into the SAME drive.         |\n", o);
        fputs("|                                                          |\n", o);
    }
    fputs("| Insert the right disc and press ENTER to retry.          |\n", o);
    fputs("| Type 'q' then ENTER to abort.                            |\n", o);
    if (auto_detect_active(l)) {
        fputs("| (A disc that mounts by itself is found without ENTER.)   |\n", o);
    }
    fputs("+----------------------------------------------------------+\n", o);
    fputs("> ", o);
    fflush(o);
    l->mid_prompt_line = 1;
}

/*
 * Next byte of the heir's answer.  With fd >= 0 (the auto-detect
 * path) read(2) the descriptor directly: select() can't see bytes
 * already sitting in a FILE buffer, so mixing the two would strand
 * typed-ahead input -- a buffered 'q' would sit unseen while the poll
 * waited on an empty pipe.  fd < 0 reads through stdio as before.
 */
static int
next_answer_char(lcsas_disc_locator *l, int fd)
{
#ifndef _WIN32
    if (fd >= 0) {
        unsigned char ch;
        ssize_t n;
        do {
            n = read(fd, &ch, 1);
        } while (n < 0 && errno == EINTR);
        return n == 1 ? (int)ch : EOF;
    }
#else
    (void)fd;
#endif
    return fgetc(l->prompt_in);
}

/*
 * Read one line of the heir's answer (see next_answer_char for
 * `fd`).  Returns 0 if the heir pressed Enter, -1 if they typed 'q'/'Q'
 * to abort, or -1 on EOF.
 */
static int
read_response(lcsas_disc_locator *l, int fd)
{
    int c;
    int abort_req = 0;
    l->mid_prompt_line = 0;   /* the answer's newline ends the line */
    if (l->prompt_in == NULL) return -1;
    c = next_answer_char(l, fd);
    if (c == EOF) return -1;
    if (c == 'q' || c == 'Q') abort_req = 1;
    /* Consume the rest of the line. */
    while (c != EOF && c != '\n') {
        c = next_answer_char(l, fd);
    }
    return abort_req ? -1 : 0;
}

/*
 * Wait for the heir's answer to the prompt.  With auto-detect on
 * (#455), re-scan for the missing pack every poll_ms while waiting, so
 * a disc the OS mounts by itself resumes the restore with no ENTER.
 * Returns 1 if a re-scan found the pack (out_path filled), otherwise
 * read_response()'s 0 (ENTER) or -1 ('q' / EOF).
 */
static int
wait_for_answer_or_disc(lcsas_disc_locator *l, const char *hex,
                        char *out_path, size_t out_path_cap)
{
#ifndef _WIN32
    int fd;
    int announced;

    if (!auto_detect_active(l)) return read_response(l, -1);
    fd = fileno(l->prompt_in);
    for (;;) {
        fd_set rfds;
        struct timeval tv;
        int n;

        FD_ZERO(&rfds);
        FD_SET(fd, &rfds);
        tv.tv_sec  = l->poll_ms / 1000;
        tv.tv_usec = (l->poll_ms % 1000) * 1000;
        n = select(fd + 1, &rfds, NULL, NULL, &tv);
        if (n > 0) return read_response(l, fd);
        if (n < 0 && errno != EINTR) {
            /* Not a select()able descriptor after all.  Stop polling
             * for the rest of the run and block on the answer as
             * before; nothing has gone through stdio's buffer yet, so
             * switching to it loses no input. */
            l->auto_mount = 0;
            return read_response(l, -1);
        }
        announced = refresh_discovered(l);
        if (scan_paths(l, hex, out_path, out_path_cap)) return 1;
        if (announced) {
            /* A disc went in, but not the one with this pack.  Say so,
             * and put the "> " back under the line just printed. */
            fputs("(still not found -- check the disc label and try "
                  "again)\n> ", l->prompt_out);
            fflush(l->prompt_out);
            l->mid_prompt_line = 1;
        }
    }
#else
    (void)hex; (void)out_path; (void)out_path_cap;
    return read_response(l, -1);
#endif
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
     * re-picks the freshest catalog -- if the heir inserted the
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

    /* Before we block on the heir's input, make sure the process is not
     * holding the meta disc captive via its current working directory.
     * If cwd is anywhere under meta_disc, drop to "/" so the heir can
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
     * a disc the heir mounted between prompt and keypress.  The
     * freshest catalog is re-picked at each refresh so pack->label
     * hints stay accurate after a disc swap.  With auto-detect on,
     * the same refresh also runs every poll_ms while waiting. */
    for (;;) {
        int answer;

        l->misses++;
        print_prompt(l, hex);
        answer = wait_for_answer_or_disc(l, hex, out_path, out_path_cap);
        if (answer < 0) {
            fprintf(stderr, "[lcsas-restore] aborted by user\n");
            return -1;
        }
        if (answer > 0) {
            begin_line(l);
            fprintf(l->prompt_out,
                    "[lcsas-restore] new disc detected -- no ENTER "
                    "needed.\n"
                    "[lcsas-restore] found %.16s...; continuing.\n", hex);
            return 0;
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
