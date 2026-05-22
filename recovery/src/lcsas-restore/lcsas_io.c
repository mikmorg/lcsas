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
lcsas_create_file(const char *path, unsigned int mode)
{
    int fd;
    /* O_BINARY no-op on POSIX; on Windows it prevents \r\n translation
     * that would corrupt restored binary files.  Mode is stripped by
     * the process umask during open(); fchmod() below restores the
     * requested bits so tier-1 matches tier-2 (rustic) parity. */
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_BINARY,
              (mode_t)(mode & 0777));
    if (fd < 0) return -1;
#ifndef _WIN32
    /* On Windows fchmod doesn't carry POSIX semantics; skip there. */
    if (fchmod(fd, (mode_t)(mode & 07777)) != 0) {
        /* Non-fatal: caller still gets the fd.  Mode parity is
         * best-effort if e.g. the filesystem doesn't support it. */
    }
#endif
    return fd;
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

/* Parse a fixed-width run of `n` decimal digits starting at *pp.
 * Advances *pp past the digits on success.  Returns 0/-1. */
static int
parse_fixed_digits(const char **pp, int n, long *out)
{
    const char *p = *pp;
    long v = 0;
    int i;
    for (i = 0; i < n; i++) {
        if (p[i] < '0' || p[i] > '9') return -1;
        v = v * 10 + (p[i] - '0');
    }
    *pp = p + n;
    *out = v;
    return 0;
}

/* Howard Hinnant's days-from-civil algorithm (public domain).  Given a
 * proleptic Gregorian civil date (y, m, d) with 1 <= m <= 12 and
 * 1 <= d <= 31, returns the number of days from 1970-01-01 (negative
 * for dates before).  Pure integer arithmetic — no libc, no tzdata. */
static long long
days_from_civil(long y, long m, long d)
{
    long long yy = (long long)(m <= 2 ? y - 1 : y);
    long long era = (yy >= 0 ? yy : yy - 399) / 400;
    long long yoe = yy - era * 400;                 /* [0, 399]     */
    long long mp = (m + (m > 2 ? -3 : 9));          /* [0, 11]      */
    long long doy = (153 * mp + 2) / 5 + d - 1;     /* [0, 365]     */
    long long doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; /* [0, 146096] */
    return era * 146097 + doe - 719468;
}

int
lcsas_parse_iso8601_utc(const char *s, long long *out_sec, long *out_nsec)
{
    /* Format: YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM|-HH:MM)
     *
     * We do not allow trailing junk — restic emits a clean RFC 3339
     * timestamp, so anything else is a sign of corruption or unknown
     * provenance and we'd rather skip than guess.
     */
    long year = 0, mon = 0, day = 0, hour = 0, min = 0, sec = 0;
    long nsec = 0;
    long long secs;
    int tz_sign = 0;
    long tz_h = 0, tz_m = 0;
    const char *p;

    if (!s || !out_sec || !out_nsec) return -1;
    p = s;

    if (parse_fixed_digits(&p, 4, &year) != 0) return -1;
    if (*p++ != '-') return -1;
    if (parse_fixed_digits(&p, 2, &mon) != 0) return -1;
    if (*p++ != '-') return -1;
    if (parse_fixed_digits(&p, 2, &day) != 0) return -1;
    if (*p++ != 'T' && *(p - 1) != 't') return -1;
    if (parse_fixed_digits(&p, 2, &hour) != 0) return -1;
    if (*p++ != ':') return -1;
    if (parse_fixed_digits(&p, 2, &min) != 0) return -1;
    if (*p++ != ':') return -1;
    if (parse_fixed_digits(&p, 2, &sec) != 0) return -1;

    /* Optional fractional seconds.  Restic emits up to 9 digits
     * (nanoseconds).  We accept any digit count, capture up to 9, and
     * silently truncate extra precision rather than failing. */
    if (*p == '.') {
        int i = 0;
        long scale = 100000000L;     /* 1e8: digit 0 -> nsec */
        p++;
        while (*p >= '0' && *p <= '9') {
            if (i < 9) {
                nsec += (long)(*p - '0') * scale;
                scale /= 10;
            }
            p++;
            i++;
        }
        if (i == 0) return -1;       /* "12.X" with no digits */
    }

    /* Mandatory timezone.  rustic emits `+HH:MM` (or `-HH:MM`); the
     * RFC 3339 / ISO 8601 alias `Z` means +00:00. */
    if (*p == 'Z' || *p == 'z') {
        tz_sign = 0;
        p++;
    } else if (*p == '+' || *p == '-') {
        tz_sign = (*p == '-') ? -1 : 1;
        p++;
        if (parse_fixed_digits(&p, 2, &tz_h) != 0) return -1;
        if (*p++ != ':') return -1;
        if (parse_fixed_digits(&p, 2, &tz_m) != 0) return -1;
    } else {
        return -1;
    }

    if (*p != '\0') return -1;       /* trailing garbage */

    /* Sanity-clamp the fields.  We don't try to be a full calendar
     * validator; days_from_civil is well-defined for any input but
     * obviously wrong values (hour 99) should be rejected. */
    if (mon < 1 || mon > 12) return -1;
    if (day < 1 || day > 31) return -1;
    if (hour < 0 || hour > 23) return -1;
    if (min < 0 || min > 59) return -1;
    if (sec < 0 || sec > 60) return -1;       /* 60 = leap second */
    if (tz_h < 0 || tz_h > 23) return -1;
    if (tz_m < 0 || tz_m > 59) return -1;

    secs = days_from_civil(year, mon, day) * 86400LL
         + (long long)hour * 3600
         + (long long)min * 60
         + (long long)sec;
    if (tz_sign != 0) {
        long long off = (long long)tz_h * 3600 + (long long)tz_m * 60;
        secs -= (long long)tz_sign * off;
    }

    *out_sec = secs;
    *out_nsec = nsec;
    return 0;
}
