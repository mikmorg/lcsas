/*
 * posix_compat.h -- thin POSIX/Win32 compatibility shim.
 *
 * Provides a single include surface so source files don't sprinkle
 * #ifdef _WIN32 across headers.  POSIX hosts get the standard
 * <unistd.h>/<sys/stat.h>/<dirent.h>/<fcntl.h>.  Windows (MinGW-w64
 * via zig cc) gets <io.h> + <direct.h> + <fcntl.h> plus a couple of
 * shim macros so source code keeps using the POSIX names:
 *
 *   - mkdir(path, mode)   -- on Windows, drops the mode (always 1-arg).
 *   - lseek(fd, off, w)   -- on Windows, maps to _lseeki64 for 64-bit
 *                            file offsets (required: pack files can
 *                            exceed 4 GiB).
 *   - symlink(target, link) on Windows returns -1/EPERM (Windows
 *                            symlinks require an admin privilege we
 *                            never request).  tree.c handles this
 *                            gracefully -- log + skip.
 *   - chmod(path, mode)   -- on Windows, no-op.
 *
 * FILE NAMES ON WINDOWS (#619)
 *
 * Every path in this program is UTF-8, on every platform: the names in
 * the backup are UTF-8, and so are the catalog and sqlite's file names.
 * The narrow (ANSI) Windows calls would read those bytes in the ANSI
 * code page, so a name like "café" or "日本語" came out garbled.  On
 * Windows the file calls below are therefore mapped to lcsas_w_*
 * (lcsas_io.c), which convert the UTF-8 path to UTF-16 and call the
 * wide (W) API:
 *
 *   open stat fopen mkdir rename remove unlink access chdir getcwd
 *   opendir readdir closedir (d_name comes back in UTF-8)  getenv
 *
 * A path of 248 UTF-16 units or more (the limit for making a folder;
 * MAX_PATH is 260) is made absolute and given the \\?\ prefix, so a
 * deep restore is not cut off at MAX_PATH.  main() rebuilds argv in
 * UTF-8 from the UTF-16 command line (lcsas_w_utf8_argv), and getenv
 * returns UTF-8, so a folder named by the reader may hold any
 * character too.
 * IMPORTANT INVARIANT
 *
 * On Windows we include ALL system headers first, THEN install the
 * macro shims.  Installing the macros before <dirent.h> (which
 * re-includes <io.h>) corrupts the system's own lseek/mkdir
 * declarations via macro substitution.
 *
 * The hand-written crypto and parser code (sha256/aes/poly1305/scrypt/
 * pbkdf2/b64/hex/json_q/path) is fully portable and needs no shim.
 */
#ifndef LCSAS_POSIX_COMPAT_H
#define LCSAS_POSIX_COMPAT_H

#ifdef _WIN32

#  include <errno.h>
#  include <fcntl.h>
#  include <io.h>
#  include <direct.h>
#  include <sys/stat.h>
#  include <sys/types.h>
#  include <stdio.h>          /* SEEK_SET, SEEK_CUR, SEEK_END */
#  include <dirent.h>

   /* ssize_t may not be defined by MinGW's headers in all configs. */
#  ifndef _SSIZE_T_DEFINED
     typedef long long ssize_t;
#    define _SSIZE_T_DEFINED
#  endif

#  include <stdlib.h>         /* getenv: declared before its macro */
#  include <stddef.h>         /* wchar_t */

   /* The wide shims (#619), in lcsas_io.c. */
   struct lcsas_w_dirent { char d_name[1024]; };
   typedef struct lcsas_w_dir lcsas_w_dir;
   int   lcsas_w_open(const char *path, int flags, ...);
   int   lcsas_w_stat(const char *path, struct stat *st);
   FILE *lcsas_w_fopen(const char *path, const char *mode);
   int   lcsas_w_mkdir(const char *path);
   int   lcsas_w_rename(const char *from, const char *to);
   int   lcsas_w_remove(const char *path);
   int   lcsas_w_access(const char *path, int mode);
   int   lcsas_w_chdir(const char *path);
   char *lcsas_w_getcwd(char *buf, size_t size);
   lcsas_w_dir *lcsas_w_opendir(const char *path);
   struct lcsas_w_dirent *lcsas_w_readdir(lcsas_w_dir *d);
   int   lcsas_w_closedir(lcsas_w_dir *d);
   char *lcsas_w_getenv(const char *name);
   /* `path` (UTF-8) as a malloc'd UTF-16 path for a W call, with the
    * \\?\ prefix when it is long; NULL with errno set (EILSEQ: not
    * valid UTF-8).  The caller frees it. */
   wchar_t *lcsas_w_path(const char *path);
   /* `w` as a malloc'd UTF-8 string, or NULL. */
   char *lcsas_w_to_utf8(const wchar_t *w);
   /* Replace *argv with the UTF-8 arguments of the UTF-16 command
    * line.  Returns 0, or -1 (and leaves argv alone) when it cannot. */
   int   lcsas_w_utf8_argv(int *argc, char ***argv);

   /* Install shim macros AFTER all system includes (see file header). */
