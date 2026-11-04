/*
 * main.c -- lcsas-keyshare CLI.
 *
 * Usage:
 *   lcsas-keyshare [--passphrase X] SHARE_FILE...
 *   lcsas-keyshare [--passphrase X] < shares.txt   (one mnemonic per line)
 *   lcsas-keyshare --help
 *
 * Each SHARE_FILE is a bare mnemonic file OR a printed share card (the
 * {repo}-share-N-card.txt artifact a share holder is handed): the share words
 * are recognised by their content, so card header lines and prose are
 * ignored.  When no files are given, mnemonics are read from stdin; each
 * line whose tokens are all wordlist words is one share, others (blanks,
 * card prose) are skipped, so `cat card1 card2 | lcsas-keyshare` works.
 * A stdin line that is nearly all share words is a share with typing
 * mistakes: it is checked like the others, so the bad word is named.
 *
 * Any K of the N shares rebuild the password, and more than K is fine.
 *
 * The rebuilt LCSAS repository PASSWORD is written to stdout as raw
 * bytes with NO trailing newline (binary mode on Windows, so no byte is
 * changed).  Any failure prints a message to stderr and exits non-zero
 * WITHOUT printing a partial password.  Exit status: see usage() below.
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
#ifdef _WIN32
#  include <fcntl.h>
#  include <io.h>
#endif

#define MAX_MNEMONICS  64
#define MAX_LINE       4096
/* A whole card file (header + prose + words) fits well under this. */
#define MAX_FILE       65536

/* Exit status.  0 is success; scripts branch on the rest. */
#define EXIT_CANNOT_COMBINE  1   /* too few shares, or mixed splits */
#define EXIT_USAGE           2   /* bad option, unreadable file, no shares */
#define EXIT_BAD_SHARE       3   /* a share is mistyped or incomplete */

static void usage(FILE *o)
{
    fputs("usage: lcsas-keyshare [--passphrase X] SHARE_FILE...\n"
          "       lcsas-keyshare [--passphrase X] < shares.txt\n"
          "\n"
          "Rebuilds an LCSAS repository password from its share cards and\n"
          "writes it to standard output (raw bytes, no newline).\n"
          "\n"
          "Give each share as a file (the printed card, or a file with just\n"
          "its words), or type the shares on standard input, one per line.\n"
          "Any K of the N shares rebuild the password; more than K is fine.\n"
          "A word may be cut to its first 4 letters.\n", o);
    /* Two strings: C89 compilers need only take 509 characters. */
    fputs("\n"
          "Exit status:\n"
          "  0  the password was rebuilt and written\n"
          "  1  the shares could not be combined: too few, or not all from\n"
          "     the same split (compare the Split ID on the cards)\n"
          "  2  usage error, a file could not be read, or no shares given\n"
          "  3  a share is not valid: a word is mistyped or missing (the\n"
          "     message names the share and the word)\n", o);
}

/* Count whitespace-separated tokens on one line. */
static size_t count_tokens(const char *line, size_t len)
{
    size_t i = 0;
    size_t tokens = 0;
    while (i < len) {
        while (i < len && (line[i] == ' ' || line[i] == '\t')) {
            i++;
        }
        if (i >= len) {
            break;
        }
        tokens++;
        while (i < len && line[i] != ' ' && line[i] != '\t') {
            i++;
        }
    }
    return tokens;
}

/* Fallback when strict extraction fails: copy the single line with the
 * most tokens (the likely-but-typo'd share line) verbatim into `buf`,
 * lowercased and whitespace-normalised, so a per-share check can pinpoint
 * the offending word.  Returns the token count of that line (0 if none
 * has enough tokens to plausibly be a share). */
static size_t copy_candidate_line(const char *text, char *buf, size_t cap)
{
    const char *p = text;
    const char *best = NULL;
    size_t best_len = 0;
    size_t best_tokens = 0;

    while (*p != '\0') {
        const char *line = p;
        size_t llen = 0;
        size_t tlen;
        size_t tokens;

        while (p[llen] != '\0' && p[llen] != '\n') {
            llen++;
        }
        tlen = llen;
        while (tlen > 0 &&
               (line[tlen - 1] == '\r' || line[tlen - 1] == ' ' ||
                line[tlen - 1] == '\t')) {
            tlen--;
        }
        tokens = count_tokens(line, tlen);
        if (tokens > best_tokens) {
            best_tokens = tokens;
            best = line;
            best_len = tlen;
        }
        p += llen;
        if (*p == '\n') {
            p++;
        }
    }

    /* Require enough tokens to plausibly be a share (>= the SLIP-0039
     * minimum value words); a short prose line is not a candidate. */
    if (best == NULL || best_tokens < 13) {
        if (cap > 0) {
            buf[0] = '\0';
        }
        return 0;
    }
    {
        size_t i = 0;
        size_t out = 0;
        int in_gap = 1;
        while (i < best_len && out + 1 < cap) {
            char c = best[i];
            if (c == ' ' || c == '\t') {
                if (!in_gap) {
                    buf[out++] = ' ';
                    in_gap = 1;
                }
            } else {
                if (c >= 'A' && c <= 'Z') {
                    c = (char)(c - 'A' + 'a');
                }
                buf[out++] = c;
                in_gap = 0;
            }
            i++;
        }
        if (out > 0 && buf[out - 1] == ' ') {
            out--;
        }
        buf[out] = '\0';
    }
    return best_tokens;
}

