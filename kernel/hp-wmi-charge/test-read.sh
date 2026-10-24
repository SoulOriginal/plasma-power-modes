#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Read-only test of the patched hp-wmi (run as root). Swaps the stock module
# for the patched build, shows what the firmware reports, writes nothing.
# Back to stock: rmmod hp_wmi && modprobe hp_wmi
set -u
[ "$(cat /sys/class/dmi/id/board_name)" = 8BAB ] || { echo "for HP board 8BAB only"; exit 1; }
[ -f "$(dirname "$0")/src/hp-wmi.ko" ] || { echo "run ./build.sh first"; exit 1; }
KO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/src/hp-wmi.ko"
BAT=/sys/class/power_supply/BAT1
echo "profile before: $(cat /sys/firmware/acpi/platform_profile)"
rmmod hp_wmi || exit 1
insmod "$KO" || { echo "insmod failed, restoring stock"; modprobe hp_wmi; exit 1; }
sleep 1
echo "--- dmesg"
dmesg | grep -iE "hp.wmi|charge behaviour" | tail -5
echo "--- $BAT/charge_behaviour"
cat "$BAT/charge_behaviour" 2>&1
echo "--- battery: $(cat $BAT/status), $(cat $BAT/capacity) %, AC online: $(cat /sys/class/power_supply/ACAD/online)"
echo "profile after: $(cat /sys/firmware/acpi/platform_profile)"
echo "hwmon hp: $(cat /sys/class/hwmon/*/name | grep -c '^hp$') (fans $(cat /sys/class/hwmon/hwmon*/fan1_input 2>/dev/null | head -1) RPM)"
