"""kiwi-web: serves the control page and bridges it to mod-host.

With no page open it holds no mod-host connection and runs no MIDI monitor.
It connects only after kiwi-patch and kiwi-restore have finished, so it never
competes with them for mod-host's single client slot.
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
from .midi import MidiMonitor, describe
from .modhost import HostClient, HostError
from .patchfile import parse_patch
from .state import StateStore

MAX_STREAMS = 4
STREAM_INTERVAL = 0.1
KEEPALIVE = 15.0
IDLE_DISCONNECT = 10.0
MAX_BODY = 16384


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
    """True once kiwi-patch and kiwi-restore have finished since kiwi-host last started.

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
    return all(unit.get('ActiveState') in ('active', 'failed')
               and int(unit.get('StateChangeTimestampMonotonic') or 0) >= started
               for unit in loaders)


def param_name(plugin, param):
    return param['symbol'] if plugin['kind'] == 'port' else param['uri']


def reverb_baselines(meta):
    """{reverb id: {symbol: baseline}} for every reverb the page may load."""
    return {r['id']: {q['symbol']: q['baseline'] for q in r['params']} for r in meta.get('reverbs', [])}


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


class HostLink:
    """Owns the single mod-host connection, only while a page or a change needs it."""

    def __init__(self, model, client, meta, store, units_ready, clock=time.monotonic):
        self.model = model
        self.client = client
        self.store = store
        self.units_ready = units_ready
        self.clock = clock
        self.baselines = {(p['kind'], p['instance'], param_name(p, q)): q['baseline']
                          for p in meta['plugins'] for q in p['params']}
        self.port_params = [(p['instance'], q['symbol'])
                            for p in meta['plugins'] if p['kind'] == 'port' for q in p['params']]
        self.mapped = [(m['instance'], m['symbol']) for m in meta['cc_map']]
        self.reverb_baselines = reverb_baselines(meta)
        self.default_reverb = meta.get('default_reverb')
        self.current_reverb = store.data.get('reverb') or self.default_reverb
        self.model.update('reverb:current', self.current_reverb)
        self._lock = threading.Lock()
        self._sets = {}
        self._reads = set()
        self._reverb = None
        self._reset = False
        self._viewers = 0
        self._last_need = None
        self._wake = threading.Event()
        self._stop = False

    def add_viewer(self):
        with self._lock:
            self._viewers += 1
        self._wake.set()

    def remove_viewer(self):
        with self._lock:
            self._viewers -= 1
            self._last_need = self.clock()
        self._wake.set()

    def set_value(self, kind, instance, name, value):
        with self._lock:
            self._sets[(kind, instance, name)] = value
            self._last_need = self.clock()
        self._wake.set()

    def request_read(self, instance, symbol):
        with self._lock:
            self._reads.add((instance, symbol))
        self._wake.set()

    def request_reverb(self, reverb_id):
        with self._lock:
            self._reverb = reverb_id
            self._last_need = self.clock()
        self._wake.set()

    def request_reset(self):
        with self._lock:
            self._reset = True
            self._last_need = self.clock()
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _needed(self):
        with self._lock:
            if self._viewers > 0 or self._sets or self._reset or self._reverb is not None:
                return True
            return self._last_need is not None and self.clock() - self._last_need < IDLE_DISCONNECT

    def run(self):
        next_status = 0.0
        next_resync = 0.0
        while not self._stop:
            self._wake.wait(timeout=0.2)
            self._wake.clear()
            try:
                if self.store.due() and self.store.flush():
                    self.model.update('status:saved', time.time())
            except OSError as error:
                print(f'kiwi-web: autosave failed: {error!r}', flush=True)
            if not self._needed():
                if self.client.connected:
                    self.client.close()
                    self.model.update('status:host', 'idle')
                continue
            if not self.client.connected and not self._connect():
                continue
            try:
                self._apply_reset()
                self._apply_reverb()
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
        if not self.units_ready():
            self.model.update('status:host', 'starting')
            self._wake.wait(timeout=1.0)
            return False
        try:
            self.client.connect()
            for instance, symbol in self.port_params:
                self._read(instance, symbol)
            self._read_reverb()
        except (OSError, HostError):
            self.model.update('status:host', 'offline')
            self._wake.wait(timeout=2.0)
            return False
        self.model.update('status:host', 'online')
        return True

    def _read(self, instance, symbol):
        value = self.client.param_get(instance, symbol)
        if value is not None:
            self.model.update(f'port:{instance}:{symbol}', value)
        return value

    def _read_reverb(self):
        for symbol in self.reverb_baselines.get(self.current_reverb, {}):
            self._read(reverbs.REVERB_INSTANCE, symbol)

    def _load_reverb(self, reverb_id, settings):
        """Swaps the reverb slot (the tail cuts for a moment) and reads it back."""
        if switch_reverb(self.client, reverb_id, settings) == 0:
            self.current_reverb = reverb_id
            self.model.update('reverb:current', reverb_id)
        self._read_reverb()

    def _apply_reverb(self):
        with self._lock:
            reverb_id, self._reverb = self._reverb, None
        if reverb_id is None or reverb_id == self.current_reverb:
            return
        self.store.set_reverb(reverb_id)
        self._load_reverb(reverb_id, self.store.data['reverb_params'].get(reverb_id, {}))

    def _apply_reset(self):
        with self._lock:
            reset, self._reset = self._reset, False
        if not reset:
            return
        params = dict(self.store.data['params'])
        patch_params = dict(self.store.data['patch_params'])
        tuned = dict(self.store.data['reverb_params'].get(self.current_reverb, {}))
        self.store.clear()
        with self._lock:
            self._sets.clear()
            self._reverb = None
        if self.current_reverb != self.default_reverb:
            self._load_reverb(self.default_reverb, self.reverb_baselines.get(self.default_reverb, {}))
        else:
            for symbol in tuned:
                baseline = self.reverb_baselines[self.current_reverb].get(symbol)
                if baseline is not None and self.client.param_set(reverbs.REVERB_INSTANCE, symbol, baseline) == 0:
                    self.model.update(f'port:{reverbs.REVERB_INSTANCE}:{symbol}', baseline)
        for key in params:
            instance, symbol = StateStore.split_key(key)
            baseline = self.baselines.get(('port', instance, symbol))
            if baseline is not None and self.client.param_set(instance, symbol, baseline) == 0:
                self.model.update(f'port:{instance}:{symbol}', baseline)
        for key in patch_params:
            instance, uri = StateStore.split_key(key)
            baseline = self.baselines.get(('patch', instance, uri))
            if baseline is not None and self.client.patch_set(instance, uri, baseline) == 0:
                self.model.update(f'patch:{instance}:{uri}', baseline)
        self.model.update('status:saved', time.time())

    def _apply_sets(self):
        with self._lock:
            sets, self._sets = self._sets, {}
        for (kind, instance, name), value in sets.items():
            if kind == 'port' and instance == reverbs.REVERB_INSTANCE:
                baseline = self.reverb_baselines.get(self.current_reverb, {}).get(name)
                if self.client.param_set(instance, name, value) == 0:
                    self.model.update(f'port:{instance}:{name}', value)
                    self.store.set_reverb_param(self.current_reverb, name, value, baseline)
                continue
            baseline = self.baselines.get((kind, instance, name))
            if kind == 'port':
                if self.client.param_set(instance, name, value) == 0:
                    self.model.update(f'port:{instance}:{name}', value)
                    self.store.set_param(instance, name, value, baseline)
            elif self.client.patch_set(instance, name, value) == 0:
                self.model.update(f'patch:{instance}:{name}', value)
                self.store.set_patch_param(instance, name, value, baseline)

    def _apply_reads(self):
        with self._lock:
            reads, self._reads = self._reads, set()
        for instance, symbol in reads:
            value = self._read(instance, symbol)
            if value is not None:
                self.store.set_param(instance, symbol, value, self.baselines.get(('port', instance, symbol)))


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
    return meta