/* In stdin mode: is `line` (length `len`) a share with typing mistakes,
 * rather than card prose?  Prose has few share words, a mistyped share
 * nearly all: enough tokens to plausibly be a share (13, as for a file)
 * and at most a quarter of them not share words. */
static int is_mistyped_share_line(const char *line, size_t len)
{
    size_t i = 0;
    size_t tokens = 0;
    size_t words = 0;
    while (i < len) {
        size_t start;
        while (i < len && (line[i] == ' ' || line[i] == '\t' ||
                           line[i] == '\r' || line[i] == '\n')) {
            i++;
        }
        if (i >= len) {
            break;
        }
        start = i;
        while (i < len && line[i] != ' ' && line[i] != '\t' &&
               line[i] != '\r' && line[i] != '\n') {
            i++;
        }
        tokens++;
        if (lcsas_keyshare_is_word(line + start, i - start)) {
            words++;
        }
    }
    return tokens >= 13 && (tokens - words) * 4 <= tokens;
}

/* Read an entire file (bare mnemonic OR printed share card), extract the
 * embedded share mnemonic into `buf` (NUL-terminated, capacity `cap`).
 * Returns 0 on success, or an exit status on error (an explanatory
 * message naming `path` is printed to stderr): EXIT_USAGE when the file
 * cannot be read, EXIT_BAD_SHARE when it holds no plausible share.
 *
 * On a strict-extraction failure, falls back to copying the most
 * token-dense line into `buf` (lowercased) so the per-share pre-pass in
 * main() can name the offending word.  Returns 0 (lets the caller run
 * diagnostics) when a candidate line was found. */
static int read_file_mnemonic(const char *path, char *buf, size_t cap)
{
    static char filebuf[MAX_FILE];
    FILE *f = fopen(path, "rb");
    size_t got;
    char err[64];

    if (f == NULL) {
        fprintf(stderr, "lcsas-keyshare: cannot open '%s'\n", path);
        return EXIT_USAGE;
    }
    got = fread(filebuf, 1, sizeof(filebuf) - 1, f);
    if (ferror(f)) {
        fprintf(stderr, "lcsas-keyshare: read error on '%s'\n", path);
        fclose(f);
        return EXIT_USAGE;
    }
    fclose(f);
    filebuf[got] = '\0';

    err[0] = '\0';
    if (lcsas_keyshare_extract(filebuf, buf, cap, err, sizeof(err)) == 0) {
        return 0;
    }

    /* Strict extraction failed.  If a token-dense line exists, keep it
     * for diagnosis (the pre-pass will name the bad word); otherwise the
     * file genuinely has no share line. */
    if (copy_candidate_line(filebuf, buf, cap) > 0) {
        return 0;
    }
    if (err[0] != '\0') {
        fprintf(stderr, "lcsas-keyshare: '%s': %s "
                        "- is this a complete share card?\n", path, err);
    } else {
        fprintf(stderr, "lcsas-keyshare: '%s': share too large\n", path);
    }
    return EXIT_BAD_SHARE;
}

