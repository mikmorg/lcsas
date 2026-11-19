/*
 * main.c -- lcsas-restore CLI.
 *
 * Usage:
 *   lcsas-restore --repo <dir> --password-file <file> --target <dir>
 *                 [--snapshot <id|latest>] [--list-snapshots]
 *                 [--catalog <file>] [--list-pending-packs [--repo-id <id>]]
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
 * found).  Defaults: $LCSAS_MOUNT_DIRS (colon-separated; on Windows
 * semicolon-separated) or /Volumes:/media:/mnt:/run/media.  Windows
 * has no default parents: there the root of every optical drive is
 * searched on each retry instead.
 *
 * --interactive controls behaviour when a pack is still missing:
 *   auto (default)  prompt if stdin is a TTY, else fail fast
 *   on              always prompt and retry
 *   off             always fail fast
 *
 * --catalog opens a disc catalog (catalog.db, SQLite); used both
 * for informational logging at startup and for disc-label hints
 * in interactive disc-swap prompts.
 *
 * --meta-disc identifies the meta disc's mount point.  The
 * locator excludes that path from pack searches and refuses to keep
 * the cwd inside it, so an heir with a SINGLE optical drive can eject
 * the meta disc and reuse the same drive for data discs.
 *
 * Exit status: 0 restored (or listed); 1 failed; 2 usage error;
 * 4 finished, but some items were not restored (each named on stderr
 * and in RESTORE_FAILURES.txt); 74 the target disk is full (free space, then run the same command
 * again); 75 the reader typed q at the disc prompt (run the same
 * command again to carry on); 77 wrong password.
 */
#ifdef _WIN32
/* A system header: it goes before posix_compat.h's shim macros. */
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#endif
#include "repo.h"
#include "tree.h"
#include "json_q.h"
#include "lcsas_io.h"
#include "catalog.h"
#include "disc_locator.h"
#include "posix_compat.h"

#include <errno.h>
#include <limits.h>
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

/* Distinct exit status for "a key file POSITIVELY rejected the given
 * password" (Poly1305 MAC mismatch -- issue #384).  A wrong password is
 * TERMINAL: every recovery tier reads the same keys with the same
 * password, so restore.sh / restore.bat treat this code as "stop, do not
 * fall through to the next tier" — which both avoids a pointless retry
 * and prevents a later tier from writing a partial tree before it too
 * rejects the password.  Setup-shaped key failures (missing/unreadable
 * keys dir, no candidate files, OOM) deliberately exit 1, NOT 77, so the
 * cascade is free to try other tiers when the password was never really
 * tested.  77 is the BSD sysexits.h EX_NOPERM value, kept clear of the
 * binary's other statuses (0 ok, 1 generic failure, 2 usage). */
#define EXIT_WRONG_PASSWORD 77

/* Distinct exit status for "the reader typed q at the disc-swap prompt"
 * (or its input ended there).  Nothing is wrong with the discs or the
 * password; the reader paused.  Driver scripts keep their files and
 * say how to carry on, rather than treating it as a failure or trying
 * another tier.  75 is sysexits.h EX_TEMPFAIL ("try again later"). */
#define EXIT_READER_QUIT 75

/* Distinct exit status for "the target file system is full" (ENOSPC or
 * EDQUOT while writing the restore).  Nothing is wrong with the discs
 * or the password: free some space, or pick a bigger disk, and run the
 * same command again (finished files are skipped).  Driver scripts can
 * say so instead of trying another tier, which would fill the same
 * disk.  74 is sysexits.h EX_IOERR; defined here, as sysexits.h is not
 * on every system this is built for.  Tier 3 uses the same code. */
#define EXIT_DISK_FULL 74

/* Distinct exit status for "the restore finished, but some items could
 * not be restored" (#872): each is named in a NOT RESTORED line and
 * listed in RESTORE_FAILURES.txt (#909: a missing or damaged pack too).
 * Exit 1 stays "failed" (the walk stopped early: run again).  restore.sh and
 * restore.bat use 4 for this outcome too, so they pass it on. */
#define EXIT_NOT_ALL_RESTORED 4

#define MAX_PACK_SEARCH 64
#define MAX_MOUNT_PARENTS 32

