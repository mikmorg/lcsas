/*
 * main.c -- lcsas-shares CLI.
 *
 * Usage:
 *   lcsas-shares SHARE_FILE...
 *   lcsas-shares < shares.txt   (a share a line, or a card's rows)
 *   lcsas-shares --help
 *
 * Each SHARE_FILE is a bare mnemonic file OR a printed share card (the
 * {repo}-share-N-card.txt artifact a share holder is handed): the share words
 * are recognised by their content, so card header lines and prose are
 * ignored.  When no files are given, mnemonics are read from stdin: a
 * line of share words, or a card's rows of them with no other line
 * between, is one share; others (blanks, card prose) are skipped, so
 * `cat card1 card2 | lcsas-shares` works.  A share that is nearly all
 * share words is a share with typing mistakes: it is checked like the
 * others, so the bad word is named.
 *
 * #955: a card prints its words in numbered rows of four.  Number
 * tokens (digits, with an optional '.' or ')') are skipped wherever
 * share words are read, so a card typed with its numbers combines, and
 * "word N" in a message is the word numbered N on the card.
 *
 * Any K of the N shares rebuild the password, and more than K is fine.
 *
 * The rebuilt LCSAS repository PASSWORD is written to stdout as raw
 * bytes with NO trailing newline (binary mode on Windows, so no byte is
 * changed).  Any failure prints a message to stderr and exits non-zero
 * WITHOUT printing a partial password.  Exit status: see usage() below.
 *
 * A file may be UTF-8 (with or without a BOM) or UTF-16 with a BOM, the
 * form Windows PowerShell's `>` writes (#967).
 *
 * No passphrase (#967): `lcsas password split` makes cards with the
 * SLIP-0039 passphrase empty and has no option for one, so this tool
 * reads none: --passphrase is an unknown option, and
 * LCSAS_SHARE_PASSPHRASE is not read.  The old name,
 * LCSAS_KEYSHARE_PASSPHRASE, is still refused if set (#934), as the
 * Python combiner refuses it.
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
    fputs("usage: lcsas-shares SHARE_FILE...\n"
          "       lcsas-shares < shares.txt\n"
          "\n"
          "Rebuilds an LCSAS repository password from its share cards and\n"
          "writes it to standard output (raw bytes, no newline).\n", o);
    /* Several strings: C89 compilers need only take 509 characters. */
    fputs("\n"
          "Give each share as a file (the printed card, or a file with just\n"
          "its words), or type the shares on standard input, one per line\n"
          "or in rows, with a blank line between two cards.\n"
          "Any K of the N shares rebuild the password; more than K is fine.\n"
          "A word may be cut to its first 4 letters.  The numbers a card\n"
          "prints beside its words may be typed or left out.\n", o);
    fputs("\n"
          "Exit status:\n"
          "  0  the password was rebuilt and written\n"
          "  1  the shares could not be combined: too few, or not all from\n"
          "     the same split (compare the Split ID on the cards)\n"
          "  2  usage error, a file could not be read, or no shares given\n"
          "  3  a share is not valid: a word is mistyped or missing (the\n"
          "     message names the share and the word)\n", o);
}

/* The length of the line at `p` (up to its '\n' or the end). */
static size_t line_length(const char *p)
{
    size_t len = 0;
    while (p[len] != '\0' && p[len] != '\n') {
        len++;
    }
    return len;
}

/* Count the tokens of one line (`len` bytes) and how many are share
 * words.  Number tokens (lcsas_keyshare_is_number, #955) are not
 * counted.  Returns 1 when the line has any token at all, numbers
 * included (it is not blank). */
static int line_stats(const char *line, size_t len,
                      size_t *tokens, size_t *words)
{
    size_t i = 0;
    int any = 0;

    *tokens = 0;
    *words = 0;
    while (i < len) {
        size_t start;
        while (i < len && (line[i] == ' ' || line[i] == '\t' ||
                           line[i] == '\r')) {
            i++;
        }
        if (i >= len) {
            break;
        }
        start = i;
        while (i < len && line[i] != ' ' && line[i] != '\t' &&
               line[i] != '\r') {
            i++;
        }
        any = 1;
        if (lcsas_keyshare_is_number(line + start, i - start)) {
            continue;
        }
        (*tokens)++;
        if (lcsas_keyshare_is_word(line + start, i - start)) {
            (*words)++;
        }
    }
    return any;
}

