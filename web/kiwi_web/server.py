"""kiwi-web: serves the control page and bridges it to mod-host.

State lives in RAM in one of eight preset slots (see state.py); the only disk
writes are an explicit save and the one-line `current` file on slot selection.
The mod-host connection is held only while a page is open or a change is
pending, and only after kiwi-patch and kiwi-restore have finished, so it never
competes with them for mod-host's single client slot. The MIDI monitor runs
permanently so CC mappings handled here (Pianoteq, the sampler envelope, the
actions) and the button work without a page.
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ccmap, presets, reverbs, security
from .applier import Applier
from .led import Led
from .midi import MidiMonitor, describe
from .modhost import HostClient, HostError
from .patchfile import parse_patch
from .rawmidi import RawMidi, find_device, to_7bit
from .state import StateStore

MAX_STREAMS = 4
STREAM_INTERVAL = 0.1
KEEPALIVE = 15.0
IDLE_DISCONNECT = 10.0
MAX_BODY = 16384
PANIC_CCS = (120, 123)    # all sound off, all notes off
SAVE_FLASHES = 8          # a rapid burst on save ...
SAVE_FLASH_INTERVAL = 0.1
SLOT_FLASH_INTERVAL = 0.3  # ... and the slot number, countable but brisk, on a selection


class Model:
    """Flat key -> value store with versions, so event streams send only changes."""

    def __init__(self):
        self.changed = threading.Condition()
        self.version = 0
        self._items = {}

    def update(self, key, value):
        with self.changed:
            current = self._items.get(key)
            if current is not None and current[1] == value:
                return
            self.version += 1
            self._items[key] = (self.version, value)
            self.changed.notify_all()

    def get(self, key, default=None):
        with self.changed:
            item = self._items.get(key)
            return default if item is None else item[1]

    def since(self, version):
        with self.changed:
            delta = {key: value for key, (ver, value) in self._items.items() if ver > version}
            return delta, self.version

    def wait(self, version, timeout):
        with self.changed:
            self.changed.wait_for(lambda: self.version > version, timeout=timeout)


def read_temperature(path='/sys/class/thermal/thermal_zone0/temp'):
    try:
        with open(path) as f:
            return round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        return None


def systemd_units_ready(run=subprocess.run):
    """Falsy until kiwi-patch and kiwi-restore have finished since kiwi-host last
    started; then a token that changes whenever kiwi-host starts again (the link
    reloads its slot when it sees a new token, because kiwi-restore re-applied it).

    A failed loader counts as finished (it no longer needs mod-host); a state left
    over from the previous host start does not.
    """
    result = run(['systemctl', 'show', '-p', 'ActiveState', '-p', 'ActiveEnterTimestampMonotonic',
                  '-p', 'StateChangeTimestampMonotonic', 'kiwi-host', 'kiwi-patch', 'kiwi-restore'],
                 capture_output=True, text=True)
    blocks = [dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
              for block in result.stdout.strip().split('\n\n')]
    if len(blocks) != 3:
        return False
    host, *loaders = blocks
    if host.get('ActiveState') != 'active':
        return False
    started = int(host.get('ActiveEnterTimestampMonotonic') or 0)
    finished = all(unit.get('ActiveState') in ('active', 'failed')
                   and int(unit.get('StateChangeTimestampMonotonic') or 0) >= started
                   for unit in loaders)
    return f'host-{started}' if finished else False


def param_name(plugin, param):
    return param['symbol'] if plugin['kind'] == 'port' else param['uri']


def cc_controls(meta):
    """{cc number: param} for parameters the page sends as MIDI CCs (the sampler envelope)."""
    return {q['cc']: q for p in meta['plugins'] for q in p['params'] if 'cc' in q}


def send_cc(midi, param, value):
    """Sends `value` (in the parameter's range) as a 7-bit CC on channel 1."""
    return midi.cc(0, param['cc'], to_7bit(value, param['min'], param['max']))


def port_ranges(meta):
    """{(instance, symbol): (min, max)} for every port, every reverb's included."""
    ranges = {(p['instance'], q['symbol']): (q['min'], q['max'])
              for p in meta['plugins'] if p['kind'] == 'port' for q in p['params'] if 'cc' not in q}
    for reverb in meta.get('reverbs', []):
        for q in reverb['params']:
            ranges[(reverbs.REVERB_INSTANCE, q['symbol'])] = (q['min'], q['max'])
    return ranges


def map_port(client, mapping, ranges):
    """Binds a port to a CC inside mod-host (replacing any binding the port had).
    Returns False if the port is unknown or mod-host refused."""
    target = mapping['target']
    bounds = ranges.get((target['instance'], target['symbol']))
    if bounds is None:
        return False
    client.command(f"midi_unmap {target['instance']} {target['symbol']}")
    code, _ = client.command(f"midi_map {target['instance']} {target['symbol']} {mapping['channel']} "
                             f"{mapping['cc']} {bounds[0]:.6f} {bounds[1]:.6f}")
    return code >= 0


def unmap_port(client, target):
    code, _ = client.command(f"midi_unmap {target['instance']} {target['symbol']}")
    return code >= 0


def panic(midi):
    """All sound off and all notes off on every channel, through the virtual device."""
    ok = True
    for channel in range(16):
        for number in PANIC_CCS:
            ok = midi.cc(channel, number, 0) and ok
    return ok


def switch_reverb(client, reverb_id, settings):
    """Puts a reverb into the slot through `client`. Returns the number of failed commands."""
    entry = reverbs.find(reverb_id)
    if entry is None:
        return 1
    failures = 0
    for command in reverbs.switch_commands(entry, settings):
        code, _ = client.command(command)
        failures += code < 0
    return failures


def load_preset(client, uri, bundles):
    """Loads a Pianoteq preset (bundle_add, preset_load, reverb off). False if unknown or refused."""
    bundle = bundles.get(uri)
    if bundle is None:
        return False
    ok = True
    for command in presets.load_commands(uri, bundle):
        code, _ = client.command(command)
        ok = ok and code >= 0
    return ok


def execute_ops(client, midi, cc_params, ops, on_applied=None, bundles=None):
    """Runs Applier operations against mod-host / the MIDI device.

    `on_applied(op)` is called for every operation that succeeded. Returns the
    number of failures. Raises HostError if mod-host goes away.
    """
    failures = 0
    for op in ops:
        kind = op[0]
        if kind == 'reverb':
            ok = switch_reverb(client, op[1], op[2]) == 0
        elif kind == 'preset':
            ok = load_preset(client, op[1], bundles or {})
        elif kind == 'port':
            ok = client.param_set(op[1], op[2], op[3]) == 0
        elif kind == 'patch':
            ok = client.patch_set(op[1], op[2], op[3]) == 0
        else:
            param = cc_params.get(op[1])
            ok = param is not None and midi is not None and send_cc(midi, param, op[2])
        if ok and on_applied is not None:
            on_applied(op)
        failures += not ok
    return failures


class HostLink:
    """Owns the single mod-host connection, only while a page or a change needs it."""

    def __init__(self, model, client, meta, store, units_ready, clock=time.monotonic, midi=None, led=None,
                 bundles=None, favourites=None, mappings=None):
        self.model = model
        self.client = client
        self.store = store
        self.units_ready = units_ready
        self.clock = clock
        self.midi = midi
        self.led = led or Led()
        self.bundles = bundles or {}             # preset URI -> bundle URI
        self.favourites = favourites             # presets.Favourites or None
        self.mappings = mappings                 # ccmap.CcMap or None
        self.applier = Applier(meta)
        self.cc_params = cc_controls(meta)
        self.port_params = [(p['instance'], q['symbol'])
                            for p in meta['plugins'] if p['kind'] == 'port' for q in p['params'] if 'cc' not in q]
        self.ranges = port_ranges(meta)
        self.current_reverb = self.applier.effective_reverb(store.data)
        self._lock = threading.Lock()
        self._sets = {}
        self._reads = set()
        self._maps = []                          # ('map', mapping) / ('unmap', target), in order
        self._reverb = None
        self._select = None
        self._step = 0
        self._save = False
        self._morph = None
        self._morph_slots = None                 # (slot, document, next document) cached for morphing
        self._name = None
        self._preset = None
        self._host_token = None
        self._viewers = 0
        self._last_need = None
        self._wake = threading.Event()
        self._stop = False
        self.model.update('morph:value', 0.0)
        self._publish_state()

    # ----- requests from the HTTP side ----------------------------------------

    def add_viewer(self):
        with self._lock:
            self._viewers += 1
        self._wake.set()

    def remove_viewer(self):
        with self._lock:
            self._viewers -= 1
            self._last_need = self.clock()
        self._wake.set()

    def _request(self, attribute, value):
        with self._lock:
            setattr(self, attribute, value)
            self._last_need = self.clock()
        self._wake.set()

    def set_value(self, kind, instance, name, value):
        with self._lock:
            self._sets[(kind, instance, name)] = value
            self._last_need = self.clock()
        self._wake.set()

    def request_read(self, instance, symbol):
        # A knob moved inside mod-host: RAM must learn the value even with no page open.
        with self._lock:
            self._reads.add((instance, symbol))
            self._last_need = self.clock()
        self._wake.set()

    def request_step(self, delta):
        """Next/previous relative to the slot that is current when it is applied,
        so two quick clicks move two slots even if the first is still in flight."""
        with self._lock:
            self._step += delta
            self._last_need = self.clock()
        self._wake.set()

    def request_reverb(self, reverb_id):
        self._request('_reverb', reverb_id)

    def request_map(self, channel, cc, target):
        """Binds `target` to a CC: the map file changes now, mod-host (for a port)
        when the link gets to it."""
        with self._lock:
            self.mappings.set(channel, cc, target)
            if target['kind'] == 'port':
                self._maps.append(('map', {'channel': channel, 'cc': cc, 'target': target}))
            self._last_need = self.clock()
        self.model.update('map:mappings', list(self.mappings.mappings))
        self._wake.set()

    def request_unmap(self, target):
        with self._lock:
            removed = self.mappings.remove(target)
            if removed is not None and target['kind'] == 'port':
                self._maps.append(('unmap', target))
            self._last_need = self.clock()
        self.model.update('map:mappings', list(self.mappings.mappings))
        self._wake.set()

    def request_select(self, slot):
        self._request('_select', slot)

    def request_save(self):
        self._request('_save', True)

    def request_morph(self, t):
        self._request('_morph', min(max(float(t), 0.0), 1.0))

    def request_preset(self, uri):
        self._request('_preset', uri)

    def set_favourite(self, uri, on):
        """Stars/unstars a preset (the one deliberate write outside save/select)."""
        if self.favourites is None:
            return False
        self.favourites.toggle(uri, on)
        self.model.update('preset:favourites', list(self.favourites.uris))
        return True

    def request_name(self, name, slot=None):
        """Renames the current slot; refused (False) if `slot` names another one."""
        with self._lock:
            if slot is not None and slot != self.store.slot:
                return False
            self._name = name
        self._wake.set()
        return True

    def stop(self):
        self._stop = True
        self._wake.set()

    def _needed(self):
        with self._lock:
            if (self._viewers > 0 or self._sets or self._reads or self._maps or self._reverb is not None
                    or self._select is not None or self._step or self._save or self._morph is not None
                    or self._preset is not None):
                return True
            # A CC the service forwards itself must not wait for a reconnect.
            if self.mappings is not None and self.mappings.forwarding():
                return True
            return self._last_need is not None and self.clock() - self._last_need < IDLE_DISCONNECT

    # ----- the loop ----------------------------------------------------------------

    def run(self):
        next_status = 0.0
        next_resync = 0.0
        while not self._stop:
            self._wake.wait(timeout=0.2)
            self._wake.clear()
            # Renaming and saving are RAM/disk only: they must work while mod-host is away.
            self._apply_name()
            self._apply_save()
            if not self._needed():
                if self.client.connected:
                    self.client.close()
                    self.model.update('status:host', 'idle')
                continue
            if not self.client.connected and not self._connect():
                continue
            try:
                self._apply_reads()      # knob moves first, so a save or select sees them
                self._apply_select()
                self._apply_reverb()
                self._apply_preset()
                self._apply_morph()
                self._apply_maps()
                self._apply_sets()
                self._apply_reads()
                now = self.clock()
                if now >= next_status:
                    cpu = self.client.cpu_load()
                    self.model.update('status:cpu', None if cpu is None else round(cpu, 1))
                    self.model.update('status:temp', read_temperature())
                    next_status = now + 1.0
                if now >= next_resync:
                    for instance, symbol in self._mapped_ports():
                        self._read(instance, symbol)
                    next_resync = now + 5.0
            except HostError:
                self.model.update('status:host', 'offline')
            except Exception as error:  # never let the link thread die silently
                print(f'kiwi-web: host link error: {error!r}', flush=True)
                self.client.close()
                self.model.update('status:host', 'offline')
                self._wake.wait(timeout=2.0)

    def _connect(self):
        token = self.units_ready()
        if not token:
            self.model.update('status:host', 'starting')
            self._wake.wait(timeout=1.0)
            return False
        if self._host_token is not None and token != self._host_token:
            # mod-host restarted and kiwi-restore re-applied the saved slot: unsaved
            # RAM edits are gone from the instrument, so RAM follows.
            self.store.load()
            self.current_reverb = self.applier.effective_reverb(self.store.data)
            self._morph_slots = None
            with self._lock:
                self._sets.clear()
                self._reads.clear()
                self._morph = None
            self.model.update('morph:value', 0.0)
        self._host_token = token
        try:
            self.client.connect()
            # RAM takes the instrument's readable values (a knob may have moved while
            # the link was idle, or kiwi-web may have restarted with edits still playing).
            for instance, symbol in self.port_params:
                self._learn(instance, symbol, self._read(instance, symbol))
            self._read_reverb(into_store=True)
        except (OSError, HostError):
            self.model.update('status:host', 'offline')
            self._wake.wait(timeout=2.0)
            return False
        self.model.update('status:host', 'online')
        self._publish_state()
        return True

    # ----- model bookkeeping --------------------------------------------------------

    def _mapped_ports(self):
        """Ports a CC drives inside mod-host: their values change without us hearing."""
        if self.mappings is None:
            return []
        return [(m['target']['instance'], m['target']['symbol']) for m in self.mappings.ports()]

    def _publish_state(self):
        store = self.store
        self.model.update('map:mappings', list(self.mappings.mappings) if self.mappings else [])
        self.model.update('slot:current', store.slot)
        self.model.update('slot:name', store.data['name'])
        summaries = store.summaries()
        self.model.update('slot:names', [s['name'] for s in summaries])
        self.model.update('slot:summaries', summaries)
        self.model.update('slot:dirty', store.dirty)
        self.model.update('reverb:current', self.current_reverb)
        self.model.update('preset:current', self.applier.effective_preset(store.data))
        self.model.update('preset:favourites', list(self.favourites.uris) if self.favourites else [])
        for number, param in self.cc_params.items():
            self.model.update(f'cc:{number}', store.data['cc'].get(str(number), param['baseline']))
        for key, value in store.data['patch_params'].items():
            instance, uri = StateStore.split_key(key)
            self.model.update(f'patch:{instance}:{uri}', value)

    def _read(self, instance, symbol):
        value = self.client.param_get(instance, symbol)
        if value is not None:
            self.model.update(f'port:{instance}:{symbol}', value)
        return value

    def _learn(self, instance, symbol, value):
        """Records a value read from mod-host in RAM, only if RAM disagrees (so a
        sync never marks the slot dirty by itself)."""
        if value is None:
            return
        param = self.applier.port.get((instance, symbol))
        if param is None:
            return
        held = self.store.data['params'].get(f'{instance}:{symbol}', param['baseline'])
        if abs(held - value) > 1e-6:
            self.store.set_param(instance, symbol, value, param['baseline'])

    def _read_reverb(self, into_store=False):
        for symbol, param in self.applier.reverb.get(self.current_reverb, {}).items():
            value = self._read(reverbs.REVERB_INSTANCE, symbol)
            if into_store and value is not None:
                held = self.store.data['reverb_params'].get(self.current_reverb, {}).get(symbol, param['baseline'])
                if abs(held - value) > 1e-6:
                    self.store.set_reverb_param(self.current_reverb, symbol, value, param['baseline'])

    def _on_applied(self, op):
        kind = op[0]
        if kind == 'reverb':
            self.current_reverb = op[1]
            self.model.update('reverb:current', op[1])
            self._read_reverb()
            # The slot holds a new instance: its CC bindings went with the old one.
            # Only this reverb's own parameters can be bound again.
            if self.mappings is not None:
                symbols = self.applier.reverb.get(op[1], {})
                for mapping in self.mappings.ports():
                    target = mapping['target']
                    if target['instance'] == reverbs.REVERB_INSTANCE and target['symbol'] in symbols:
                        map_port(self.client, mapping, self.ranges)
        elif kind == 'port':
            self.model.update(f'port:{op[1]}:{op[2]}', op[3])
        elif kind == 'patch':
            self.model.update(f'patch:{op[1]}:{op[2]}', op[3])
        elif kind == 'preset':
            self.model.update('preset:current', op[1])
        else:
            self.model.update(f'cc:{op[1]}', op[2])

    def _execute(self, ops):
        return execute_ops(self.client, self.midi, self.cc_params, ops, self._on_applied, self.bundles)

    # ----- the actions --------------------------------------------------------------

    def _apply_name(self):
        with self._lock:
            name, self._name = self._name, None
        if name is not None:
            self.store.set_name(name)
            self._publish_state()

    def _apply_select(self):
        with self._lock:
            slot, self._select = self._select, None
            step, self._step = self._step, 0
        if slot is None and step == 0:
            return
        if slot is None:
            slot = (self.store.slot - 1 + step) % self.store.slots + 1
        previous = json.loads(json.dumps(self.store.data))
        self.store.select(slot)
        self._morph_slots = None
        with self._lock:
            self._sets.clear()
            self._reads.clear()
            self._morph = None
            self._reverb = None
        failures = self._execute(self.applier.ops(previous, self.store.data, replace=True))
        if failures:
            print(f'kiwi-web: slot {slot}: {failures} value(s) not applied', flush=True)
        self.current_reverb = self.applier.effective_reverb(self.store.data)
        self.model.update('morph:value', 0.0)
        self._publish_state()
        self.led.blink(slot, SLOT_FLASH_INTERVAL)

    def _apply_save(self):
        with self._lock:
            save, self._save = self._save, False
        if not save:
            return
        self.store.save()
        self._morph_slots = None
        self._publish_state()
        self.led.blink(SAVE_FLASHES, SAVE_FLASH_INTERVAL)

    def _apply_maps(self):
        with self._lock:
            maps, self._maps = self._maps, []
        for action, item in maps:
            ok = map_port(self.client, item, self.ranges) if action == 'map' else unmap_port(self.client, item)
            if not ok:
                print(f'kiwi-web: mod-host refused {action} {item}', flush=True)

    def _apply_reverb(self):
        with self._lock:
            reverb_id, self._reverb = self._reverb, None
        if reverb_id is None or reverb_id == self.current_reverb:
            return
        # RAM follows only once the slot really holds the new reverb.
        target = json.loads(json.dumps(self.store.data))
        target['reverb'] = reverb_id
        self._execute(self.applier.ops(self.store.data, target, replace=False))
        if self.current_reverb == reverb_id:
            self.store.set_reverb(reverb_id)
        else:
            print(f'kiwi-web: mod-host refused reverb {reverb_id}', flush=True)
        self._publish_state()

    def _apply_preset(self):
        with self._lock:
            uri, self._preset = self._preset, None
        if uri is None or uri == self.applier.effective_preset(self.store.data):
            return
        # RAM takes the preset only once mod-host has loaded it.
        if self._execute([('preset', uri)]) == 0:
            self.store.set_preset(uri)
        else:
            print(f'kiwi-web: mod-host refused preset {uri}', flush=True)
        self._publish_state()

    def _apply_morph(self):
        with self._lock:
            t, self._morph = self._morph, None
        if t is None:
            return
        slot = self.store.slot
        # The two slot files are read once per selection, not once per CC tick.
        if self._morph_slots is None or self._morph_slots[0] != slot:
            self._morph_slots = (slot, self.store.read_slot(slot), self.store.read_slot(slot % self.store.slots + 1))
        _, a, b = self._morph_slots
        target = self.applier.morph(a, b, t, self.current_reverb, preset=self.store.data['preset'])
        # RAM takes only what mod-host accepted.
        applied = []

        def on_applied(op):
            self._on_applied(op)
            applied.append(op)
        execute_ops(self.client, self.midi, self.cc_params, self.applier.ops(self.store.data, target, replace=False),
                    on_applied, self.bundles)
        for op in applied:
            if op[0] == 'port' and op[1] == reverbs.REVERB_INSTANCE:
                baseline = self.applier.reverb.get(self.current_reverb, {}).get(op[2], {}).get('baseline')
                self.store.set_reverb_param(self.current_reverb, op[2], op[3], baseline)
            elif op[0] == 'port':
                self.store.set_param(op[1], op[2], op[3], self.applier.port[(op[1], op[2])]['baseline'])
            elif op[0] == 'cc':
                self.store.set_cc(op[1], op[2], self.applier.cc[op[1]]['baseline'])
        self.model.update('morph:value', t)
        self._publish_state()

    def _apply_sets(self):
        with self._lock:
            sets, self._sets = self._sets, {}
        for (kind, instance, name), value in sets.items():
            if kind == 'cc':
                param = self.cc_params.get(int(name))
                if param is not None and self.midi is not None and send_cc(self.midi, param, value):
                    self.model.update(f'cc:{name}', value)
                    self.store.set_cc(int(name), value, param['baseline'])
            elif kind == 'port' and instance == reverbs.REVERB_INSTANCE:
                baseline = self.applier.reverb.get(self.current_reverb, {}).get(name, {}).get('baseline')
                if self.client.param_set(instance, name, value) == 0:
                    self.model.update(f'port:{instance}:{name}', value)
                    self.store.set_reverb_param(self.current_reverb, name, value, baseline)
            elif kind == 'port':
                baseline = self.applier.port.get((instance, name), {}).get('baseline')
                if self.client.param_set(instance, name, value) == 0:
                    self.model.update(f'port:{instance}:{name}', value)
                    self.store.set_param(instance, name, value, baseline)
            elif self.client.patch_set(instance, name, value) == 0:
                self.model.update(f'patch:{instance}:{name}', value)
                self.store.set_patch_param(instance, name, value, self.applier.patch.get((instance, name)))
        if sets:
            self.model.update('slot:dirty', self.store.dirty)

    def _apply_reads(self):
        with self._lock:
            reads, self._reads = self._reads, set()
        for instance, symbol in reads:
            self._learn(instance, symbol, self._read(instance, symbol))
        if reads:
            self.model.update('slot:dirty', self.store.dirty)


def load_metadata(params_path, patch):
    """Plugin parameter metadata with each parameter's baseline (patch value or default)."""
    with open(params_path) as f:
        meta = json.load(f)
    for plugin in meta['plugins']:
        for param in plugin['params']:
            if plugin['kind'] == 'port':
                param['baseline'] = patch.baseline.get((plugin['instance'], param['symbol']), param['default'])
            else:
                param['baseline'] = param['default']
    meta.setdefault('reverbs', [])
    meta.setdefault('default_reverb', None)
    for reverb in meta['reverbs']:
        for param in reverb['params']:
            if reverb['id'] == meta['default_reverb']:
                param['baseline'] = patch.baseline.get((reverbs.REVERB_INSTANCE, param['symbol']), param['default'])
            else:
                param['baseline'] = param['default']
    meta['slots'] = StateStore.SLOTS
    meta['default_preset'] = patch.presets.get(presets.PIANOTEQ_INSTANCE)
    return meta


def load_presets(path):
    """The exported Pianoteq presets (web/presets.json); an empty list if absent."""
    try:
        with open(path) as f:
            entries = json.load(f).get('presets', [])
    except (OSError, ValueError, AttributeError):
        return []
    return [p for p in entries if isinstance(p, dict) and all(isinstance(p.get(k), str)
                                                              for k in ('uri', 'name', 'family', 'bundle'))]


class App:
    def __init__(self, args, units_ready=None, monitor_command=None):
        with open(args.patch) as f:
            patch = parse_patch(f.read())
        self.meta = load_metadata(args.params, patch)
        self.ranges = {(p['kind'], p['instance'], param_name(p, q)): (q['min'], q['max'])
                       for p in self.meta['plugins'] for q in p['params'] if 'cc' not in q}
        self.ranges.update({('cc', p['instance'], str(q['cc'])): (q['min'], q['max'])
                            for p in self.meta['plugins'] for q in p['params'] if 'cc' in q})
        self.reverb_ranges = {r['id']: {q['symbol']: (q['min'], q['max']) for q in r['params']}
                              for r in self.meta['reverbs']}
        self.model = Model()
        self.store = StateStore(args.state_dir)
        self.store.load()
        self.presets = load_presets(args.presets)
        self.bundles = {p['uri']: p['bundle'] for p in self.presets}
        self.favourites = presets.Favourites(os.path.join(args.state_dir, 'favourites.json'))
        self.favourites.load()
        self.known_targets = ccmap.known_targets(self.meta)
        self.mappings = ccmap.CcMap(os.path.join(args.state_dir, 'ccmap.json'), self.known_targets)
        self.mappings.load()
        self.learning = None                     # the target waiting for its CC, in map mode
        self.client = HostClient(('127.0.0.1', args.host_port))
        self.link = HostLink(self.model, self.client, self.meta, self.store,
                             units_ready or systemd_units_ready,
                             midi=RawMidi(args.midi_device or find_device()), led=Led(args.led),
                             bundles=self.bundles, favourites=self.favourites, mappings=self.mappings)
        self.model.update('map:learning', None)
        self.active_notes = set()
        self._streams = 0
        self._streams_lock = threading.Lock()
        with open(os.path.join(args.static, 'index.html'), 'rb') as f:
            self.index = self._cached(f.read())
        self.meta_file = self._cached(json.dumps(self.meta).encode())
        self.presets_file = self._cached(json.dumps({'presets': self.presets}).encode())
        # Always on: the morph CC and the button must work with no page open.
        self.monitor = MidiMonitor(self.on_midi, command=monitor_command)
        try:
            self.monitor.start()
        except OSError as error:
            print(f'kiwi-web: MIDI monitor not started: {error!r}', flush=True)

    @staticmethod
    def _cached(body):
        return body, gzip.compress(body), '"' + hashlib.sha1(body).hexdigest()[:16] + '"'

    def on_midi(self, event):
        kind = event['type']
        if kind == 'note_on':
            self.active_notes.add(event['note'])
        elif kind == 'note_off':
            self.active_notes.discard(event['note'])
        elif kind == 'cc':
            self.model.update(f"midi:cc:{event['controller']}", event['value'])
            learning, self.learning = self.learning, None
            if learning is not None:
                self.link.request_map(event['channel'], event['controller'], learning)
                self.model.update('map:learning', None)
            else:
                for target in self.mappings.targets(event['channel'], event['controller']):
                    self._drive(target, event['value'])
        self.model.update('midi:notes', sorted(self.active_notes))
        self.model.update('midi:last', describe(event))

    def _drive(self, target, value):
        """One mapped CC arrived: ports changed inside mod-host already (RAM re-reads
        them); the rest is this service's job."""
        kind = target['kind']
        if kind == 'port':
            self.link.request_read(target['instance'], target['symbol'])
        elif kind in ('patch', 'cc'):
            name = target['uri'] if kind == 'patch' else str(target['number'])
            low, high = self.ranges[(kind, target['instance'], name)]
            self.link.set_value(kind, target['instance'], name, low + (high - low) * value / 127)
        elif target['name'] == 'morph':
            self.link.request_morph(value / 127)
        elif value >= 64:
            if target['name'] == 'panic':
                self.panic()
            elif target['name'] == 'slot_next':
                self.link.request_step(+1)
            elif target['name'] == 'slot_prev':
                self.link.request_step(-1)
            elif target['name'] == 'slot_save':
                self.link.request_save()

    def panic(self):
        return self.link.midi is not None and panic(self.link.midi)

    def learn(self, target):
        """Map mode: the next CC binds to `target`. False if it is not a known target."""
        target = ccmap.normalise(target, self.known_targets)
        if target is None:
            return False
        self.learning = target
        self.model.update('map:learning', target)
        return True

    def cancel_learning(self):
        self.learning = None
        self.model.update('map:learning', None)

    def unmap(self, target):
        target = ccmap.normalise(target, self.known_targets)
        if target is None:
            return False
        self.link.request_unmap(target)
        return True

    def apply_changes(self, changes):
        if not isinstance(changes, list):
            return False
        accepted = []
        for change in changes:
            try:
                instance = int(change['instance'])
                value = float(change['value'])
                if 'cc' in change:
                    kind, name = 'cc', str(int(change['cc']))
                elif 'symbol' in change:
                    kind, name = 'port', str(change['symbol'])
                else:
                    kind, name = 'patch', str(change['uri'])
            except (KeyError, TypeError, ValueError):
                return False
            if kind == 'port' and instance == reverbs.REVERB_INSTANCE:
                bounds = self.reverb_ranges.get(self.link.current_reverb, {}).get(name)
            else:
                bounds = self.ranges.get((kind, instance, name))
            if bounds is None or not math.isfinite(value):
                return False
            accepted.append((kind, instance, name, min(max(value, bounds[0]), bounds[1])))
        for kind, instance, name, value in accepted:
            self.link.set_value(kind, instance, name, value)
        return True

    def open_stream(self):
        with self._streams_lock:
            if self._streams >= MAX_STREAMS:
                return False
            self._streams += 1
        self.link.add_viewer()
        return True

    def close_stream(self):
        with self._streams_lock:
            self._streams -= 1
        self.link.remove_viewer()


class Handler(BaseHTTPRequestHandler):
    server_version = 'kiwi-web'
    protocol_version = 'HTTP/1.1'
    # A phone that drops off Wi-Fi never closes its connections; time them out.
    # Event streams write at least every KEEPALIVE seconds, well within this.
    timeout = 30

    def log_message(self, format, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def _send(self, code, body=b'', content_type='text/plain', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_cached(self, cached, content_type):
        body, body_gz, etag = cached
        if self.headers.get('If-None-Match') == etag:
            self._send(304, headers={'ETag': etag})
            return
        headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
        if 'gzip' in self.headers.get('Accept-Encoding', ''):
            headers['Content-Encoding'] = 'gzip'
            body = body_gz
        self._send(200, body, content_type, headers)

    def _loopback(self):
        return self.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def _outsider(self):
        if (security.is_private(self.client_address[0])
                and security.host_allowed(self.headers.get('Host'), self.connection.getsockname()[0],
                                          self.server.host_names)):
            return False
        self._send(403, b'forbidden\n')
        return True

    def do_GET(self):
        if self._outsider():
            return
        path = self.path.split('?', 1)[0]
        if path == '/':
            self._send_cached(self.app.index, 'text/html; charset=utf-8')
        elif path == '/params.json':
            self._send_cached(self.app.meta_file, 'application/json')
        elif path == '/presets.json':
            self._send_cached(self.app.presets_file, 'application/json')
        elif path == '/events':
            self._stream()
        else:
            self._send(404, b'not found\n')

    def do_POST(self):
        if self._outsider():
            return
        # The button scripts post from the Pi itself with no Origin; only the slot
        # endpoints get that exemption.
        path = self.path.split('?', 1)[0]
        button_request = self._loopback() and path.startswith('/slot/')
        if not button_request and not security.same_origin(self.headers.get('Origin'), self.headers.get('Host')):
            self._send(403, b'cross-origin request refused\n')
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._send(413 if length > 0 else 400, b'bad length\n')
            return
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            self._send(400, b'bad json\n')
            return
        if not isinstance(body, dict):
            body = {}
        link = self.app.link
        store = self.app.store
        if path == '/set':
            self._send(204 if self.app.apply_changes(body.get('changes')) else 400)
        elif path == '/panic':
            if self.app.panic():
                self._send(204)
            else:
                self._send(503, b'no MIDI device\n')
        elif path == '/map/learn':
            if self.app.learn(body.get('target')):
                self._send(204)
            else:
                self._send(409, b'unknown target\n')
        elif path == '/map/cancel':
            self.app.cancel_learning()
            self._send(204)
        elif path == '/map/remove':
            if self.app.unmap(body.get('target')):
                self._send(204)
            else:
                self._send(409, b'unknown target\n')
        elif path == '/reverb':
            if body.get('id') in self.app.reverb_ranges:
                link.request_reverb(body['id'])
                self._send(204)
            else:
                self._send(400, b'unknown reverb\n')
        elif path == '/slot/next':
            link.request_step(+1)
            self._send(204)
        elif path == '/slot/prev':
            link.request_step(-1)
            self._send(204)
        elif path == '/slot/select':
            slot = body.get('slot')
            if type(slot) is int and 1 <= slot <= store.slots:
                link.request_select(slot)
                self._send(204)
            else:
                self._send(400, b'bad slot\n')
        elif path == '/slot/save':
            link.request_save()
            self._send(204)
        elif path == '/slot/name':
            name = body.get('name')
            slot = body.get('slot')
            if not isinstance(name, str) or not name.strip():
                self._send(400, b'bad name\n')
            elif link.request_name(name, slot if type(slot) is int else None):
                self._send(204)
            else:
                self._send(409, b'that slot is no longer current\n')
        elif path == '/morph':
            value = body.get('value')
            if isinstance(value, (int, float)) and math.isfinite(value):
                link.request_morph(value)
                self._send(204)
            else:
                self._send(400, b'bad value\n')
        elif path == '/preset':
            uri = body.get('uri')
            if uri in self.app.bundles:
                link.request_preset(uri)
                self._send(204)
            else:
                self._send(400, b'unknown preset\n')
        elif path == '/favourite':
            uri = body.get('uri')
            on = body.get('on')
            if uri in self.app.bundles and isinstance(on, bool):
                link.set_favourite(uri, on)
                self._send(204)
            else:
                self._send(400, b'bad favourite\n')
        else:
            self._send(404, b'not found\n')

    def _stream(self):
        if not self.app.open_stream():
            self._send(503, b'too many viewers\n')
            return
        self.close_connection = True
        # A peer that vanished without closing (phone left Wi-Fi) would otherwise
        # keep this stream and the mod-host link alive for ~15 min.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_USER_TIMEOUT'):
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20000)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            version = 0
            while True:
                self.app.model.wait(version, timeout=KEEPALIVE)
                delta, version = self.app.model.since(version)
                chunk = f'data: {json.dumps(delta)}\n\n' if delta else ': keepalive\n\n'
                self.wfile.write(chunk.encode())
                self.wfile.flush()
                time.sleep(STREAM_INTERVAL)
        except OSError:
            pass
        finally:
            self.app.close_stream()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app
        name = socket.gethostname()
        self.host_names = {'localhost', name, f'{name}.local'}


def make_server(address, app):
    return Server(address, app)


def restore(args):
    """Applies the current preset slot to a freshly loaded patch (kiwi-restore.service)."""
    store = StateStore(args.state_dir)
    store.load()
    try:
        with open(args.patch) as f:
            meta = load_metadata(args.params, parse_patch(f.read()))
    except (OSError, ValueError) as error:
        print(f'kiwi-web: no parameter metadata, nothing restored: {error}', flush=True)
        return 0
    applier = Applier(meta)
    ops = applier.ops(StateStore.empty(), store.data, replace=False)
    mappings = ccmap.CcMap(os.path.join(args.state_dir, 'ccmap.json'), ccmap.known_targets(meta))
    mappings.load()
    ports = mappings.ports()
    if not ops and not ports:
        print(f'kiwi-web: slot {store.slot}: nothing to restore', flush=True)
        return 0
    client = HostClient(('127.0.0.1', args.host_port))
    deadline = time.monotonic() + 60
    while True:
        try:
            client.connect()
            break
        except OSError:
            if time.monotonic() > deadline:
                print('kiwi-web: mod-host not reachable', flush=True)
                return 1
            time.sleep(1)
    midi = RawMidi(args.midi_device or find_device())
    bundles = {p['uri']: p['bundle'] for p in load_presets(args.presets)}
    failures = 0
    ranges = port_ranges(meta)
    try:
        failures = execute_ops(client, midi, cc_controls(meta), ops, bundles=bundles)
        for mapping in ports:
            failures += not map_port(client, mapping, ranges)
    except HostError as error:
        # Logged, not fatal: a failed unit would keep kiwi-web waiting.
        print(f'kiwi-web: restore stopped: {error}', flush=True)
        failures = len(ops) + len(ports)
    finally:
        client.close()
        midi.close()
    print(f'kiwi-web: slot {store.slot}: {len(ops)} operation(s), {len(ports)} CC mapping(s), '
          f'{failures} failure(s)', flush=True)
    return 0


def parse_args(argv=None):
    web = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo = os.path.dirname(web)
    parser = argparse.ArgumentParser(description='kiwi web UI')
    parser.add_argument('--port', type=int, default=80)
    parser.add_argument('--host-port', type=int, default=5555)
    parser.add_argument('--patch', default=os.path.join(repo, 'host', 'kiwi.patch'))
    parser.add_argument('--params', default=os.path.join(web, 'params.json'))
    parser.add_argument('--presets', default=os.path.join(web, 'presets.json'))
    parser.add_argument('--static', default=os.path.join(web, 'static'))
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/kiwi'))
    parser.add_argument('--midi-device', default=None,
                        help='raw MIDI device for CC controls (default: the VirMIDI card)')
    parser.add_argument('--led', default='/sys/kernel/pisound/led')
    parser.add_argument('--restore', action='store_true')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.restore:
        return restore(args)
    app = App(args)
    threading.Thread(target=app.link.run, daemon=True).start()
    server = make_server(('0.0.0.0', args.port), app)
    print(f'kiwi-web: serving on port {args.port}', flush=True)
    server.serve_forever()
    return 0