#  undef  open
#  define open               lcsas_w_open
#  define stat(p, s)         lcsas_w_stat((p), (s))
#  define fopen(p, m)        lcsas_w_fopen((p), (m))
#  define mkdir(p, m)        lcsas_w_mkdir(p)
#  define rename(a, b)       lcsas_w_rename((a), (b))
#  define remove(p)          lcsas_w_remove(p)
#  define unlink(p)          lcsas_w_remove(p)
#  define access(p, m)       lcsas_w_access((p), (m))
#  define chdir(p)           lcsas_w_chdir(p)
#  define getcwd(b, n)       lcsas_w_getcwd((b), (n))
#  define DIR                lcsas_w_dir
#  define dirent             lcsas_w_dirent
#  define opendir(p)         lcsas_w_opendir(p)
#  define readdir(d)         lcsas_w_readdir(d)
#  define closedir(d)        lcsas_w_closedir(d)
#  define getenv(n)          lcsas_w_getenv(n)
#  undef  lseek
#  define lseek(fd, off, w)  _lseeki64((fd), (off), (w))
#  define symlink(t, l)      (errno = EPERM, -1)
#  define chmod(p, m)        ((void)(p), (void)(m), 0)
#  ifndef fsync
#    define fsync(fd)        _commit(fd)
#  endif

#else  /* POSIX */

#  include <unistd.h>
#  include <fcntl.h>
#  include <sys/stat.h>
#  include <sys/types.h>
#  include <dirent.h>

#endif

/* O_BINARY: a Windows-only flag suppressing CRLF translation on read/write.
 * Defined to 0 on POSIX so call sites can use `O_RDONLY | O_BINARY`
 * uniformly. */
#ifndef O_BINARY
#  define O_BINARY 0
#endif

/* O_NOFOLLOW (POSIX.1-2008): open() fails instead of following a
 * symlink in the last path component (#784).  Windows has no such
 * flag; tree.c checks for a reparse point there before it writes. */
#ifndef O_NOFOLLOW
#  define O_NOFOLLOW 0
#endif

/* Path helpers for the mkdir -p walks and path comparisons.
 *   LCSAS_IS_SEP(c)        a directory separator: '/', and on Windows
 *                          '\' as well.
 *   LCSAS_PATH_START(p)    the index to start looking for separators:
 *                          past a leading drive ("C:" or "C:\") on
 *                          Windows, so "C:" itself is never made.
 *   LCSAS_PATH_NCMP(a,b,n) compare n bytes of two paths; Windows paths
 *                          ignore case.  Needs <string.h>. */
#ifdef _WIN32
#  define LCSAS_IS_SEP(c)          ((c) == '/' || (c) == '\\')
#  define LCSAS_PATH_START(p) \
       (((p)[0] != '\0' && (p)[1] == ':') \
            ? (LCSAS_IS_SEP((p)[2]) ? 3 : 2) : 1)
#  define LCSAS_PATH_NCMP(a, b, n) _strnicmp((a), (b), (n))
#else
#  define LCSAS_IS_SEP(c)          ((c) == '/')
#  define LCSAS_PATH_START(p)      1
#  define LCSAS_PATH_NCMP(a, b, n) memcmp((a), (b), (n))
#endif

#endif  /* LCSAS_POSIX_COMPAT_H */