/* A line that may be part of a share: it holds a share word, and at
 * most one other token or at most a quarter of them (a card's row of
 * four words with one mistyped). */
static int is_near_line(size_t tokens, size_t words)
{
    size_t bad = tokens - words;
    return words >= 1 && (bad <= 1 || bad * 4 <= tokens);
}

/* Lines read together that hold a share, perhaps with typing mistakes,
 * rather than prose: enough tokens for a share (13, the SLIP-0039
 * minimum value words) and at most a quarter of them not share words. */
static int is_share_like(size_t tokens, size_t words)
{
    return tokens >= 13 && (tokens - words) * 4 <= tokens;
}

/* Lines [start, end) of a text: their tokens and share words. */
struct block {
    const char *start;
    const char *end;
    size_t tokens;
    size_t words;
    size_t lines;
    unsigned long first;   /* the first line's number, from 1 */
};

/* Copy the tokens of the lines [start, end) into `buf` (capacity
 * `cap`), lowercased, one space apart, the numbers left out (#955).
 * Returns 0, or -1 when they do not fit. */
static int copy_block(const char *start, const char *end,
                      char *buf, size_t cap)
{
    const char *p = start;
    size_t out = 0;

    if (cap == 0) {
        return -1;
    }
    while (p < end) {
        size_t i = 0;
        size_t len = line_length(p);
        while (i < len) {
            size_t s;
            size_t k;
            while (i < len && (p[i] == ' ' || p[i] == '\t' || p[i] == '\r')) {
                i++;
            }
            if (i >= len) {
                break;
            }
            s = i;
            while (i < len && p[i] != ' ' && p[i] != '\t' && p[i] != '\r') {
                i++;
            }
            if (lcsas_keyshare_is_number(p + s, i - s)) {
                continue;
            }
            if (out + (i - s) + 2 > cap) {
                buf[0] = '\0';
                return -1;
            }
            if (out > 0) {
                buf[out++] = ' ';
            }
            for (k = s; k < i; k++) {
                char c = p[k];
                if (c >= 'A' && c <= 'Z') {
                    c = (char)(c - 'A' + 'a');
                }
                buf[out++] = c;
            }
        }
        p += len;
        if (*p == '\n') {
            p++;
        }
    }
    buf[out] = '\0';
    return 0;
}

/* The fallback when a file's share words are not a valid share: of the
 * runs of consecutive lines that may be part of a share (is_near_line),
 * the first with the most tokens that is share-like.  A card's rows with
 * a mistyped word in one of them, or a share typed on one line with
 * mistakes, are such a run, so the check can name the bad word by its
 * place in the share.  #844: a card's prose is never taken (it was, and
 * the check then named a prose word, e.g. word 1 'tip:').  The Python
 * combiner's _candidate_block uses the same rule.  Returns 1 and fills
 * `best` when there is one. */
static int find_candidate(const char *text, struct block *best)
{
    const char *p = text;
    struct block run;
    int found = 0;

    run.start = NULL;
    run.end = NULL;
    run.tokens = 0;
    run.words = 0;
    run.lines = 0;
    run.first = 0;
    best->tokens = 0;
    for (;;) {
        size_t len = line_length(p);
        size_t tokens;
        size_t words;
        int near;

        (void)line_stats(p, len, &tokens, &words);
        near = is_near_line(tokens, words);
        if (near) {
            if (run.start == NULL) {
                run.start = p;
                run.tokens = 0;
                run.words = 0;
            }
            run.tokens += tokens;
            run.words += words;
            run.end = p + len;
        }
        if (!near || p[len] == '\0') {
            if (run.start != NULL && is_share_like(run.tokens, run.words) &&
                run.tokens > best->tokens) {
                *best = run;
                found = 1;
            }
            run.start = NULL;
        }
        if (p[len] == '\0') {
            break;
        }
        p += len + 1;
    }
    return found;
}

