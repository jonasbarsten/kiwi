#!/usr/bin/env python3
"""Prints web/presets.json: the Pianoteq presets exported under the given root."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from kiwi_web.presets import index_presets  # noqa: E402

DEFAULT_ROOT = '/home/patch/kiwi-data/pianoteq-presets'


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    # Space-free symlinks to the bundles, next to the export (see kiwi_web.presets).
    presets = index_presets(root, link_root=root.rstrip('/') + '-bundles')
    if not presets:
        print(f'gen_presets: no presets found under {root}', file=sys.stderr)
    json.dump({'presets': presets}, sys.stdout)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
