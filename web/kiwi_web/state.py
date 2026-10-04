"""Autosaved UI state: only values that differ from the patch, written atomically.

Writes happen at most once per `delay` seconds after the last change.
"""
import json
import math
import os
import time


def _empty():
    return {'params': {}, 'patch_params': {}, 'preset': None, 'favourites': [],
            'reverb': None, 'reverb_params': {}, 'cc': {}}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_key(key):
    instance, sep, name = str(key).partition(':')
    return bool(sep) and instance.isdigit() and bool(name)


class StateStore:
    def __init__(self, path, delay=5.0, clock=time.monotonic):
        self.path = path
        self.delay = delay
        self.clock = clock
        self.data = _empty()
        self._changed_at = None

    @staticmethod
    def split_key(key):
        instance, _, name = key.partition(':')
        return int(instance), name

    def load(self):
        try:
            with open(self.path) as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = {}
        self.data = _empty()
        if isinstance(loaded, dict):
            for key in ('params', 'patch_params'):
                if isinstance(loaded.get(key), dict):
                    self.data[key] = {k: float(v) for k, v in loaded[key].items()
                                      if _is_key(k) and _is_number(v)}
            if isinstance(loaded.get('preset'), str):
                self.data['preset'] = loaded['preset']
            if isinstance(loaded.get('favourites'), list):
                self.data['favourites'] = [u for u in loaded['favourites'] if isinstance(u, str)]
            if isinstance(loaded.get('reverb'), str):
                self.data['reverb'] = loaded['reverb']
            if isinstance(loaded.get('cc'), dict):
                self.data['cc'] = {k: float(v) for k, v in loaded['cc'].items()
                                   if str(k).isdigit() and _is_number(v)}
            if isinstance(loaded.get('reverb_params'), dict):
                for reverb_id, settings in loaded['reverb_params'].items():
                    if isinstance(reverb_id, str) and isinstance(settings, dict):
                        kept = {s: float(v) for s, v in settings.items() if isinstance(s, str) and _is_number(v)}
                        if kept:
                            self.data['reverb_params'][reverb_id] = kept
        return self.data

    def _touch(self):
        self._changed_at = self.clock()

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

    def clear(self):
        self.data = _empty()
        self._changed_at = None
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def due(self):
        return self._changed_at is not None and self.clock() - self._changed_at >= self.delay

    def flush(self):
        if self._changed_at is None:
            return False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._changed_at = None
        return True
