/*
 * path.c -- path traversal safety.
 *
 * The symlink check is purely lexical: we do NOT resolve real
 * filesystem links during validation, because the target file does not
 * yet exist when we validate.  Lexical normalization is sufficient
 * because we set the symlink atomically and then never re-traverse it
 * during this restore.
 */
#include "path.h"

#include <string.h>

int
lcsas_path_safe_name(const char *name)
{
    size_t i;
    size_t seg_start;

    if (!name || !name[0]) return -1;
    if (name[0] == '/') return -1;

    seg_start = 0;
    for (i = 0; ; i++) {
        char c = name[i];
        if (c == '\0' || c == '/') {
            size_t seglen = i - seg_start;
            if (seglen == 0 && i != 0) {
                /* empty segment ("/foo//bar") -- reject */
                return -1;
            }
            if (seglen == 2
                    && name[seg_start    ] == '.'
                    && name[seg_start + 1] == '.') {
                return -1;
            }
            if (c == '\0') return 0;
            seg_start = i + 1;
        }
    }
}

/*
 * Tokenize an absolute path into segments, applying "." (skip) and
 * ".." (pop, but never below 0).  Returns the number of segments.
 */
static size_t
tokenize(const char *path,
         const char *segs[],
         size_t lens[],
         size_t cap)
{
    size_t nseg = 0;
    size_t i = 0;
    while (path[i]) {
        size_t start;
        size_t len;
        if (path[i] == '/') { i++; continue; }
        start = i;
        while (path[i] && path[i] != '/') i++;
        len = i - start;
        if (len == 1 && path[start] == '.') continue;
        if (len == 2 && path[start] == '.' && path[start + 1] == '.') {
            if (nseg > 0) nseg--;
            continue;
        }
        if (nseg >= cap) return cap;
        segs[nseg] = path + start;
        lens[nseg] = len;
        nseg++;
    }
    return nseg;
}

/*
 * Lexically resolve `joined` and check it stays inside `root` after
 * normalization.  Returns 0 if safe, -1 if not.
 */
static int
lex_resolve_inside(const char *root, const char *joined)
{
    const char *rsegs[64];
    size_t     rlens[64];
    const char *jsegs[256];
    size_t     jlens[256];
    size_t rcount = tokenize(root, rsegs, rlens, 64);
    size_t jcount = tokenize(joined, jsegs, jlens, 256);
    size_t i;
    size_t k;

    if (rcount == 64 || jcount == 256) return -1;  /* overflow */
    if (jcount < rcount) return -1;                /* shallower than root */

    for (i = 0; i < rcount; i++) {
        if (jlens[i] != rlens[i]) return -1;
        for (k = 0; k < rlens[i]; k++) {
            if (jsegs[i][k] != rsegs[i][k]) return -1;
        }
    }
    return 0;
}

int
lcsas_path_safe_symlink(const char *root,
                        const char *from_dir,
                        const char *target)
{
    /* Build "from_dir/target" if target is relative, else "target". */
    char joined[8192];
    size_t off = 0;
    size_t i;

    if (!target || !target[0]) return -1;
    if (target[0] == '/') {
        /* Absolute target: allowed (issue #187 — a decision to
         * match tier-2 / rustic behaviour).  The restore-tree
         * containment property is no longer enforced for absolute
         * symlinks; heirs who rely on containment must sandbox
         * the restore target separately.  Relative symlinks are
         * still resolved + validated via lex_resolve_inside below. */
        return 0;
    }

    /* Copy from_dir, ensure trailing slash. */
    for (i = 0; from_dir[i] && off < sizeof(joined) - 1; i++) {
        joined[off++] = from_dir[i];
    }
    if (off > 0 && joined[off - 1] != '/' && off < sizeof(joined) - 1) {
        joined[off++] = '/';
    }
    for (i = 0; target[i] && off < sizeof(joined) - 1; i++) {
        joined[off++] = target[i];
    }
    if (off == sizeof(joined) - 1) return -1;
    joined[off] = '\0';

    return lex_resolve_inside(root, joined);
}

/* ── Issue #781: node-name unescaping ─────────────────────────── */

