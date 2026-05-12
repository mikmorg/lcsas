/*
 * path.h -- path traversal safety.
 *
 * Reproduces the Python checks at
 * tests/unit/test_restic_fallback_path_traversal.py:
 *   - Reject absolute paths.
 *   - Reject any segment equal to "..".
 *   - Reject NUL bytes in path components.
 *   - Reject symlink targets that, when joined against the restore
 *     target, escape the restore root after normalization.
 */
#ifndef LCSAS_PATH_H
#define LCSAS_PATH_H

#include <stddef.h>

/*
 * Validate that `name` is a safe restic tree-blob name.  Returns 0 if
 * safe; non-zero on rejection.
 */
int lcsas_path_safe_name(const char *name);

/*
 * Validate that `target`, when resolved relative to `from_dir` under
 * `root`, stays inside `root` after lexical normalization.  All three
 * are NUL-terminated.  Returns 0 if safe.
 */
int lcsas_path_safe_symlink(const char *root,
                            const char *from_dir,
                            const char *target);

#endif
