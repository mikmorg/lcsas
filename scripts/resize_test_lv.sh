#!/usr/bin/env bash
# =============================================================================
# Resize lcsas-data LV to use all available VG free space
# Run as: sudo bash scripts/resize_test_lv.sh
# =============================================================================
set -euo pipefail

VG_NAME="mikmorg-7510-vg"
LV_NAME="lcsas-data"
MOUNT_POINT="/mnt/lcsas-data"
LV_PATH="/dev/${VG_NAME}/${LV_NAME}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# Show current state
info "Current LV size:"
lvs "${VG_NAME}/${LV_NAME}" -o lv_size --noheadings --units g

info "VG free space:"
vgs "${VG_NAME}" -o vg_free --noheadings --units g

FREE_EXTENTS=$(vgs "${VG_NAME}" -o vg_free_count --noheadings | tr -d ' ')
if [[ "$FREE_EXTENTS" -eq 0 ]]; then
    warn "No free space in VG — nothing to do"
    exit 0
fi

# Extend LV to use all free space
info "Extending LV to use all free VG space (+${FREE_EXTENTS} extents)..."
lvextend -l +100%FREE "${LV_PATH}"

# Resize filesystem online (ext4 supports this)
info "Resizing ext4 filesystem..."
resize2fs "${LV_PATH}"

# Show result
info "New LV size:"
lvs "${VG_NAME}/${LV_NAME}" -o lv_size --noheadings --units g

info "New filesystem usage:"
df -h "${MOUNT_POINT}"

info "Done! lcsas-data is now ready for E2E testing."