/* Value of one hex digit, or -1. */
static int
hex_val(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Read exactly `n` hex digits at in[*i]; advance *i.  Returns the value
 * or -1 when fewer than n digits are left or one is not a hex digit
 * (strict: rustic's from_str_radix would also take a leading '+'). */
static long
read_hex(const char *in, size_t in_len, size_t *i, int n)
{
    unsigned long v = 0;
    int k;
    if (in_len - *i < (size_t)n) return -1;
    for (k = 0; k < n; k++) {
        int d = hex_val(in[*i + (size_t)k]);
        if (d < 0) return -1;
        v = (v << 4) | (unsigned long)d;
    }
    *i += (size_t)n;
    /* 8 digits can exceed LONG_MAX on 32-bit; anything above U+10FFFF
     * is refused by the caller anyway, so clamp to a sentinel. */
    if (v > 0x10FFFFUL) return 0x110000L;
    return (long)v;
}

long
lcsas_unescape_name(const char *in, size_t in_len, char *out, size_t cap)
{
    size_t i = 0, j = 0;

    if (!in || !out || cap == 0) return -1;
    while (i < in_len) {
        unsigned char b[4];
        size_t nb = 1;
        char c = in[i++];

        if (c != '\\') {
            b[0] = (unsigned char)c;
        } else {
            long v;
            if (i >= in_len) return -1;        /* trailing backslash */
            c = in[i++];
            switch (c) {
            case '\\': b[0] = '\\'; break;
            case '"':  b[0] = '"';  break;
            case '\'': b[0] = '\''; break;
            case '`':  b[0] = '`';  break;
            case 'a':  b[0] = 0x07; break;
            case 'b':  b[0] = 0x08; break;
            case 'f':  b[0] = 0x0c; break;
            case 'n':  b[0] = 0x0a; break;
            case 'r':  b[0] = 0x0d; break;
            case 't':  b[0] = 0x09; break;
            case 'v':  b[0] = 0x0b; break;
            case 'x':
                v = read_hex(in, in_len, &i, 2);
                if (v < 0) return -1;
                b[0] = (unsigned char)v;
                break;
            case 'u':
            case 'U':
                v = read_hex(in, in_len, &i, c == 'u' ? 4 : 8);
                if (v < 0 || v > 0x10FFFFL
                        || (v >= 0xD800L && v <= 0xDFFFL)) {
                    return -1;
                }
                /* UTF-8 encode the code point. */
                if (v < 0x80L) {
                    b[0] = (unsigned char)v;
                } else if (v < 0x800L) {
                    b[0] = (unsigned char)(0xC0 | (v >> 6));
                    b[1] = (unsigned char)(0x80 | (v & 0x3F));
                    nb = 2;
                } else if (v < 0x10000L) {
                    b[0] = (unsigned char)(0xE0 | (v >> 12));
                    b[1] = (unsigned char)(0x80 | ((v >> 6) & 0x3F));
                    b[2] = (unsigned char)(0x80 | (v & 0x3F));
                    nb = 3;
                } else {
                    b[0] = (unsigned char)(0xF0 | (v >> 18));
                    b[1] = (unsigned char)(0x80 | ((v >> 12) & 0x3F));
                    b[2] = (unsigned char)(0x80 | ((v >> 6) & 0x3F));
                    b[3] = (unsigned char)(0x80 | (v & 0x3F));
                    nb = 4;
                }
                break;
            default:
                return -1;                     /* unknown escape */
            }
        }
        if (cap - j <= nb) return -1;          /* keep room for the NUL */
        memcpy(out + j, b, nb);
        j += nb;
    }
    out[j] = '\0';
    return (long)j;
}

const char *
lcsas_name_refusal(const char *name, size_t len)
{
    if (len == 0) return "the name is empty";
    if (memchr(name, '\0', len)) return "the name holds a NUL byte";
    if ((len == 1 && name[0] == '.')
            || (len == 2 && name[0] == '.' && name[1] == '.')) {
        return "the name is '.' or '..'";
    }
    if (memchr(name, '/', len)) return "the name holds '/'";
    return NULL;
}

/* Length of the valid UTF-8 sequence at s[0..n), or 0 if it is not
 * one (overlong forms, surrogates and values above U+10FFFF are
 * invalid, as in Rust's str::from_utf8 and Python's utf-8 codec). */
static size_t
utf8_seq_len(const unsigned char *s, size_t n)
{
    unsigned char c = s[0];
    size_t need, k;
    unsigned char lo = 0x80, hi = 0xBF;

    if (c < 0x80) return 1;
    if (c >= 0xC2 && c <= 0xDF) need = 2;
    else if (c >= 0xE0 && c <= 0xEF) {
        need = 3;
        if (c == 0xE0) lo = 0xA0;              /* no overlongs */
        if (c == 0xED) hi = 0x9F;              /* no surrogates */
    } else if (c >= 0xF0 && c <= 0xF4) {
        need = 4;
        if (c == 0xF0) lo = 0x90;              /* no overlongs */
        if (c == 0xF4) hi = 0x8F;              /* <= U+10FFFF */
    } else {
        return 0;
    }
    if (n < need) return 0;
    if (s[1] < lo || s[1] > hi) return 0;
    for (k = 2; k < need; k++) {
        if (s[k] < 0x80 || s[k] > 0xBF) return 0;
    }
    return need;
}

int
lcsas_name_is_utf8(const char *name, size_t len)
{
    const unsigned char *s = (const unsigned char *)name;
    size_t i = 0;
    while (i < len) {
        size_t n = utf8_seq_len(s + i, len - i);
        if (n == 0) return 0;
        i += n;
    }
    return 1;
}

const char *
lcsas_name_windows_refusal(const char *name, size_t len)
{
    size_t i;
    for (i = 0; i < len; i++) {
        unsigned char c = (unsigned char)name[i];
        if (c < 0x20 || strchr("\\:*?\"<>|", (int)c) != NULL) {
            return "Windows cannot store a name that holds "
                   "\\ : * ? \" < > | or a control character";
        }
    }
    if (!lcsas_name_is_utf8(name, len)) {
        return "Windows cannot store a name that is not valid UTF-8";
    }
    return NULL;
}
