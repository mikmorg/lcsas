/*
 * gf256.h -- GF(2^8) arithmetic for dvdisaster RS03 error correction.
 *
 * dvdisaster's RS03 codec works in GF(2^8) built from the primitive
 * polynomial 0x187 (x^8 + x^7 + x^2 + x + 1), NOT the more common
 * 0x11D used by most Reed-Solomon libraries.  The log/exp tables MUST
 * be generated from 0x187 or the parity will not match the discs that
 * dvdisaster wrote.  See docs/DVDISASTER_RS03_FORMAT.md sec 4.4.
 *
 * C89; no dependencies beyond the standard library.
 */
#ifndef LCSAS_ECC_GF256_H
#define LCSAS_ECC_GF256_H

/* Primitive polynomial used by dvdisaster RS03 (RS_GENERATOR_POLY). */
#define GF_PRIM_POLY 0x187
#define GF_FIELDSIZE 256
#define GF_FIELDMAX  255

/*
 * Initialise the global log/exp tables.  Idempotent; safe to call more
 * than once.  Must be called before any other gf_* function.
 */
void gf_init(void);

/* Multiply two field elements. */
unsigned char gf_mul(unsigned char a, unsigned char b);

/* Divide a by b (b must be non-zero; returns 0 if b == 0). */
unsigned char gf_div(unsigned char a, unsigned char b);

/* Multiplicative inverse of a (a must be non-zero; returns 0 if a==0). */
unsigned char gf_inv(unsigned char a);

/* alpha^e where alpha is the field generator (e taken mod 255). */
unsigned char gf_exp(int e);

/* Discrete log base alpha of a (a must be non-zero; returns 0 if a==0). */
int gf_log(unsigned char a);

/* dvdisaster index/exp accessors (src/galois.c convention).  These match
 * dvdisaster's `index_of`/`alpha_to` tables exactly and are used by the
 * faithfully-ported RS03 error+erasure decoder:
 *   - gf_index_of(0)   == GF_ALPHA0 (255), the "log of zero" sentinel;
 *   - gf_alpha_to(e)   for e in [0, GF_FIELDMAX]; gf_alpha_to(GF_ALPHA0)==0.
 * Callers reduce exponents with mod_fieldmax before indexing.  */
#define GF_ALPHA0 GF_FIELDMAX
int gf_index_of(unsigned char a);
unsigned char gf_alpha_to(int e);

#endif /* LCSAS_ECC_GF256_H */
