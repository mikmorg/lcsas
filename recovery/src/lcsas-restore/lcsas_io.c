/*
 * io.c -- POSIX I/O wrappers.
 *
 * Strict C89 + POSIX.1-2001.  Requires `unistd.h` for read/write/lseek
 * and `sys/stat.h` for `mkdir` / `fstat`.
 */
#include "lcsas_io.h"
#include "posix_compat.h"

#include <errno.h>
#include <stdlib.h>

int
lcsas_pread_exact(int fd, void *buf, size_t len, long long off)
{
    unsigned char *p = (unsigned char *)buf;
    while (len > 0) {
        ssize_t n;
        if (lseek(fd, (off_t)off, SEEK_SET) == (off_t)-1) return -1;
        n = read(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (n == 0) return -1;             /* unexpected EOF */
        p += n;
        len -= (size_t)n;
        off += n;
    }
    return 0;
}

int
lcsas_write_exact(int fd, const void *buf, size_t len)
{
    const unsigned char *p = (const unsigned char *)buf;
    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

int
lcsas_read_file(const char *path, unsigned char **buf, size_t *len_out)
{
    int fd;
    struct stat st;
    unsigned char *p;
    size_t total;
    ssize_t n;

    /* O_BINARY is required on Windows: open() defaults to text mode
     * there, which corrupts binary reads with \r\n translation.  On
     * POSIX, O_BINARY is defined to 0 in posix_compat.h. */
    fd = open(path, O_RDONLY | O_BINARY);
    if (fd < 0) return -1;
    if (fstat(fd, &st) < 0) { close(fd); return -1; }
    if (st.st_size < 0) { close(fd); return -1; }

    total = (size_t)st.st_size;
    p = (unsigned char *)malloc(total + 1);   /* +1 for NUL ergonomic */
    if (!p) { close(fd); return -1; }

    {
        size_t off = 0;
        while (off < total) {
            n = read(fd, p + off, total - off);
            if (n < 0) {
                if (errno == EINTR) continue;
                free(p); close(fd); return -1;
            }
            if (n == 0) break;
            off += (size_t)n;
        }
        if (off != total) { free(p); close(fd); return -1; }
        p[total] = 0;
    }

    close(fd);
    *buf = p;
    *len_out = total;
    return 0;
}

int
lcsas_create_file(const char *path)
{
    /* O_BINARY no-op on POSIX; on Windows it prevents \r\n translation
     * that would corrupt restored binary files. */
    return open(path, O_WRONLY | O_CREAT | O_TRUNC | O_BINARY, 0600);
}

int
lcsas_mkdir_p(const char *path)
{
    char tmp[4096];
    size_t i, len = 0;
    int rc;

    while (path[len] && len < sizeof(tmp) - 1) {
        tmp[len] = path[len];
        len++;
    }
    if (len == sizeof(tmp) - 1) return -1;       /* path too long */
    tmp[len] = '\0';

    /* Walk forwards, calling mkdir() at each separator. */
    for (i = 1; i <= len; i++) {
        if (tmp[i] == '/' || tmp[i] == '\0') {
            char saved = tmp[i];
            tmp[i] = '\0';
            rc = mkdir(tmp, 0700);
            if (rc < 0 && errno != EEXIST) return -1;
            tmp[i] = saved;
        }
    }
    return 0;
}
