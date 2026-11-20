#!/usr/bin/env bash
# =============================================================================
# LCSAS — virtual optical drive (cdemu) helper
# =============================================================================
# Drives a single emulated SCSI CD/DVD-ROM via cdemu-daemon so the LCSAS
# restore path can be exercised against /dev/sr0 without real hardware.
#
# Models the realistic single-drive case: the heir has exactly one
# optical drive and must swap discs between volumes.
#
# Subcommands:
#   setup        Install pkgs, persist udev/modprobe, start daemon (one-time)
#   start        Make sure vhba is loaded, cdemu-daemon user service is up
#                and device 0 is mapped to a /dev/srN (restarts the daemon
#                once if it is not, else fails naming the problem)
#   stop         Stop cdemu-daemon user service
#   status       Show device + loaded image
#   load <iso>   Insert an ISO image into the virtual drive
#   unload       Eject the virtual drive (waits until it is empty, #756)
#   swap <iso>   unload then load (the typical restore inner loop)
#   device       Print device 0's kernel node (e.g. /dev/sr0)
#
# Notes:
# - Runs as your normal user; the daemon is a systemd --user unit.
# - lingering is enabled so the daemon survives logout.
# - The vhba kernel module's /dev/vhba_ctl is granted to group `cdrom` via a
#   udev rule + setfacl, since uaccess only grants the seat user (lightdm on
#   this headless VM).
# =============================================================================

set -euo pipefail

PROG="$(basename "$0")"
DEVICE="/dev/sr0"   # the usual node; `start` and `device` read the real one
VHBA_CTL="${VHBA_CTL:-/dev/vhba_ctl}"
MAP_TRIES="${CDEMU_MAP_TRIES:-25}"   # polls of 0.2 s for device 0's node
UNLOAD_TRIES="${CDEMU_UNLOAD_TRIES:-50}"   # polls of 0.2 s for an unload

err()  { printf '\033[0;31m[ERR]\033[0m %s\n' "$*" >&2; }
info() { printf '\033[0;32m[+]\033[0m %s\n' "$*"; }

# Device 0's SCSI CD-ROM node from `cdemu device-mapping` (a row
# "0  /dev/sr0  /dev/sg0"), or nothing when the daemon is not answering
# or device 0 has no node.  After a run of blind-restore teardowns the
# daemon can stay up with device 0 unmapped (#768).
device0_node() {
  cdemu device-mapping 2>/dev/null \
    | awk '$1 == "0" && $2 ~ /^\/dev\// { print $2; exit }' || true
}

# Wait up to MAP_TRIES x 0.2 s (5 s) for device 0 to be mapped to a node
# that exists.  Prints the node; returns 1 if it never is.
wait_device0() {
  local node="" _
  for _ in $(seq "$MAP_TRIES"); do
    node="$(device0_node)"
    if [[ -n $node && -e $node ]]; then
      printf '%s\n' "$node"
      return 0
    fi
    sleep 0.2
  done
  return 1
}

# Whether device 0 holds an image: its row of `cdemu status` ("0  True
# FILE") says True.  A daemon that does not answer holds none.
device0_loaded() {
  cdemu status 2>/dev/null | awk '$1 == "0" { print $2; exit }' \
    | grep -qx True
}

# `cdemu unload 0` returns at once, and exits 0, while the image can stay
# in for about 2 s more after the disc was unmounted; a `load` then fails
# with "Device is already loaded" (#756, the root smoke run).  So ask,
# then wait up to UNLOAD_TRIES x 0.2 s (10 s) for device 0 to be empty,
# asking again each second.  Returns 1, naming the image, if it stays in.
unload_device0() {
  local i
  cdemu unload 0 >/dev/null 2>&1 || true
  for i in $(seq "$UNLOAD_TRIES"); do
    device0_loaded || return 0
    if (( i % 5 == 0 )); then
      cdemu unload 0 >/dev/null 2>&1 || true
    fi
    sleep 0.2
  done
  device0_loaded || return 0
  err "device 0 still holds an image after the unload: is it mounted?"
  cdemu status 2>/dev/null | sed 's/^/    /' >&2 || true
  return 1
}

need_user() {
  if [[ $EUID -eq 0 ]]; then
    err "run as your normal user, not root (the daemon is a --user service)"
    exit 1
  fi
}

