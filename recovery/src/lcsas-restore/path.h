/*
 * path.h -- path traversal safety.
 *
 * Reproduces the Python checks at
 * tests/unit/test_restic_fallback_path_traversal.py:
 *   - Reject absolute paths.
 *   - Reject any segment equal to "..".
 *   - Reject NUL bytes in path components.
 *   - lcsas_path_safe_symlink: report a symlink target that, joined
 *     against the restore target, escapes the restore root.  The tree
 *     walk no longer calls it (#782): every link is restored as
 *     stored, as rustic does, and tree.c instead never writes through
 *     a link (#784).  Kept, with its unit and fuzz tests.
 */
#ifndef LCSAS_PATH_H
#define LCSAS_PATH_H

#include <stddef.h>

/*
 * Validate that `name` is a safe relative path: not empty, not
 * absolute, no empty segment and no ".." segment.  Returns 0 if safe;
 * non-zero on rejection.  The Windows build applies the Windows rule
 * (lcsas_path_safe_name_windows).  The tree walk checks each decoded
 * name with lcsas_name_refusal and lcsas_name_windows_refusal, which
 * are stricter; this one is kept, with its unit and fuzz tests.
 */
int lcsas_path_safe_name(const char *name);

/*
 * Issue #619 -- the Windows rule, compiled on every platform so it can
 * be tested anywhere: '\' separates segments as '/' does, so "..\x"
 * and "a\..\b" are refused, as are a leading '\' and any ':' (a
 * drive prefix such as "C:", or "name:stream", an NTFS alternate data
 * stream).
 */
int lcsas_path_safe_name_windows(const char *name);

/*
 * Validate that `target`, when resolved relative to `from_dir` under
 * `root`, stays inside `root` after lexical normalization.  All three
 * are NUL-terminated.  Returns 0 if safe.
 */
int lcsas_path_safe_symlink(const char *root,
                            const char *from_dir,
                            const char *target);

/*
 * Issue #781 -- undo the escaping of a tree node's "name".
 *
 * rustic (and restic) store a name as Go strconv.Quote text without
 * the outer quotes when the name holds '"', '\', a control character
 * or a byte that is not valid UTF-8: `quo"te` is stored as `quo\"te`,
 * a tab as `\t`, the Latin-1 byte 0xE9 as `\xe9`.  This is rustic's
 * unescape_filename: the escapes \\ \" \' \` \a \b \f \n \r \t \v,
 * \xHH (one raw byte), \uHHHH and \UHHHHHHHH (a code point, written
 * as UTF-8).  Every other character is copied as is.
 *
 * `in` is the JSON-decoded name (`in_len` bytes).  Writes the raw name
 * plus a terminating NUL to `out` (`cap` bytes); the result is never
 * longer than `in`, so cap >= in_len + 1 always fits.  Returns the
 * raw length (the name may hold NUL bytes: check it with
 * lcsas_name_refusal), or -1 when the text cannot be decoded (unknown
 * escape, trailing '\', short or non-hex digits, a surrogate or a code
 * point above U+10FFFF) or `out` is too small.  rustic would use such
 * a name literally; we refuse it, so it is never restored under a
 * name nobody chose.
 */
long lcsas_unescape_name(const char *in, size_t in_len,
                         char *out, size_t cap);

/*
 * Path-safety check on a DECODED name (`len` raw bytes).  Returns NULL
 * when the name can be created as one directory entry, or a short
 * reason when it is empty, ".", "..", or holds '/' or a NUL byte.
 */
const char *lcsas_name_refusal(const char *name, size_t len);

/*
 * The extra rule on Windows: NULL when NTFS can store the decoded name,
 * else a short reason: the name holds \ : * ? " < > | or a control
 * character, is not valid UTF-8, ends in a dot or a space (#619:
 * Windows would drop it), is a device name (CON, PRN, AUX, NUL,
 * COM1-9, LPT1-9, alone or with an extension, in any case), or is
 * longer than 255 UTF-16 units.  Compiled on every platform so it
 * can be tested anywhere; tree.c calls it only in the Windows build.
 */
const char *lcsas_name_windows_refusal(const char *name, size_t len);

/*
 * 1 when the `len` bytes are valid UTF-8 (no overlong forms, no
 * surrogates, nothing above U+10FFFF), else 0.  macOS file systems
 * (APFS, HFS+) refuse any other name, so tree.c refuses it up front
 * in the macOS build rather than fail the whole walk on the create.
 */
int lcsas_name_is_utf8(const char *name, size_t len);

#endif
