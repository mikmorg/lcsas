/*
 * io.c -- POSIX I/O wrappers.
 *
 * Strict C89 + POSIX.1-2001.  Requires `unistd.h` for read/write/lseek
 * and `sys/stat.h` for `mkdir` / `fstat`.
 */
#ifdef _WIN32
/* System headers: they go before posix_compat.h's shim macros. */
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <stdarg.h>
#  include <string.h>
#  include <wchar.h>
#endif
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
        if (n == 0) {
            /* Unexpected EOF: the source shrank under us (truncated /
             * disc ejected mid-read).  Set errno so callers can
             * classify this as an I/O-loss event (issue #222) rather
             * than a generic "read failed". */
            errno = EIO;
            return -1;
        }
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
     * that would corrupt restored binary files.
     *
     * Issue #655 — on POSIX the file is created owner-read/write (0600)
     * and stays that way while its content is written.  The caller
     * (restore_file_node) applies the node's final mode after the last
     * blob.  Setting a read-only final mode here meant a file left
     * part-written (q at the swap prompt, Ctrl-C, power loss, disk
     * full) could never be reopened by a resume: EACCES on every run.
     * A part-written read-only file left by an older build is made
     * writable once and the open retried. */
#ifndef _WIN32
    (void)mode;
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_BINARY | O_NOFOLLOW,
              (mode_t)0600);
    if (fd < 0 && errno == EACCES) {
        struct stat st;
        if (lstat(path, &st) == 0 && S_ISREG(st.st_mode)
                && chmod(path, (mode_t)0600) == 0) {
            fd = open(path,
                      O_WRONLY | O_CREAT | O_TRUNC | O_BINARY | O_NOFOLLOW,
                      (mode_t)0600);
        }
    }
#else
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_BINARY,
              (mode_t)(mode & 0777));
#endif
    return fd;
}

int
lcsas_entry_kind(const char *path)
{
#ifdef _WIN32
    /* #619: the wide call, so a UTF-8 name is not read as ANSI. */
    wchar_t *w = lcsas_w_path(path);
    DWORD a = w ? GetFileAttributesW(w) : INVALID_FILE_ATTRIBUTES;
    free(w);
    if (a == INVALID_FILE_ATTRIBUTES) return LCSAS_ENTRY_NONE;
    if (a & FILE_ATTRIBUTE_REPARSE_POINT) return LCSAS_ENTRY_LINK;
    if (a & FILE_ATTRIBUTE_DIRECTORY) return LCSAS_ENTRY_DIR;
    return LCSAS_ENTRY_FILE;
#else
    struct stat st;
    if (lstat(path, &st) != 0) return LCSAS_ENTRY_NONE;
    if (S_ISLNK(st.st_mode)) return LCSAS_ENTRY_LINK;
    if (S_ISDIR(st.st_mode)) return LCSAS_ENTRY_DIR;
    if (S_ISREG(st.st_mode)) return LCSAS_ENTRY_FILE;
    return LCSAS_ENTRY_OTHER;
#endif
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

    /* Walk forwards, calling mkdir() at each separator.  On Windows
     * '\' separates too and a leading drive is skipped (posix_compat.h). */
    for (i = LCSAS_PATH_START(tmp); i <= len; i++) {
        if (LCSAS_IS_SEP(tmp[i]) || tmp[i] == '\0') {
            char saved = tmp[i];
            tmp[i] = '\0';
#ifdef _WIN32
            {
                /* An existing folder: nothing to make.  MSVCRT's mkdir
                 * on a drive root fails with EACCES, not EEXIST. */
                struct stat st;
                if (stat(tmp, &st) == 0 && S_ISDIR(st.st_mode)) {
                    tmp[i] = saved;
                    continue;
                }
            }
#endif
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

#ifdef _WIN32
/* ── Issue #619: UTF-8 paths through the wide (UTF-16) Windows API ──
 *
 * posix_compat.h maps the file calls to these.  Each one converts its
 * UTF-8 path with lcsas_w_path and calls the CRT's or Win32's W
 * function, so a name is never read in the ANSI code page.  The real
 * functions are named with their wide spelling here (_wopen, ...), so
 * the macros do not apply to them. */

/* The longest path (in UTF-16 units, without the NUL) used as is: a
 * folder cannot be made at MAX_PATH - 12 or longer without \\?\. */
#define LCSAS_W_SHORT_MAX 247

static wchar_t *
utf8_to_wide(const char *s)
{
    int n;
    wchar_t *w;
    if (!s) { errno = EINVAL; return NULL; }
    n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, -1, NULL, 0);
    if (n <= 0) { errno = EILSEQ; return NULL; }
    w = (wchar_t *)malloc((size_t)n * sizeof(wchar_t));
    if (!w) { errno = ENOMEM; return NULL; }
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, -1, w, n)
            != n) {
        free(w);
        errno = EILSEQ;
        return NULL;
    }
    return w;
}

