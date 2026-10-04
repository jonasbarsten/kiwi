"""User MIDI CC mappings: which control changes drive which controls.

One file, global (it describes the controller, not a sound). A CC may drive any
number of targets; a target has at most one CC. Written only when a mapping
changes. Without a file the map is the fixed one earlier versions had in
kiwi.patch: the mix and carrier knobs on CC 20-26 and morph on CC 27.

Targets:
    {'kind': 'port',   'instance': 5, 'symbol': 'piano_vol'}   mapped inside mod-host
    {'kind': 'patch',  'instance': 0, 'uri': '...:Volume'}     forwarded by the service
    {'kind': 'cc',     'instance': 1, 'number': 102}           forwarded by the service
    {'kind': 'action', 'name': 'panic'}                        handled by the service
"""
import json
import os
import threading

from . import reverbs

ACTIONS = ('panic', 'morph', 'slot_next', 'slot_prev', 'slot_save')
MORPH_CC = 27

_KNOBS = [(5, 'piano_vol', 20), (5, 'piano_send', 21), (5, 'sampler_vol', 22), (5, 'sampler_send', 23),
          (5, 'vocoder_vol', 24), (5, 'vocoder_send', 25), (3, 'blend', 26)]


def defaults():
    mappings = [{'channel': 0, 'cc': cc, 'target': {'kind': 'port', 'instance': instance, 'symbol': symbol}}
                for instance, symbol, cc in _KNOBS]
    mappings.append({'channel': 0, 'cc': MORPH_CC, 'target': {'kind': 'action', 'name': 'morph'}})
    return mappings


def target_key(target):
    """What makes two targets the same control."""
    kind = target['kind']
    if kind == 'port':
        return (kind, target['instance'], target['symbol'])
    if kind == 'patch':
        return (kind, target['instance'], target['uri'])
    if kind == 'cc':
        return (kind, target['instance'], target['number'])
    return (kind, target['name'])


def known_targets(meta):
    """{target_key: target} for everything the metadata lets us map: every port
    (including every reverb's, since the slot can hold any of them), every patch
    parameter, every sampler CC control and the actions."""
    known = {}
    for plugin in meta['plugins']:
        for param in plugin['params']:
            if 'cc' in param:
                target = {'kind': 'cc', 'instance': plugin['instance'], 'number': int(param['cc'])}
            elif plugin['kind'] == 'port':
                target = {'kind': 'port', 'instance': plugin['instance'], 'symbol': param['symbol']}
            else:
                target = {'kind': 'patch', 'instance': plugin['instance'], 'uri': param['uri']}
            known[target_key(target)] = target
    for reverb in meta.get('reverbs', []):
        for param in reverb['params']:
            target = {'kind': 'port', 'instance': reverbs.REVERB_INSTANCE, 'symbol': param['symbol']}
            known[target_key(target)] = target
    for name in ACTIONS:
        known[('action', name)] = {'kind': 'action', 'name': name}
    return known


def normalise(target, known):
    """The known target a request means, or None."""
    if not isinstance(target, dict):
        return None
    try:
        return known.get(target_key(target))
    except (KeyError, TypeError):
        return None


class CcMap:
    def __init__(self, path, known):
        self.path = path
        self.known = known
        self.mappings = []
        self._lock = threading.Lock()   # read by the MIDI thread, changed by HTTP threads

    def load(self):
        try:
            with open(self.path) as f:
                loaded = json.load(f).get('mappings')
        except (OSError, ValueError, AttributeError):
            loaded = None
        if loaded is None:
            loaded = defaults()
        self.mappings = []
        for entry in loaded:
            try:
                channel, cc = int(entry['channel']), int(entry['cc'])
                target = normalise(entry['target'], self.known)
            except (KeyError, TypeError, ValueError):
                continue
            if target is not None and 0 <= channel <= 15 and 0 <= cc <= 127:
                self.mappings.append({'channel': channel, 'cc': cc, 'target': target})
        return self.mappings

    def targets(self, channel, cc):
        with self._lock:
            return [m['target'] for m in self.mappings if m['channel'] == channel and m['cc'] == cc]

    def mapping_for(self, target):
        with self._lock:
            return self._find(target)

    def _find(self, target):
        key = target_key(target)
        return next((m for m in self.mappings if target_key(m['target']) == key), None)

    def set(self, channel, cc, target):
        """Binds `target` to the CC, replacing its previous binding. Returns that
        previous mapping (or None). The file is written before the binding counts."""
        with self._lock:
            previous = self._find(target)
            mappings = [m for m in self.mappings if m is not previous]
            mappings.append({'channel': channel, 'cc': cc, 'target': target})
            self._write(mappings)
            self.mappings = mappings
        return previous

    def remove(self, target):
        with self._lock:
            previous = self._find(target)
            if previous is not None:
                mappings = [m for m in self.mappings if m is not previous]
                self._write(mappings)
                self.mappings = mappings
        return previous

    def ports(self):
        """The mappings mod-host applies itself."""
        with self._lock:
            return [m for m in self.mappings if m['target']['kind'] == 'port']

    def forwarding(self):
        """True if the service itself has to turn CCs into parameter changes."""
        with self._lock:
            return any(m['target']['kind'] in ('patch', 'cc') for m in self.mappings)

    def _write(self, mappings):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'mappings': mappings}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
