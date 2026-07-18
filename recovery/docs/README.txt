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

  BUILD.txt           -- compilation and cross-compilation
  RECOVER.txt         -- macOS / Linux manual recovery walkthrough
  RECOVER_WINDOWS.txt -- Windows recovery walkthrough
  TIERS.txt           -- recovery tier hierarchy + Python-free guarantee
  BOOT.txt            -- no-OS recovery procedure (live-USB; the
                         discs themselves are NOT bootable)
  FORMAT.txt          -- on-disc data formats (restic + LCSAS)
  CRYPTO.txt          -- cryptographic primitives with test vectors
  ../specs/           -- bundled reference specs (FIPS, RFCs, ECMA)

SOURCE LAYOUT

  src/lcsas-restore/   the C89 recovery binary
    sha256, aes, pbkdf2, poly1305, scrypt  -- crypto
    io, b64, hex, path                     -- support
    json_q                                  -- JSON tokenizer
    repo                                    -- restic repo reader
    tree                                    -- recursive restorer
    main                                    -- CLI

  scripts/             POSIX-sh drivers
  tests/               FIPS/RFC test vectors
  docs/                plain-text documentation
  vendored/            third-party source (Phase 2)
  bin/<arch>/          prebuilt binaries (output of cross-compile)

  (The former boot/ live-boot scaffolding was dropped -- never
  bootable, never built -- and quarantined to ../experimental/boot/
  in the source repository.  It does not ship on discs.)

DESIGN DECISIONS

See ../plans/ in the LCSAS repository for the full design plan.
Summary:

  Architectures:   x86_64, aarch64, riscv64 (Phase 3)
  Bootstrap:       prebuilt + source; no compiler bundled
  Userland:        none vendored (BusyBox was planned, never added;
                   the boot stack is experimental -- see
                   ../experimental/boot/ in the source repository)
  Kernel:          none built (Linux LTS 6.6 + FreeBSD 13.4 were
                   planned; configs quarantined with the boot stack)
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
    - lcsas-init C89 init for the live-boot initramfs.  (The C89 init
      source exists, but no userland or kernel was ever built; the
      live-boot stack itself was dropped -- see the Phase 3 note.)
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

    The live-boot items above (kernel configs, boot menus, initramfs
    assembly) were DROPPED in 2026: the boot stack was never built
    and no LCSAS disc was ever bootable.  The scaffolding is
    quarantined under ../experimental/boot/ in the source repository;
    the no-OS recovery path is now a current live-Linux USB stick.
    See docs/BOOT.txt.
