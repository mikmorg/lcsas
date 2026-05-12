#
# recovery/Makefile -- POSIX make build for the LCSAS recovery toolchain.
#
# Targets:
#   all       -- build host-arch lcsas-restore (default)
#   test      -- build and run unit tests
#   clean     -- remove build artifacts
#   manifest  -- regenerate docs/MANIFEST.sha256
#
# Strict C89 + standard POSIX flags.  Reproducible via SOURCE_DATE_EPOCH.
#
# Cross-compile by overriding CC, e.g.:
#   make CC=aarch64-linux-musl-gcc bin/aarch64/lcsas-restore
#

CC      = cc
CFLAGS  = -std=c89 -pedantic -Wall -Wextra -Wshadow -Wno-long-long -O2 \
          -D_POSIX_C_SOURCE=200809L -D_FILE_OFFSET_BITS=64
LDFLAGS =
STATIC  = -static

SRCDIR  = src/lcsas-restore
TESTDIR = tests
BUILD   = build

# Hand-written modules in dependency order.
SRC = \
	$(SRCDIR)/arena.c \
	$(SRCDIR)/io.c \
	$(SRCDIR)/b64.c \
	$(SRCDIR)/hex.c \
	$(SRCDIR)/sha256.c \
	$(SRCDIR)/aes.c \
	$(SRCDIR)/pbkdf2.c \
	$(SRCDIR)/poly1305.c \
	$(SRCDIR)/scrypt.c \
	$(SRCDIR)/json_q.c \
	$(SRCDIR)/path.c

OBJ = \
	$(BUILD)/arena.o \
	$(BUILD)/io.o \
	$(BUILD)/b64.o \
	$(BUILD)/hex.o \
	$(BUILD)/sha256.o \
	$(BUILD)/aes.o \
	$(BUILD)/pbkdf2.o \
	$(BUILD)/poly1305.o \
	$(BUILD)/scrypt.o \
	$(BUILD)/json_q.o \
	$(BUILD)/path.o \
	$(BUILD)/repo.o \
	$(BUILD)/tree.o

TEST_BINS = \
	$(BUILD)/test_sha256 \
	$(BUILD)/test_aes \
	$(BUILD)/test_poly1305 \
	$(BUILD)/test_pbkdf2 \
	$(BUILD)/test_scrypt \
	$(BUILD)/test_b64 \
	$(BUILD)/test_hex \
	$(BUILD)/test_path \
	$(BUILD)/test_json

all: $(BUILD)/lcsas-restore

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/lcsas-restore: $(BUILD) $(OBJ) $(BUILD)/main.o
	$(CC) $(CFLAGS) $(LDFLAGS) $(OBJ) $(BUILD)/main.o -o $@

$(BUILD)/main.o: $(SRCDIR)/main.c $(SRCDIR)/repo.h $(SRCDIR)/tree.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/main.c -o $@

$(BUILD)/repo.o: $(SRCDIR)/repo.c $(SRCDIR)/repo.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/repo.c -o $@

$(BUILD)/tree.o: $(SRCDIR)/tree.c $(SRCDIR)/tree.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/tree.c -o $@

$(BUILD)/libcrypto.a: $(BUILD) $(OBJ)
	rm -f $@
	ar rcs $@ $(OBJ)

$(BUILD)/arena.o: $(SRCDIR)/arena.c $(SRCDIR)/arena.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/arena.c -o $@

$(BUILD)/io.o: $(SRCDIR)/io.c $(SRCDIR)/io.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/io.c -o $@

$(BUILD)/b64.o: $(SRCDIR)/b64.c $(SRCDIR)/b64.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/b64.c -o $@

$(BUILD)/hex.o: $(SRCDIR)/hex.c $(SRCDIR)/hex.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/hex.c -o $@

$(BUILD)/sha256.o: $(SRCDIR)/sha256.c $(SRCDIR)/sha256.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/sha256.c -o $@

$(BUILD)/aes.o: $(SRCDIR)/aes.c $(SRCDIR)/aes.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/aes.c -o $@

$(BUILD)/pbkdf2.o: $(SRCDIR)/pbkdf2.c $(SRCDIR)/pbkdf2.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/pbkdf2.c -o $@

$(BUILD)/poly1305.o: $(SRCDIR)/poly1305.c $(SRCDIR)/poly1305.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/poly1305.c -o $@

$(BUILD)/scrypt.o: $(SRCDIR)/scrypt.c $(SRCDIR)/scrypt.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/scrypt.c -o $@

$(BUILD)/json_q.o: $(SRCDIR)/json_q.c $(SRCDIR)/json_q.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/json_q.c -o $@

$(BUILD)/path.o: $(SRCDIR)/path.c $(SRCDIR)/path.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/path.c -o $@

# ── test binaries ──────────────────────────────────────────────────

$(BUILD)/test_sha256: $(TESTDIR)/test_sha256.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_sha256.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_aes: $(TESTDIR)/test_aes.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_aes.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_poly1305: $(TESTDIR)/test_poly1305.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_poly1305.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_pbkdf2: $(TESTDIR)/test_pbkdf2.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_pbkdf2.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_scrypt: $(TESTDIR)/test_scrypt.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_scrypt.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_b64: $(TESTDIR)/test_b64.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_b64.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_hex: $(TESTDIR)/test_hex.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_hex.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_path: $(TESTDIR)/test_path.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_path.c $(BUILD)/libcrypto.a -o $@

$(BUILD)/test_json: $(TESTDIR)/test_json.c $(BUILD)/libcrypto.a
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_json.c $(BUILD)/libcrypto.a -o $@

test: $(TEST_BINS) $(BUILD)/lcsas-restore
	@set -e; for t in $(TEST_BINS); do echo "==> $$t"; $$t; done; echo "ALL OK"

test-e2e: $(BUILD)/lcsas-restore
	@echo "==> end-to-end synthetic repo"
	python3 tests/test_e2e.py

clean:
	rm -rf $(BUILD)

manifest:
	cd docs && find . -type f ! -name MANIFEST.sha256 -exec sha256sum {} \; \
		| sort -k 2 > MANIFEST.sha256

.PHONY: all test test-e2e clean manifest
