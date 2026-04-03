#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Live Recovery — Alpine rootfs builder
#
#  Builds a minimal Alpine Linux rootfs suitable for a bootable
#  recovery disc.  Intended to run inside an Alpine Docker/podman
#  container for hermetic, reproducible builds.
#
#  Usage:
#    docker run --rm -v "$PWD/output:/output" alpine:3.21 \
#        sh /path/to/build_rootfs.sh /output
#
#  OR directly on an Alpine host:
#    sudo ./build_rootfs.sh /path/to/output
#
#  Outputs:
#    <output>/vmlinuz          — Linux kernel
#    <output>/initramfs        — Initial ramdisk
#    <output>/rootfs.squashfs  — Compressed root filesystem
#
#  Approximate sizes:
#    vmlinuz        ~12 MB
#    initramfs      ~20 MB
#    rootfs.squashfs ~50 MB
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

OUTPUT_DIR="${1:?Usage: $0 <output-dir>}"
ROOTFS="${OUTPUT_DIR}/rootfs"
ALPINE_VERSION="3.21"
ARCH="x86_64"

echo "=== LCSAS Live Recovery rootfs builder ==="
echo "Output: ${OUTPUT_DIR}"

# ── Install build tools ──────────────────────────────────────────
apk update
apk add alpine-base mkinitfs squashfs-tools

# ── Create rootfs skeleton ───────────────────────────────────────
mkdir -p "${ROOTFS}"

# Install base system into rootfs
apk add --root "${ROOTFS}" --initdb --no-cache \
    alpine-base \
    linux-lts \
    mkinitfs \
    dialog \
    eudev \
    e2fsprogs \
    ntfs-3g \
    ntfs-3g-progs \
    util-linux \
    blkid \
    eject \
    squashfs-tools \
    bash \
    coreutils \
    findutils \
    grep \
    sed \
    gawk \
    less \
    nano \
    xorriso

# ── Copy kernel ──────────────────────────────────────────────────
KERNEL_PATH=$(find "${ROOTFS}/boot" -name 'vmlinuz-*-lts' | head -1)
if [[ -z "$KERNEL_PATH" ]]; then
    echo "ERROR: kernel not found in rootfs" >&2
    exit 1
fi
cp "$KERNEL_PATH" "${OUTPUT_DIR}/vmlinuz"
echo "Kernel: $(du -h "${OUTPUT_DIR}/vmlinuz" | cut -f1)"

# ── Configure initramfs ─────────────────────────────────────────
# Features needed for booting from optical media with overlay
mkdir -p "${ROOTFS}/etc/mkinitfs"
cat > "${ROOTFS}/etc/mkinitfs/mkinitfs.conf" << 'INITCONF'
features="ata base cdrom ext4 keymap squashfs usb"
INITCONF

# ── Build initramfs ──────────────────────────────────────────────
KERNEL_VER=$(basename "$KERNEL_PATH" | sed 's/vmlinuz-//')
# Build initramfs within the rootfs chroot
chroot "${ROOTFS}" mkinitfs -o /boot/initramfs "${KERNEL_VER}"
cp "${ROOTFS}/boot/initramfs" "${OUTPUT_DIR}/initramfs"
echo "Initramfs: $(du -h "${OUTPUT_DIR}/initramfs" | cut -f1)"

# ── Configure auto-login ────────────────────────────────────────
# Replace default inittab — auto-login root on tty1
cat > "${ROOTFS}/etc/inittab" << 'INITTAB'
# LCSAS Recovery — auto-login to restore wizard
::sysinit:/sbin/openrc sysinit
::sysinit:/sbin/openrc boot
::wait:/sbin/openrc default

# Auto-login root on tty1 (main console)
tty1::respawn:/bin/login -f root

# Additional consoles for troubleshooting
tty2::respawn:/sbin/getty 38400 tty2
tty3::respawn:/sbin/getty 38400 tty3

::shutdown:/sbin/openrc shutdown
INITTAB

