/*
 * arena.c -- bump allocator.
 *
 * Strict C89.  Each `lcsas_arena` is a linked list of fixed-size
 * chunks; allocations larger than `default_chunk` get their own chunk.
 */
#include "arena.h"
#include <stdlib.h>

struct lcsas_arena_chunk {
    lcsas_arena_chunk *next;
    size_t cap;
    size_t used;
    unsigned char data[1];
};

#define ALIGN 16

static size_t
align_up(size_t v, size_t a)
{
    return (v + a - 1) & ~(a - 1);
}

void
lcsas_arena_init(lcsas_arena *a, size_t default_chunk)
{
    a->head = NULL;
    a->default_chunk = default_chunk ? default_chunk : 65536;
}

static lcsas_arena_chunk *
new_chunk(size_t cap)
{
    lcsas_arena_chunk *c;
    c = (lcsas_arena_chunk *)malloc(sizeof(*c) + cap);
    if (!c) return NULL;
    c->next = NULL;
    c->cap = cap;
    c->used = 0;
    return c;
}

void *
lcsas_arena_alloc(lcsas_arena *a, size_t bytes)
{
    size_t need = align_up(bytes, ALIGN);
    lcsas_arena_chunk *c = a->head;
    void *p;

    if (need == 0) need = ALIGN;

    if (c == NULL || c->used + need > c->cap) {
        size_t cap = a->default_chunk;
        if (need > cap) cap = need;
        c = new_chunk(cap);
        if (!c) return NULL;
        c->next = a->head;
        a->head = c;
    }

    p = &c->data[c->used];
    c->used += need;
    return p;
}

void *
lcsas_arena_calloc(lcsas_arena *a, size_t bytes)
{
    unsigned char *p = (unsigned char *)lcsas_arena_alloc(a, bytes);
    size_t i;
    if (p) {
        for (i = 0; i < bytes; i++) p[i] = 0;
    }
    return p;
}

char *
lcsas_arena_strdup(lcsas_arena *a, const char *s, size_t len)
{
    char *p = (char *)lcsas_arena_alloc(a, len + 1);
    size_t i;
    if (!p) return NULL;
    for (i = 0; i < len; i++) p[i] = s[i];
    p[len] = '\0';
    return p;
}

void
lcsas_arena_reset(lcsas_arena *a)
{
    lcsas_arena_chunk *c = a->head;
    while (c) {
        lcsas_arena_chunk *next = c->next;
        free(c);
        c = next;
    }
    a->head = NULL;
}
