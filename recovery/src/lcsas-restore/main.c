/*
 * main.c -- lcsas-restore CLI.
 *
 * Usage:
 *   lcsas-restore --repo <dir> --password-file <file> --target <dir>
 *                 [--snapshot <id|latest>] [--list-snapshots]
 *                 [--catalog <file>]
 *                 [--pack-search <dir>]   (repeatable)
 *                 [--mount-parent <dir>]  (repeatable; see below)
 *                 [--interactive {auto|on|off}]
 *                 [--verbose]
 *
 * --repo points at an assembled restic repository (keys/, config,
 * index/, snapshots/, data/).
 *
 * --pack-search adds additional mount points to scan when a pack
 * file is not in --repo/data/.  Repeatable.  See
 * recovery/docs/MULTI_DISC_DESIGN.txt.
 *
 * --mount-parent names directories whose children are scanned on
 * every retry (so a disc auto-mounted AFTER the binary started is
 * found).  Defaults: $LCSAS_MOUNT_DIRS (colon-separated) or
 * /Volumes:/media:/mnt:/run/media.
 *
 * --interactive controls behaviour when a pack is still missing:
 *   auto (default)  prompt if stdin is a TTY, else fail fast
 *   on              always prompt and retry
 *   off             always fail fast
 *
 * --catalog opens the on-disc SQLite catalog (catalog.db); used both
 * for informational logging at startup and for volume-label hints
 * in interactive disc-swap prompts.
 *
 * --meta-disc identifies the recovery medium's mount point.  The
 * locator excludes that path from pack searches and refuses to keep
 * the cwd inside it, so a user with a SINGLE optical drive can eject
 * the recovery disc and reuse the same drive for data discs.
 */
#include "repo.h"
#include "tree.h"
#include "lcsas_io.h"
#include "catalog.h"
#include "disc_locator.h"
#include "posix_compat.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef _WIN32
#  include <unistd.h>      /* isatty */
#else
#  include <io.h>
#  define isatty _isatty
#  define STDIN_FILENO 0
#endif

#define MAX_PACK_SEARCH 64
#define MAX_MOUNT_PARENTS 32

/* Default mount parents scanned on every retry when LCSAS_MOUNT_DIRS
 * is unset.  Mirrors the restore.sh shell-side default and the
 * design doc (MULTI_DISC_DESIGN.txt §"DRIVER SCRIPT CHANGES"). */
static const char *DEFAULT_MOUNT_PARENTS[] = {
    "/Volumes",
    "/media",
    "/mnt",
    "/run/media"
};
#define N_DEFAULT_MOUNT_PARENTS \
    (sizeof DEFAULT_MOUNT_PARENTS / sizeof DEFAULT_MOUNT_PARENTS[0])

static void
usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s --repo DIR --password-file FILE --target DIR\n"
        "           [--snapshot ID|latest] [--list-snapshots] [--verbose]\n"
        "           [--catalog FILE]\n"
        "           [--list-pending-packs]\n"
        "           [--pack-search DIR ...]\n"
        "           [--mount-parent DIR ...]\n"
        "           [--interactive {auto|on|off}]\n"
        "           [--meta-disc DIR]\n"
        "\n"
        "Restore a restic-format repository.  --repo must point to an\n"
        "assembled tree (with subdirs keys/, index/, snapshots/, data/).\n"
        "Use scripts/restore.sh to assemble an on-disc LCSAS repo first.\n"
        "\n"
        "--list-pending-packs requires --catalog.  Prints a summary of\n"
        "which discs you will need and how many packs each holds, then\n"
        "exits without performing any restore:\n"
        "    Pending packs by disc:\n"
        "      LCSAS_X: 4 packs (12.3 MB)\n"
        "      LCSAS_Y: 2 packs (8.1 MB)\n"
        "    Total: 6 packs, 20.4 MB across 2 discs.\n"
        "\n"
        "--pack-search adds additional mount points to scan when a pack\n"
        "file is missing from --repo/data/.  Repeatable.\n"
        "\n"
        "--mount-parent names a directory whose children may be newly-\n"
        "inserted optical discs (e.g. /Volumes, /media, /mnt).  On every\n"
        "missing-pack retry the locator re-enumerates each parent so a\n"
        "disc inserted AFTER the binary started is discovered.  Multiple\n"
        "--mount-parent flags may be supplied.  When none are given, the\n"
        "binary reads colon-separated $LCSAS_MOUNT_DIRS, then falls back\n"
        "to /Volumes:/media:/mnt:/run/media.\n"
        "\n"
        "--interactive controls behaviour on missing pack:\n"
        "    auto (default)  prompt if stdin is a TTY\n"
        "    on              always prompt + retry\n"
        "    off             always fail fast (old behaviour)\n"
        "\n"
        "--catalog opens the on-disc SQLite catalog (catalog.db) for\n"
        "informational logging at startup and for volume-label hints\n"
        "in interactive disc-swap prompts.  On each retry the locator\n"
        "also probes any newly-mounted disc for a catalog.db and uses\n"
        "the freshest one it can open for hash->label resolution.\n"
        "\n"
        "--meta-disc names the mount point of the recovery / meta disc.\n"
        "The locator excludes that path from pack searches and drops\n"
        "any cwd inside it before prompting, so a user with one optical\n"
        "drive can eject the recovery disc and reuse the same drive\n"
        "for data discs.  Driver scripts set this automatically.\n",
        argv0);
}