/* #967: undo a byte-order mark at the start of `buf` (`*len` bytes),
 * in place.  A UTF-8 BOM is dropped.  UTF-16 (little- or big-endian,
 * with its BOM: what Windows PowerShell's `>` writes) becomes one byte a
 * character: ASCII as it is, anything else '?', which no share word
 * holds.  `buf` is NUL-terminated on return. */
static void undo_bom(char *buf, size_t *len)
{
    const unsigned char *u = (const unsigned char *)buf;
    size_t n = *len;
    size_t i;
    size_t out = 0;
    int big;

    if (n >= 3 && u[0] == 0xEF && u[1] == 0xBB && u[2] == 0xBF) {
        memmove(buf, buf + 3, n - 3);
        n -= 3;
    } else if (n >= 2 && ((u[0] == 0xFF && u[1] == 0xFE) ||
                          (u[0] == 0xFE && u[1] == 0xFF))) {
        big = (u[0] == 0xFE);
        for (i = 2; i + 1 < n; i += 2) {
            unsigned int unit = big
                ? ((unsigned int)u[i] << 8) | u[i + 1]
                : ((unsigned int)u[i + 1] << 8) | u[i];
            buf[out++] = (unit != 0 && unit < 0x80) ? (char)unit : '?';
        }
        n = out;
    }
    buf[n] = '\0';
    *len = n;
}

/* Read an entire file (bare mnemonic OR printed share card), extract the
 * embedded share mnemonic into `buf` (NUL-terminated, capacity `cap`).
 * Returns 0 on success, or an exit status on error (an explanatory
 * message naming `path` is printed to stderr): EXIT_USAGE when the file
 * cannot be read, EXIT_BAD_SHARE when it holds no plausible share.
 *
 * The share is the file's lines of share words (and numbers), joined.
 * When they are not a valid share and a run of lines that are nearly
 * all share words holds a word that is not one (find_candidate), that
 * run is kept instead, so the per-share pre-pass in main() names the
 * word by its place in the share: "word 23" is the word numbered 23 on
 * the card (#955).  Returns 0 (lets the caller run diagnostics) then. */
static int read_file_mnemonic(const char *path, char *buf, size_t cap)
{
    static char filebuf[MAX_FILE];
    FILE *f = fopen(path, "rb");
    size_t got;
    char err[64];
    int strict_ok;
    struct block cand;
    int have_cand;

    if (f == NULL) {
        fprintf(stderr, "lcsas-shares: cannot open '%s'\n", path);
        return EXIT_USAGE;
    }
    got = fread(filebuf, 1, sizeof(filebuf) - 1, f);
    if (ferror(f)) {
        fprintf(stderr, "lcsas-shares: read error on '%s'\n", path);
        fclose(f);
        return EXIT_USAGE;
    }
    fclose(f);
    undo_bom(filebuf, &got);

    err[0] = '\0';
    strict_ok = (lcsas_keyshare_extract(filebuf, buf, cap,
                                        err, sizeof(err)) == 0);
    if (strict_ok && lcsas_keyshare_check_share(buf, NULL, 0) == 0) {
        return 0;
    }

    /* Not a valid share.  A run of lines with a word that is not a
     * share word in it is kept for diagnosis (the pre-pass names the
     * word); otherwise the share words found are kept (the pre-pass
     * says what is wrong with them), or the file has no share. */
    have_cand = find_candidate(filebuf, &cand);
    if (have_cand && (cand.words < cand.tokens || !strict_ok) &&
        copy_block(cand.start, cand.end, buf, cap) == 0) {
        return 0;
    }
    if (strict_ok) {
        return 0;
    }
    if (strncmp(err, "found 0 ", 8) == 0) {
        /* #844: say it plainly. */
        fprintf(stderr, "lcsas-shares: '%s': no share words were found "
                        "in this file - is it a share card?\n", path);
    } else if (err[0] != '\0') {
        fprintf(stderr, "lcsas-shares: '%s': %s "
                        "- is this a complete share card?\n", path, err);
    } else {
        fprintf(stderr, "lcsas-shares: '%s': share too large\n", path);
    }
    return EXIT_BAD_SHARE;
}