/* LCSAS_MOUNT_DIRS splits on ':' -- except on Windows, where ':' is in
 * every drive letter, so it splits on ';' like Windows PATH. */
#ifdef _WIN32
#  define MOUNT_DIRS_SEP ';'
#else
#  define MOUNT_DIRS_SEP ':'
#endif

#ifndef _WIN32
/* Default mount parents scanned on every retry when LCSAS_MOUNT_DIRS
 * is unset.  Mirrors the restore.sh shell-side default and the
 * design doc (MULTI_DISC_DESIGN.txt §"DRIVER SCRIPT CHANGES").
 * Windows has none: the locator searches every optical drive's root
 * there instead, and these paths would only be listed, never found. */
static const char *DEFAULT_MOUNT_PARENTS[] = {
    "/Volumes",
    "/media",
    "/mnt",
    "/run/media"
};
#define N_DEFAULT_MOUNT_PARENTS \
    (sizeof DEFAULT_MOUNT_PARENTS / sizeof DEFAULT_MOUNT_PARENTS[0])
#endif

static void
usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s --repo DIR --password-file FILE --target DIR\n"
        "           [--snapshot ID|latest] [--list-snapshots] [--verbose]\n"
        "           [--catalog FILE]\n"
        "           [--list-pending-packs [--repo-id ID]]\n"
        "           [--pack-search DIR ...]\n"
        "           [--mount-parent DIR ...]\n"
        "           [--interactive {auto|on|off}]\n"
        "           [--auto-mount {on|off}]\n"
        "           [--meta-disc DIR]\n"
        "\n"
        "Restore a restic-format repository.  --repo must point to an\n"
        "assembled tree (with subdirs keys/, index/, snapshots/, data/).\n"
        "Use scripts/restore.sh to assemble one from LCSAS discs first.\n"
        "\n"
        "--list-pending-packs requires --catalog.  Prints a summary of\n"
        "which discs you will need and how many packs each holds, then\n"
        "exits without performing any restore:\n"
        "    Pending packs by disc:\n"
        "      LCSAS_X: 4 packs (12.3 MB)\n"
        "      LCSAS_Y: 2 packs (8.1 MB)\n"
        "    Total: 6 packs, 20.4 MB across 2 discs.\n"
        "With --repo-id ID it counts only that repository's packs (its\n"
        "id is the folder name under metadata/ on a data disc).\n"
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
        "to /Volumes:/media:/mnt:/run/media.  On Windows the variable is\n"
        "semicolon-separated, there is no fallback, and every optical\n"
        "drive (D:\\ and so on) is searched on each retry anyway.\n"
        "\n"
        "--interactive controls behaviour on missing pack:\n"
        "    auto (default)  prompt if stdin is a TTY\n"
        "    on              always prompt + retry\n"
        "    off             always fail fast (old behaviour)\n"
        "\n"
        "--auto-mount on (default) re-scans every 2 s while the disc-swap\n"
        "prompt waits, so a disc your desktop mounts by itself resumes\n"
        "the restore with no ENTER; ENTER and 'q' still work.  off waits\n"
        "for ENTER only.  Linux/macOS; on Windows the prompt always waits\n"
        "for ENTER.\n"
        "\n"
        "--catalog opens a disc catalog (catalog.db, SQLite) for\n"
        "informational logging at startup and for disc-label hints\n"
        "in interactive disc-swap prompts.  On each retry the locator\n"
        "also probes any newly-mounted disc for a catalog.db and uses\n"
        "the freshest one it can open to name the disc for each pack.\n"
        "\n"
        "--meta-disc names the mount point of the meta disc.\n"
        "The locator excludes that path from pack searches and drops\n"
        "any cwd inside it before prompting, so an heir with one optical\n"
        "drive can eject the meta disc and reuse the same drive\n"
        "for data discs.  Driver scripts set this automatically.\n"
        "\n"
        "LCSAS_PACK_CACHE_DIR names a folder to copy each data disc\n"
        "into once it is found, so no disc has to go back in later.\n"
        "\n"
        "Exit status:\n"
        "    0   restored (or listed)\n"
        "    1   failed\n"
        "    2   usage error\n"
        "    4   finished, but some items were not restored (each one\n"
        "        is named in a NOT RESTORED line and listed in\n"
        "        RESTORE_FAILURES.txt)\n"
        "    74  the disk you restore to is full; free some space, then\n"
        "        run the same command again\n"
        "    75  you typed q at the disc prompt; run the same command\n"
        "        again to carry on (files already restored are kept)\n"
        "    77  wrong password\n",
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
    const char *auto_mount_arg = "on";
    const char *meta_disc = NULL;
    const char *pending_repo_id = NULL;
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

