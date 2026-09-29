#!/usr/bin/env bash
# Builds and installs the sfizz LV2 plugin (pinned version) into /usr/local/lib/lv2.
set -euo pipefail

VERSION=1.2.3
if [ -f /usr/local/lib/lv2/sfizz.lv2/manifest.ttl ] && [ "$(cat /usr/local/lib/lv2/sfizz.lv2/.kiwi-version 2>/dev/null)" = "$VERSION" ]; then
    echo "sfizz $VERSION already installed"
    exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
# The LV2 plugin lives in sfizz-ui (the sfizz tarball is the library only);
# its tarball bundles the library.
curl -sSL -o sfizz.tar.gz "https://github.com/sfztools/sfizz-ui/releases/download/$VERSION/sfizz-$VERSION.tar.gz"
tar xzf sfizz.tar.gz
cd "sfizz-$VERSION"
cmake -B build -DCMAKE_BUILD_TYPE=Release \
    -DPLUGIN_LV2=ON -DPLUGIN_LV2_UI=OFF -DPLUGIN_VST3=OFF -DPLUGIN_AU=OFF -DPLUGIN_PUREDATA=OFF \
    -DSFIZZ_JACK=OFF -DSFIZZ_RENDER=OFF -DSFIZZ_SHARED=OFF -DSFIZZ_TESTS=OFF -DSFIZZ_DEMOS=OFF \
    -DSFIZZ_DEVTOOLS=OFF -DSFIZZ_BENCHMARKS=OFF
cmake --build build -j4
sudo cmake --install build
echo "$VERSION" | sudo tee /usr/local/lib/lv2/sfizz.lv2/.kiwi-version >/dev/null
