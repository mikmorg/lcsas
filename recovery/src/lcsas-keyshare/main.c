/*
 * main.c -- lcsas-keyshare CLI.
 *
 * Usage:
 *   lcsas-keyshare [--passphrase X] SHARE_FILE...
 *   lcsas-keyshare [--passphrase X] < shares.txt   (one mnemonic per line)
 *
 * Each SHARE_FILE is a bare mnemonic file OR a printed share card (the
 * {repo}-share-N-card.txt artifact a holder is handed): the share words
 * are recognised by their content, so card header lines and prose are
 * ignored.  When no files are given, mnemonics are read from stdin; each
 * line whose tokens are all wordlist words is one share, others (blanks,
 * card prose) are skipped, so `cat card1 card2 | lcsas-keyshare` works.
 *
 * The recovered LCSAS repository PASSWORD is written to stdout as raw
 * bytes with NO trailing newline.  Any failure prints a message to
 * stderr and exits non-zero WITHOUT printing a partial password.
 *
 * Passphrase: default empty.  Overridden by --passphrase X, else by the
 * environment variable LCSAS_KEYSHARE_PASSPHRASE.
 *
 * C89; reuses the SLIP-0039 combiner library.
 */

#include "slip39.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_MNEMONICS  64
#define MAX_LINE       4096
/* A whole card file (header + prose + words) fits well under this. */
#define MAX_FILE       65536

/* Read an entire file (bare mnemonic OR printed share card), extract the
 * embedded share mnemonic into `buf` (NUL-terminated, capacity `cap`).
 * Returns 0 on success, nonzero on error (an explanatory message naming
 * `path` is printed to stderr). */
static int read_file_mnemonic(const char *path, char *buf, size_t cap)
{
    static char filebuf[MAX_FILE];
    FILE *f = fopen(path, "rb");
    size_t got;
    char err[64];

    if (f == NULL) {
        fprintf(stderr, "lcsas-keyshare: cannot open '%s'\n", path);
        return -1;
    }
    got = fread(filebuf, 1, sizeof(filebuf) - 1, f);
    if (ferror(f)) {
        fprintf(stderr, "lcsas-keyshare: read error on '%s'\n", path);
        fclose(f);
        return -1;
    }
    fclose(f);
    filebuf[got] = '\0';

    err[0] = '\0';
    if (lcsas_keyshare_extract(filebuf, buf, cap, err, sizeof(err)) != 0) {
        if (err[0] != '\0') {
            fprintf(stderr, "lcsas-keyshare: '%s': %s "
                            "— is this a complete share card?\n", path, err);
        } else {
            fprintf(stderr, "lcsas-keyshare: '%s': share too large\n", path);
        }
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    static char storage[MAX_MNEMONICS][MAX_LINE];
    const char *mnemonics[MAX_MNEMONICS];
    size_t n = 0;
    const unsigned char *passphrase = (const unsigned char *)"";
    size_t plen = 0;
    const char *pp_opt = NULL;
    int argi = 1;
    int from_stdin;
    unsigned char pw[LCSAS_KEYSHARE_MAX_PW];
    size_t pwlen = 0;
    size_t i;

    /* Parse options (only --passphrase X is recognised). */
    while (argi < argc) {
        if (strcmp(argv[argi], "--passphrase") == 0) {
            if (argi + 1 >= argc) {
                fprintf(stderr, "lcsas-keyshare: --passphrase needs a value\n");
                return 2;
            }
            pp_opt = argv[argi + 1];
            argi += 2;
        } else if (strcmp(argv[argi], "--") == 0) {
            argi++;
            break;
        } else if (argv[argi][0] == '-' && argv[argi][1] != '\0') {
            fprintf(stderr, "lcsas-keyshare: unknown option '%s'\n", argv[argi]);
            return 2;
        } else {
            break;
        }
    }

    /* Resolve the passphrase: --passphrase, else env, else empty. */
    if (pp_opt != NULL) {
        passphrase = (const unsigned char *)pp_opt;
        plen = strlen(pp_opt);
    } else {
        const char *env = getenv("LCSAS_KEYSHARE_PASSPHRASE");
        if (env != NULL) {
            passphrase = (const unsigned char *)env;
            plen = strlen(env);
        }
    }

    from_stdin = (argi >= argc);

    if (!from_stdin) {
        /* Each remaining arg is a file holding one mnemonic. */
        for (; argi < argc; argi++) {
            if (n >= MAX_MNEMONICS) {
                fprintf(stderr, "lcsas-keyshare: too many shares (max %d)\n",
                        MAX_MNEMONICS);
                return 2;
            }
            if (read_file_mnemonic(argv[argi], storage[n], MAX_LINE) != 0) {
                return 2;
            }
            if (storage[n][0] == '\0') {
                fprintf(stderr, "lcsas-keyshare: '%s' is empty\n", argv[argi]);
                return 2;
            }
            mnemonics[n] = storage[n];
            n++;
        }
    } else {
        /* Read mnemonics from stdin, one per line.  A line is a share iff
         * its tokens are all wordlist words and number 20 or 33 (what
         * lcsas_keyshare_extract validates); blank lines and card prose
         * fail that and are skipped, so `cat card1 card2 | ...` works. */
        static char linebuf[MAX_LINE];
        while (n < MAX_MNEMONICS && fgets(linebuf, MAX_LINE, stdin) != NULL) {
            if (lcsas_keyshare_extract(linebuf, storage[n], MAX_LINE,
                                       NULL, 0) != 0) {
                continue;
            }
            mnemonics[n] = storage[n];
            n++;
        }
        if (n >= MAX_MNEMONICS && fgets(linebuf, MAX_LINE, stdin) != NULL) {
            /* fgets above already consumed; this branch is defensive only
             * if exactly MAX_MNEMONICS lines were read. */
            fprintf(stderr, "lcsas-keyshare: too many shares (max %d)\n",
                    MAX_MNEMONICS);
            return 2;
        }
    }

    if (n == 0) {
        fprintf(stderr, "lcsas-keyshare: no shares provided\n");
        fprintf(stderr, "usage: lcsas-keyshare [--passphrase X] SHARE_FILE...\n");
        return 2;
    }

    if (lcsas_keyshare_recover_password(mnemonics, n, passphrase, plen,
                                        pw, &pwlen) != 0) {
        fprintf(stderr, "lcsas-keyshare: failed to recover the password "
                        "(insufficient, corrupt, or mismatched shares)\n");
        return 1;
    }

    /* Write the raw password bytes, NO trailing newline. */
    for (i = 0; i < pwlen; i++) {
        if (putchar(pw[i]) == EOF) {
            fprintf(stderr, "lcsas-keyshare: write error\n");
            return 1;
        }
    }
    if (fflush(stdout) != 0) {
        fprintf(stderr, "lcsas-keyshare: write error\n");
        return 1;
    }
    return 0;
}