static int
parse_arg(const char *flag, int argc, char **argv, int *i,
          const char **out)
{
    if (strcmp(argv[*i], flag) == 0) {
        if (*i + 1 >= argc) {
            fprintf(stderr, "missing value for %s\n", flag);
            return -1;
        }
        *out = argv[++(*i)];
        return 1;
    }
    return 0;
}

int
main(int argc, char **argv)
{
    const char *repo_path = NULL;
    const char *pwfile = NULL;
    const char *target = NULL;
    const char *snapshot_arg = NULL;
    const char *catalog_path = NULL;
    const char *interactive_arg = "auto";
    const char *meta_disc = NULL;
    const char *pack_search[MAX_PACK_SEARCH];
    size_t n_pack_search = 0;
    const char *mount_parents[MAX_MOUNT_PARENTS];
    size_t n_mount_parents = 0;
    char *mount_parents_buf = NULL;  /* owns the splittable copy of $LCSAS_MOUNT_DIRS */
    int list_only = 0;
    int list_pending_packs = 0;
    int verbose = 0;
    int i;

    unsigned char *pw = NULL;
    size_t pw_len = 0;
    lcsas_master_key mk;
    lcsas_blob_index ix;
    lcsas_snapshot_list snaps;
    lcsas_catalog *catalog = NULL;
    lcsas_disc_locator locator;
    int interactive = 0;
    long sidx;
    int rc = 1;
    char keys_dir[4096];

    for (i = 1; i < argc; i++) {
        int matched = 0;
        const char *tmp = NULL;
        if (parse_arg("--repo",          argc, argv, &i, &repo_path) > 0) matched = 1;
        else if (parse_arg("--password-file", argc, argv, &i, &pwfile) > 0) matched = 1;
        else if (parse_arg("--target",   argc, argv, &i, &target) > 0) matched = 1;
        else if (parse_arg("--snapshot", argc, argv, &i, &snapshot_arg) > 0) matched = 1;
        else if (parse_arg("--catalog",  argc, argv, &i, &catalog_path) > 0) matched = 1;
        else if (parse_arg("--interactive", argc, argv, &i, &interactive_arg) > 0) matched = 1;
        else if (parse_arg("--meta-disc", argc, argv, &i, &meta_disc) > 0) matched = 1;
        else if (parse_arg("--pack-search", argc, argv, &i, &tmp) > 0) {
            if (n_pack_search >= MAX_PACK_SEARCH) {
                fprintf(stderr, "too many --pack-search entries (max %d)\n",
                        MAX_PACK_SEARCH);
                return 2;
            }
            pack_search[n_pack_search++] = tmp;
            matched = 1;
        } else if (parse_arg("--mount-parent", argc, argv, &i, &tmp) > 0) {
            if (n_mount_parents >= MAX_MOUNT_PARENTS) {
                fprintf(stderr, "too many --mount-parent entries (max %d)\n",
                        MAX_MOUNT_PARENTS);
                return 2;
            }
            mount_parents[n_mount_parents++] = tmp;
            matched = 1;
        } else if (strcmp(argv[i], "--list-snapshots") == 0) {
            list_only = 1; matched = 1;
        } else if (strcmp(argv[i], "--list-pending-packs") == 0) {
            list_pending_packs = 1; matched = 1;
        } else if (strcmp(argv[i], "--verbose") == 0
                || strcmp(argv[i], "-v") == 0) {
            verbose = 1; matched = 1;
        } else if (strcmp(argv[i], "--help") == 0
                || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 0;
        }
        if (!matched) {
            fprintf(stderr, "unknown argument: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }

    if (!repo_path || !pwfile) {
        usage(argv[0]);
        return 2;
    }
    if (!list_only && !list_pending_packs && !target) {
        usage(argv[0]);
        return 2;
    }

    /* --list-pending-packs: print disc plan from catalog and exit.
     * Catalog is required; no key load, no restore. */
    if (list_pending_packs) {
        lcsas_catalog *cat = NULL;
        int lpp_rc;
        if (!catalog_path) {
            fprintf(stderr,
                    "ERROR: --list-pending-packs requires --catalog\n");
            return 1;
        }
        cat = lcsas_catalog_open(catalog_path);
        if (!cat) {
            fprintf(stderr,
                    "ERROR: cannot open catalog %s\n", catalog_path);
            return 1;
        }
        lpp_rc = lcsas_catalog_print_pending_packs(cat);
        lcsas_catalog_close(cat);
        return lpp_rc == 0 ? 0 : 1;
    }

    /* Resolve --interactive mode. */
    if (strcmp(interactive_arg, "on") == 0) {
        interactive = 1;
    } else if (strcmp(interactive_arg, "off") == 0) {
        interactive = 0;
    } else { /* auto */
        interactive = isatty(STDIN_FILENO) ? 1 : 0;
    }

    if (catalog_path) {
        catalog = lcsas_catalog_open(catalog_path);
        if (!catalog) {
            fprintf(stderr, "WARN: cannot open catalog %s; continuing\n",
                    catalog_path);
        } else if (verbose) {
            lcsas_catalog_describe(catalog);
        }
    }

    lcsas_disc_locator_init(&locator, pack_search, n_pack_search,
                            catalog, interactive);
    /* Pin the caller's catalog mtime as the floor so the locator
     * never swaps to an OLDER catalog discovered on a mounted disc. */
    if (catalog && catalog_path) {
        lcsas_disc_locator_set_catalog_floor(&locator, catalog_path);
    }
    if (!meta_disc) {
        /* Allow the env var as a fallback when scripts can't pass it
         * through (e.g. minimal initramfs sh that drops args). */
        const char *env = getenv("LCSAS_META_DISC");
        if (env && *env) meta_disc = env;
    }
    if (meta_disc) lcsas_disc_locator_set_meta(&locator, meta_disc);

    /* Opt-in opportunistic pack cache.  When LCSAS_PACK_CACHE_DIR is
     * set, the locator copies the rest of each disc's data/ subtree
     * into the cache on every successful pack hit so subsequent
     * packs from the same disc resolve from local storage.  Trades
     * disk space for swap reduction; unset by default. */
    {
        const char *cache_env = getenv("LCSAS_PACK_CACHE_DIR");
        if (cache_env && *cache_env) {
            lcsas_disc_locator_set_cache_dir(&locator, cache_env);
            if (verbose) {
                fprintf(stderr,
                        "[lcsas-restore] opportunistic pack cache: %s\n",
                        cache_env);
            }
        }
    }

    /* Determine the mount-parent list scanned on each retry to pick up
     * freshly-inserted discs.  Precedence (highest first):
     *   1. one or more --mount-parent CLI flags
     *   2. $LCSAS_MOUNT_DIRS colon-separated env var
     *   3. compiled-in defaults (/Volumes, /media, /mnt, /run/media)
     * The driver script reuses the same env var, so the C and shell
     * sides stay in sync. */
    if (n_mount_parents == 0) {
        const char *env = getenv("LCSAS_MOUNT_DIRS");
        if (env && *env) {
            size_t len = strlen(env);
            char *p, *next;
            mount_parents_buf = (char *)malloc(len + 1);
            if (mount_parents_buf) {
                memcpy(mount_parents_buf, env, len + 1);
                p = mount_parents_buf;
                while (p && *p && n_mount_parents < MAX_MOUNT_PARENTS) {
                    next = strchr(p, ':');
                    if (next) { *next = '\0'; next++; }
                    if (*p) mount_parents[n_mount_parents++] = p;
                    p = next;
                }
            }
        } else {
            size_t k;
            for (k = 0; k < N_DEFAULT_MOUNT_PARENTS
                     && n_mount_parents < MAX_MOUNT_PARENTS; k++) {
                mount_parents[n_mount_parents++] = DEFAULT_MOUNT_PARENTS[k];
            }
        }
    }
    lcsas_disc_locator_set_mount_parents(&locator, mount_parents,
                                         n_mount_parents);

    if (verbose) {
        fprintf(stderr,
                "[lcsas-restore] interactive=%s pack-search-dirs=%zu "
                "mount-parents=%zu meta-disc=%s\n",
                interactive ? "on" : "off", n_pack_search,
                n_mount_parents,
                meta_disc ? meta_disc : "(none)");
    }

    lcsas_blob_index_init(&ix);
    lcsas_snapshot_list_init(&snaps);

    if (lcsas_read_file(pwfile, &pw, &pw_len) != 0) {
        fprintf(stderr, "cannot read password file: %s\n", pwfile);
        goto out;
    }
    /* Strip trailing newlines / CR. */
    while (pw_len > 0 && (pw[pw_len - 1] == '\n' || pw[pw_len - 1] == '\r')) {
        pw[--pw_len] = '\0';
    }

    snprintf(keys_dir, sizeof keys_dir, "%s/keys", repo_path);
    if (lcsas_repo_load_keys_dir(keys_dir, pw, pw_len, &mk) != 0) {
        fprintf(stderr, "ERROR: could not decrypt any key file (wrong password?)\n");
        goto out;
    }
    if (verbose) fprintf(stderr, "[lcsas-restore] master key loaded\n");

    if (lcsas_repo_load_index(repo_path, &mk, &ix) != 0) {
        fprintf(stderr, "ERROR: index load failed\n");
        goto out;
    }
    if (verbose)
        fprintf(stderr, "[lcsas-restore] indexed %lu blobs\n",
                (unsigned long)ix.count);

    if (lcsas_repo_load_snapshots(repo_path, &mk, &snaps) != 0) {
        fprintf(stderr, "ERROR: snapshot load failed\n");
        goto out;
    }
    if (verbose)
        fprintf(stderr, "[lcsas-restore] loaded %lu snapshot(s)\n",
                (unsigned long)snaps.count);

    if (list_only || !target) {
        size_t k;
        printf("ID                                               TIME                          PATH\n");
        for (k = 0; k < snaps.count; k++) {
            printf("%.8s  %-28s  %s\n",
                   snaps.items[k].file_name,
                   snaps.items[k].time,
                   snaps.items[k].first_path);
        }
        rc = 0;
        goto out;
    }

    if (snapshot_arg == NULL || strcmp(snapshot_arg, "latest") == 0) {
        sidx = lcsas_snapshot_latest(&snaps);
    } else {
        sidx = lcsas_snapshot_find(&snaps, snapshot_arg);
    }
    if (sidx < 0) {
        fprintf(stderr, "ERROR: snapshot not found: %s\n",
                snapshot_arg ? snapshot_arg : "(none -- empty repo)");
        goto out;
    }

    fprintf(stderr, "[lcsas-restore] restoring snapshot %.12s -> %s\n",
            snaps.items[sidx].file_name, target);

    if (lcsas_mkdir_p(target) != 0) {
        fprintf(stderr, "ERROR: cannot create target dir %s\n", target);
        goto out;
    }

    {
        lcsas_progress progress;
        int tree_rc;
        /* total_blob_hint is the loaded index size -- an upper bound
         * on what this snapshot can reference.  We surface that
         * explicitly so the operator isn't misled by the denominator. */
        lcsas_progress_init(&progress, (unsigned long long)ix.count);
        fprintf(stderr,
                "[lcsas-restore] progress: 0/%llu blobs, 0 MB"
                " (denominator is index size, not snapshot subset)\n",
                progress.total_blob_hint);

        tree_rc = lcsas_tree_restore(repo_path, &mk, &ix,
                                     snaps.items[sidx].tree_id_hex,
                                     target, target, &locator,
                                     &progress);
        lcsas_progress_finish(&progress);
        if (tree_rc != 0) {
            fprintf(stderr, "ERROR: tree restore failed\n");
            goto out;
        }
    }

    fprintf(stderr, "[lcsas-restore] restore complete\n");
    if (locator.misses > 0) {
        fprintf(stderr,
                "[lcsas-restore] %lu disc swap(s) handled during restore\n",
                locator.misses);
    }
    rc = 0;

out:
    lcsas_blob_index_free(&ix);
    lcsas_snapshot_list_free(&snaps);
    lcsas_disc_locator_free(&locator);
    if (catalog) lcsas_catalog_close(catalog);
    free(mount_parents_buf);
    free(pw);
    return rc;
}
