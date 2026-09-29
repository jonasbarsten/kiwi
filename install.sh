#!/usr/bin/env bash
# Kiwi installer. Run on the Pi as user patch from ~/kiwi. Safe to re-run.
set -euo pipefail

KIWI_DIR=$(cd "$(dirname "$0")" && pwd)
# The v24.12 release asset rtpmidid_24.12.2_arm64.deb reports this package version.
RTPMIDID_DEB_VERSION="24.12~1~g66d57"

section() { printf '\n== %s\n' "$1"; }

section "Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq dragonfly-reverb x42-plugins lilv-utils cmake build-essential lv2-dev

section "sfizz"
"$KIWI_DIR/system/build-sfizz.sh"

section "rtpmidid"
if [ "$(dpkg-query -W -f='${Version}' rtpmidid 2>/dev/null || true)" != "$RTPMIDID_DEB_VERSION" ]; then
    deb=/tmp/rtpmidid_24.12.2_arm64.deb
    curl -sSL -o "$deb" "https://github.com/davidmoreno/rtpmidid/releases/download/v24.12/rtpmidid_24.12.2_arm64.deb"
    sudo apt-get install -y -qq "$deb"
fi
sudo systemctl enable --now rtpmidid

section "MIDI routing"
# Replaces the /etc/amidiminder.rules symlink (to /etc/default/amidiminder.rules)
# with the kiwi rules; the Patchbox default file is left untouched.
sudo install -m 644 "$KIWI_DIR/system/amidiminder.rules" /etc/amidiminder.rules
sudo systemctl restart amidiminder

section "Kiwi plugins"
make -C "$KIWI_DIR/plugins/kiwi" clean test install

echo
echo "kiwi install done"
