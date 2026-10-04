"""The instrument's state: a document edited in RAM, kept in one of eight preset slots.

Nothing is written while playing. `save()` writes the current slot's file and
`select()` writes the one-line `current` file; both atomically. Boot loads the
slot named in `current`.

Layout under the state directory:
    current            "3\\n"
    presets/1.json … 8.json
"""
import json
import math
import os
import shutil

SLOTS = 8
NAME_MAX = 40


def _empty(name=''):
    return {'params': {}, 'patch_params': {}, 'preset': None, 'favourites': [],
            'reverb': None, 'reverb_params': {}, 'cc': {}, 'name': name}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_key(key):
    instance, sep, name = str(key).partition(':')
    return bool(sep) and instance.isdigit() and bool(name)


def _clean(loaded, default_name):
    """A valid document from whatever was read from disk."""
    data = _empty(default_name)
    if not isinstance(loaded, dict):
        return data
    for key in ('params', 'patch_params'):
        if isinstance(loaded.get(key), dict):
            data[key] = {k: float(v) for k, v in loaded[key].items() if _is_key(k) and _is_number(v)}
    if isinstance(loaded.get('preset'), str):
        data['preset'] = loaded['preset']
    if isinstance(loaded.get('favourites'), list):
        data['favourites'] = [u for u in loaded['favourites'] if isinstance(u, str)]
    if isinstance(loaded.get('reverb'), str):
        data['reverb'] = loaded['reverb']
    if isinstance(loaded.get('cc'), dict):
        data['cc'] = {k: float(v) for k, v in loaded['cc'].items() if str(k).isdigit() and _is_number(v)}
    if isinstance(loaded.get('reverb_params'), dict):
        for reverb_id, settings in loaded['reverb_params'].items():
            if isinstance(reverb_id, str) and isinstance(settings, dict):
                kept = {s: float(v) for s, v in settings.items() if isinstance(s, str) and _is_number(v)}
                if kept:
                    data['reverb_params'][reverb_id] = kept
    if isinstance(loaded.get('name'), str) and loaded['name'].strip():
        data['name'] = loaded['name'].strip()[:NAME_MAX]
    return data


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class StateStore:
    SLOTS = SLOTS

    def __init__(self, directory, slots=SLOTS):
        self.directory = directory
        self.slots = slots
        self.slot = 1
        self.data = _empty('Preset 1')
        self.dirty = False

    @staticmethod
    def split_key(key):
        instance, _, name = key.partition(':')
        return int(instance), name

    @staticmethod
    def empty():
        return _empty()

    def _slot_path(self, slot):
        return os.path.join(self.directory, 'presets', f'{slot}.json')

    def _check(self, slot):
        if not isinstance(slot, int) or not 1 <= slot <= self.slots:
            raise ValueError(f'slot must be 1..{self.slots}')

    def read_slot(self, slot):
        self._check(slot)
        try:
            with open(self._slot_path(slot)) as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = None
        return _clean(loaded, f'Preset {slot}')

    def names(self):
        return [self.data['name'] if n == self.slot else self.read_slot(n)['name']
                for n in range(1, self.slots + 1)]

    def _migrate(self):
        """The pre-preset autosave file becomes slot 1, once."""
        legacy = os.path.join(self.directory, 'state.json')
        if os.path.exists(legacy) and not os.path.exists(self._slot_path(1)):
            try:
                with open(legacy) as f:
                    document = _clean(json.load(f), 'Preset 1')
            except (OSError, ValueError):
                document = None
            if document is not None:
                _write_atomic(self._slot_path(1), json.dumps(document, indent=1, sort_keys=True))
            shutil.move(legacy, legacy + '.migrated')

    def load(self):
        """Reads `current` and that slot into RAM. Returns the document."""
        self._migrate()
        slot = 1
        try:
            with open(os.path.join(self.directory, 'current')) as f:
                slot = int(f.read().strip())
        except (OSError, ValueError):
            pass
        if not 1 <= slot <= self.slots:
            slot = 1
        self.slot = slot
        self.data = self.read_slot(slot)
        self.dirty = False
        return self.data

    def select(self, slot):
        """Makes `slot` current (one small atomic write) and loads it, discarding RAM edits."""
        self._check(slot)
        _write_atomic(os.path.join(self.directory, 'current'), f'{slot}\n')
        self.slot = slot
        self.data = self.read_slot(slot)
        self.dirty = False

    def save(self):
        """Writes RAM into the current slot's file."""
        _write_atomic(self._slot_path(self.slot), json.dumps(self.data, indent=1, sort_keys=True))
        self.dirty = False

    def _touch(self):
        self.dirty = True

    def _store(self, section, key, value, baseline):
        if baseline is not None and abs(value - baseline) < 1e-6:
            section.pop(key, None)
        else:
            section[key] = value
        self._touch()

    def set_param(self, instance, symbol, value, baseline):
        self._store(self.data['params'], f'{instance}:{symbol}', value, baseline)

    def set_patch_param(self, instance, uri, value, baseline):
        self._store(self.data['patch_params'], f'{instance}:{uri}', value, baseline)

    def set_cc(self, number, value, baseline):
        self._store(self.data['cc'], str(number), value, baseline)

    def set_reverb(self, reverb_id):
        self.data['reverb'] = reverb_id
        self._touch()

    def set_reverb_param(self, reverb_id, symbol, value, baseline):
        settings = self.data['reverb_params'].setdefault(reverb_id, {})
        self._store(settings, symbol, value, baseline)
        if not settings:
            del self.data['reverb_params'][reverb_id]

    def set_name(self, name):
        name = str(name).strip()[:NAME_MAX]
        self.data['name'] = name or f'Preset {self.slot}'
        self._touch()

    def clear(self):
        """RAM back to the patch defaults; the slot's file is untouched."""
        self.data = _empty(self.data['name'])
        self._touch()