/* #844: stdin lines were skipped and the shares read did not rebuild
 * the password.  A share broken over lines with something between them
 * is the likely cause.  The Python combiner prints the same. */
static void print_skipped_hint(size_t skipped)
{
    if (skipped == 0) {
        return;
    }
    fprintf(stderr, "%lu line(s) of input were not read as shares (blank "
                    "lines are not counted).\n", (unsigned long)skipped);
    fputs("Type each card's share words on ONE line, or in rows with no "
          "blank line\nbetween them: a share broken up otherwise is "
          "skipped.\n", stderr);
}

/* Standard input, all of it: many cards' worth. */
#define MAX_STDIN  (4 * MAX_FILE)

/* End a group of stdin lines (`g`): when it is share-like it is the next
 * share, kept in storage[*n] and named by its first line; otherwise its
 * lines are counted in *skipped.  Returns 0, or -1 (with the message
 * printed) when there are too many shares. */
static int end_group(struct block *g, char storage[][MAX_LINE],
                     char labels[][MAX_LINE], const char **mnemonics,
                     size_t *n, size_t *skipped)
{
    if (!is_share_like(g->tokens, g->words) ||
        (*n < MAX_MNEMONICS &&
         copy_block(g->start, g->end, storage[*n], MAX_LINE) != 0)) {
        *skipped += g->lines;
    } else if (*n >= MAX_MNEMONICS) {
        fprintf(stderr, "lcsas-shares: too many shares (max %d)\n",
                MAX_MNEMONICS);
        return -1;
    } else {
        mnemonics[*n] = storage[*n];
        sprintf(labels[*n], "stdin line %lu", g->first);
        (*n)++;
    }
    g->start = NULL;
    return 0;
}

