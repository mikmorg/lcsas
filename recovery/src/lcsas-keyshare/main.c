/*
 * main.c -- lcsas-keyshare CLI.
 *
 * Usage:
 *   lcsas-keyshare [--passphrase X] SHARE_FILE...
 *   lcsas-keyshare [--passphrase X] < shares.txt   (one mnemonic per line)
 *
 * Each SHARE_FILE holds exactly one SLIP-0039 mnemonic (the whole file is
 * one mnemonic; leading/trailing whitespace and newlines are ignored).
 * When no files are given, mnemonics are read from stdin, one per line.
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

/* Read an entire file into `buf` (NUL-terminated), trimming surrounding
 * whitespace.  Returns 0 on success, nonzero on error. */
static int read_file_mnemonic(const char *path, char *buf, size_t cap)
{
    FILE *f = fopen(path, "rb");
    size_t got;
    size_t start, end;

    if (f == NULL) {
        fprintf(stderr, "lcsas-keyshare: cannot open '%s'\n", path);
        return -1;
    }
    got = fread(buf, 1, cap - 1, f);
    if (ferror(f)) {
        fprintf(stderr, "lcsas-keyshare: read error on '%s'\n", path);
        fclose(f);
        return -1;
    }
    fclose(f);
    buf[got] = '\0';

    /* Trim leading/trailing whitespace in place. */
    start = 0;
    while (buf[start] == ' ' || buf[start] == '\t' ||
           buf[start] == '\n' || buf[start] == '\r') {
        start++;
    }
    end = got;
    while (end > start) {
        char c = buf[end - 1];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            end--;
        } else {
            break;
        }
    }
    if (start > 0) {
        memmove(buf, buf + start, end - start);
    }
    buf[end - start] = '\0';
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
        /* Read mnemonics from stdin, one per line. */
        while (n < MAX_MNEMONICS && fgets(storage[n], MAX_LINE, stdin) != NULL) {
            char *line = storage[n];
            size_t len = strlen(line);
            /* Strip trailing newline/CR. */
            while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
                line[--len] = '\0';
            }
            /* Skip blank lines. */
            {
                size_t s = 0;
                while (line[s] == ' ' || line[s] == '\t') {
                    s++;
                }
                if (line[s] == '\0') {
                    continue;
                }
            }
            mnemonics[n] = storage[n];
            n++;
        }
        if (n >= MAX_MNEMONICS && fgets(storage[0], MAX_LINE, stdin) != NULL) {
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
