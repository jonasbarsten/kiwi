"""Autosaved UI state: only values that differ from the patch, written atomically.

Writes happen at most once per `delay` seconds after the last change.
"""
import json
import os
import time


def _empty():
    return {'params': {}, 'patch_params': {}, 'preset': None, 'favourites': []}


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
                                      if isinstance(v, (int, float))}
            if isinstance(loaded.get('preset'), str):
                self.data['preset'] = loaded['preset']
            if isinstance(loaded.get('favourites'), list):
                self.data['favourites'] = [u for u in loaded['favourites'] if isinstance(u, str)]
        return self.data

    def _store(self, section, key, value, baseline):
        if baseline is not None and abs(value - baseline) < 1e-6:
            self.data[section].pop(key, None)
        else:
            self.data[section][key] = value
        self._changed_at = self.clock()

    def set_param(self, instance, symbol, value, baseline):
        self._store('params', f'{instance}:{symbol}', value, baseline)

    def set_patch_param(self, instance, uri, value, baseline):
        self._store('patch_params', f'{instance}:{uri}', value, baseline)

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
