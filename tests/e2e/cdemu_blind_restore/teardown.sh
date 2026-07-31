#!/usr/bin/env bash
# teardown.sh — remove everything setup.py installed. Idempotent.

set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

# Unmount whatever the blind run mounted from the cdemu virtual drive
# BEFORE unloading it -- a drive can't cleanly unload while its disc is
# still mounted, and a left-behind mount is exactly what broke #440: wine
# maps `d: -> /media` in every fresh WINEPREFIX when something is really
# mounted there, so a stale /media mount makes restore.bat's D:-Z: scan
# (tests/recovery_hardening/test_restore_bat_wine_smoke.py) find a phantom
# backup set instead of reporting "could not find an LCSAS backup set".
# Only unmount mountpoints actually backed by the cdemu drive (/dev/sr0),
# never anything else that might happen to be mounted at /media.
for mp in $(findmnt -rno TARGET -S /dev/sr0 2>/dev/null || true); do
    umount "$mp" >/dev/null 2>&1 || true
done

bash /home/mikmorg/git/lcsas/scripts/cdemu_drive.sh unload >/dev/null 2>&1 || true

rm -rf /var/lib/disc-vault
rm -rf /opt/disc-robot
rm -rf /var/lib/lcsas-blind-test
rm -rf /scratch/lcsas-blind-claude-cfg  # agent CLAUDE_CONFIG_DIR (off "/")
rm -rf /home/lcsas-blind/.claude-cfg     # legacy on-"/" location, pre-fix
rm -rf /mnt/lcsas-data/blind-test  # legacy v1 location, harmless if absent
rm -f  /usr/local/bin/disc-loader
rm -f  /usr/local/libexec/cdemu_drive.sh
rm -f  /var/log/disc-loader.log
rm -f  /etc/sudoers.d/lcsas-blind
rm -f  /etc/sysctl.d/99-blind-restore.conf

if id lcsas-blind >/dev/null 2>&1; then
    pkill -u lcsas-blind || true
    userdel -r lcsas-blind 2>/dev/null || userdel lcsas-blind 2>/dev/null || true
fi

sysctl -w kernel.dmesg_restrict=0 >/dev/null 2>&1 || true

# Restore hidden vhba udev rule if setup.py renamed it.
if [[ -f /etc/udev/rules.d/.60-vhba.rules.bak ]]; then
    mv /etc/udev/rules.d/.60-vhba.rules.bak /etc/udev/rules.d/60-vhba.rules
fi

# Restore renamed directories and unlock sealed ones.
[[ -d /mnt/.optical-test ]] && mv /mnt/.optical-test /mnt/cdemu-test 2>/dev/null || true
[[ -d /scratch/.optical-test ]] && mv /scratch/.optical-test /scratch/cdemu-test 2>/dev/null || true
for d in /mnt/cdemu-test /mnt/staging /scratch/cdemu-test /scratch/cargo-target; do
    [[ -d "$d" ]] && chmod 755 "$d" 2>/dev/null || true
done

echo "teardown complete"