#ifdef _WIN32
    /* Probing an empty optical drive must fail quietly, not raise the
     * "There is no disk in the drive" dialog box and wait for a click. */
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX);
    /* #619: every path is UTF-8 inside; the C library's argv is in the
     * ANSI code page, which cannot hold every name a folder may have.
     * Take the arguments from the UTF-16 command line instead. */
    if (lcsas_w_utf8_argv(&argc, &argv) != 0) {
        fprintf(stderr, "ERROR: cannot read the command line as "
                        "Unicode\n");
        return 1;
    }
#endif

    for (i = 1; i < argc; i++) {
        int matched = 0;
        const char *tmp = NULL;
        if (parse_arg("--repo",          argc, argv, &i, &repo_path) > 0) matched = 1;
        else if (parse_arg("--password-file", argc, argv, &i, &pwfile) > 0) matched = 1;
        else if (parse_arg("--target",   argc, argv, &i, &target) > 0) matched = 1;
        else if (parse_arg("--snapshot", argc, argv, &i, &snapshot_arg) > 0) matched = 1;
        else if (parse_arg("--catalog",  argc, argv, &i, &catalog_path) > 0) matched = 1;
        else if (parse_arg("--interactive", argc, argv, &i, &interactive_arg) > 0) matched = 1;
        else if (parse_arg("--auto-mount", argc, argv, &i, &auto_mount_arg) > 0) matched = 1;
        else if (parse_arg("--meta-disc", argc, argv, &i, &meta_disc) > 0) matched = 1;
        else if (parse_arg("--repo-id", argc, argv, &i, &pending_repo_id) > 0) matched = 1;
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
    if (strcmp(auto_mount_arg, "on") != 0
            && strcmp(auto_mount_arg, "off") != 0) {
        fprintf(stderr, "--auto-mount takes on or off, not '%s'\n",
                auto_mount_arg);
        return 2;
    }
    if (!list_only && !list_pending_packs && !target) {
        usage(argv[0]);
        return 2;
    }
    if (pending_repo_id && !list_pending_packs) {
        fprintf(stderr, "--repo-id goes with --list-pending-packs\n");
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
        lpp_rc = lcsas_catalog_print_pending_packs(cat, pending_repo_id);
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
    lcsas_disc_locator_set_auto_mount(&locator,
                                      strcmp(auto_mount_arg, "on") == 0);

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
     *   2. $LCSAS_MOUNT_DIRS colon-separated env var (';' on Windows)
     *   3. compiled-in defaults (/Volumes, /media, /mnt, /run/media;
     *      none on Windows, which searches its optical drives instead)
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
                    next = strchr(p, MOUNT_DIRS_SEP);
                    if (next) { *next = '\0'; next++; }
                    if (*p) mount_parents[n_mount_parents++] = p;
                    p = next;
                }
            }
        } else {
#ifndef _WIN32
            size_t k;
            for (k = 0; k < N_DEFAULT_MOUNT_PARENTS
                     && n_mount_parents < MAX_MOUNT_PARENTS; k++) {
                mount_parents[n_mount_parents++] = DEFAULT_MOUNT_PARENTS[k];
            }
#endif
        }
    }
    lcsas_disc_locator_set_mount_parents(&locator, mount_parents,
                                         n_mount_parents);

    if (verbose) {
        fprintf(stderr,
                "[lcsas-restore] interactive=%s auto-mount=%s "
                "pack-search-dirs=%zu mount-parents=%zu meta-disc=%s\n",
                interactive ? "on" : "off", auto_mount_arg, n_pack_search,
                n_mount_parents,
                meta_disc ? meta_disc : "(none)");
    }

    lcsas_blob_index_init(&ix);
    lcsas_snapshot_list_init(&snaps);

    if (lcsas_read_file(pwfile, &pw, &pw_len) != 0) {
        fprintf(stderr, "cannot read password file: %s\n", pwfile);
        goto out;
    }
    /* Issue #641 -- the password is the file's first line, read as
     * rustic reads it: every byte before the first LF, less one CR at
     * its end.  Spaces are kept.  Tier 3 and `lcsas key` use the same
     * rule, so a file of two lines gives every reader one password. */
    {
        size_t k = 0;
        while (k < pw_len && pw[k] != '\n') k++;
        if (k > 0 && pw[k - 1] == '\r') k--;
        pw_len = k;
        pw[pw_len] = '\0';
    }

    snprintf(keys_dir, sizeof keys_dir, "%s/keys", repo_path);
    {
        int krc = lcsas_repo_load_keys_dir(keys_dir, pw, pw_len, &mk);
        if (krc == LCSAS_REPO_ERR_WRONG_PASSWORD) {
            /* A well-formed key file positively rejected the password
             * (MAC mismatch).  Terminal -- see restore.sh / .bat (#384). */
            fprintf(stderr, "ERROR: could not decrypt any key file (wrong password?)\n");
            rc = EXIT_WRONG_PASSWORD;
            goto out;
        }
        if (krc != 0) {
            /* Not a password verdict: no key file could be tested at
             * all -- the directory is missing/unreadable, holds no key
             * files, OR every key file is corrupt/truncated (fails
             * before its MAC check), or an allocation failed.  Generic
             * failure (1) so the cascade may still try another tier.
             * Name all the plausible causes so a heir with a present
             * but bit-rotted key file is not misdirected to a
             * directory-permissions problem (#384 review round 3). */
            fprintf(stderr, "ERROR: could not read or decrypt any repository "
                            "key file under %s\n"
                            "       (keys directory missing/unreadable, no "
                            "key files, or a key file is corrupt)\n",
                    keys_dir);
            goto out;
        }
    }
    if (verbose) fprintf(stderr, "[lcsas-restore] master key loaded\n");

    /* T1C-01: optional ceiling on adaptive JSON token-buffer memory.
     * Defaults to 256 MiB (see lcsas_json_max_tok_bytes); LCSAS_MAX_JSON_MIB
     * overrides it.  This is both a test seam (clamp it tiny to force the
     * "too large for tier-1" path) and an escape hatch on memory-starved
     * 32-bit hosts. */
    {
        const char *env = getenv("LCSAS_MAX_JSON_MIB");
        if (env && *env) {
            char *endp = NULL;
            unsigned long mib = strtoul(env, &endp, 10);
            if (endp && *endp == '\0' && mib > 0) {
                lcsas_json_max_tok_bytes = (size_t)mib * 1024UL * 1024UL;
                if (verbose) {
                    fprintf(stderr,
                            "[lcsas-restore] JSON token-buffer ceiling: "
                            "%lu MiB\n", mib);
                }
            } else {
                fprintf(stderr,
                        "WARNING: ignoring invalid LCSAS_MAX_JSON_MIB=%s "
                        "(want a positive integer)\n", env);
            }
        }
    }

    /* T1C-04: optional override of the tree-walk recursion-depth cap.
     * Defaults to 1000 (see lcsas_tree_max_depth); a deeper tree fails
     * loud rather than SIGSEGV.  Raising this only makes sense alongside
     * `ulimit -s unlimited` since each level costs ~6 KB of stack. */
    {
        const char *env = getenv("LCSAS_MAX_TREE_DEPTH");
        if (env && *env) {
            char *endp = NULL;
            long d = strtol(env, &endp, 10);
            if (endp && *endp == '\0' && d > 0 && d <= INT_MAX) {
                lcsas_tree_max_depth = (int)d;
                if (verbose) {
                    fprintf(stderr,
                            "[lcsas-restore] tree recursion-depth cap: "
                            "%d\n", lcsas_tree_max_depth);
                }
            } else {
                fprintf(stderr,
                        "WARNING: ignoring invalid LCSAS_MAX_TREE_DEPTH=%s "
                        "(want a positive integer)\n", env);
            }
        }
    }

    /* #791: the LCSAS_STRESS_LOOKUPS bench that ran here is now
     * recovery/tests/bench_index.c, built only for scaling_bench.py. */
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
        if (snaps.unreadable > 0) {
            printf("(%lu snapshot file(s) could not be read; see the "
                   "warnings above)\n", (unsigned long)snaps.unreadable);
        }
        rc = 0;
        goto out;
    }

    if (snapshot_arg == NULL || strcmp(snapshot_arg, "latest") == 0) {
        sidx = lcsas_snapshot_latest(&snaps);
        /* Issue #662 — a snapshot file that could not be read may be
         * the newest one.  Taking the newest READABLE snapshot without
         * a word would restore older data and still exit 0.  Stop, or
         * ask when someone is at the keyboard. */
        if (sidx >= 0 && snaps.unreadable > 0) {
            fprintf(stderr,
                    "WARNING: %lu snapshot file(s) could not be read "
                    "(first: %s).\n"
                    "         The newest snapshot may be one of them.\n"
                    "         The newest snapshot that could be read is "
                    "%.8s from %s.\n",
                    (unsigned long)snaps.unreadable, snaps.unreadable_name,
                    snaps.items[sidx].file_name, snaps.items[sidx].time);
            if (!interactive) {
                fprintf(stderr,
                        "ERROR: not restoring \"latest\" without a "
                        "choice.  Run again with --snapshot %.8s to "
                        "restore that snapshot, or use another copy of "
                        "the disc.\n",
                        snaps.items[sidx].file_name);
                goto out;
            } else {
                char answer[16];
                fprintf(stderr,
                        "Restore snapshot %.8s instead? [y/N] ",
                        snaps.items[sidx].file_name);
                fflush(stderr);
                if (fgets(answer, sizeof answer, stdin) == NULL
                        || (answer[0] != 'y' && answer[0] != 'Y')) {
                    fprintf(stderr, "Not restoring.\n");
                    goto out;
                }
            }
        }
    } else {
        sidx = lcsas_snapshot_find(&snaps, snapshot_arg);
    }
    if (sidx < 0) {
        fprintf(stderr, "ERROR: snapshot not found: %s\n",
                snapshot_arg ? snapshot_arg : "(none -- empty repository)");
        goto out;
    }

    fprintf(stderr, "[lcsas-restore] restoring snapshot %.12s -> %s\n",
            snaps.items[sidx].file_name, target);

    if (lcsas_mkdir_p(target) != 0) {
        int saved_errno = errno;
        if (saved_errno == ENOSPC || saved_errno == EDQUOT) {
            fprintf(stderr,
                    "ERROR: target directory out of space "
                    "(path=%s, errno=%d)\n",
                    target, saved_errno);
            rc = EXIT_DISK_FULL;
        } else {
            fprintf(stderr,
                    "ERROR: cannot create target dir %s (errno=%d)\n",
                    target, saved_errno);
        }
        goto out;
    }

    {
        lcsas_progress progress;
        int tree_rc;
        /* total_blob_hint is the loaded index size -- an upper bound
         * on what this snapshot can reference.  We surface that
         * explicitly so the heir isn't misled by the denominator. */
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
        if (tree_rc != 0 && locator.reader_quit) {
            fprintf(stderr,
                    "[lcsas-restore] stopped at the disc prompt.  Files "
                    "restored so far are kept.\n"
                    "[lcsas-restore] To carry on, run the same command "
                    "again; finished files are skipped.\n");
            rc = EXIT_READER_QUIT;
            goto out;
        }
        if (tree_rc == LCSAS_TREE_DISK_FULL) {
            fprintf(stderr,
                    "[lcsas-restore] stopped: the disk that holds %s is "
                    "full.  Files restored so far are kept.\n"
                    "[lcsas-restore] Free some space there, or restore to "
                    "a bigger disk, then run the same command again.\n",
                    target);
            rc = EXIT_DISK_FULL;
            goto out;
        }
        if (tree_rc == LCSAS_TREE_INCOMPLETE) {
            /* Issue #781: the walk finished, but some items were not
             * restored (each one is named above).  Not 0, so the
             * restore is never reported as complete; 4, not 1 (#872),
             * so a script can tell it from a failed run. */
            fprintf(stderr,
                    "[lcsas-restore] finished with errors: every other "
                    "item was restored to %s\n", target);
            rc = EXIT_NOT_ALL_RESTORED;
            goto out;
        }
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
