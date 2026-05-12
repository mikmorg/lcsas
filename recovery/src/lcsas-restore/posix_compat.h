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

   /* Install shim macros AFTER all system includes (see file header). */
#  define mkdir(p, m)        _mkdir(p)
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

#endif  /* LCSAS_POSIX_COMPAT_H */
