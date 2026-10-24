#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Install the patched hp-wmi through DKMS and load it now (needs sudo).
# The stock module stays on disk; uninstall.sh brings it back.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=hp-wmi-charge
VER=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$SRC/dkms.conf")
KVER=$(uname -r)
die() { echo "error: $*" >&2; exit 1; }

[[ "$(cat /sys/class/dmi/id/board_name 2>/dev/null)" == 8BAB ]] \
    || die "this module is for the HP OMEN 16-wf0 (board 8BAB) only"
command -v dkms >/dev/null || die "dkms is missing (Arch: pacman -S dkms; Fedora: dnf install dkms; Debian/Ubuntu: apt install dkms)"
command -v curl >/dev/null || die "curl is missing"
[[ -d /lib/modules/$KVER/build ]] || die "no kernel headers for $KVER (Arch: linux-headers, Manjaro: linuxXYZ-headers, Fedora: kernel-devel, Debian/Ubuntu: linux-headers-$KVER)"

# Everything prepare-source.sh checks, before anything is installed.
"$SRC/prepare-source.sh" "$KVER"

sudo rm -rf "/usr/src/$NAME-$VER"
sudo mkdir -p "/usr/src/$NAME-$VER/src" "/usr/src/$NAME-$VER/cache"
sudo install -m644 "$SRC/dkms.conf" "$SRC/hp-wmi-charge.patch" "/usr/src/$NAME-$VER/"
sudo install -m755 "$SRC/prepare-source.sh" "/usr/src/$NAME-$VER/"
sudo install -m644 "$SRC/src/Makefile" "/usr/src/$NAME-$VER/src/"
sudo cp "$SRC"/cache/hp-wmi-*.c "/usr/src/$NAME-$VER/cache/" 2>/dev/null || true
sudo dkms remove "$NAME/$VER" --all >/dev/null 2>&1 || true
sudo dkms install "$NAME/$VER" -k "$KVER"

# Swapping the module resets the platform profile; put the current one back.
profile=$(powerprofilesctl get 2>/dev/null || true)
sudo sh -c 'rmmod hp_wmi; modprobe hp_wmi'
sleep 2
[[ -n "$profile" ]] && powerprofilesctl set "$profile" || true

bat=$(grep -ls inhibit-charge /sys/class/power_supply/*/charge_behaviour | head -1 || true)
if [[ -n "$bat" ]]; then
    echo "ok: $(modinfo -F filename hp_wmi)"
    echo "    $bat: $(cat "$bat")"
else
    die "installed, but charge_behaviour did not appear; see: sudo dmesg | grep hp-wmi"
fi