char *
lcsas_w_to_utf8(const wchar_t *w)
{
    int n;
    char *s;
    if (!w) return NULL;
    n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w, -1,
                            NULL, 0, NULL, NULL);
    if (n <= 0) { errno = EILSEQ; return NULL; }
    s = (char *)malloc((size_t)n);
    if (!s) { errno = ENOMEM; return NULL; }
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, w, -1,
                            s, n, NULL, NULL) != n) {
        free(s);
        errno = EILSEQ;
        return NULL;
    }
    return s;
}

wchar_t *
lcsas_w_path(const char *path)
{
    wchar_t *w = utf8_to_wide(path);
    wchar_t *full, *out;
    DWORD need, got;
    size_t flen;
    int unc;

    if (!w) return NULL;
    /* Already a \\?\ or \\.\ path: Windows takes it as it is. */
    if (w[0] == L'\\' && w[1] == L'\\'
            && (w[2] == L'?' || w[2] == L'.') && w[3] == L'\\') {
        return w;
    }
    /* A relative path can be long once the current folder is put in
     * front of it, so look at the full path, not just at `w`. */
    need = GetFullPathNameW(w, 0, NULL, NULL);
    if (need == 0) return w;
    full = (wchar_t *)malloc((size_t)need * sizeof(wchar_t));
    if (!full) { free(w); errno = ENOMEM; return NULL; }
    got = GetFullPathNameW(w, need, full, NULL);
    if (got == 0 || got >= need) { free(full); return w; }
    flen = (size_t)got;
    if (flen <= LCSAS_W_SHORT_MAX) { free(full); return w; }
    /* \\?\ turns off Windows' own path parsing, so it needs the full
     * path with '\' only, which GetFullPathNameW gives: '/' becomes
     * '\', and "." and ".." are resolved.  A share (\\server\share)
     * becomes \\?\UNC\server\share. */
    free(w);
    unc = full[0] == L'\\' && full[1] == L'\\';
    out = (wchar_t *)malloc((flen + 8) * sizeof(wchar_t));
    if (!out) { free(full); errno = ENOMEM; return NULL; }
    if (unc) {
        wcscpy(out, L"\\\\?\\UNC");
        wcscat(out, full + 1);
    } else {
        wcscpy(out, L"\\\\?\\");
        wcscat(out, full);
    }
    free(full);
    return out;
}

int
lcsas_w_open(const char *path, int flags, ...)
{
    int mode = 0, fd, saved;
    wchar_t *w;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    w = lcsas_w_path(path);
    if (!w) return -1;
    fd = _wopen(w, flags, mode);
    saved = errno;
    free(w);
    errno = saved;
    return fd;
}

int
lcsas_w_stat(const char *path, struct stat *st)
{
    struct _stat64 s64;
    int rc, saved;
    wchar_t *w = lcsas_w_path(path);
    if (!w) return -1;
    rc = _wstat64(w, &s64);
    saved = errno;
    free(w);
    errno = saved;
    if (rc != 0) return rc;
    memset(st, 0, sizeof *st);
    st->st_dev   = s64.st_dev;
    st->st_ino   = s64.st_ino;
    st->st_mode  = s64.st_mode;
    st->st_nlink = s64.st_nlink;
    st->st_uid   = s64.st_uid;
    st->st_gid   = s64.st_gid;
    st->st_rdev  = s64.st_rdev;
    st->st_size  = s64.st_size;
    st->st_atime = s64.st_atime;
    st->st_mtime = s64.st_mtime;
    st->st_ctime = s64.st_ctime;
    return 0;
}

FILE *
lcsas_w_fopen(const char *path, const char *mode)
{
    wchar_t wmode[8];
    size_t i;
    FILE *f;
    int saved;
    wchar_t *w;
    for (i = 0; mode[i] && i < 7; i++) wmode[i] = (wchar_t)mode[i];
    wmode[i] = L'\0';
    w = lcsas_w_path(path);
    if (!w) return NULL;
    f = _wfopen(w, wmode);
    saved = errno;
    free(w);
    errno = saved;
    return f;
}

