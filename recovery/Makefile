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
# Relaxed flags for vendored third-party C (sqlite, zstd) which assume C99.
VENDOR_CFLAGS = -std=gnu99 -O2 -DNDEBUG \
                -D_POSIX_C_SOURCE=200809L -D_FILE_OFFSET_BITS=64 \
                -Wno-deprecated-declarations -Wno-unused-function
LDFLAGS =
STATIC  = -static

SRCDIR    = src/lcsas-restore
TESTDIR   = tests
BUILD     = build
VENDOR    = vendored

# Hand-written modules in dependency order.
SRC = \
	$(SRCDIR)/arena.c \
	$(SRCDIR)/lcsas_io.c \
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
	$(BUILD)/lcsas_io.o \
	$(BUILD)/b64.o \
	$(BUILD)/hex.o \
	$(BUILD)/sha256.o \
	$(BUILD)/aes.o \
	$(BUILD)/pbkdf2.o \
	$(BUILD)/poly1305.o \
	$(BUILD)/scrypt.o \
	$(BUILD)/json_q.o \
	$(BUILD)/path.o \
	$(BUILD)/zstd_dec.o \
	$(BUILD)/zstddeclib.o \
	$(BUILD)/catalog.o \
	$(BUILD)/sqlite3.o \
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
	$(BUILD)/test_json \
	$(BUILD)/test_catalog \
	$(BUILD)/test_zstd

# Binary extension is overridden by the Windows cross-build target.
BIN_EXT ?=

all: $(BUILD)/lcsas-restore$(BIN_EXT) $(BUILD)/lcsas-iso9660$(BIN_EXT) $(BUILD)/lcsas-init$(BIN_EXT)

$(BUILD):
	mkdir -p $(BUILD)

# When BIN_EXT is empty (host build) these rules ARE the rules for the
# extensionless target names referenced by tests.  When BIN_EXT=.exe
# (windows target) the resulting binaries are *.exe.
$(BUILD)/lcsas-restore$(BIN_EXT): $(BUILD) $(OBJ) $(BUILD)/main.o
	$(CC) $(CFLAGS) $(LDFLAGS) $(OBJ) $(BUILD)/main.o -o $@

$(BUILD)/lcsas-iso9660$(BIN_EXT): $(BUILD) $(BUILD)/iso9660.o $(BUILD)/iso9660_main.o
	$(CC) $(CFLAGS) $(LDFLAGS) $(BUILD)/iso9660.o $(BUILD)/iso9660_main.o -o $@

$(BUILD)/iso9660.o: src/lcsas-iso9660/iso9660.c src/lcsas-iso9660/iso9660.h $(BUILD)
	$(CC) $(CFLAGS) -c src/lcsas-iso9660/iso9660.c -o $@

$(BUILD)/iso9660_main.o: src/lcsas-iso9660/main.c src/lcsas-iso9660/iso9660.h $(BUILD)
	$(CC) $(CFLAGS) -c src/lcsas-iso9660/main.c -o $@

$(BUILD)/lcsas-init$(BIN_EXT): $(BUILD) src/lcsas-init/init.c
	$(CC) $(CFLAGS) -c src/lcsas-init/init.c -o $(BUILD)/init.o
	$(CC) $(CFLAGS) $(LDFLAGS) $(BUILD)/init.o -o $@

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

$(BUILD)/lcsas_io.o: $(SRCDIR)/lcsas_io.c $(SRCDIR)/lcsas_io.h $(SRCDIR)/posix_compat.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/lcsas_io.c -o $@

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

$(BUILD)/zstd_dec.o: $(SRCDIR)/zstd_dec.c $(SRCDIR)/zstd_dec.h $(VENDOR)/zstd/zstd.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/zstd_dec.c -o $@

# Vendored zstd amalgamation: build with relaxed flags (C99) and no -pedantic.
$(BUILD)/zstddeclib.o: $(VENDOR)/zstd/zstddeclib.c $(VENDOR)/zstd/zstd.h $(BUILD)
	$(CC) $(VENDOR_CFLAGS) -c $(VENDOR)/zstd/zstddeclib.c -o $@

$(BUILD)/catalog.o: $(SRCDIR)/catalog.c $(SRCDIR)/catalog.h $(VENDOR)/sqlite/sqlite3.h $(BUILD)
	$(CC) $(CFLAGS) -c $(SRCDIR)/catalog.c -o $@