int main(int argc, char **argv)
{
    static char storage[MAX_MNEMONICS][MAX_LINE];
    static char labels[MAX_MNEMONICS][MAX_LINE];
    const char *mnemonics[MAX_MNEMONICS];
    size_t n = 0;
    int argi = 1;
    int from_stdin;
    unsigned char pw[LCSAS_KEYSHARE_MAX_PW];
    size_t pwlen = 0;
    size_t i;
    size_t skipped = 0;   /* non-blank stdin lines not read as shares */
    int any_bad = 0;

    /* #934: the old variable is refused, first, so a passphrase set
     * under the old name is never silently ignored.  (No passphrase is
     * read under any name, #967: the cards are made without one.) */
    if (getenv("LCSAS_KEYSHARE_PASSPHRASE") != NULL) {
        fprintf(stderr, "lcsas-shares: LCSAS_KEYSHARE_PASSPHRASE was "
                        "renamed: use LCSAS_SHARE_PASSPHRASE\n");
        return EXIT_USAGE;
    }

    /* Parse options (--help). */
    while (argi < argc) {
        if (strcmp(argv[argi], "--help") == 0 ||
                   strcmp(argv[argi], "-h") == 0) {
            usage(stdout);
            return 0;
        } else if (strcmp(argv[argi], "--") == 0) {
            argi++;
            break;
        } else if (argv[argi][0] == '-' && argv[argi][1] != '\0') {
            fprintf(stderr, "lcsas-shares: unknown option '%s'\n", argv[argi]);
            usage(stderr);
            return EXIT_USAGE;
        } else {
            break;
        }
    }

    from_stdin = (argi >= argc);

    if (!from_stdin) {
        /* Each remaining arg is a file holding one mnemonic. */
        for (; argi < argc; argi++) {
            int frc;
            if (n >= MAX_MNEMONICS) {
                fprintf(stderr, "lcsas-shares: too many shares (max %d)\n",
                        MAX_MNEMONICS);
                return EXIT_USAGE;
            }
            frc = read_file_mnemonic(argv[argi], storage[n], MAX_LINE);
            if (frc != 0) {
                return frc;
            }
            if (storage[n][0] == '\0') {
                fprintf(stderr, "lcsas-shares: '%s' is empty\n", argv[argi]);
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
        /* Read the shares from stdin.  A share is a line, or a run of
         * lines with no other line between them (a card's rows, #955),
         * that is nearly all share words (is_share_like); a line long
         * enough to be a share on its own (13 tokens or more) is one,
         * so shares typed one a line stay apart.  Blank lines and card
         * prose are skipped, so `cat card1 card2 | ...` works.  A share
         * with typing mistakes is kept, so the check below names the
         * bad word as it does for a file. */
        static char inbuf[MAX_STDIN];
        const char *p = inbuf;
        struct block group;
        size_t got;
        unsigned long lineno = 0;

#ifdef _WIN32
        (void)_setmode(_fileno(stdin), _O_BINARY);
#endif
        got = fread(inbuf, 1, sizeof(inbuf) - 1, stdin);
        if (ferror(stdin)) {
            fprintf(stderr, "lcsas-shares: read error on standard input\n");
            return EXIT_USAGE;
        }
        if (got == sizeof(inbuf) - 1 && getchar() != EOF) {
            fprintf(stderr, "lcsas-shares: standard input is too large\n");
            return EXIT_USAGE;
        }
        undo_bom(inbuf, &got);

        group.start = NULL;
        for (;;) {
            size_t len = line_length(p);
            size_t tokens;
            size_t words;
            int any;
            int near;

            lineno++;
            any = line_stats(p, len, &tokens, &words);
            near = is_near_line(tokens, words);
            /* A line that is not part of the group ends it, and so does
             * a line that is a share on its own. */
            if (group.start != NULL && (!near || tokens >= 13)) {
                if (end_group(&group, storage, labels, mnemonics,
                              &n, &skipped) != 0) {
                    return EXIT_USAGE;
                }
            }
            if (near) {
                if (group.start == NULL) {
                    group.start = p;
                    group.tokens = 0;
                    group.words = 0;
                    group.lines = 0;
                    group.first = lineno;
                }
                group.tokens += tokens;
                group.words += words;
                group.lines++;
                group.end = p + len;
                if (tokens >= 13 &&
                    end_group(&group, storage, labels, mnemonics,
                              &n, &skipped) != 0) {
                    return EXIT_USAGE;
                }
            } else if (any) {
                skipped++;
            }
            if (p[len] == '\0') {
                break;
            }
            p += len + 1;
        }
        if (group.start != NULL &&
            end_group(&group, storage, labels, mnemonics, &n, &skipped) != 0) {
            return EXIT_USAGE;
        }
    }

    if (n == 0) {
        fprintf(stderr, "lcsas-shares: no shares provided\n");
        print_skipped_hint(skipped);
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
        fprintf(stderr, "lcsas-shares: one or more shares failed individual "
                        "validation; fix the flagged words above and retry\n");
        return EXIT_BAD_SHARE;
    }

    if (lcsas_keyshare_recover_password(mnemonics, n,
                                        (const unsigned char *)"", 0,
                                        pw, &pwlen) != 0) {
        /* Every share is internally valid, so the failure is a set-level
         * problem: too few shares, or shares from different splits. */
        fprintf(stderr, "lcsas-shares: failed to rebuild the password "
                        "(too few, corrupt, or mismatched shares)\n");
        fprintf(stderr, "lcsas-shares: supply at least K shares from the "
                        "SAME archive (same Split ID on every card)\n");
        print_skipped_hint(skipped);
        return EXIT_CANNOT_COMBINE;
    }

#ifdef _WIN32
    /* Text-mode stdout would turn a '\n' byte into "\r\n". */
    (void)_setmode(_fileno(stdout), _O_BINARY);
#endif
    /* Write the raw password bytes, NO trailing newline. */
    for (i = 0; i < pwlen; i++) {
        if (putchar(pw[i]) == EOF) {
            fprintf(stderr, "lcsas-shares: write error\n");
            return EXIT_CANNOT_COMBINE;
        }
    }
    if (fflush(stdout) != 0) {
        fprintf(stderr, "lcsas-shares: write error\n");
        return EXIT_CANNOT_COMBINE;
    }
    return 0;
}