cmd_setup() {
  if [[ $EUID -ne 0 ]]; then
    info "re-execing under sudo for package + udev install"
    exec sudo -E bash "$0" setup
  fi

  info "installing cdemu-daemon, cdemu-client, vhba-dkms, xorriso"
  if ! command -v cdemu-daemon >/dev/null; then
    add-apt-repository -y ppa:cdemu/ppa
  fi
  apt-get update -qq
  apt-get install -y cdemu-daemon cdemu-client vhba-dkms xorriso acl

  info "writing /etc/modules-load.d/vhba.conf"
  echo vhba > /etc/modules-load.d/vhba.conf
  echo "options vhba virtual_devices=1" > /etc/modprobe.d/vhba.conf

  info "writing /etc/udev/rules.d/60-vhba.rules"
  cat > /etc/udev/rules.d/60-vhba.rules <<'EOF'
# LCSAS test rig: let group `cdrom` access vhba so cdemu-daemon can run as
# a normal --user systemd unit on a headless box (no seat).
KERNEL=="vhba_ctl", SUBSYSTEM=="misc", GROUP="cdrom", MODE="0660", \
    RUN+="/usr/bin/setfacl -m g:cdrom:rw /dev/vhba_ctl"
EOF
  udevadm control --reload-rules
  modprobe vhba 2>/dev/null || true
  if [[ -e /dev/vhba_ctl ]]; then
    chgrp cdrom /dev/vhba_ctl
    chmod 660  /dev/vhba_ctl
    setfacl -m g:cdrom:rw /dev/vhba_ctl
  fi

  REAL_USER="${SUDO_USER:-$USER}"
  info "enabling lingering for $REAL_USER"
  loginctl enable-linger "$REAL_USER" || true

  info "installing systemd --user override (1 device, null audio)"
  USER_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
  install -d -o "$REAL_USER" -g "$REAL_USER" \
      "$USER_HOME/.config/systemd/user/cdemu-daemon.service.d"
  cat > "$USER_HOME/.config/systemd/user/cdemu-daemon.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/cdemu-daemon --num-devices 1 --audio-driver null
EOF
  chown -R "$REAL_USER:$REAL_USER" \
      "$USER_HOME/.config/systemd/user/cdemu-daemon.service.d"

  info "setup complete — run: $PROG start"
}

cmd_start() {
  need_user
  if ! lsmod | grep -q '^vhba'; then
    info "loading vhba kernel module"
    sudo modprobe vhba
  fi
  if [[ ! -e $VHBA_CTL ]]; then
    err "$VHBA_CTL missing — did setup run?"
    exit 1
  fi
  # Preflight: the cdemu daemon runs as the seat user and needs RW on
  # /dev/vhba_ctl.  The setup phase installs a udev rule + ACL that
  # grants the `cdrom` group access.  If setup wasn't run (or the
  # udev rule wasn't applied — e.g. vhba was loaded BEFORE the rule
  # was installed, common after a one-shot `modprobe vhba`), the
  # daemon will fail with `Permission denied!` deep in libMirage.
  # Detect it here and print the exact fix command rather than leaving
  # you to chase a cryptic systemctl failure.
  if [[ ! -r $VHBA_CTL || ! -w $VHBA_CTL ]]; then
    err "$VHBA_CTL: $USER lacks rw permission."
    err "  current owner/mode: $(stat -c '%U:%G %a' "$VHBA_CTL" 2>/dev/null)"
    err "  granted via ACL:    $(getfacl "$VHBA_CTL" 2>/dev/null | \
        grep -E '^(user|group):[^:]+:' | tr '\n' ' ')"
    err ""
    err "Fix options (pick one):"
    err "  (a) Run setup once:        sudo bash $0 setup"
    err "  (b) Quick one-shot ACL:    sudo setfacl -m u:$USER:rw /dev/vhba_ctl"
    err "      (resets on next vhba reload — use (a) for persistence)"
    exit 1
  fi
  systemctl --user daemon-reload
  systemctl --user start cdemu-daemon.service
  # The daemon being up is not enough: device 0 must be mapped to a
  # /dev/srN.  It can lose that mapping and stay up (#768); a restart of
  # the user daemon brings it back, so try that once.
  local node
  if ! node="$(wait_device0)"; then
    info "cdemu device 0 has no /dev/srN mapping; restarting cdemu-daemon once"
    systemctl --user restart cdemu-daemon.service
    if ! node="$(wait_device0)"; then
      err "cdemu device 0 is still not mapped to a /dev/srN after one restart"
      err "of cdemu-daemon.  What cdemu device-mapping shows:"
      cdemu device-mapping >&2 2>&1 || true
      err "Check: journalctl --user -u cdemu-daemon"
      err "Then try: sudo modprobe -r vhba && $PROG start"
      exit 1
    fi
  fi
  DEVICE="$node"
  info "virtual drive ready at $DEVICE"
}

cmd_stop() {
  need_user
  systemctl --user stop cdemu-daemon.service || true
  info "cdemu-daemon stopped"
}

cmd_status() {
  need_user
  cdemu status
  cdemu device-mapping
}

cmd_load() {
  need_user
  local iso="${1:?usage: $PROG load <iso>}"
  [[ -f $iso ]] || { err "no such file: $iso"; exit 1; }
  iso=$(readlink -f "$iso")
  cdemu load 0 "$iso"
  info "loaded $iso into $DEVICE"
}

cmd_unload() {
  need_user
  unload_device0
  info "ejected $DEVICE"
}

cmd_swap() {
  need_user
  local iso="${1:?usage: $PROG swap <iso>}"
  unload_device0
  cmd_load "$iso"
}

cmd_device() {
  local node
  node="$(device0_node)"
  echo "${node:-$DEVICE}"
}

case "${1:-}" in
  setup)   shift; cmd_setup   "$@" ;;
  start)   shift; cmd_start   "$@" ;;
  stop)    shift; cmd_stop    "$@" ;;
  status)  shift; cmd_status  "$@" ;;
  load)    shift; cmd_load    "$@" ;;
  unload)  shift; cmd_unload  "$@" ;;
  swap)    shift; cmd_swap    "$@" ;;
  device)  shift; cmd_device  "$@" ;;
  ""|-h|--help)
    sed -n '2,30p' "$0"
    ;;
  *)
    err "unknown subcommand: $1"
    exit 2
    ;;
esac
