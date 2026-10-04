#!/usr/bin/env bash
# Copies this repo to the Pi and runs install.sh there. Reads KIWI_HOST from kiwi.env
# (default kiwi.local); kiwi.env itself is copied too, so the installer sees the
# Wi-Fi settings. Usage: ./deploy.sh [--no-install]
set -euo pipefail

cd "$(dirname "$0")"
KIWI_HOST=kiwi.local
if [ -f kiwi.env ]; then
    # shellcheck source=/dev/null
    . ./kiwi.env
fi
target="patch@${KIWI_HOST}"

rsync -a --delete \
    --exclude .git --exclude .superpowers --exclude backup --exclude legacy \
    --exclude pure-data --exclude Cookbook --exclude SwiftCrossCompilers \
    --exclude plugins/kiwi/build --exclude web/params.json --exclude web/presets.json \
    --exclude __pycache__ --exclude .DS_Store \
    ./ "$target:kiwi/"

if [ "${1:-}" != "--no-install" ]; then
    ssh "$target" 'cd ~/kiwi && ./install.sh'
fi
