LCSAS RECOVERY TOOLCHAIN
=========================

This directory tree contains a strict-C89 + POSIX-sh recovery
toolchain for LCSAS archives.  It is designed for 50-year archival
survivability: minimal dependencies, primary-source-driven crypto,
plain-text documentation.

QUICK START

  # Build (host architecture):
  make

  # Run tests:
  make test

  # Restore from a recovery medium:
  sh scripts/restore.sh /path/to/recovery /path/to/target

DOCUMENTATION

  BUILD.txt    -- compilation and cross-compilation
  RECOVER.txt  -- step-by-step manual recovery
  FORMAT.txt   -- on-disc data formats (restic + LCSAS)
  CRYPTO.txt   -- cryptographic primitives with test vectors

SOURCE LAYOUT

  src/lcsas-restore/   the C89 recovery binary
    sha256, aes, pbkdf2, poly1305, scrypt  -- crypto
    arena, io, b64, hex, path              -- support
    json_q                                  -- JSON tokenizer
    repo                                    -- restic repo reader
    tree                                    -- recursive restorer
    main                                    -- CLI

  scripts/             POSIX-sh drivers
  tests/               FIPS/RFC test vectors
  docs/                plain-text documentation
  vendored/            third-party source (Phase 2)
  boot/                live-boot bootloader/kernel config (Phase 2)
  bin/<arch>/          prebuilt binaries (output of cross-compile)

DESIGN DECISIONS

See ../plans/ in the LCSAS repository for the full design plan.
Summary:

  Architectures:   x86_64, aarch64, riscv64 (Phase 3)
  Bootstrap:       prebuilt + source; no compiler bundled
  Userland:        BusyBox static (Phase 2)
  Kernel:          Linux LTS 6.6 + FreeBSD 13.4 (Phase 2)
  Language:        strict C89 + POSIX sh (no bashisms)

PHASE STATUS

  Phase 1 (MVP):  COMPLETE
    - All cryptographic primitives implemented and tested.
    - Restic v1 repo restore working.
    - POSIX-sh driver scripts.
    - Plain-text docs.

  Phase 2 (Hardening): COMPLETE
    - zstd 1.5.6 vendored; restic v2 (compressed) repos restore round-trip.
    - SQLite 3.46.0 vendored; on-disc catalog query module (catalog.c).
    - lcsas-iso9660 mini-reader (no kernel mount-loop needed).
    - lcsas-init C89 init for the live-boot initramfs.
    - Reproducible-build verification (make repro-check).
    - End-to-end integration test against a Python-built synthetic
      restic repo, covering both v1 and v2 layouts.

  Phase 3 (Multi-arch + FreeBSD): SOURCE COMPLETE
    - Cross-compile Makefile targets for aarch64 / riscv64 (using
      musl-cross or zig cc).
    - Linux 6.6 LTS kernel configs for all three arches.
    - FreeBSD 13.4 kernel config and bootloader configuration.
    - Boot menus (isolinux.cfg / grub.cfg) wired for all four boot
      paths: Linux primary, FreeBSD alternate, shell, direct restore.
    - Initramfs assembly script + manifest (reproducible cpio.gz).

    Runtime validation on real hardware (BD-R burn + boot test) is
    deferred to the build host environment with cross-compilers,
    QEMU, and physical media.  See docs/BOOT.txt.
