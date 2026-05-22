/*
 * io.h -- POSIX I/O wrappers (EINTR-safe; portable to any UNIX).
 */
#ifndef LCSAS_IO_H
#define LCSAS_IO_H

#include <stddef.h>

/*
 * Read exactly `len` bytes from `path` into a newly malloc'd buffer.
 * Returns 0 on success and stores buffer+length in *buf and *len_out
 * (caller frees with `free`).  Returns non-zero on error.
 */
int lcsas_read_file(const char *path, unsigned char **buf, size_t *len_out);

/*
 * Read exactly `len` bytes starting at `off` from open fd `fd` into
 * `buf`.  Loops on short reads and EINTR.  Returns 0 on success.
 */
int lcsas_pread_exact(int fd, void *buf, size_t len, long long off);

/*
 * Write exactly `len` bytes to fd, looping on EINTR / short writes.
 */
int lcsas_write_exact(int fd, const void *buf, size_t len);

/*
 * Create (or overwrite) a regular file with the given mode bits
 * (umask-stripped during open(); restored via fchmod() after).
 * Pass the mode from the restic tree node's "mode" field so the
 * restored file matches the original.  Returns fd or -1 on error.
 * The intermediate directories must already exist.
 */
int lcsas_create_file(const char *path, unsigned int mode);

/*
 * Create directory `path` and all parents (mode 0700).  Returns 0 on
 * success or if the directory already exists.
 */
int lcsas_mkdir_p(const char *path);

/*
 * Parse a restic-style RFC 3339 / ISO 8601 timestamp into a POSIX
 * seconds/nanoseconds pair (UTC epoch).  Accepts:
 *
 *   YYYY-MM-DDTHH:MM:SS[.fraction]<tz>
 *
 * where <tz> is one of `Z`, `+HH:MM`, or `-HH:MM`.  Rustic emits
 * `+00:00` (not `Z`) by default; we MUST accept both or every restore
 * silently loses mtime (see issue #188).
 *
 * Returns 0 on success and writes the result to *out_sec / *out_nsec.
 * Returns -1 on any parse error; *out_sec / *out_nsec are left
 * unchanged in that case.
 *
 * Implementation note: uses Howard Hinnant's days-from-civil algorithm
 * (pure integer arithmetic) — does NOT depend on timegm/mktime/tzdata,
 * so it is portable to musl, mingw-w64, and macOS without surprise.
 */
int lcsas_parse_iso8601_utc(const char *s,
                            long long *out_sec,
                            long *out_nsec);

#endif
