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
        } else if (c == '\0') {
            return 0;
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
        /* Absolute target: forbid entirely.  This matches the
         * Python rule that an absolute symlink can never be safely
         * dropped onto a restore tree. */
        return -1;
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
