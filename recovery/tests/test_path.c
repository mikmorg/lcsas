/*
 * test_path.c -- path-traversal safety.  Mirrors
 * tests/unit/test_restic_fallback_path_traversal.py.
 */
#include "path.h"
#include <stdio.h>

static int fails = 0;

static void
expect_safe(const char *label, const char *name, int want_ok)
{
    int rc = lcsas_path_safe_name(name);
    int got_ok = (rc == 0);
    if (got_ok != want_ok) {
        fprintf(stderr, "FAIL %s name='%s' rc=%d (want %s)\n",
                label, name, rc, want_ok ? "OK" : "REJECT");
        fails++;
    }
}

static void
expect_sym(const char *label, const char *root, const char *from,
           const char *target, int want_ok)
{
    int rc = lcsas_path_safe_symlink(root, from, target);
    int got_ok = (rc == 0);
    if (got_ok != want_ok) {
        fprintf(stderr, "FAIL %s root=%s from=%s tgt=%s rc=%d (want %s)\n",
                label, root, from, target, rc, want_ok ? "OK" : "REJECT");
        fails++;
    }
}

int main(void)
{
    expect_safe("plain",         "foo/bar",       1);
    expect_safe("deep",          "a/b/c/d.txt",   1);
    expect_safe("dotdot",        "foo/../bar",    0);
    expect_safe("dotdot-start",  "../bar",        0);
    expect_safe("absolute",      "/etc/passwd",   0);
    expect_safe("empty",         "",              0);
    expect_safe("trailing-slash","foo//bar",      0);
    expect_safe("dot-only",      ".",             1);

    /* Symlinks */
    expect_sym("relative inside", "/restore", "/restore/a", "b",        1);
    expect_sym("relative escape", "/restore", "/restore/a", "../../etc/passwd", 0);
    /* Absolute targets allowed (issue #187 — user decision to match
     * tier-2 / rustic behaviour).  See path.c comment for the
     * containment-property tradeoff. */
    expect_sym("absolute",        "/restore", "/restore/a", "/etc/passwd",      1);
    expect_sym("dotdot to root",  "/restore", "/restore/a", "..",       1);  /* lands at /restore */
    expect_sym("dotdot past root","/restore", "/restore",   "..",       0);

    if (fails == 0) printf("test_path: OK\n");
    return fails ? 1 : 0;
}
