#!/usr/bin/env python3
"""Prints web/params.json: the parameters the web UI may show and set.

Runs on the Pi (install.sh): reads instances from host/kiwi.patch, port
parameters from `lv2info`, and Pianoteq's parameters from its dsp.ttl.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from kiwi_web.patchfile import parse_patch  # noqa: E402

LV2_PATH = '/home/patch/.lv2:/usr/local/lib/lv2:/usr/lib/lv2:/var/modep/lv2'
PIANOTEQ_TTL = '/home/patch/.vst/Pianoteq 8.lv2/dsp.ttl'
PIANOTEQ_PREFIX = 'https://www.modartt.com/lv2/Pianoteq8:'

# instance, title, only these symbols (None = all), never these symbols.
# Routing-critical parameters are left out so the UI cannot change them.
PORT_PLUGINS = [
    (5, 'Mix', None, set()),
    (3, 'Vocoder carrier', None, set()),
    (6, 'Reverb', None, {'dry_level'}),
    (7, 'Limiter', None, {'enable'}),
    (4, 'Vocoder', {'quality'}, set()),
    (1, 'Sampler', {'volume', 'tuning_frequency', 'stretched_tuning', 'sustain_cancels_release',
                    'sample_quality', 'oscillator_quality'}, set()),
    (2, 'Synth', None, set()),
]
PIANOTEQ_INSTANCE = 0
PIANOTEQ_EXCLUDED = {'Reverb Switch'}
PIANOTEQ_CURATED = ['Volume', 'Dynamics', 'Condition', 'Post Effect Gain', 'Unison Width',
                    'Hammer Hard. Piano', 'Hammer Hard. Mezzo', 'Hammer Hard. Forte',
                    'Direct Sound Duration', 'Sympathetic Resonance', 'Stereo Width', 'Lid Position',
                    'Damping Duration', 'Diapason']

_FIELD = re.compile(r'^\s*(Type|Symbol|Name|Group|Designation|Minimum|Maximum|Default|Properties|'
                    r'Scale Points):\s*(.*)$')
_OPTION = re.compile(r'^(-?[\d.]+) = "(.*)"$')


def parse_lv2info(text):
    params = []
    for block in re.split(r'\n\s*Port \d+:\n', text)[1:]:
        fields, types, props, options = {}, [], [], []
        key = None
        for line in block.splitlines():
            match = _FIELD.match(line)
            if match:
                key, value = match.group(1), match.group(2).strip()
            else:
                value = line.strip()
                if not value:
                    continue
            if key == 'Type':
                types.append(value)
            elif key == 'Properties':
                props.append(value)
            elif key == 'Scale Points':
                option = _OPTION.match(value)
                if option:
                    options.append([float(option.group(1)), option.group(2)])
            elif match and key:
                fields[key] = value
        is_control = any(t.endswith('#ControlPort') for t in types)
        is_input = any(t.endswith('#InputPort') for t in types)
        if not (is_control and is_input) or 'Symbol' not in fields or 'Minimum' not in fields:
            continue
        kind = 'float'
        if any(p.endswith('#toggled') for p in props):
            kind = 'toggle'
        elif options or any(p.endswith('#enumeration') for p in props):
            kind = 'enum'
        elif any(p.endswith('#integer') for p in props):
            kind = 'int'
        param = {'symbol': fields['Symbol'], 'name': fields.get('Name', fields['Symbol']),
                 'min': float(fields['Minimum']), 'max': float(fields['Maximum']),
                 'default': float(fields.get('Default', fields['Minimum'])), 'type': kind}
        if options:
            param['options'] = sorted(options)
        if 'Group' in fields:
            param['group'] = fields['Group'].rsplit('#', 1)[-1].replace('group_', '').replace('_', ' ')
        params.append(param)
    return params


def parse_pianoteq_ttl(text):
    groups = dict(re.findall(r'lv2:symbol "(paramgroup_\w+)"\s*;\s*lv2:name "([^"]*)"', text))
    params = []
    for name, body in re.findall(r'^plug:(\S+)\n\s+a lv2:Parameter ;(.*?)\s\.\s*$', text, re.M | re.S):
        label = re.search(r'rdfs:label "([^"]*)"', body)
        group = re.search(r'pg:group plug:(\w+)', body)

        def number(key, fallback):
            found = re.search(rf'lv2:{key} (-?[\d.eE+-]+)', body)
            return float(found.group(1)) if found else fallback

        params.append({'uri': PIANOTEQ_PREFIX + name,
                       'name': label.group(1) if label else name,
                       'group': groups.get(group.group(1), 'Other') if group else 'Other',
                       'min': number('minimum', 0.0), 'max': number('maximum', 1.0),
                       'default': number('default', 0.0)})
    return params


def build(patch, lv2info, pianoteq_ttl):
    plugins = []
    for instance, title, only, never in PORT_PLUGINS:
        params = [p for p in parse_lv2info(lv2info(patch.instances[instance]))
                  if (only is None or p['symbol'] in only) and p['symbol'] not in never]
        plugins.append({'instance': instance, 'title': title, 'kind': 'port', 'params': params})
    pianoteq = [dict(p, curated=p['name'] in PIANOTEQ_CURATED)
                for p in parse_pianoteq_ttl(pianoteq_ttl) if p['name'] not in PIANOTEQ_EXCLUDED]
    missing = set(PIANOTEQ_CURATED) - {p['name'] for p in pianoteq}
    for name in sorted(missing):
        print(f'gen_params: curated Pianoteq parameter not found: {name}', file=sys.stderr)
    plugins.append({'instance': PIANOTEQ_INSTANCE, 'title': 'Pianoteq', 'kind': 'patch', 'params': pianoteq})
    return {'plugins': plugins}


def run_lv2info(uri):
    env = dict(os.environ, LV2_PATH=LV2_PATH)
    return subprocess.run(['lv2info', uri], env=env, capture_output=True, text=True, check=True).stdout


def main():
    repo = os.path.dirname(os.path.dirname(HERE))
    with open(os.path.join(repo, 'host', 'kiwi.patch')) as f:
        patch = parse_patch(f.read())
    with open(PIANOTEQ_TTL) as f:
        ttl = f.read()
    json.dump(build(patch, run_lv2info, ttl), sys.stdout, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
