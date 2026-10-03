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
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import security
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
    # `systemctl is-active a b` exits 0 when ANY unit is active, so check every state line.
    result = run(['systemctl', 'is-active', 'kiwi-patch', 'kiwi-restore'],
                 capture_output=True, text=True)
    states = result.stdout.split()
    return len(states) == 2 and all(state == 'active' for state in states)


def param_name(plugin, param):
    return param['symbol'] if plugin['kind'] == 'port' else param['uri']


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
        self._lock = threading.Lock()
        self._sets = {}
        self._reads = set()
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
            if self._viewers > 0 or self._sets or self._reset:
                return True
            return self._last_need is not None and self.clock() - self._last_need < IDLE_DISCONNECT

    def run(self):
        next_status = 0.0
        next_resync = 0.0
        while not self._stop:
            self._wake.wait(timeout=0.2)
            self._wake.clear()
            if self.store.due() and self.store.flush():
                self.model.update('status:saved', time.time())
            if not self._needed():
                if self.client.connected:
                    self.client.close()
                    self.model.update('status:host', 'idle')
                continue
            if not self.client.connected and not self._connect():
                continue
            try:
                self._apply_reset()
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

    def _connect(self):
        if not self.units_ready():
            self.model.update('status:host', 'starting')
            self._wake.wait(timeout=1.0)
            return False
        try:
            self.client.connect()
            for instance, symbol in self.port_params:
                self._read(instance, symbol)
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

    def _apply_reset(self):
        with self._lock:
            reset, self._reset = self._reset, False
        if not reset:
            return
        params = dict(self.store.data['params'])
        patch_params = dict(self.store.data['patch_params'])
        self.store.clear()
        with self._lock:
            self._sets.clear()
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
    meta['cc_map'] = [vars(m) for m in patch.cc_map]
    return meta


class App:
    def __init__(self, args, units_ready=None, monitor_command=None):
        with open(args.patch) as f:
            patch = parse_patch(f.read())
        self.meta = load_metadata(args.params, patch)
        self.ranges = {(p['kind'], p['instance'], param_name(p, q)): (q['min'], q['max'])
                       for p in self.meta['plugins'] for q in p['params']}
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
            first = self._streams == 1
        if first:
            self.monitor.start()
        self.link.add_viewer()
        return True

    def close_stream(self):
        with self._streams_lock:
            self._streams -= 1
            last = self._streams == 0
        self.link.remove_viewer()
        if last:
            self.monitor.stop()


class Handler(BaseHTTPRequestHandler):
    server_version = 'kiwi-web'
    protocol_version = 'HTTP/1.1'

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
        if security.is_private(self.client_address[0]):
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
        else:
            self._send(404, b'not found\n')

    def _stream(self):
        if not self.app.open_stream():
            self._send(503, b'too many viewers\n')
            return
        self.close_connection = True
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


def make_server(address, app):
    return Server(address, app)


def restore(args):
    """Replays the autosaved state into mod-host (kiwi-restore.service)."""
    store = StateStore(args.state)
    data = store.load()
    if not data['params'] and not data['patch_params']:
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
    try:
        for key, value in data['params'].items():
            instance, symbol = StateStore.split_key(key)
            failures += client.param_set(instance, symbol, value) != 0
        for key, value in data['patch_params'].items():
            instance, uri = StateStore.split_key(key)
            failures += client.patch_set(instance, uri, value) != 0
    finally:
        client.close()
    total = len(data['params']) + len(data['patch_params'])
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
