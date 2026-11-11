#!/bin/sh
# Prepare apt on a GitHub-hosted Ubuntu runner before `apt-get update`.
#
# On 2026-11-11 every job's apt step failed for hours with
# "Temporary failure resolving 'azure.archive.ubuntu.com'" while
# github.com still resolved.  The runner images send apt through
# mirror+file:/etc/apt/apt-mirrors.txt with the Azure mirror first.
# This script makes apt retry, and when the Azure mirror does not
# resolve it removes it from the mirror list and from any source that
# names it directly, so apt uses archive.ubuntu.com instead.
set -u

echo 'Acquire::Retries "5";' | sudo tee /etc/apt/apt.conf.d/80-lcsas-retries >/dev/null

if getent hosts azure.archive.ubuntu.com >/dev/null 2>&1; then
    exit 0
fi

echo "apt-prepare: azure.archive.ubuntu.com does not resolve; using archive.ubuntu.com"
if [ -f /etc/apt/apt-mirrors.txt ]; then
    sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt
    if ! grep -q 'archive\.ubuntu\.com' /etc/apt/apt-mirrors.txt; then
        echo 'http://archive.ubuntu.com/ubuntu/' | sudo tee -a /etc/apt/apt-mirrors.txt >/dev/null
    fi
fi
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    sudo sed -i 's#azure\.archive\.ubuntu\.com#archive.ubuntu.com#g' "$f"
done
exit 0
