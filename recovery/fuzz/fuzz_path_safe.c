/*
 * fuzz_path_safe.c -- LibFuzzer harness for path traversal safety checks.
 *
 * Feeds arbitrary bytes to lcsas_path_safe_name (and, #619, its
 * Windows rule lcsas_path_safe_name_windows) and
 * lcsas_path_safe_symlink, and (issue #781) the whole input to the
 * node-name decoder lcsas_unescape_name and the decoded-name checks
 * lcsas_name_refusal / lcsas_name_windows_refusal.  The goal is to find inputs where the
 * sanitizer returns 0 (safe) for a genuinely unsafe path, or where the
 * implementation crashes/reads out-of-bounds.
 *
 * Input layout (split on the first two '\0' bytes):
 *   root\0from_dir\0target
 * If fewer than two '\0' bytes are present, the remaining parts default
 * to empty strings.
 *
 * Compile:
 *   make -C recovery fuzz-path-smoke
 */
#include "path.h"
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    /* We need NUL-terminated strings; work on a copy. */
    char *buf;
    const char *root, *from_dir, *target;
    const char *p0, *p1;
    size_t off0, off1;
    int rc1, rc2;

    if (size == 0) return 0;

    buf = (char *)malloc(size + 1);
    if (buf == NULL) return 0;
    memcpy(buf, data, size);
    buf[size] = '\0';

    /* Locate up to two NUL separators in the fuzz input. */
    p0 = (const char *)memchr(buf, '\0', size);
    if (p0 == NULL) {
        /* No separator: treat entire buf as name, use empty for symlink args */
        root = buf; from_dir = ""; target = "";
        off0 = size;
        off1 = off0;
    } else {
        off0 = (size_t)(p0 - buf);
        p1 = (const char *)memchr(p0 + 1, '\0', size - off0 - 1);
        if (p1 == NULL) {
            root = buf; from_dir = p0 + 1; target = "";
            off1 = size;
        } else {
            off1 = (size_t)(p1 - buf);
            root = buf; from_dir = p0 + 1; target = p1 + 1;
        }
    }

    /* Exercise lcsas_path_safe_name on each segment. */
    rc1 = lcsas_path_safe_name(root);
    rc2 = lcsas_path_safe_name(from_dir);
    (void)rc1; (void)rc2;

    /* Issue #619: a path the Windows rule accepts holds no ':', does
     * not start with a separator, and has no ".." segment when split
     * on both '/' and '\'. */
    if (lcsas_path_safe_name_windows(root) == 0) {
        size_t k, seg = 0;
        if (strchr(root, ':') || root[0] == '/' || root[0] == '\\') abort();
        for (k = 0; ; k++) {
            char c = root[k];
            if (c == '\0' || c == '/' || c == '\\') {
                if (k - seg == 2 && root[seg] == '.' && root[seg + 1] == '.') {
                    abort();
                }
                if (c == '\0') break;
                seg = k + 1;
            }
        }
    }

    /* Exercise lcsas_path_safe_symlink. */
    lcsas_path_safe_symlink(root, from_dir, target);

    /* Issue #781: decode the whole input as an escaped node name.  The
     * decoded name is never longer than the text and is NUL-terminated;
     * a name the checks accept is one plain directory entry. */
    {
        char *dec = (char *)malloc(size + 1);
        long n;
        if (dec == NULL) { free(buf); return 0; }
        n = lcsas_unescape_name((const char *)data, size, dec, size + 1);
        if (n >= 0) {
            if ((size_t)n > size || dec[n] != '\0') abort();
            if (lcsas_name_refusal(dec, (size_t)n) == NULL) {
                if (n == 0 || memchr(dec, '/', (size_t)n)
                        || memchr(dec, '\0', (size_t)n)
                        || strcmp(dec, ".") == 0
                        || strcmp(dec, "..") == 0) {
                    abort();
                }
                if (lcsas_name_windows_refusal(dec, (size_t)n) == NULL
                        && (memchr(dec, '\\', (size_t)n)
                            || memchr(dec, ':', (size_t)n)
                            || dec[n - 1] == '.' || dec[n - 1] == ' ')) {
                    abort();
                }
            }
        }
        free(dec);
        /* A buffer one byte short of the worst case must never be
         * overrun (ASan catches a write past this exact-size block). */
        dec = (char *)malloc(size);
        if (dec != NULL) {
            (void)lcsas_unescape_name((const char *)data, size, dec, size);
            free(dec);
        }
    }

    free(buf);
    return 0;
}
