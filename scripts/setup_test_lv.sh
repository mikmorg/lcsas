#!/usr/bin/env bash
# =============================================================================
# LCSAS End-to-End Test Environment Setup
# =============================================================================
# Run as: sudo bash scripts/setup_test_lv.sh
#
# Creates a 10 GB logical volume, formats it, mounts it, and sets up the
# directory structure needed for end-to-end testing.
# =============================================================================

set -euo pipefail

VG_NAME="mikmorg-7510-vg"
LV_NAME="lcsas-data"
LV_SIZE="10G"
MOUNT_POINT="/mnt/lcsas-data"
TEST_USER="${SUDO_USER:-mikmorg}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── 1. Create logical volume ─────────────────────────────────────────────────

if lvs "${VG_NAME}/${LV_NAME}" &>/dev/null; then
    warn "LV ${VG_NAME}/${LV_NAME} already exists — skipping creation"
else
    info "Creating ${LV_SIZE} logical volume: ${VG_NAME}/${LV_NAME}"
    lvcreate -L "${LV_SIZE}" -n "${LV_NAME}" "${VG_NAME}"
fi

LV_PATH="/dev/${VG_NAME}/${LV_NAME}"

# ── 2. Format with ext4 ──────────────────────────────────────────────────────

# Check if already formatted
if blkid "${LV_PATH}" | grep -q ext4; then
    warn "LV already formatted as ext4 — skipping"
else
    info "Formatting ${LV_PATH} as ext4"
    mkfs.ext4 -L lcsas-data "${LV_PATH}"
fi

# ── 3. Mount ──────────────────────────────────────────────────────────────────

mkdir -p "${MOUNT_POINT}"

if mountpoint -q "${MOUNT_POINT}"; then
    warn "${MOUNT_POINT} already mounted — skipping"
else
    info "Mounting at ${MOUNT_POINT}"
    mount "${LV_PATH}" "${MOUNT_POINT}"
fi

# ── 4. Create directory structure ─────────────────────────────────────────────

info "Creating test directory structure"
mkdir -p "${MOUNT_POINT}"/{mirror,staging,iso_output,restore,db,test_data}

# Own everything by the test user
chown -R "${TEST_USER}:${TEST_USER}" "${MOUNT_POINT}"

# ── 5. Install required tools ────────────────────────────────────────────────

info "Installing required packages (rustic, xorriso, dvdisaster)"
apt-get update -qq
apt-get install -y -qq rustic xorriso dvdisaster 2>/dev/null || {
    warn "Some packages may not be available. Checking individually..."
    for pkg in rustic xorriso; do
        apt-get install -y -qq "$pkg" 2>/dev/null || warn "Failed to install $pkg"
    done
    # dvdisaster may not be in repos — optional
    apt-get install -y -qq dvdisaster 2>/dev/null || warn "dvdisaster not available — ECC testing will be skipped"
}

# ── 6. Summary ───────────────────────────────────────────────────────────────

echo ""
info "=========================================="
info " LV Setup Complete"
info "=========================================="
echo ""
echo "  LV:         ${LV_PATH}"
echo "  Mount:      ${MOUNT_POINT}"
echo "  Size:       ${LV_SIZE}"
echo ""
echo "  Directories:"
echo "    Mirror:   ${MOUNT_POINT}/mirror"
echo "    Staging:  ${MOUNT_POINT}/staging"
echo "    ISO out:  ${MOUNT_POINT}/iso_output"
echo "    Restore:  ${MOUNT_POINT}/restore"
echo "    DB:       ${MOUNT_POINT}/db"
echo "    Test data: ${MOUNT_POINT}/test_data"
echo ""
echo "  Tools:"
echo "    rustic:     $(which rustic 2>/dev/null || echo 'NOT FOUND')"
echo "    xorriso:    $(which xorriso 2>/dev/null || echo 'NOT FOUND')"
echo "    dvdisaster: $(which dvdisaster 2>/dev/null || echo 'NOT FOUND')"
echo ""
echo "  Next steps:"
echo "    cd /home/mikmorg/lcsas"
echo "    source .venv/bin/activate"
echo "    python scripts/e2e_test.py"
echo ""
