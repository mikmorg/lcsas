#!/usr/bin/env bash
# =============================================================================
# LCSAS — virtual optical drive (cdemu) helper
# =============================================================================
# Drives a single emulated SCSI CD/DVD-ROM via cdemu-daemon so the LCSAS
# restore path can be exercised against /dev/sr0 without real hardware.
#
# Models the realistic single-drive case: the restorer has exactly one
# optical drive and must swap discs between volumes.
#
# Subcommands:
#   setup        Install pkgs, persist udev/modprobe, start daemon (one-time)
#   start        Make sure vhba is loaded and cdemu-daemon user service is up
#   stop         Stop cdemu-daemon user service
#   status       Show device + loaded image
#   load <iso>   Insert an ISO into the virtual drive
#   unload       Eject the virtual drive
#   swap <iso>   unload then load (the typical restore inner loop)
#   device       Print the kernel device node (e.g. /dev/sr0)
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
DEVICE="/dev/sr0"

err()  { printf '\033[0;31m[ERR]\033[0m %s\n' "$*" >&2; }
info() { printf '\033[0;32m[+]\033[0m %s\n' "$*"; }

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
  if [[ ! -e /dev/vhba_ctl ]]; then
    err "/dev/vhba_ctl missing — did setup run?"
    exit 1
  fi
  # Preflight: the cdemu daemon runs as the seat user and needs RW on
  # /dev/vhba_ctl.  The setup phase installs a udev rule + ACL that
  # grants the `cdrom` group access.  If setup wasn't run (or the
  # udev rule wasn't applied — e.g. vhba was loaded BEFORE the rule
  # was installed, common after a one-shot `modprobe vhba`), the
  # daemon will fail with `Permission denied!` deep in libMirage.
  # Detect it here and print the exact fix command rather than letting
  # the operator chase a cryptic systemctl failure.
  if [[ ! -r /dev/vhba_ctl || ! -w /dev/vhba_ctl ]]; then
    err "/dev/vhba_ctl: $USER lacks rw permission."
    err "  current owner/mode: $(stat -c '%U:%G %a' /dev/vhba_ctl 2>/dev/null)"
    err "  granted via ACL:    $(getfacl /dev/vhba_ctl 2>/dev/null | \
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
  # wait for /dev/sr0 to appear
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -e $DEVICE ]] && break
    sleep 0.2
  done
  if [[ ! -e $DEVICE ]]; then
    err "$DEVICE never appeared; check: journalctl --user -u cdemu-daemon"
    exit 1
  fi
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
  cdemu unload 0 || true
  info "ejected $DEVICE"
}

cmd_swap() {
  need_user
  local iso="${1:?usage: $PROG swap <iso>}"
  cdemu unload 0 || true
  cmd_load "$iso"
}

cmd_device() {
  echo "$DEVICE"
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
