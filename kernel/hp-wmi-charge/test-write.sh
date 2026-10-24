#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Write test (run as root, on AC). Switches charging off for 20 s, then back
# to auto, showing what the battery does. Only the two modes the patched
# driver offers are ever written: inhibit-charge and auto.
set -u
[ "$(cat /sys/class/dmi/id/board_name)" = 8BAB ] || { echo "for HP board 8BAB only"; exit 1; }
BAT=/sys/class/power_supply/BAT1
show() { echo "$(date +%T) $1: $(cat $BAT/charge_behaviour) | $(cat $BAT/status), $(cat $BAT/capacity) %, power_now $(( $(cat $BAT/power_now) / 1000 )) mW"; }
[ "$(cat /sys/class/power_supply/ACAD/online)" = 1 ] || { echo "plug in the charger first"; exit 1; }
if ! grep -qs inhibit-charge $BAT/charge_behaviour; then
    # Swapping hp-wmi resets the platform profile to balanced; put the
    # user's profile back afterwards.
    profile=$(powerprofilesctl get)
    ko="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/src/hp-wmi.ko"
    [ -f "$ko" ] || { echo "run ./build.sh first"; exit 1; }
    rmmod hp_wmi && insmod "$ko" \
        || { echo "insmod failed, restoring stock"; modprobe hp_wmi; exit 1; }
    sleep 2
    powerprofilesctl set "$profile"
    echo "patched hp-wmi loaded, profile back to $profile"
fi
grep -qs inhibit-charge $BAT/charge_behaviour || { echo "no charge_behaviour after loading"; exit 1; }
trap 'echo auto > $BAT/charge_behaviour; show "restored on exit"' EXIT
show "before"
echo inhibit-charge > $BAT/charge_behaviour; show "inhibit written"
for i in 1 2 3 4; do sleep 5; show "inhibit +$((i*5))s"; done
echo auto > $BAT/charge_behaviour; show "auto written"
for i in 1 2 3; do sleep 5; show "auto +$((i*5))s"; done
trap - EXIT