# Vendored sqlite amalgamation.  Build with -DSQLITE_OMIT_LOAD_EXTENSION to
# keep the binary smaller and avoid dlsym/dlopen.  Read-only use only.
$(BUILD)/sqlite3.o: $(VENDOR)/sqlite/sqlite3.c $(VENDOR)/sqlite/sqlite3.h $(BUILD)
	$(CC) $(VENDOR_CFLAGS) \
	    -DSQLITE_OMIT_LOAD_EXTENSION=1 \
	    -DSQLITE_DEFAULT_MEMSTATUS=0 \
	    -DSQLITE_OMIT_DEPRECATED=1 \
	    -DSQLITE_THREADSAFE=0 \
	    -DSQLITE_OMIT_AUTHORIZATION=1 \
	    -c $(VENDOR)/sqlite/sqlite3.c -o $@

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

$(BUILD)/test_catalog: $(TESTDIR)/test_catalog.c $(BUILD)/catalog.o $(BUILD)/sqlite3.o
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_catalog.c \
	    $(BUILD)/catalog.o $(BUILD)/sqlite3.o -lpthread -o $@

$(BUILD)/test_zstd: $(TESTDIR)/test_zstd.c $(BUILD)/zstd_dec.o $(BUILD)/zstddeclib.o
	$(CC) $(CFLAGS) -I$(SRCDIR) $(TESTDIR)/test_zstd.c \
	    $(BUILD)/zstd_dec.o $(BUILD)/zstddeclib.o -o $@

test: $(TEST_BINS) $(BUILD)/lcsas-restore
	@set -e; for t in $(TEST_BINS); do echo "==> $$t"; $$t; done; echo "ALL OK"

test-e2e: $(BUILD)/lcsas-restore
	@echo "==> end-to-end synthetic repo"
	python3 tests/test_e2e.py

test-bare-path: $(BUILD)/lcsas-restore
	@echo "==> bare-path Python-free recovery"
	@sh tests/test_bare_path.sh

test-windows: bin/x86_64-windows/lcsas-restore.exe
	@echo "==> Windows binary e2e via wine"
	@sh tests/test_e2e_windows.sh

# ── Cross-compilation targets ─────────────────────────────────────
#
# Override CC explicitly for clarity.  Each target builds host-arch's
# OBJ files into a per-arch bin/<arch>/ tree.

bin/x86_64/lcsas-restore: ARCH=x86_64
bin/aarch64/lcsas-restore: ARCH=aarch64
bin/riscv64/lcsas-restore: ARCH=riscv64

bin/x86_64/lcsas-restore:
	$(MAKE) clean
	$(MAKE) BUILD=build/x86_64 CC=$${CC_X86_64:-cc}
	mkdir -p bin/x86_64
	cp build/x86_64/lcsas-restore bin/x86_64/
	cp build/x86_64/lcsas-iso9660 bin/x86_64/
	cp build/x86_64/lcsas-init    bin/x86_64/

bin/aarch64/lcsas-restore:
	$(MAKE) clean
	$(MAKE) BUILD=build/aarch64 \
	        CC=$${CC_AARCH64:-aarch64-linux-musl-gcc} \
	        LDFLAGS=-static
	mkdir -p bin/aarch64
	cp build/aarch64/lcsas-restore bin/aarch64/
	cp build/aarch64/lcsas-iso9660 bin/aarch64/
	cp build/aarch64/lcsas-init    bin/aarch64/

bin/riscv64/lcsas-restore:
	$(MAKE) clean
	$(MAKE) BUILD=build/riscv64 \
	        CC=$${CC_RISCV64:-riscv64-linux-musl-gcc} \
	        LDFLAGS=-static
	mkdir -p bin/riscv64
	cp build/riscv64/lcsas-restore bin/riscv64/
	cp build/riscv64/lcsas-iso9660 bin/riscv64/
	cp build/riscv64/lcsas-init    bin/riscv64/

all-arches: bin/x86_64/lcsas-restore bin/aarch64/lcsas-restore bin/riscv64/lcsas-restore

# Cross-compile for Windows via zig cc (ships its own MinGW headers).
# Produces a PE32+ binary that runs on Windows 7+ without Visual C++
# redistributables or admin privileges.
ZIG_CC = python3 -m ziglang cc

