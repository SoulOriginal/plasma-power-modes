#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Build the module for the running kernel without DKMS, as your user, for
# test-read.sh and test-write.sh.
set -e
cd "$(dirname "$0")"
./prepare-source.sh "$(uname -r)"
make -s -C src
echo "built src/hp-wmi.ko"
