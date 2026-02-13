#!/usr/bin/env bash
# =============================================================================
# LCSAS Test Environment Teardown
# =============================================================================
# Run as: sudo bash scripts/teardown_test_lv.sh
# =============================================================================

set -euo pipefail

VG_NAME="mikmorg-7510-vg"
LV_NAME="lcsas-test"
MOUNT_POINT="/mnt/lcsas-test"

GREEN='\033[0;32m'
NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }

if mountpoint -q "${MOUNT_POINT}" 2>/dev/null; then
    info "Unmounting ${MOUNT_POINT}"
    umount "${MOUNT_POINT}"
fi

if [ -d "${MOUNT_POINT}" ]; then
    info "Removing mount point ${MOUNT_POINT}"
    rmdir "${MOUNT_POINT}" 2>/dev/null || true
fi

if lvs "${VG_NAME}/${LV_NAME}" &>/dev/null; then
    info "Removing logical volume ${VG_NAME}/${LV_NAME}"
    lvremove -f "${VG_NAME}/${LV_NAME}"
fi

info "Teardown complete"
