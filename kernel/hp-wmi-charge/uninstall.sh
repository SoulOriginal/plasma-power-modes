#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Remove the patched hp-wmi and go back to the kernel's own module.
set -euo pipefail
NAME=hp-wmi-charge
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$SRC/dkms.conf")
# Charging back to normal before the driver that can change it goes.
echo auto | sudo tee /sys/class/power_supply/BAT1/charge_behaviour >/dev/null 2>&1 || true
sudo dkms remove "$NAME/$VER" --all || true
sudo rm -rf "/usr/src/$NAME-$VER"
profile=$(powerprofilesctl get 2>/dev/null || true)
sudo sh -c 'rmmod hp_wmi; modprobe hp_wmi'
sleep 2
[[ -n "$profile" ]] && powerprofilesctl set "$profile" || true
echo "stock hp-wmi: $(modinfo -F filename hp_wmi)"
