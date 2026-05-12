/*
 * lcsas-init -- minimal /init for the LCSAS recovery initramfs.
 *
 * Tasks:
 *   1. Mount /proc, /sys, /dev (devtmpfs).
 *   2. Create /run, /tmp tmpfs mounts.
 *   3. Find the recovery medium (CD/DVD/BD) and mount it RO at /mnt.
 *   4. exec /mnt/recovery/scripts/restore.sh or drop to a busybox shell.
 *
 * Strict C89.  No libc dependencies beyond mount(2), execve(2),
 * stat(2), and basic stdio.  Designed to be statically linked against
 * musl libc as PID 1.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static void
log_msg(const char *m)
{
    write(2, "[lcsas-init] ", 13);
    write(2, m, strlen(m));
    write(2, "\n", 1);
}

static int
try_mount(const char *src, const char *target, const char *fstype,
          unsigned long flags, const char *data)
{
    if (mount(src, target, fstype, flags, data) == 0) return 0;
    /* EBUSY = already mounted (ok). */
    if (errno == EBUSY) return 0;
    return -1;
}

static int
ensure_dir(const char *path)
{
    if (mkdir(path, 0755) == 0) return 0;
    if (errno == EEXIST) return 0;
    return -1;
}

/*
 * Try the well-known disc device nodes.  Returns 0 on first successful
 * mount or -1 if none worked.
 */
static int
try_discs(const char *mount_point)
{
    const char *candidates[] = {
        "/dev/sr0", "/dev/sr1", "/dev/sr2", "/dev/sr3",
        "/dev/cdrom", "/dev/dvd",
        NULL
    };
    int i;
    for (i = 0; candidates[i]; i++) {
        if (access(candidates[i], R_OK) != 0) continue;
        /* Try iso9660 first, then udf. */
        if (try_mount(candidates[i], mount_point, "iso9660",
                      MS_RDONLY, NULL) == 0) {
            log_msg("mounted recovery medium (iso9660)");
            return 0;
        }
        if (try_mount(candidates[i], mount_point, "udf",
                      MS_RDONLY, NULL) == 0) {
            log_msg("mounted recovery medium (udf)");
            return 0;
        }
    }
    return -1;
}

static int
file_exists(const char *p)
{
    struct stat st;
    return stat(p, &st) == 0;
}

int
main(int argc, char **argv)
{
    int rc;
    (void)argc; (void)argv;

    log_msg("starting");

    /* ── Mount essential virtual filesystems. ── */
    ensure_dir("/proc");
    if (try_mount("proc", "/proc", "proc", 0, NULL) < 0) {
        log_msg("warning: cannot mount /proc");
    }
    ensure_dir("/sys");
    if (try_mount("sysfs", "/sys", "sysfs", 0, NULL) < 0) {
        log_msg("warning: cannot mount /sys");
    }
    ensure_dir("/dev");
    if (try_mount("devtmpfs", "/dev", "devtmpfs", 0, "mode=0755") < 0) {
        log_msg("warning: cannot mount /dev (devtmpfs)");
    }
    ensure_dir("/run");
    try_mount("tmpfs", "/run", "tmpfs", 0, "mode=0755,size=64m");
    ensure_dir("/tmp");
    try_mount("tmpfs", "/tmp", "tmpfs", 0, "mode=1777,size=256m");
    ensure_dir("/mnt");

    /* ── Find and mount the recovery medium. ── */
    if (try_discs("/mnt") < 0) {
        log_msg("WARNING: no optical disc found; dropping to shell");
        execl("/bin/busybox", "busybox", "sh", (char *)NULL);
        execl("/bin/sh", "sh", (char *)NULL);
        log_msg("FATAL: no shell available");
        return 1;
    }

    /* ── Hand off to restore.sh. ── */
    if (file_exists("/mnt/recovery/scripts/restore.sh")) {
        log_msg("handing off to /mnt/recovery/scripts/restore.sh");
        execl("/bin/busybox", "busybox", "sh",
              "/mnt/recovery/scripts/restore.sh",
              "/mnt/recovery", "/tmp/restored", "latest",
              (char *)NULL);
        execl("/bin/sh", "sh",
              "/mnt/recovery/scripts/restore.sh",
              "/mnt/recovery", "/tmp/restored", "latest",
              (char *)NULL);
        log_msg("FATAL: cannot exec restore.sh");
    }

    log_msg("no restore.sh; dropping to shell");
    execl("/bin/busybox", "busybox", "sh", (char *)NULL);
    execl("/bin/sh", "sh", (char *)NULL);

    log_msg("FATAL: no shell available");
    /* Keep PID 1 alive so the kernel does not panic. */
    for (;;) {
        sleep(60);
    }
    rc = 1;
    return rc;
}
