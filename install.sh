#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Install (or remove) the Power Modes widget, its two root helpers, their
# polkit actions and the battery charge limit service.
#
#   ./install.sh               install, copying the widget
#   ./install.sh --dev         install, symlinking the widget to this checkout
#   ./install.sh --uninstall   put everything back and remove it
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLET_ID="io.github.souloriginal.powermodes"
PLASMOID_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/plasma/plasmoids"
BIN=/usr/local/bin
POLKIT=/usr/share/polkit-1/actions
UNIT=/etc/systemd/system/ecomode-charge.service
SLEEP_HOOK=/usr/lib/systemd/system-sleep/ecomode-charge

say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

# The package that provides a missing piece, per distribution family.
pkg_hint() {   # $1: arch fedora suse debian names, separated by |
    local id=" $(. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}") "
    IFS='|' read -r arch fedora suse debian <<<"$1"
    case "$id" in
        *" arch "*|*" manjaro "*|*" endeavouros "*) echo "sudo pacman -S $arch" ;;
        *" fedora "*|*" rhel "*)                   echo "sudo dnf install $fedora" ;;
        *" opensuse"*|*" suse "*)                   echo "sudo zypper install $suse" ;;
        *" debian "*|*" ubuntu "*)                  echo "sudo apt install $debian" ;;
        *)                                          echo "install: $arch (Arch name)" ;;
    esac
}

uninstall() {
    echo "Removing Power Modes..."
    if [[ -x $BIN/ecomode ]]; then
        # Charging, power limits and Max back to how they were first.
        sudo "$BIN/ecomode" charge off >/dev/null 2>&1 || true
        sudo "$BIN/ecomode" off >/dev/null 2>&1 || true
    fi
    if [[ -x $BIN/ecomode-max ]]; then
        sudo "$BIN/ecomode-max" off >/dev/null 2>&1 || true
        sudo "$BIN/ecomode-max" watts off >/dev/null 2>&1 || true
    fi
    sudo systemctl disable --now ecomode-charge.service >/dev/null 2>&1 || true
    sudo rm -f "$UNIT" "$SLEEP_HOOK" "$BIN/ecomode" "$BIN/ecomode-max" \
        "$POLKIT/$APPLET_ID.policy" "$POLKIT/$APPLET_ID.max.policy"
    sudo rm -rf /var/lib/ecomode
    sudo systemctl daemon-reload
    rm -rf "${PLASMOID_DIR:?}/$APPLET_ID"
    say "removed; restart Plasma to drop the widget from a running panel:"
    say "systemctl --user restart plasma-plasmashell.service"
    exit 0
}

MODE=copy
case "${1:-}" in
    --uninstall) uninstall ;;
    --dev) MODE=link ;;
    "") ;;
    *) die "unknown option $1 (use --dev or --uninstall)" ;;
esac

echo "Installing Power Modes..."

