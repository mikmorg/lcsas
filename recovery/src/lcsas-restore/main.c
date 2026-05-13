/*
 * main.c -- lcsas-restore CLI.
 *
 * Usage:
 *   lcsas-restore --repo <dir> --password-file <file> --target <dir>
 *                 [--snapshot <id|latest>] [--list-snapshots]
 *                 [--catalog <file>]
 *                 [--pack-search <dir>]  (repeatable)
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

static void
usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s --repo DIR --password-file FILE --target DIR\n"
        "           [--snapshot ID|latest] [--list-snapshots] [--verbose]\n"
        "           [--catalog FILE]\n"
        "           [--pack-search DIR ...]\n"
        "           [--interactive {auto|on|off}]\n"
        "           [--meta-disc DIR]\n"
        "\n"
        "Restore a restic-format repository.  --repo must point to an\n"
        "assembled tree (with subdirs keys/, index/, snapshots/, data/).\n"
        "Use scripts/restore.sh to assemble an on-disc LCSAS repo first.\n"
        "\n"
        "--pack-search adds additional mount points to scan when a pack\n"
        "file is missing from --repo/data/.  Repeatable.\n"
        "\n"
        "--interactive controls behaviour on missing pack:\n"
        "    auto (default)  prompt if stdin is a TTY\n"
        "    on              always prompt + retry\n"
        "    off             always fail fast (old behaviour)\n"
        "\n"
        "--catalog opens the on-disc SQLite catalog (catalog.db) for\n"
        "informational logging at startup and for volume-label hints\n"
        "in interactive disc-swap prompts.\n"
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
    int list_only = 0;
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
        } else if (strcmp(argv[i], "--list-snapshots") == 0) {
            list_only = 1; matched = 1;
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
    if (!list_only && !target) {
        usage(argv[0]);
        return 2;
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
    if (!meta_disc) {
        /* Allow the env var as a fallback when scripts can't pass it
         * through (e.g. minimal initramfs sh that drops args). */
        const char *env = getenv("LCSAS_META_DISC");
        if (env && *env) meta_disc = env;
    }
    if (meta_disc) lcsas_disc_locator_set_meta(&locator, meta_disc);

    if (verbose) {
        fprintf(stderr,
                "[lcsas-restore] interactive=%s pack-search-dirs=%zu meta-disc=%s\n",
                interactive ? "on" : "off", n_pack_search,
                meta_disc ? meta_disc : "(none)");
    }

    if (lcsas_read_file(pwfile, &pw, &pw_len) != 0) {
        fprintf(stderr, "cannot read password file: %s\n", pwfile);
        return 1;
    }
    /* Strip trailing newlines / CR. */
    while (pw_len > 0 && (pw[pw_len - 1] == '\n' || pw[pw_len - 1] == '\r')) {
        pw[--pw_len] = '\0';
    }

    snprintf(keys_dir, sizeof keys_dir, "%s/keys", repo_path);
    if (lcsas_repo_load_keys_dir(keys_dir, pw, pw_len, &mk) != 0) {
        fprintf(stderr, "ERROR: could not decrypt any key file (wrong password?)\n");
        free(pw);
        return 1;
    }
    if (verbose) fprintf(stderr, "[lcsas-restore] master key loaded\n");

    lcsas_blob_index_init(&ix);
    if (lcsas_repo_load_index(repo_path, &mk, &ix) != 0) {
        fprintf(stderr, "ERROR: index load failed\n");
        goto out;
    }
    if (verbose)
        fprintf(stderr, "[lcsas-restore] indexed %lu blobs\n",
                (unsigned long)ix.count);

    lcsas_snapshot_list_init(&snaps);
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

    if (lcsas_tree_restore(repo_path, &mk, &ix,
                           snaps.items[sidx].tree_id_hex,
                           target, target, &locator) != 0) {
        fprintf(stderr, "ERROR: tree restore failed\n");
        goto out;
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
    if (catalog) lcsas_catalog_close(catalog);
    free(pw);
    return rc;
}
