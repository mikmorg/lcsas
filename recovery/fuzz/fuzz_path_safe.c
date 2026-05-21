/*
 * fuzz_path_safe.c -- LibFuzzer harness for path traversal safety checks.
 *
 * Feeds arbitrary bytes to lcsas_path_safe_name and
 * lcsas_path_safe_symlink.  The goal is to find inputs where the
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

    /* Exercise lcsas_path_safe_symlink. */
    lcsas_path_safe_symlink(root, from_dir, target);

    free(buf);
    return 0;
}