bin/x86_64-windows/lcsas-restore.exe:
	rm -rf build/x86_64-windows
	$(MAKE) BUILD=build/x86_64-windows \
	        CC="$(ZIG_CC) -target x86_64-windows-gnu" \
	        VENDOR_CFLAGS="-std=gnu99 -O2 -DNDEBUG -Wno-deprecated-declarations -Wno-unused-function" \
	        BIN_EXT=.exe \
	        build/x86_64-windows/lcsas-restore.exe
	mkdir -p bin/x86_64-windows
	cp build/x86_64-windows/lcsas-restore.exe bin/x86_64-windows/

bin/aarch64-windows/lcsas-restore.exe:
	rm -rf build/aarch64-windows
	$(MAKE) BUILD=build/aarch64-windows \
	        CC="$(ZIG_CC) -target aarch64-windows-gnu" \
	        VENDOR_CFLAGS="-std=gnu99 -O2 -DNDEBUG -Wno-deprecated-declarations -Wno-unused-function" \
	        BIN_EXT=.exe \
	        build/aarch64-windows/lcsas-restore.exe
	mkdir -p bin/aarch64-windows
	cp build/aarch64-windows/lcsas-restore.exe bin/aarch64-windows/

windows: bin/x86_64-windows/lcsas-restore.exe

# Cross-compile with zig cc (single binary, deterministic).  Requires:
#   apt-get install zig   OR   pip install ziglang
zig-aarch64:
	$(MAKE) clean
	$(MAKE) CC="zig cc -target aarch64-linux-musl" \
	        LDFLAGS=-static \
	        BUILD=build/aarch64-zig
	mkdir -p bin/aarch64
	cp build/aarch64-zig/lcsas-restore bin/aarch64/
	cp build/aarch64-zig/lcsas-iso9660 bin/aarch64/
	cp build/aarch64-zig/lcsas-init    bin/aarch64/

zig-riscv64:
	$(MAKE) clean
	$(MAKE) CC="zig cc -target riscv64-linux-musl" \
	        LDFLAGS=-static \
	        BUILD=build/riscv64-zig
	mkdir -p bin/riscv64
	cp build/riscv64-zig/lcsas-restore bin/riscv64/
	cp build/riscv64-zig/lcsas-iso9660 bin/riscv64/
	cp build/riscv64-zig/lcsas-init    bin/riscv64/

# ── Reproducible-build verification ───────────────────────────────
#
# Two clean builds with the same SOURCE_DATE_EPOCH should produce a
# byte-identical lcsas-restore.
repro-check:
	@echo "==> reproducible build check"
	$(MAKE) clean
	SOURCE_DATE_EPOCH=1735689600 $(MAKE) >/dev/null
	cp $(BUILD)/lcsas-restore /tmp/lcsas-restore.first
	$(MAKE) clean
	SOURCE_DATE_EPOCH=1735689600 $(MAKE) >/dev/null
	cp $(BUILD)/lcsas-restore /tmp/lcsas-restore.second
	@if cmp -s /tmp/lcsas-restore.first /tmp/lcsas-restore.second; then \
		echo "REPRODUCIBLE: byte-identical"; \
		rm /tmp/lcsas-restore.first /tmp/lcsas-restore.second; \
	else \
		echo "NOT REPRODUCIBLE -- diff:"; \
		ls -la /tmp/lcsas-restore.first /tmp/lcsas-restore.second; \
		exit 1; \
	fi

clean:
	rm -rf $(BUILD) build

manifest:
	@cd docs && find . -type f ! -name MANIFEST.sha256 -exec sha256sum {} \; \
		| sort -k 2 > MANIFEST.sha256
	@echo "wrote docs/MANIFEST.sha256"
	@find . -path ./build -prune \
	    -o -path ./bin -prune \
	    -o -path ./.git -prune \
	    -o -name MANIFEST.sha256 -prune \
	    -o -type f -print \
	    | sort | xargs sha256sum \
	    > MANIFEST.sha256
	@echo "wrote MANIFEST.sha256 (top-level, $(shell wc -l < MANIFEST.sha256) entries)"

.PHONY: all all-arches test test-e2e test-bare-path test-windows windows clean manifest repro-check zig-aarch64 zig-riscv64