/* One path in, an int out: the shape of mkdir, remove, chdir. */
static int
w_call1(const char *path, int (*fn)(const wchar_t *))
{
    int rc, saved;
    wchar_t *w = lcsas_w_path(path);
    if (!w) return -1;
    rc = fn(w);
    saved = errno;
    free(w);
    errno = saved;
    return rc;
}

int lcsas_w_mkdir(const char *path)  { return w_call1(path, _wmkdir); }
int lcsas_w_remove(const char *path) { return w_call1(path, _wremove); }
int lcsas_w_chdir(const char *path)  { return w_call1(path, _wchdir); }

int
lcsas_w_rename(const char *from, const char *to)
{
    int rc, saved;
    wchar_t *wf = lcsas_w_path(from);
    wchar_t *wt;
    if (!wf) return -1;
    wt = lcsas_w_path(to);
    if (!wt) { free(wf); return -1; }
    rc = _wrename(wf, wt);
    saved = errno;
    free(wf);
    free(wt);
    errno = saved;
    return rc;
}

int
lcsas_w_access(const char *path, int mode)
{
    int rc, saved;
    wchar_t *w = lcsas_w_path(path);
    if (!w) return -1;
    rc = _waccess(w, mode);
    saved = errno;
    free(w);
    errno = saved;
    return rc;
}

char *
lcsas_w_getcwd(char *buf, size_t size)
{
    wchar_t *w = _wgetcwd(NULL, 0);
    char *s;
    size_t n;
    if (!w) return NULL;
    s = lcsas_w_to_utf8(w);
    free(w);
    if (!s) return NULL;
    n = strlen(s);
    if (n >= size) { free(s); errno = ERANGE; return NULL; }
    memcpy(buf, s, n + 1);
    free(s);
    return buf;
}

struct lcsas_w_dir {
    HANDLE h;                   /* INVALID_HANDLE_VALUE: no entries */
    WIN32_FIND_DATAW fd;
    int pending;                /* fd holds an entry not yet returned */
    struct lcsas_w_dirent ent;
};

lcsas_w_dir *
lcsas_w_opendir(const char *path)
{
    lcsas_w_dir *d;
    char *pat;
    wchar_t *w;
    size_t n = strlen(path);
    DWORD err;

    pat = (char *)malloc(n + 3);
    if (!pat) { errno = ENOMEM; return NULL; }
    memcpy(pat, path, n);
    if (n > 0 && path[n - 1] != '/' && path[n - 1] != '\\') {
        pat[n++] = '\\';
    }
    pat[n++] = '*';
    pat[n] = '\0';
    w = lcsas_w_path(pat);
    free(pat);
    if (!w) return NULL;
    d = (lcsas_w_dir *)malloc(sizeof *d);
    if (!d) { free(w); errno = ENOMEM; return NULL; }
    d->h = FindFirstFileW(w, &d->fd);
    err = GetLastError();
    free(w);
    d->pending = d->h != INVALID_HANDLE_VALUE;
    if (!d->pending && err != ERROR_FILE_NOT_FOUND) {
        /* No such folder, or it cannot be read.  (An empty drive root
         * has no "." or ".." and gives ERROR_FILE_NOT_FOUND: that is
         * an empty listing.) */
        free(d);
        errno = (err == ERROR_ACCESS_DENIED) ? EACCES : ENOENT;
        return NULL;
    }
    return d;
}

struct lcsas_w_dirent *
lcsas_w_readdir(lcsas_w_dir *d)
{
    for (;;) {
        int n;
        if (!d->pending) {
            if (d->h == INVALID_HANDLE_VALUE
                    || !FindNextFileW(d->h, &d->fd)) {
                return NULL;
            }
        }
        d->pending = 0;
        n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
                                d->fd.cFileName, -1, d->ent.d_name,
                                (int)sizeof d->ent.d_name, NULL, NULL);
        /* A name UTF-8 cannot spell (an unpaired surrogate) is not one
         * this program wrote or looks for: skip it. */
        if (n > 0) return &d->ent;
    }
}

