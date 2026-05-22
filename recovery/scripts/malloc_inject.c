/*
 * malloc_inject.c -- LD_PRELOAD shim that fails the Nth allocation.
 *
 * Compile:
 *   cc -shared -fPIC -O0 -g -D_GNU_SOURCE \
 *      recovery/scripts/malloc_inject.c -o recovery/build/malloc_inject.so -ldl
 *
 * Usage:
 *   # First pass: count allocations
 *   LD_PRELOAD=$(pwd)/recovery/build/malloc_inject.so \
 *       recovery/build/test_repo
 *   # → "[malloc_inject] total allocations: 1234"
 *
 *   # Sweep: fail allocation N=1..total
 *   for n in $(seq 1 1234); do
 *       LD_PRELOAD=$(pwd)/recovery/build/malloc_inject.so \
 *           LCSAS_FAIL_AT=$n LCSAS_FAIL_QUIET=1 \
 *           recovery/build/test_repo
 *   done
 *
 * Used by recovery/scripts/run_fault_inject.py and the
 * `make audit-gate-fault-inject` opt-in Makefile target.
 *
 * Implementation notes:
 *   - dlsym() itself can call malloc; we use a bootstrap pool until
 *     the real symbols are resolved.
 *   - free() of bootstrap-pool pointers is a no-op (leak; <64 KiB).
 *   - realloc() of bootstrap pointers migrates to the real heap.
 *   - The fault counter excludes bootstrap allocations.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Forward declaration of libgcov's flush-and-write entry point.
 * Available since GCC 11; calls fwrite/etc. so MUST run before
 * the process exits if we want .gcda data to land.
 * If linker complains, build with -lgcov in the shim's link line —
 * but typically the instrumented test binary already pulls libgcov in. */
extern void __gcov_dump(void);

#define BOOTSTRAP_SIZE 65536

static long fail_at = 0;
static long alloc_count = 0;
static int  state = 0;   /* 0=unresolved, -1=resolving (dlsym in flight), 1=ready */

static char   bootstrap[BOOTSTRAP_SIZE];
static size_t bootstrap_off = 0;

static void *(*real_malloc)(size_t)             = NULL;
static void *(*real_calloc)(size_t, size_t)     = NULL;
static void *(*real_realloc)(void *, size_t)    = NULL;
static void  (*real_free)(void *)               = NULL;

static int in_bootstrap(const void *p) {
    return (const char *)p >= bootstrap
        && (const char *)p < bootstrap + BOOTSTRAP_SIZE;
}

static void *boot_alloc(size_t size) {
    size = (size + 15) & ~(size_t)15;
    if (bootstrap_off + size > BOOTSTRAP_SIZE) { errno = ENOMEM; return NULL; }
    void *p = &bootstrap[bootstrap_off];
    bootstrap_off += size;
    return p;
}

static void resolve(void) {
    if (state == 1 || state == -1) return;
    state = -1;
    const char *e = getenv("LCSAS_FAIL_AT");
    if (e) fail_at = atol(e);
    real_malloc  = dlsym(RTLD_NEXT, "malloc");
    real_calloc  = dlsym(RTLD_NEXT, "calloc");
    real_realloc = dlsym(RTLD_NEXT, "realloc");
    real_free    = dlsym(RTLD_NEXT, "free");
    state = 1;
}

static int should_fail(void) {
    long n = __sync_add_and_fetch(&alloc_count, 1);
    return fail_at > 0 && n == fail_at;
}

void *malloc(size_t size) {
    if (state != 1) {
        if (state == 0) resolve();
        if (state != 1) return boot_alloc(size);
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_malloc(size);
}

void *calloc(size_t nmemb, size_t size) {
    if (state != 1) {
        if (state == 0) resolve();
        if (state != 1) {
            size_t bytes = nmemb * size;
            void *p = boot_alloc(bytes);
            if (p) memset(p, 0, bytes);
            return p;
        }
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_calloc(nmemb, size);
}

void *realloc(void *ptr, size_t size) {
    if (state != 1) resolve();
    if (state != 1) return boot_alloc(size);
    if (ptr && in_bootstrap(ptr)) {
        /* Migrate from bootstrap → real heap.  We don't know the
         * original size; copy up to `size` from bootstrap (this is
         * safe — bootstrap is read-only memory containing zero or
         * the original data).  Worst-case reads past the original
         * allocation but stays inside BOOTSTRAP_SIZE. */
        if (should_fail()) { errno = ENOMEM; return NULL; }
        void *neu = real_malloc(size);
        if (neu) memcpy(neu, ptr, size);
        return neu;
    }
    if (should_fail()) { errno = ENOMEM; return NULL; }
    return real_realloc(ptr, size);
}

void free(void *ptr) {
    if (!ptr) return;
    if (in_bootstrap(ptr)) return; /* leak; trivial, capped at 64 KiB */
    if (state != 1) resolve();
    if (real_free) real_free(ptr);
}

__attribute__((destructor))
static void summary(void) {
    if (getenv("LCSAS_FAIL_QUIET") == NULL) {
        fprintf(stderr, "[malloc_inject] total allocations: %ld\n", alloc_count);
    }
}

/* Fault-tolerant gcov support.
 *
 * Coverage-instrumented binaries call __gcov_dump() at normal exit to
 * write .gcda files.  When we deliberately fail a malloc to exercise
 * production-code error paths, the process may crash mid-execution
 * (SIGSEGV from a NULL deref the production code didn't expect — these
 * are the bugs we WANT to find) OR may exit cleanly via the error
 * branch.  Either way, .gcda must flush.
 *
 * The trick: install SIGSEGV/SIGABRT/etc. handlers that
 *   1. DISABLE further fault injection (otherwise __gcov_dump's own
 *      mallocs would fail and the handler would recurse into the
 *      same crash),
 *   2. call __gcov_dump() to write counters,
 *   3. _exit(1) without touching libc state.
 *
 * Enable by setting LCSAS_FAULT_INJECT_GCOV=1 alongside LCSAS_FAIL_AT.
 * Without that env var, the handlers are not installed (avoids
 * confusing crash diagnostics when fault-inject isn't intended). */

static void
gcov_dump_and_exit(int signo)
{
    (void)signo;
    /* Critical: disable further fault injection BEFORE calling
     * __gcov_dump.  Otherwise libgcov's own malloc would fail and
     * we'd recurse into the same crash. */
    fail_at = -1;
    __gcov_dump();
    _exit(1);
}

__attribute__((constructor))
static void install_gcov_handlers(void)
{
    if (getenv("LCSAS_FAULT_INJECT_GCOV") == NULL) return;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = gcov_dump_and_exit;
    sa.sa_flags = SA_RESETHAND;  /* one-shot — second signal aborts */
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS,  &sa, NULL);
    sigaction(SIGFPE,  &sa, NULL);
}
