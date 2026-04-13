/*
 * disc-loader — operator front-end for the optical robot control unit.
 *
 * Execs the libexec helper with operator-supplied args. Nothing in this
 * source mentions the underlying hardware (intentional: the operator may
 * read this file; it must not describe the drive implementation).
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define ROBOT_BACKEND "/opt/disc-robot/libexec/cdr-robotctl"

int main(int argc, char *argv[]) {
    if (setuid(0) != 0) {
        fprintf(stderr, "disc-loader: failed to acquire robot privileges: %s\n",
                strerror(errno));
        return 1;
    }
    if (setgid(0) != 0) {
        fprintf(stderr, "disc-loader: failed to acquire robot group: %s\n",
                strerror(errno));
        return 1;
    }

    struct stat st;
    if (stat(ROBOT_BACKEND, &st) != 0) {
        fprintf(stderr, "disc-loader: optical robot backend not installed\n");
        return 1;
    }

    char **new_argv = calloc((size_t)argc + 1, sizeof(char *));
    if (!new_argv) {
        fprintf(stderr, "disc-loader: out of memory\n");
        return 1;
    }
    new_argv[0] = (char *)ROBOT_BACKEND;
    for (int i = 1; i < argc; ++i) {
        new_argv[i] = argv[i];
    }
    new_argv[argc] = NULL;

    char *clean_env[] = {
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DISC_LOG=/var/log/disc-loader.log",
        NULL
    };

    execve(ROBOT_BACKEND, new_argv, clean_env);
    fprintf(stderr, "disc-loader: robot backend exec failed: %s\n",
            strerror(errno));
    return 1;
}