class App:
    def __init__(self, args, units_ready=None, monitor_command=None):
        with open(args.patch) as f:
            patch = parse_patch(f.read())
        self.meta = load_metadata(args.params, patch)
        self.ranges = {(p['kind'], p['instance'], param_name(p, q)): (q['min'], q['max'])
                       for p in self.meta['plugins'] for q in p['params']}
        self.reverb_ranges = {r['id']: {q['symbol']: (q['min'], q['max']) for q in r['params']}
                              for r in self.meta['reverbs']}
        self.model = Model()
        self.store = StateStore(args.state, delay=args.save_delay)
        self.store.load()
        for key, value in self.store.data['patch_params'].items():
            instance, uri = StateStore.split_key(key)
            self.model.update(f'patch:{instance}:{uri}', value)
        self.client = HostClient(('127.0.0.1', args.host_port))
        self.link = HostLink(self.model, self.client, self.meta, self.store,
                             units_ready or systemd_units_ready)
        self.monitor = MidiMonitor(self.on_midi, command=monitor_command)
        self.cc_targets = {(m.channel, m.cc): (m.instance, m.symbol) for m in patch.cc_map}
        self.active_notes = set()
        self._streams = 0
        self._streams_lock = threading.Lock()
        with open(os.path.join(args.static, 'index.html'), 'rb') as f:
            self.index = self._cached(f.read())
        self.meta_file = self._cached(json.dumps(self.meta).encode())

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
                if 'symbol' in change:
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
        # Monitor start/stop happen under the lock so a closing and an opening
        # viewer cannot leave the monitor stopped while someone is watching.
        with self._streams_lock:
            if self._streams >= MAX_STREAMS:
                return False
            self._streams += 1
            if self._streams == 1:
                try:
                    self.monitor.start()
                except OSError as error:
                    print(f'kiwi-web: MIDI monitor not started: {error!r}', flush=True)
        self.link.add_viewer()
        return True

    def close_stream(self):
        with self._streams_lock:
            self._streams -= 1
            if self._streams == 0:
                self.monitor.stop()
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
        if not security.same_origin(self.headers.get('Origin'), self.headers.get('Host')):
            self._send(403, b'cross-origin request refused\n')
            return
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            self._send(413, b'too large\n')
            return
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            self._send(400, b'bad json\n')
            return
        path = self.path.split('?', 1)[0]
        if path == '/set':
            ok = isinstance(body, dict) and self.app.apply_changes(body.get('changes'))
            self._send(204 if ok else 400)
        elif path == '/reset':
            self.app.link.request_reset()
            self._send(204)
        elif path == '/reverb':
            reverb_id = body.get('id') if isinstance(body, dict) else None
            if reverb_id in self.app.reverb_ranges:
                self.app.link.request_reverb(reverb_id)
                self._send(204)
            else:
                self._send(400, b'unknown reverb\n')
        else:
            self._send(404, b'not found\n')

    def _stream(self):
        if not self.app.open_stream():
            self._send(503, b'too many viewers\n')
            return
        self.close_connection = True
        # A peer that vanished without closing (phone left Wi-Fi) would otherwise
        # keep this stream, the mod-host link and the MIDI monitor alive for ~15 min.
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
    """Replays the autosaved state into mod-host (kiwi-restore.service)."""
    store = StateStore(args.state)
    data = store.load()
    try:
        with open(args.patch) as f:
            meta = load_metadata(args.params, parse_patch(f.read()))
    except (OSError, ValueError) as error:
        print(f'kiwi-web: no parameter metadata, reverb not restored: {error}', flush=True)
        meta = {'reverbs': [], 'default_reverb': None}
    default_reverb = meta['default_reverb']
    reverb_id = data['reverb'] if data['reverb'] in reverb_baselines(meta) else default_reverb
    reverb_settings = data['reverb_params'].get(reverb_id, {}) if reverb_id else {}
    if not data['params'] and not data['patch_params'] and reverb_id == default_reverb and not reverb_settings:
        print('kiwi-web: nothing to restore', flush=True)
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
    failures = 0
    total = len(data['params']) + len(data['patch_params'])
    try:
        if reverb_id != default_reverb:
            failures += switch_reverb(client, reverb_id, reverb_settings)
            total += 1
        elif reverb_settings:
            for symbol, value in reverb_settings.items():
                failures += client.param_set(reverbs.REVERB_INSTANCE, symbol, value) != 0
            total += len(reverb_settings)
        for key, value in data['params'].items():
            instance, symbol = StateStore.split_key(key)
            if instance == reverbs.REVERB_INSTANCE:
                continue   # reverb settings live in reverb_params; this is a stale entry
            failures += client.param_set(instance, symbol, value) != 0
        for key, value in data['patch_params'].items():
            instance, uri = StateStore.split_key(key)
            failures += client.patch_set(instance, uri, value) != 0
    except HostError as error:
        # Logged, not fatal: a failed unit would keep kiwi-web waiting.
        print(f'kiwi-web: restore stopped: {error}', flush=True)
        failures = total
    finally:
        client.close()
    print(f'kiwi-web: restored {total} value(s), {failures} failure(s)', flush=True)
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
    parser.add_argument('--state', default=os.path.expanduser('~/.local/state/kiwi/state.json'))
    parser.add_argument('--save-delay', type=float, default=5.0)
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
