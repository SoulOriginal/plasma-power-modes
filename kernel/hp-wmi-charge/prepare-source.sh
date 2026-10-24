#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Make src/hp-wmi.c for one kernel: that kernel's own hp-wmi.c plus the patch.
# Run by DKMS before every build (PRE_BUILD) and by install.sh.
#
#   prepare-source.sh <kernelver>
#
# 1. Take hp-wmi.c of the kernel's upstream version: from cache/, else from
#    kernel.org (stable tree, tag v6.18.49 for 6.18.49-1-MANJARO).
# 2. Build it unchanged and compare its srcversion with the kernel's own
#    hp-wmi module. They differ when the distribution patched the driver, or
#    the version guess was wrong: then this stops, and the stock module stays.
# 3. Apply the patch. If it does not apply cleanly to this version, stop too.
#
# Any failure exits non-zero; DKMS then skips the build and the kernel keeps
# its own hp-wmi, without charge control.
set -eu
kver="${1:?usage: prepare-source.sh <kernelver>}"
here=$(cd "$(dirname "$0")" && pwd)
build="/lib/modules/$kver/build"
fail() { echo "hp-wmi-charge: $*; the kernel's own hp-wmi stays" >&2; exit 1; }

[ -d "$build" ] || fail "no headers for $kver ($build)"
base=$(echo "$kver" | sed -n 's/^\([0-9]*\.[0-9]*\)\.\([0-9]*\).*/\1.\2/p')
[ -n "$base" ] || fail "cannot read a version from $kver"
case "$base" in *.0) tag="v${base%.0}" ;; *) tag="v$base" ;; esac

stock=$(find "/lib/modules/$kver/kernel" -name 'hp-wmi.ko*' 2>/dev/null | head -1)
[ -n "$stock" ] || fail "$kver has no hp-wmi module of its own"
want=$(modinfo -F srcversion "$stock")

orig="$here/cache/hp-wmi-$base.c"
if [ ! -s "$orig" ]; then
    url="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/platform/x86/hp/hp-wmi.c?h=$tag"
    mkdir -p "$here/cache"
    curl -fsSL --max-time 60 -o "$orig.part" "$url" || fail "could not download hp-wmi.c $tag"
    mv "$orig.part" "$orig"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$orig" "$tmp/hp-wmi.c"
printf 'obj-m := hp-wmi.o\n' > "$tmp/Makefile"
make -s -C "$build" M="$tmp" modules >/dev/null 2>&1 || fail "the unchanged $tag source does not build for $kver"
got=$(modinfo -F srcversion "$tmp/hp-wmi.ko")
[ "$got" = "$want" ] || fail "$kver's hp-wmi ($want) is not upstream $tag ($got)"

cp "$orig" "$tmp/patched.c"
patch -s --no-backup-if-mismatch -F0 "$tmp/patched.c" "$here/hp-wmi-charge.patch" \
    || fail "the patch does not apply to $tag"
cp "$tmp/patched.c" "$here/src/hp-wmi.c"
echo "hp-wmi-charge: patched upstream $tag for $kver"
