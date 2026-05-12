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
 * Create a file (or overwrite), 0600.  Returns fd or -1 on error.
 * The intermediate directories must already exist.
 */
int lcsas_create_file(const char *path);

/*
 * Create directory `path` and all parents (mode 0700).  Returns 0 on
 * success or if the directory already exists.
 */
int lcsas_mkdir_p(const char *path);

#endif