int
lcsas_w_closedir(lcsas_w_dir *d)
{
    if (!d) return -1;
    if (d->h != INVALID_HANDLE_VALUE) FindClose(d->h);
    free(d);
    return 0;
}

/* getenv in UTF-8.  Each value is read once and kept for the whole run
 * (callers hold on to it, as they may with getenv). */
struct w_env {
    struct w_env *next;
    char *value;                /* NULL: not set */
    char name[1];               /* the name, NUL-terminated */
};
static struct w_env *g_w_env = NULL;

char *
lcsas_w_getenv(const char *name)
{
    struct w_env *e;
    wchar_t *wn;
    const wchar_t *wv;
    size_t n = strlen(name);

    for (e = g_w_env; e; e = e->next) {
        if (strcmp(e->name, name) == 0) return e->value;
    }
    wn = utf8_to_wide(name);
    if (!wn) return NULL;
    wv = _wgetenv(wn);
    free(wn);
    e = (struct w_env *)malloc(sizeof *e + n);
    if (!e) return NULL;
    memcpy(e->name, name, n + 1);
    e->value = wv ? lcsas_w_to_utf8(wv) : NULL;
    e->next = g_w_env;
    g_w_env = e;
    return e->value;
}

/* Split the UTF-16 command line as the C library splits it for argv:
 * blanks (space, tab) separate arguments outside double quotes; 2n
 * backslashes before a '"' give n backslashes and the '"' opens or
 * closes a quoted part; 2n+1 give n backslashes and a literal '"';
 * "" inside a quoted part is a literal '"'; other backslashes are
 * literal.  The program name (argv[0]) runs to the next blank, or is
 * everything inside its quotes, with no escapes.  Writes the argument
 * starting at *pp into out (when not NULL), advances *pp, and returns
 * its length; returns -1 at the end of the line. */
static long
next_arg(const wchar_t **pp, wchar_t *out, int first)
{
    const wchar_t *p = *pp;
    long len = 0;
    int quoted = 0;

    while (*p == L' ' || *p == L'\t') p++;
    if (*p == L'\0') { *pp = p; return -1; }
    if (first) {
        if (*p == L'"') {
            p++;
            while (*p && *p != L'"') { if (out) out[len] = *p; len++; p++; }
            if (*p == L'"') p++;
        } else {
            while (*p && *p != L' ' && *p != L'\t') {
                if (out) out[len] = *p;
                len++;
                p++;
            }
        }
        *pp = p;
        return len;
    }
    for (;;) {
        long bs = 0, k;
        while (*p == L'\\') { bs++; p++; }
        if (*p == L'"') {
            for (k = 0; k < bs / 2; k++) { if (out) out[len] = L'\\'; len++; }
            if (bs % 2) {
                if (out) out[len] = L'"';
                len++;
                p++;
                continue;
            }
            if (quoted && p[1] == L'"') {
                if (out) out[len] = L'"';
                len++;
                p += 2;
                continue;
            }
            quoted = !quoted;
            p++;
            continue;
        }
        for (k = 0; k < bs; k++) { if (out) out[len] = L'\\'; len++; }
        if (*p == L'\0' || (!quoted && (*p == L' ' || *p == L'\t'))) break;
        if (out) out[len] = *p;
        len++;
        p++;
    }
    *pp = p;
    return len;
}

int
lcsas_w_utf8_argv(int *argc, char ***argv)
{
    const wchar_t *line = GetCommandLineW();
    const wchar_t *p;
    char **av;
    wchar_t *buf;
    long len, maxlen = 0;
    int n = 0, i;

    if (!line) return -1;
    for (p = line; (len = next_arg(&p, NULL, n == 0)) >= 0; n++) {
        if (len > maxlen) maxlen = len;
    }
    if (n == 0) return -1;
    av = (char **)calloc((size_t)n + 1, sizeof *av);
    buf = (wchar_t *)malloc(((size_t)maxlen + 1) * sizeof(wchar_t));
    if (!av || !buf) { free(av); free(buf); return -1; }
    p = line;
    for (i = 0; i < n; i++) {
        len = next_arg(&p, buf, i == 0);
        buf[len] = L'\0';
        av[i] = lcsas_w_to_utf8(buf);
        if (!av[i]) {
            while (i > 0) free(av[--i]);
            free(av);
            free(buf);
            return -1;
        }
    }
    free(buf);
    *argc = n;
    *argv = av;
    return 0;
}
#endif  /* _WIN32 */