int main(int argc, char **argv)
{
    static char storage[MAX_MNEMONICS][MAX_LINE];
    static char labels[MAX_MNEMONICS][MAX_LINE];
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
    int any_bad = 0;

    /* Parse options (--passphrase X and --help). */
    while (argi < argc) {
        if (strcmp(argv[argi], "--passphrase") == 0) {
            if (argi + 1 >= argc) {
                fprintf(stderr, "lcsas-keyshare: --passphrase needs a value\n");
                return EXIT_USAGE;
            }
            pp_opt = argv[argi + 1];
            argi += 2;
        } else if (strcmp(argv[argi], "--help") == 0 ||
                   strcmp(argv[argi], "-h") == 0) {
            usage(stdout);
            return 0;
        } else if (strcmp(argv[argi], "--") == 0) {
            argi++;
            break;
        } else if (argv[argi][0] == '-' && argv[argi][1] != '\0') {
            fprintf(stderr, "lcsas-keyshare: unknown option '%s'\n", argv[argi]);
            usage(stderr);
            return EXIT_USAGE;
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
            int frc;
            if (n >= MAX_MNEMONICS) {
                fprintf(stderr, "lcsas-keyshare: too many shares (max %d)\n",
                        MAX_MNEMONICS);
                return EXIT_USAGE;
            }
            frc = read_file_mnemonic(argv[argi], storage[n], MAX_LINE);
            if (frc != 0) {
                return frc;
            }
            if (storage[n][0] == '\0') {
                fprintf(stderr, "lcsas-keyshare: '%s' is empty\n", argv[argi]);
                return EXIT_BAD_SHARE;
            }
            mnemonics[n] = storage[n];
            {
                /* Record the source file path as this share's name, for
                 * per-share diagnostics below. */
                const char *path = argv[argi];
                size_t j = 0;
                while (path[j] != '\0' && j + 1 < MAX_LINE) {
                    labels[n][j] = path[j];
                    j++;
                }
                labels[n][j] = '\0';
            }
            n++;
        }
    } else {
        /* Read mnemonics from stdin, one per line.  A line is a share iff
         * its tokens are all share words and number at least 20 (what
         * lcsas_keyshare_extract validates); blank lines and card prose
         * fail that and are skipped, so `cat card1 card2 | ...` works.
         * A line that is nearly all share words is kept anyway, as a
         * share with typing mistakes, so the check below names the bad
         * word as it does for a file. */
        static char linebuf[MAX_LINE];
        size_t lineno = 0;
        while (n < MAX_MNEMONICS && fgets(linebuf, MAX_LINE, stdin) != NULL) {
            lineno++;
            if (lcsas_keyshare_extract(linebuf, storage[n], MAX_LINE,
                                       NULL, 0) != 0) {
                if (!is_mistyped_share_line(linebuf, strlen(linebuf)) ||
                    copy_candidate_line(linebuf, storage[n], MAX_LINE) == 0) {
                    continue;
                }
            }
            mnemonics[n] = storage[n];
            sprintf(labels[n], "stdin line %lu", (unsigned long)lineno);
            n++;
        }
        if (n >= MAX_MNEMONICS && fgets(linebuf, MAX_LINE, stdin) != NULL) {
            /* fgets above already consumed; this branch is defensive only
             * if exactly MAX_MNEMONICS lines were read. */
            fprintf(stderr, "lcsas-keyshare: too many shares (max %d)\n",
                    MAX_MNEMONICS);
            return EXIT_USAGE;
        }
    }

    if (n == 0) {
        fprintf(stderr, "lcsas-keyshare: no shares provided\n");
        usage(stderr);
        return EXIT_USAGE;
    }

    /* Per-share pre-pass: validate each share independently and print a
     * named verdict, so a single mistyped card is pinpointed (file + word
     * position + token) instead of collapsing into one generic failure. */
    for (i = 0; i < n; i++) {
        char err[128];
        err[0] = '\0';
        if (lcsas_keyshare_check_share(mnemonics[i], err, sizeof(err)) == 0) {
            fprintf(stderr, "share %lu (%s): OK\n",
                    (unsigned long)(i + 1), labels[i]);
        } else {
            fprintf(stderr, "share %lu (%s): %s\n",
                    (unsigned long)(i + 1), labels[i], err);
            any_bad = 1;
        }
    }
    if (any_bad) {
        fprintf(stderr, "lcsas-keyshare: one or more shares failed individual "
                        "validation; fix the flagged words above and retry\n");
        return EXIT_BAD_SHARE;
    }

    if (lcsas_keyshare_recover_password(mnemonics, n, passphrase, plen,
                                        pw, &pwlen) != 0) {
        /* Every share is internally valid, so the failure is a set-level
         * problem: too few shares, or shares from different splits. */
        fprintf(stderr, "lcsas-keyshare: failed to rebuild the password "
                        "(too few, corrupt, or mismatched shares)\n");
        fprintf(stderr, "lcsas-keyshare: supply at least K shares from the "
                        "SAME archive (same Split ID on every card)\n");
        return EXIT_CANNOT_COMBINE;
    }

#ifdef _WIN32
    /* Text-mode stdout would turn a '\n' byte into "\r\n". */
    (void)_setmode(_fileno(stdout), _O_BINARY);
#endif
    /* Write the raw password bytes, NO trailing newline. */
    for (i = 0; i < pwlen; i++) {
        if (putchar(pw[i]) == EOF) {
            fprintf(stderr, "lcsas-keyshare: write error\n");
            return EXIT_CANNOT_COMBINE;
        }
    }
    if (fflush(stdout) != 0) {
        fprintf(stderr, "lcsas-keyshare: write error\n");
        return EXIT_CANNOT_COMBINE;
    }
    return 0;
}
