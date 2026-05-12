/*
 * main.c -- lcsas-restore CLI.
 *
 * Usage:
 *   lcsas-restore --repo <dir> --password-file <file> --target <dir>
 *                 [--snapshot <id|latest>] [--list-snapshots]
 *                 [--verbose]
 *
 * Phase 1 MVP: --repo points at an assembled restic repository (with
 * keys/, config, index/, snapshots/, data/ subdirs).  Phase 2 will add
 * --catalog <file> to drive volume-mounting and pack-fetching from a
 * SQLite catalog.
 */
#include "repo.h"
#include "tree.h"
#include "io.h"
#include "catalog.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void
usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s --repo DIR --password-file FILE --target DIR\n"
        "           [--snapshot ID|latest] [--list-snapshots] [--verbose]\n"
        "           [--catalog FILE]\n"
        "\n"
        "Restore a restic-format repository.  --repo must point to an\n"
        "assembled tree (with subdirs keys/, index/, snapshots/, data/).\n"
        "Use scripts/restore.sh to assemble an on-disc LCSAS repo first.\n"
        "\n"
        "--catalog opens the on-disc SQLite catalog (catalog.db) for\n"
        "informational logging; future versions will use it to prompt\n"
        "for disc swaps during multi-volume recovery.\n",
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
    int list_only = 0;
    int verbose = 0;
    int i;

    unsigned char *pw = NULL;
    size_t pw_len = 0;
    lcsas_master_key mk;
    lcsas_blob_index ix;
    lcsas_snapshot_list snaps;
    lcsas_catalog *catalog = NULL;
    long sidx;
    int rc = 1;
    char keys_dir[4096];

    for (i = 1; i < argc; i++) {
        int matched = 0;
        if (parse_arg("--repo",          argc, argv, &i, &repo_path) > 0) matched = 1;
        else if (parse_arg("--password-file", argc, argv, &i, &pwfile) > 0) matched = 1;
        else if (parse_arg("--target",   argc, argv, &i, &target) > 0) matched = 1;
        else if (parse_arg("--snapshot", argc, argv, &i, &snapshot_arg) > 0) matched = 1;
        else if (parse_arg("--catalog",  argc, argv, &i, &catalog_path) > 0) matched = 1;
        else if (strcmp(argv[i], "--list-snapshots") == 0) {
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

    if (catalog_path) {
        catalog = lcsas_catalog_open(catalog_path);
        if (!catalog) {
            fprintf(stderr, "WARN: cannot open catalog %s; continuing\n",
                    catalog_path);
        } else if (verbose) {
            lcsas_catalog_describe(catalog);
        }
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
                           target, target) != 0) {
        fprintf(stderr, "ERROR: tree restore failed\n");
        goto out;
    }

    fprintf(stderr, "[lcsas-restore] restore complete\n");
    rc = 0;

out:
    lcsas_blob_index_free(&ix);
    lcsas_snapshot_list_free(&snaps);
    if (catalog) lcsas_catalog_close(catalog);
    free(pw);
    return rc;
}
