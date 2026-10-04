"""Turns state documents into the operations that make mod-host match them.

An operation is one of
    ('reverb', reverb_id, {symbol: value})   swap the reverb slot
    ('port', instance, symbol, value)        param_set
    ('patch', instance, uri, value)          patch_set (Pianoteq)
    ('cc', number, value)                    MIDI CC through the virtual device
Values in documents are differences from the patch; a missing key means the
patch baseline. The same code serves slot selection, morphing and boot restore.
"""
from . import reverbs

REVERB = reverbs.REVERB_INSTANCE


class Applier:
    def __init__(self, meta):
        self.port = {}        # (instance, symbol) -> {'baseline', 'type'}
        self.patch = {}       # (instance, uri) -> baseline
        self.cc = {}          # number -> {'baseline', 'type'}
        for plugin in meta['plugins']:
            for param in plugin['params']:
                if 'cc' in param:
                    self.cc[int(param['cc'])] = {'baseline': param['baseline'], 'type': param.get('type', 'float')}
                elif plugin['kind'] == 'port':
                    self.port[(plugin['instance'], param['symbol'])] = {
                        'baseline': param['baseline'], 'type': param.get('type', 'float')}
                else:
                    self.patch[(plugin['instance'], param['uri'])] = param['baseline']
        self.reverb = {r['id']: {p['symbol']: {'baseline': p['baseline'], 'type': p.get('type', 'float')}
                                 for p in r['params']} for r in meta.get('reverbs', [])}
        self.default_reverb = meta.get('default_reverb')
        self.default_preset = meta.get('default_preset')   # the Pianoteq preset kiwi.patch loads, if any

    def effective_reverb(self, document):
        reverb_id = document.get('reverb')
        return reverb_id if reverb_id in self.reverb else self.default_reverb

    def effective_preset(self, document):
        return document.get('preset') or self.default_preset

    @staticmethod
    def _diff(current, target, baselines, replace):
        """Keys whose value changes between the two documents, with the value to send."""
        changes = []
        keys = list(target) + ([k for k in current if k not in target] if replace else [])
        for key in keys:
            if key not in baselines:
                continue
            baseline = baselines[key]
            before = current.get(key, baseline)
            after = target.get(key, baseline)
            if abs(after - before) > 1e-9:
                changes.append((key, after))
        return changes

    def ops(self, current, target, replace):
        operations = []
        current_reverb = self.effective_reverb(current)
        target_reverb = self.effective_reverb(target)
        if target_reverb != current_reverb:
            # A re-added plugin starts from its own defaults, so send every baseline
            # explicitly, then the slot's values on top.
            settings = {s: p['baseline'] for s, p in self.reverb.get(target_reverb, {}).items()}
            settings.update(target['reverb_params'].get(target_reverb, {}))
            operations.append(('reverb', target_reverb, settings))
        # The Pianoteq preset goes before Pianoteq's parameters, which apply on top of it.
        # With no preset in the target and none in the patch, the piano is left as it is.
        target_preset = self.effective_preset(target)
        if target_preset is not None and target_preset != self.effective_preset(current):
            operations.append(('preset', target_preset))
        port_baselines = {f'{i}:{s}': p['baseline'] for (i, s), p in self.port.items()}
        for key, value in self._diff(current['params'], target['params'], port_baselines, replace):
            instance, symbol = key.split(':', 1)
            operations.append(('port', int(instance), symbol, value))
        if target_reverb == current_reverb and target_reverb in self.reverb:
            baselines = {s: p['baseline'] for s, p in self.reverb[target_reverb].items()}
            before = current['reverb_params'].get(target_reverb, {})
            after = target['reverb_params'].get(target_reverb, {})
            for symbol, value in self._diff(before, after, baselines, replace):
                operations.append(('port', REVERB, symbol, value))
        patch_baselines = {f'{i}:{u}': b for (i, u), b in self.patch.items()}
        for key, value in self._diff(current['patch_params'], target['patch_params'], patch_baselines, replace):
            instance, uri = key.split(':', 1)
            operations.append(('patch', int(instance), uri, value))
        cc_baselines = {str(n): p['baseline'] for n, p in self.cc.items()}
        for key, value in self._diff(current['cc'], target['cc'], cc_baselines, replace):
            operations.append(('cc', int(key), value))
        return operations

    def morph(self, a, b, t, reverb_id, preset=None):
        """A document between `a` (t = 0) and `b` (t = 1) for continuous parameters only."""
        t = min(max(float(t), 0.0), 1.0)
        # The reverb choice and the Pianoteq preset are never morphed: the target keeps
        # the current ones, so applying it against RAM never emits a switch.
        target = {'params': {}, 'patch_params': {}, 'preset': preset, 'favourites': [],
                  'reverb': reverb_id if reverb_id in self.reverb else None,
                  'reverb_params': {}, 'cc': {}, 'name': ''}

        def blend(section, key, baseline):
            start = a[section].get(key, baseline)
            end = b[section].get(key, baseline)
            return start + (end - start) * t

        for (instance, symbol), param in self.port.items():
            if param['type'] == 'float':
                key = f'{instance}:{symbol}'
                target['params'][key] = blend('params', key, param['baseline'])
        for number, param in self.cc.items():
            if param['type'] == 'float':
                target['cc'][str(number)] = blend('cc', str(number), param['baseline'])
        if (reverb_id in self.reverb and self.effective_reverb(a) == reverb_id
                and self.effective_reverb(b) == reverb_id):
            settings = {}
            for symbol, param in self.reverb[reverb_id].items():
                if param['type'] == 'float':
                    start = a['reverb_params'].get(reverb_id, {}).get(symbol, param['baseline'])
                    end = b['reverb_params'].get(reverb_id, {}).get(symbol, param['baseline'])
                    settings[symbol] = start + (end - start) * t
            if settings:
                target['reverb_params'][reverb_id] = settings
        return target