# ── 1. What this machine has ────────────────────────────────────────────────
missing=0
has plasmashell || die "KDE Plasma is not installed"
plasma=$(plasmashell --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
[[ "${plasma%%.*}" -ge 6 ]] || die "Plasma 6 is needed, found ${plasma:-unknown}"
say "plasma    $plasma"
has python3 || die "python3 is missing: $(pkg_hint 'python|python3|python3|python3')"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "python 3.9 or newer is needed"
for tool in pkexec systemctl systemd-run; do
    has "$tool" || { warn "$tool is missing"; missing=1; }
done
[[ $missing -eq 0 ]] || die "polkit and systemd are required: $(pkg_hint 'polkit|polkit|polkit|polkit')"

if ! has powerprofilesctl; then
    warn "power-profiles-daemon not found: Max and 'Follow KDE profile' need it"
    warn "  $(pkg_hint 'power-profiles-daemon|power-profiles-daemon|power-profiles-daemon|power-profiles-daemon')"
    warn "  (it cannot run next to TLP or auto-cpufreq; remove those first)"
fi
python3 -c 'import gi' 2>/dev/null \
    || warn "PyGObject not found: Max needs it - $(pkg_hint 'python-gobject|python3-gobject|python3-gobject|python3-gi')"
[[ -d /sys/class/powercap/intel-rapl:0 ]] \
    || warn "no Intel RAPL: Eco has no power limits to set on this CPU"

# ── 2. Widget ───────────────────────────────────────────────────────────────
mkdir -p "$PLASMOID_DIR"
rm -rf "${PLASMOID_DIR:?}/$APPLET_ID"
if [[ $MODE == link ]]; then
    ln -s "$SRC" "$PLASMOID_DIR/$APPLET_ID"
    say "widget    $PLASMOID_DIR/$APPLET_ID -> $SRC"
else
    mkdir -p "$PLASMOID_DIR/$APPLET_ID"
    cp -r "$SRC/metadata.json" "$SRC/contents" "$PLASMOID_DIR/$APPLET_ID/"
    say "widget    $PLASMOID_DIR/$APPLET_ID"
fi

# ── 3. Root helpers, polkit actions, charge service (sudo) ─────────────────
sudo install -Dm755 "$SRC/backend/ecomode" "$BIN/ecomode"
sudo install -Dm755 "$SRC/backend/ecomode-max" "$BIN/ecomode-max"
sudo install -Dm644 "$SRC/polkit/$APPLET_ID.policy" "$POLKIT/$APPLET_ID.policy"
sudo install -Dm644 "$SRC/polkit/$APPLET_ID.max.policy" "$POLKIT/$APPLET_ID.max.policy"
say "helpers   $BIN/ecomode, $BIN/ecomode-max"
# Installed always, enabled only by the Battery care switch.
sudo install -Dm644 "$SRC/systemd/ecomode-charge.service" "$UNIT"
sudo install -Dm755 "$SRC/systemd/ecomode-charge.sleep" "$SLEEP_HOOK"
sudo systemctl daemon-reload
# A running service keeps the old code until it is restarted.
if systemctl is-active --quiet ecomode-charge.service; then
    sudo systemctl restart ecomode-charge.service
fi
say "service   $UNIT (off until Battery care is switched on)"
say "sleep     $SLEEP_HOOK (holds charging while suspended)"

# ── 4. They must answer ─────────────────────────────────────────────────────
"$BIN/ecomode" --json status | python3 -c \
    'import json, sys; d = json.load(sys.stdin); sys.exit(0 if "mode" in d else 1)' \
    || die "the helper is installed but 'ecomode status' fails"
"$BIN/ecomode-max" --help >/dev/null 2>&1 || die "ecomode-max does not start"
say "verified  helpers respond"

# ── 5. What this machine can do ─────────────────────────────────────────────
"$BIN/ecomode" --json status | python3 -c '
import json, sys
d = json.load(sys.stdin)
b, g = d.get("battery") or {}, d.get("dgpu") or {}
print("  max       " + ("available" if d.get("max_supported") else
                        "not on this machine (" + str(d.get("max_unsupported_reason")) + ")"))
print("  battery   " + ("charge limit available" if b.get("charge_supported") else
                        "no charge limit" if b else "no battery"))
print("  gpu       " + ("NVIDIA found" if g.get("present") else "no NVIDIA GPU"))
'
if [[ "$(cat /sys/class/dmi/id/board_name 2>/dev/null)" == 8BAB ]] \
        && ! grep -qs inhibit-charge /sys/class/power_supply/BAT*/charge_behaviour; then
    say "          for the charge limit on this OMEN: kernel/hp-wmi-charge/install.sh"
fi

cat <<EOF

Done. Add it with right click on the panel → Add Widgets... → "Power Modes".
If an older copy was already on the panel, restart Plasma:
  systemctl --user restart plasma-plasmashell.service
EOF
