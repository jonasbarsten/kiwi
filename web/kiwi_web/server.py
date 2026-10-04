"""kiwi-web: serves the control page and bridges it to mod-host.

State lives in RAM in one of eight preset slots (see state.py); the only disk
writes are an explicit save and the one-line `current` file on slot selection.
The mod-host connection is held only while a page is open or a change is
pending, and only after kiwi-patch and kiwi-restore have finished, so it never
competes with them for mod-host's single client slot. The MIDI monitor runs
permanently so the morph CC works without a page.
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

from . import reverbs, security
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
MORPH_CC = 27
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


def execute_ops(client, midi, cc_params, ops, on_applied=None):
    """Runs Applier operations against mod-host / the MIDI device.

    `on_applied(op)` is called for every operation that succeeded. Returns the
    number of failures. Raises HostError if mod-host goes away.
    """
    failures = 0
    for op in ops:
        kind = op[0]
        if kind == 'reverb':
            ok = switch_reverb(client, op[1], op[2]) == 0
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

    def __init__(self, model, client, meta, store, units_ready, clock=time.monotonic, midi=None, led=None):
        self.model = model
        self.client = client
        self.store = store
        self.units_ready = units_ready
        self.clock = clock
        self.midi = midi
        self.led = led or Led()
        self.applier = Applier(meta)
        self.cc_params = cc_controls(meta)
        self.port_params = [(p['instance'], q['symbol'])
                            for p in meta['plugins'] if p['kind'] == 'port' for q in p['params'] if 'cc' not in q]
        self.mapped = [(m['instance'], m['symbol']) for m in meta['cc_map']]
        self.current_reverb = self.applier.effective_reverb(store.data)
        self._lock = threading.Lock()
        self._sets = {}
        self._reads = set()
        self._reverb = None
        self._reset = False
        self._select = None
        self._step = 0
        self._save = False
        self._morph = None
        self._name = None
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

    def request_reset(self):
        self._request('_reset', True)

    def request_select(self, slot):
        self._request('_select', slot)

    def request_save(self):
        self._request('_save', True)

    def request_morph(self, t):
        self._request('_morph', min(max(float(t), 0.0), 1.0))

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
            if (self._viewers > 0 or self._sets or self._reads or self._reset or self._reverb is not None
                    or self._select is not None or self._step or self._save or self._morph is not None):
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
                self._apply_reset()
                self._apply_reverb()
                self._apply_morph()
                self._apply_sets()
                self._apply_reads()
                now = self.clock()
                if now >= next_status:
                    cpu = self.client.cpu_load()
                    self.model.update('status:cpu', None if cpu is None else round(cpu, 1))
                    self.model.update('status:temp', read_temperature())
                    next_status = now + 1.0
                if now >= next_resync:
                    for instance, symbol in self.mapped:
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

    def _publish_state(self):
        store = self.store
        self.model.update('slot:current', store.slot)
        self.model.update('slot:name', store.data['name'])
        self.model.update('slot:names', store.names())
        self.model.update('slot:dirty', store.dirty)
        self.model.update('reverb:current', self.current_reverb)
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
        elif kind == 'port':
            self.model.update(f'port:{op[1]}:{op[2]}', op[3])
        elif kind == 'patch':
            self.model.update(f'patch:{op[1]}:{op[2]}', op[3])
        else:
            self.model.update(f'cc:{op[1]}', op[2])

    def _execute(self, ops):
        return execute_ops(self.client, self.midi, self.cc_params, ops, self._on_applied)

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
        self._publish_state()
        self.led.blink(SAVE_FLASHES, SAVE_FLASH_INTERVAL)

    def _apply_reset(self):
        with self._lock:
            reset, self._reset = self._reset, False
        if not reset:
            return
        with self._lock:
            self._sets.clear()
            self._reverb = None
            self._morph = None
        self._execute(self.applier.ops(self.store.data, StateStore.empty(), replace=True))
        self.store.clear()
        self.current_reverb = self.applier.default_reverb
        self.model.update('morph:value', 0.0)
        self._publish_state()

    def _apply_reverb(self):
        with self._lock:
            reverb_id, self._reverb = self._reverb, None
        if reverb_id is None or reverb_id == self.current_reverb:
            return
        previous = json.loads(json.dumps(self.store.data))
        self.store.set_reverb(reverb_id)
        self._execute(self.applier.ops(previous, self.store.data, replace=False))
        self._publish_state()

    def _apply_morph(self):
        with self._lock:
            t, self._morph = self._morph, None
        if t is None:
            return
        slot = self.store.slot
        a = self.store.read_slot(slot)
        b = self.store.read_slot(slot % self.store.slots + 1)
        target = self.applier.morph(a, b, t, self.current_reverb)
        self._execute(self.applier.ops(self.store.data, target, replace=False))
        for key, value in target['params'].items():
            instance, symbol = StateStore.split_key(key)
            self.store.set_param(instance, symbol, value, self.applier.port[(instance, symbol)]['baseline'])
        for number, value in target['cc'].items():
            self.store.set_cc(int(number), value, self.applier.cc[int(number)]['baseline'])
        for reverb_id, settings in target['reverb_params'].items():
            for symbol, value in settings.items():
                self.store.set_reverb_param(reverb_id, symbol, value, self.applier.reverb[reverb_id][symbol]['baseline'])
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
    meta['cc_map'] = [vars(m) for m in patch.cc_map]
    meta['slots'] = StateStore.SLOTS
    meta['morph_cc'] = MORPH_CC
    return meta


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
        self.client = HostClient(('127.0.0.1', args.host_port))
        self.link = HostLink(self.model, self.client, self.meta, self.store,
                             units_ready or systemd_units_ready,
                             midi=RawMidi(args.midi_device or find_device()), led=Led(args.led))
        self.cc_targets = {(m.channel, m.cc): (m.instance, m.symbol) for m in patch.cc_map}
        self.active_notes = set()
        self._streams = 0
        self._streams_lock = threading.Lock()
        with open(os.path.join(args.static, 'index.html'), 'rb') as f:
            self.index = self._cached(f.read())
        self.meta_file = self._cached(json.dumps(self.meta).encode())
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
            if event['channel'] == 0 and event['controller'] == MORPH_CC:
                self.link.request_morph(event['value'] / 127)
            target = self.cc_targets.get((event['channel'], event['controller']))
            if target is not None:
                self.link.request_read(*target)
        self.model.update('midi:notes', sorted(self.active_notes))
        self.model.update('midi:last', describe(event))

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
        elif path == '/reset':
            link.request_reset()
            self._send(204)
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
    if not ops:
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
    failures = 0
    try:
        failures = execute_ops(client, midi, cc_controls(meta), ops)
    except HostError as error:
        # Logged, not fatal: a failed unit would keep kiwi-web waiting.
        print(f'kiwi-web: restore stopped: {error}', flush=True)
        failures = len(ops)
    finally:
        client.close()
        midi.close()
    print(f'kiwi-web: slot {store.slot}: {len(ops)} operation(s), {failures} failure(s)', flush=True)
    return 0


def parse_args(argv=None):
    web = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo = os.path.dirname(web)
    parser = argparse.ArgumentParser(description='kiwi web UI')
    parser.add_argument('--port', type=int, default=80)
    parser.add_argument('--host-port', type=int, default=5555)
    parser.add_argument('--patch', default=os.path.join(repo, 'host', 'kiwi.patch'))
    parser.add_argument('--params', default=os.path.join(web, 'params.json'))
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
