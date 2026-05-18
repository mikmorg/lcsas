/*
 * disc-loader — operator front-end for the optical robot control unit.
 *
 * Execs the libexec helper with operator-supplied args. Nothing in this
 * source mentions the underlying hardware (intentional: the operator may
 * read this file; it must not describe the drive implementation).
 *
 * "status" is special-cased: the wrapper captures the backend's output
 * and decorates it with the disc role ([meta] / [data] / [unknown])
 * inferred from the LCSAS label prefix.  Every other command is exec'd
 * directly so the wrapper stays out of the data path.
 */

/* fdopen() requires POSIX (not in ISO C89). */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef ROBOT_BACKEND
#define ROBOT_BACKEND "/opt/disc-robot/libexec/cdr-robotctl"
#endif

/* Decorate a single backend status line on stdout.
 *
 * Input lines we care about:
 *   "LOADED <label>\n"   →   "LOADED <label> [meta|data|unknown]\n"
 *   "EMPTY\n"            →   passed through unchanged
 *
 * Everything else is echoed verbatim (the backend may legitimately emit
 * diagnostics on stdout that we shouldn't drop).
 */
static void decorate_status_line(const char *line) {
    static const char prefix[] = "LOADED ";
    const size_t prefix_len = sizeof(prefix) - 1;
    char label[256];
    size_t i;
    const char *p;
    const char *role;

    if (strncmp(line, prefix, prefix_len) != 0) {
        fputs(line, stdout);
        return;
    }

    /* Copy label minus trailing newline into a fixed buffer. */
    p = line + prefix_len;
    i = 0;
    while (i + 1 < sizeof(label) && p[i] != '\0'
           && p[i] != '\n' && p[i] != '\r') {
        label[i] = p[i];
        ++i;
    }
    label[i] = '\0';

    role = "unknown";
    if (strncmp(label, "LCSAS_META", 10) == 0) {
        role = "meta";
    } else if (strncmp(label, "LCSAS_", 6) == 0) {
        role = "data";
    }
    printf("LOADED %s [%s]\n", label, role);
}

/* Capture the backend's stdout, decorate it, and return its exit code. */
static int run_status_decorated(char *const argv[], char *const envp[]) {
    int pipefd[2];
    pid_t pid;
    int status;
    FILE *fp;
    char buf[512];

    if (pipe(pipefd) != 0) {
        fprintf(stderr, "disc-loader: pipe failed: %s\n", strerror(errno));
        return 1;
    }

    pid = fork();
    if (pid < 0) {
        fprintf(stderr, "disc-loader: fork failed: %s\n", strerror(errno));
        close(pipefd[0]);
        close(pipefd[1]);
        return 1;
    }
    if (pid == 0) {
        /* Child: backend stdout → pipe. */
        close(pipefd[0]);
        if (dup2(pipefd[1], 1) < 0) {
            _exit(127);
        }
        close(pipefd[1]);
        execve(ROBOT_BACKEND, argv, envp);
        _exit(127);
    }

    /* Parent: read pipe line-by-line and decorate. */
    close(pipefd[1]);
    fp = fdopen(pipefd[0], "r");
    if (fp == NULL) {
        fprintf(stderr, "disc-loader: fdopen failed: %s\n", strerror(errno));
        close(pipefd[0]);
        waitpid(pid, &status, 0);
        return 1;
    }
    while (fgets(buf, (int)sizeof(buf), fp) != NULL) {
        decorate_status_line(buf);
    }
    fclose(fp);

    if (waitpid(pid, &status, 0) < 0) {
        return 1;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}

int main(int argc, char *argv[]) {
    struct stat st;
    char **new_argv;
    int i;
    static char *clean_env[] = {
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DISC_LOG=/var/log/disc-loader.log",
        NULL
    };

    /* Only attempt privilege elevation when actually setuid (production
     * install: chmod 4755 root:root).  Unit-test runs as a normal user
     * skip silently and just invoke the (stubbed) backend.
     */
    if (geteuid() != getuid()) {
        if (setuid(0) != 0) {
            fprintf(stderr,
                    "disc-loader: failed to acquire robot privileges: %s\n",
                    strerror(errno));
            return 1;
        }
        if (setgid(0) != 0) {
            fprintf(stderr,
                    "disc-loader: failed to acquire robot group: %s\n",
                    strerror(errno));
            return 1;
        }
    }

    if (stat(ROBOT_BACKEND, &st) != 0) {
        fprintf(stderr, "disc-loader: optical robot backend not installed\n");
        return 1;
    }

    new_argv = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (!new_argv) {
        fprintf(stderr, "disc-loader: out of memory\n");
        return 1;
    }
    new_argv[0] = (char *)ROBOT_BACKEND;
    for (i = 1; i < argc; ++i) {
        new_argv[i] = argv[i];
    }
    new_argv[argc] = NULL;

    /* "status" gets role decoration; everything else exec's straight through. */
    if (argc >= 2 && strcmp(argv[1], "status") == 0) {
        int rc = run_status_decorated(new_argv, clean_env);
        free(new_argv);
        return rc;
    }

    execve(ROBOT_BACKEND, new_argv, clean_env);
    fprintf(stderr, "disc-loader: robot backend exec failed: %s\n",
            strerror(errno));
    return 1;
}
