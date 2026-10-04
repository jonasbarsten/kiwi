#!/usr/bin/env python3
"""Rewrites the Pianoteq preset export under <root> into <out>, in the form the
plugin loads (see kiwi_web.pianoteq_state), and prints web/presets.json."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from kiwi_web.presets import index_presets  # noqa: E402

DEFAULT_ROOT = '/home/patch/kiwi-data/pianoteq-presets'


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    out = sys.argv[2] if len(sys.argv) > 2 else root.rstrip('/') + '-lv2'
    presets = index_presets(root, out)
    if not presets:
        print(f'gen_presets: no presets found under {root}', file=sys.stderr)
    json.dump({'presets': presets}, sys.stdout)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