# ── Auto-launch wizard on login ─────────────────────────────────
mkdir -p "${ROOTFS}/etc/profile.d"
cat > "${ROOTFS}/etc/profile.d/lcsas-autostart.sh" << 'AUTOSTART'
#!/bin/sh
# Auto-launch LCSAS restore wizard on tty1 only
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$LCSAS_NO_WIZARD" ]; then
    clear
    cat /usr/share/lcsas/banner.txt 2>/dev/null || true
    echo ""
    echo "Starting LCSAS Restore Wizard..."
    echo ""
    sleep 1

    # Try Python wizard first, fall back to restore.sh
    if [ -x /usr/share/lcsas/restore_wizard.py ] && command -v python3 >/dev/null 2>&1; then
        python3 /usr/share/lcsas/restore_wizard.py
    elif [ -x /usr/share/lcsas/restore.sh ]; then
        echo "Python wizard not available. Use restore.sh manually:"
        echo "  /usr/share/lcsas/restore.sh --help"
        exec /bin/sh
    else
        echo "No restore tools found. Dropping to shell."
        exec /bin/sh
    fi
fi
AUTOSTART
chmod +x "${ROOTFS}/etc/profile.d/lcsas-autostart.sh"

# ── Create LCSAS directories ────────────────────────────────────
mkdir -p "${ROOTFS}/usr/share/lcsas"
mkdir -p "${ROOTFS}/mnt/disc"
mkdir -p "${ROOTFS}/mnt/target"
mkdir -p "${ROOTFS}/mnt/usb"

# ── Write banner ─────────────────────────────────────────────────
cat > "${ROOTFS}/usr/share/lcsas/banner.txt" << 'BANNER'
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║    ██╗      ██████╗███████╗ █████╗ ███████╗                     ║
║    ██║     ██╔════╝██╔════╝██╔══██╗██╔════╝                     ║
║    ██║     ██║     ███████╗███████║███████╗                      ║
║    ██║     ██║     ╚════██║██╔══██║╚════██║                      ║
║    ███████╗╚██████╗███████║██║  ██║███████║                      ║
║    ╚══════╝ ╚═════╝╚══════╝╚═╝  ╚═╝╚══════╝                     ║
║                                                                  ║
║    Linux Cold Storage Archival Suite — Recovery Environment       ║
║                                                                  ║
║    This disc contains your backup recovery tools.                ║
║    A guided wizard will help you restore your files.             ║
║                                                                  ║
║    Press Alt+F2 or Alt+F3 for additional terminals.              ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
BANNER

# ── fstab — empty (dynamic mounts only) ─────────────────────────
echo "# LCSAS Recovery — all mounts are dynamic" > "${ROOTFS}/etc/fstab"

# ── hostname ─────────────────────────────────────────────────────
echo "lcsas-recovery" > "${ROOTFS}/etc/hostname"

# ── Enable essential services ────────────────────────────────────
chroot "${ROOTFS}" rc-update add udev sysinit 2>/dev/null || true
chroot "${ROOTFS}" rc-update add udev-trigger sysinit 2>/dev/null || true

# ── Clean up to reduce size ──────────────────────────────────────
rm -rf "${ROOTFS}/var/cache/apk/"*
rm -rf "${ROOTFS}/usr/share/man"
rm -rf "${ROOTFS}/usr/share/doc"
rm -rf "${ROOTFS}/usr/share/info"
rm -rf "${ROOTFS}/boot"  # kernel + initramfs already copied out

# ── Build squashfs ───────────────────────────────────────────────
mksquashfs "${ROOTFS}" "${OUTPUT_DIR}/rootfs.squashfs" \
    -comp xz \
    -Xdict-size 100% \
    -b 1M \
    -no-exports \
    -noappend \
    -no-recovery

echo "Squashfs: $(du -h "${OUTPUT_DIR}/rootfs.squashfs" | cut -f1)"

# ── Summary ──────────────────────────────────────────────────────
echo ""
echo "=== Build complete ==="
echo "  Kernel:    ${OUTPUT_DIR}/vmlinuz"
echo "  Initramfs: ${OUTPUT_DIR}/initramfs"
echo "  Rootfs:    ${OUTPUT_DIR}/rootfs.squashfs"
TOTAL=$(du -ch "${OUTPUT_DIR}/vmlinuz" "${OUTPUT_DIR}/initramfs" "${OUTPUT_DIR}/rootfs.squashfs" | tail -1 | cut -f1)
echo "  Total:     ${TOTAL}"
echo ""
echo "Next: copy LCSAS tools into rootfs.squashfs or overlay them"
echo "      at boot via the meta-volume /tools/ directory."
