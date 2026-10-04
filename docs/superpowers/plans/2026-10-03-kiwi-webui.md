# Kiwi Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A mobile-first, kiwi-themed web page on the Pi that monitors and controls the instrument live (knobs, all exposed plugin parameters, Pianoteq parameters, MIDI activity), with autosave, without disturbing audio.

**Architecture:** One Python 3 stdlib process (`web/kiwi-web`) at idle CPU/IO priority serves a single static page plus Server-Sent Events, and owns one lockstep connection to mod-host only while a page is open. `kiwi-restore.service` replays the autosaved state after each patch load. Parameter metadata is generated from the installed plugins at install time.

**Tech Stack:** Python 3.10+ stdlib (`http.server`, `socket`, `threading`, `subprocess`, `unittest`), vanilla HTML/CSS/JS, systemd, `aseqdump`, `lv2info`.

**Spec:** `docs/superpowers/specs/2026-10-03-kiwi-webui-design.md`

## Global Constraints

- Python code must run on Python 3.10 (Mac) and 3.11 (Pi); stdlib only.
- mod-host: one client at a time, one command per send, wait for `resp` (`resp 0 0.8000`, errors negative like `resp -103`).
- `kiwi-web` never connects to 5555 unless `kiwi-patch` and `kiwi-restore` are both active, and only while a page is open or a change is pending; disconnects 10 s after.
- No feedback port (`-f`) on mod-host.
- Systemd: `Nice=19`, `CPUSchedulingPolicy=idle`, `IOSchedulingClass=idle`, `MemoryMax=64M`, port 80 via `CAP_NET_BIND_SERVICE`.
- Mobile first: works at 360 px wide, touch targets ≥ 44 px, text ≥ 16 px, no hover-only controls, single static `index.html` with no external assets.
- Refuse requests from non-private addresses; POST requires `Origin` to match `Host`.
- Only parameters present in the generated metadata can be set (routing-locked ones are absent).
- Local file changes only via Edit/Write (user rule); shell for reading, testing, git, rsync and running `install.sh` on the Pi.
- Deploy: `rsync -a --delete --exclude backup --exclude legacy --exclude pure-data --exclude Cookbook --exclude SwiftCrossCompilers --exclude .git --exclude .superpowers --exclude plugins/kiwi/build --exclude web/params.json ./ patch@kiwi.local:kiwi/` then `ssh patch@kiwi.local 'cd ~/kiwi && ./install.sh'`.
- Audio budget: after this plan, `host/kiwi-stress 600` with two pages open must report 0.
- Push to `main` is allowed.

## Review Focus

1. **A page left open on a phone in a pocket** must cost nothing: the page closes its event stream when hidden; the server disconnects from mod-host 10 s after the last viewer. Pinned by `test_link_disconnects_when_idle` (Task 7).
2. **kiwi-web must never block `kiwi-load`**: it only connects when the units are ready. Pinned by `test_link_waits_for_units` (Task 7).
3. **Garbage or out-of-range values from the page** must not reach mod-host: unknown parameters are refused, values clamped, NaN refused. Pinned by `test_set_rejects_unknown_and_clamps` (Task 7).
4. **A mod-host restart while a page is open** must recover without restarting kiwi-web. Pinned by `test_client_drops_on_eof` (Task 2) and the on-device restart step (Task 9).
5. **Autosave must not write while untouched, and must survive a corrupt file.** Pinned by `test_not_due_without_changes` and `test_corrupt_file_loads_empty` (Task 4).

---

## File Structure

```
web/kiwi-web                    entry script (adds web/ to sys.path, runs kiwi_web.server.main)
web/kiwi_web/__init__.py        package marker
web/kiwi_web/patchfile.py       parse host/kiwi.patch: instances, baselines, midi_map
web/kiwi_web/modhost.py         lockstep mod-host client
web/kiwi_web/midi.py            aseqdump parser + monitor process
web/kiwi_web/state.py           autosave store
web/kiwi_web/security.py        private-address and same-origin checks
web/kiwi_web/server.py          model, host link, HTTP/SSE server, restore mode, main()
web/tools/gen_params.py         generates web/params.json on the Pi (lv2info + Pianoteq TTL)
web/static/index.html           the page (inline CSS/JS/SVG)
web/tests/fakehost.py           fake lockstep mod-host for tests
web/tests/test_*.py             unit + integration tests
system/kiwi-web.service         web server unit
system/kiwi-restore.service     autosave replay unit
install.sh                      + "Web UI" section
README.md                       + web UI section; rsync exclude
.gitignore                      + web/params.json, __pycache__
```

Test command for every task: `python3 -m unittest discover -s web/tests -t web -v` (run from repo root).

---

### Task 1: Package skeleton and patch-file parser

**Files:**
- Create: `web/kiwi_web/__init__.py`, `web/kiwi_web/patchfile.py`, `web/tests/__init__.py`, `web/tests/test_patchfile.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `parse_patch(text) -> Patch` with `Patch.instances: dict[int, str]`, `Patch.baseline: dict[tuple[int, str], float]`, `Patch.patch_baseline: dict[tuple[int, str], str]`, `Patch.cc_map: list[CcMapping]`; `CcMapping(instance, symbol, channel, cc, minimum, maximum)`.

- [ ] **Step 1: Write the failing test** `web/tests/test_patchfile.py`

```python
import unittest

from kiwi_web.patchfile import CcMapping, parse_patch

SAMPLE = """# comment
add https://www.modartt.com/lv2/Pianoteq8 0
add https://github.com/jonasbarsten/kiwi#mix 5

patch_set 0 https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch 0
param_set 6 decay 1.6
connect @MIDI_IN@ effect_0:in
midi_map 5 piano_vol 0 20 0 1
midi_map 3 blend 0 26 0 2
"""


class ParsePatchTest(unittest.TestCase):
    def test_instances(self):
        patch = parse_patch(SAMPLE)
        self.assertEqual(patch.instances, {
            0: 'https://www.modartt.com/lv2/Pianoteq8',
            5: 'https://github.com/jonasbarsten/kiwi#mix',
        })

    def test_baselines(self):
        patch = parse_patch(SAMPLE)
        self.assertEqual(patch.baseline, {(6, 'decay'): 1.6})
        self.assertEqual(patch.patch_baseline,
                         {(0, 'https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch'): '0'})

    def test_cc_map(self):
        patch = parse_patch(SAMPLE)
        self.assertEqual(patch.cc_map, [
            CcMapping(5, 'piano_vol', 0, 20, 0.0, 1.0),
            CcMapping(3, 'blend', 0, 26, 0.0, 2.0),
        ])

    def test_ignores_comments_and_connects(self):
        patch = parse_patch('# add x 1\nconnect a b\n')
        self.assertEqual(patch.instances, {})


if __name__ == '__main__':
    unittest.main()
```

`web/tests/__init__.py` and `web/kiwi_web/__init__.py` are empty files except a docstring:

```python
"""kiwi web UI."""
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s web/tests -t web -v`
Expected: ERROR `ModuleNotFoundError: No module named 'kiwi_web.patchfile'`.

- [ ] **Step 3: Implement** `web/kiwi_web/patchfile.py`

```python
"""Parses host/kiwi.patch: plugin instances, baseline values and the CC map."""
from dataclasses import dataclass, field


@dataclass
class CcMapping:
    instance: int
    symbol: str
    channel: int
    cc: int
    minimum: float
    maximum: float


@dataclass
class Patch:
    instances: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    patch_baseline: dict = field(default_factory=dict)
    cc_map: list = field(default_factory=list)


def parse_patch(text):
    patch = Patch()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        command = parts[0]
        if command == 'add' and len(parts) == 3:
            patch.instances[int(parts[2])] = parts[1]
        elif command == 'param_set' and len(parts) == 4:
            patch.baseline[(int(parts[1]), parts[2])] = float(parts[3])
        elif command == 'patch_set' and len(parts) >= 4:
            patch.patch_baseline[(int(parts[1]), parts[2])] = ' '.join(parts[3:])
        elif command == 'midi_map' and len(parts) == 7:
            patch.cc_map.append(CcMapping(int(parts[1]), parts[2], int(parts[3]), int(parts[4]),
                                          float(parts[5]), float(parts[6])))
    return patch
```

- [ ] **Step 4: Run to verify it passes** — same command. Expected: 4 tests OK.

- [ ] **Step 5: `.gitignore`** (Edit tool) — add:

```
# Web UI
web/params.json
__pycache__/
```

- [ ] **Step 6: Commit**

```bash
git add web .gitignore && git commit -m "Web UI: package skeleton and patch parser" && git push
```

---

### Task 2: Lockstep mod-host client and fake host

**Files:**
- Create: `web/kiwi_web/modhost.py`, `web/tests/fakehost.py`, `web/tests/test_modhost.py`

**Interfaces:**
- Produces: `HostClient(address=('127.0.0.1', 5555), timeout=60.0)` with `connect()`, `close()`, `connected`, `command(line) -> (int, str|None)`, `param_get(i, sym) -> float|None`, `param_set(i, sym, v) -> int`, `patch_set(i, uri, v) -> int`, `cpu_load() -> float|None`; `HostError`; `parse_response(bytes)`.
- Produces (tests): `FakeHost(params=None, close_after=None)` with `.port`, `.params`, `.patch`, `.log`, `.chunks`, `.close()`.

- [ ] **Step 1: Write the fake host** `web/tests/fakehost.py`

```python
"""A stand-in for mod-host's command socket: one client at a time, lockstep replies."""
import socket
import threading


class FakeHost:
    def __init__(self, params=None, close_after=None):
        self.params = dict(params or {})
        self.patch = {}
        self.log = []
        self.chunks = []
        self.cpu = 12.5
        self.close_after = close_after
        self.server = socket.create_server(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                buffer = b''
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.chunks.append(chunk)
                    buffer += chunk
                    while b'\0' in buffer:
                        raw, buffer = buffer.split(b'\0', 1)
                        if self.close_after is not None and len(self.log) >= self.close_after:
                            return
                        conn.sendall(self.handle(raw.decode()).encode() + b'\0')

    def handle(self, line):
        self.log.append(line)
        parts = line.split()
        if parts[0] == 'param_get':
            key = (int(parts[1]), parts[2])
            return f'resp 0 {self.params[key]:.4f}' if key in self.params else 'resp -103'
        if parts[0] == 'param_set':
            key = (int(parts[1]), parts[2])
            if key not in self.params:
                return 'resp -103'
            self.params[key] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'patch_set':
            self.patch[(int(parts[1]), parts[2])] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'cpu_load':
            return f'resp 0 {self.cpu:.4f}'
        return 'resp -1'

    def close(self):
        self.server.close()
```

- [ ] **Step 2: Write the failing tests** `web/tests/test_modhost.py`

```python
import unittest

from kiwi_web.modhost import HostClient, HostError, parse_response
from tests.fakehost import FakeHost


class ParseResponseTest(unittest.TestCase):
    def test_value(self):
        self.assertEqual(parse_response(b'resp 0 0.8000'), (0, '0.8000'))

    def test_error_code(self):
        self.assertEqual(parse_response(b'resp -103'), (-103, None))

    def test_garbage(self):
        with self.assertRaises(HostError):
            parse_response(b'hello')


class HostClientTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        self.client = HostClient(('127.0.0.1', self.host.port), timeout=5.0)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.host.close()

    def test_param_roundtrip(self):
        self.assertAlmostEqual(self.client.param_get(5, 'piano_vol'), 0.8)
        self.assertEqual(self.client.param_set(5, 'piano_vol', 0.5), 0)
        self.assertAlmostEqual(self.host.params[(5, 'piano_vol')], 0.5)

    def test_unknown_param(self):
        self.assertIsNone(self.client.param_get(5, 'nope'))
        self.assertEqual(self.client.param_set(5, 'nope', 1.0), -103)

    def test_one_command_per_send(self):
        for _ in range(5):
            self.client.cpu_load()
        self.assertTrue(all(chunk.count(b'\0') == 1 for chunk in self.host.chunks))

    def test_rejects_nul(self):
        with self.assertRaises(ValueError):
            self.client.command('cpu_load\0cpu_load')


class DropTest(unittest.TestCase):
    def test_client_drops_on_eof(self):
        host = FakeHost({(5, 'piano_vol'): 0.8}, close_after=1)
        client = HostClient(('127.0.0.1', host.port), timeout=5.0)
        client.connect()
        client.cpu_load()
        with self.assertRaises(HostError):
            client.cpu_load()
        self.assertFalse(client.connected)
        host.close()


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run to verify failure** — Expected: `ModuleNotFoundError: No module named 'kiwi_web.modhost'`.

- [ ] **Step 4: Implement** `web/kiwi_web/modhost.py`

```python
"""Lockstep client for mod-host's command socket.

mod-host serves one client at a time and treats each received chunk as one
command, so every command is sent on its own and its reply is read before the
next one is sent.
"""
import socket
import threading


class HostError(Exception):
    pass


def parse_response(raw):
    text = raw.decode('utf-8', 'replace').strip()
    if not text.startswith('resp '):
        raise HostError(f'unexpected reply: {text!r}')
    code, _, value = text[5:].partition(' ')
    return int(code), (value or None)


class HostClient:
    def __init__(self, address=('127.0.0.1', 5555), timeout=60.0):
        self.address = address
        self.timeout = timeout
        self._sock = None
        self._buffer = b''
        self._lock = threading.Lock()

    @property
    def connected(self):
        return self._sock is not None

    def connect(self):
        with self._lock:
            if self._sock is None:
                sock = socket.create_connection(self.address, timeout=5.0)
                sock.settimeout(self.timeout)
                self._sock = sock
                self._buffer = b''

    def close(self):
        # Taking the lock guarantees no reply is outstanding when the socket closes.
        with self._lock:
            self._drop()

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b''

    def command(self, line):
        if '\0' in line:
            raise ValueError('command contains NUL')
        with self._lock:
            if self._sock is None:
                raise HostError('not connected')
            try:
                self._sock.sendall(line.encode() + b'\0')
                while b'\0' not in self._buffer:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise HostError('mod-host closed the connection')
                    self._buffer += chunk
            except (OSError, HostError) as error:
                self._drop()
                raise HostError(str(error)) from error
            raw, self._buffer = self._buffer.split(b'\0', 1)
            return parse_response(raw)

    def param_get(self, instance, symbol):
        code, value = self.command(f'param_get {instance} {symbol}')
        return float(value) if code == 0 and value is not None else None

    def param_set(self, instance, symbol, value):
        code, _ = self.command(f'param_set {instance} {symbol} {value:.6f}')
        return code

    def patch_set(self, instance, uri, value):
        code, _ = self.command(f'patch_set {instance} {uri} {value:.6f}')
        return code

    def cpu_load(self):
        code, value = self.command('cpu_load')
        return float(value) if code == 0 and value is not None else None
```

- [ ] **Step 5: Run tests** — Expected: all pass (Task 1's 4 + 8 new).

- [ ] **Step 6: Commit** — `git add web && git commit -m "Web UI: lockstep mod-host client" && git push`

---

### Task 3: MIDI monitor

**Files:**
- Create: `web/kiwi_web/midi.py`, `web/tests/test_midi.py`

**Interfaces:**
- Produces: `parse_line(str) -> dict|None` (`{'type': 'note_on'|'note_off'|'cc'|'bend'|'program', 'channel': int, ...}`); `describe(event) -> str`; `MidiMonitor(on_event, port='Midi Through', command=None)` with `start()`, `stop()`.

- [ ] **Step 1: Write the failing test** `web/tests/test_midi.py`

```python
import sys
import threading
import unittest

from kiwi_web.midi import MidiMonitor, describe, parse_line

LINES = [
    'Waiting for data. Press Ctrl+C to end.',
    'Source  Event                  Ch  Data',
    '  0:1   Port subscribed            130:0 -> 132:0',
    ' 14:0   Control change          0, controller 20, value 64',
    ' 14:0   Note on                 0, note 60, velocity 100',
    ' 14:0   Note off                0, note 60, velocity 64',
    ' 14:0   Pitch bend              0, value 0',
    ' 14:0   Program change          0, program 5',
]


class ParseLineTest(unittest.TestCase):
    def test_headers_ignored(self):
        self.assertEqual([parse_line(line) for line in LINES[:3]], [None, None, None])

    def test_events(self):
        self.assertEqual(parse_line(LINES[3]), {'type': 'cc', 'channel': 0, 'controller': 20, 'value': 64})
        self.assertEqual(parse_line(LINES[4]), {'type': 'note_on', 'channel': 0, 'note': 60, 'velocity': 100})
        self.assertEqual(parse_line(LINES[5]), {'type': 'note_off', 'channel': 0, 'note': 60, 'velocity': 64})
        self.assertEqual(parse_line(LINES[6]), {'type': 'bend', 'channel': 0, 'value': 0})
        self.assertEqual(parse_line(LINES[7]), {'type': 'program', 'channel': 0, 'program': 5})

    def test_note_on_velocity_zero_is_note_off(self):
        event = parse_line(' 14:0   Note on                 3, note 61, velocity 0')
        self.assertEqual(event['type'], 'note_off')

    def test_describe(self):
        self.assertEqual(describe(parse_line(LINES[4])), 'ch1 note on C4 vel 100')
        self.assertEqual(describe(parse_line(LINES[3])), 'ch1 CC 20 = 64')


class MonitorTest(unittest.TestCase):
    def test_reads_child_output(self):
        events = []
        done = threading.Event()

        def on_event(event):
            events.append(event)
            if len(events) == 5:
                done.set()

        script = 'import sys; sys.stdout.write(%r)' % ('\n'.join(LINES) + '\n')
        monitor = MidiMonitor(on_event, command=[sys.executable, '-c', script])
        monitor.start()
        self.assertTrue(done.wait(5))
        monitor.stop()
        self.assertEqual([e['type'] for e in events], ['cc', 'note_on', 'note_off', 'bend', 'program'])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError: No module named 'kiwi_web.midi'`.

- [ ] **Step 3: Implement** `web/kiwi_web/midi.py`

```python
"""Incoming MIDI monitor: parses `aseqdump` output for the Midi Through port.

The aseqdump child only runs while a page is open.
"""
import re
import subprocess
import threading

_LINE = re.compile(
    r'^\s*\d+:\d+\s+(Note on|Note off|Control change|Pitch bend|Program change)\s+(\d+),\s*(.*)$')
_TYPES = {'Note on': 'note_on', 'Note off': 'note_off', 'Control change': 'cc',
          'Pitch bend': 'bend', 'Program change': 'program'}
_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def parse_line(line):
    match = _LINE.match(line)
    if not match:
        return None
    kind, channel, data = match.groups()
    event = {'type': _TYPES[kind], 'channel': int(channel)}
    for name, value in re.findall(r'(\w+) (-?\d+)', data):
        event[name] = int(value)
    if event['type'] == 'note_on' and event.get('velocity') == 0:
        event['type'] = 'note_off'
    return event


def note_name(note):
    return f'{_NOTE_NAMES[note % 12]}{note // 12 - 1}'


def describe(event):
    channel = f"ch{event['channel'] + 1}"
    kind = event['type']
    if kind in ('note_on', 'note_off'):
        state = 'on' if kind == 'note_on' else 'off'
        return f"{channel} note {state} {note_name(event['note'])} vel {event['velocity']}"
    if kind == 'cc':
        return f"{channel} CC {event['controller']} = {event['value']}"
    if kind == 'bend':
        return f"{channel} bend {event['value']}"
    return f"{channel} program {event['program']}"


class MidiMonitor:
    def __init__(self, on_event, port='Midi Through', command=None):
        self.on_event = on_event
        self.command = command or ['aseqdump', '-p', port]
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._proc is not None:
                return
            self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1)
            threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()

    def stop(self):
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _read(self, proc):
        for line in proc.stdout:
            event = parse_line(line)
            if event is not None:
                self.on_event(event)
```

- [ ] **Step 4: Run tests** — Expected: all pass.

- [ ] **Step 5: Commit** — `git add web && git commit -m "Web UI: MIDI monitor" && git push`

---

### Task 4: Autosave store

**Files:**
- Create: `web/kiwi_web/state.py`, `web/tests/test_state.py`

**Interfaces:**
- Produces: `StateStore(path, delay=5.0, clock=time.monotonic)` with `.data` (`params: {'inst:sym': float}`, `patch_params: {'inst:uri': float}`, `preset: str|None`, `favourites: list`), `load()`, `set_param(i, sym, v, baseline)`, `set_patch_param(i, uri, v, baseline)`, `clear()`, `due() -> bool`, `flush() -> bool`, `StateStore.split_key(key) -> (int, str)`.

- [ ] **Step 1: Write the failing test** `web/tests/test_state.py`

```python
import json
import os
import tempfile
import unittest

from kiwi_web.state import StateStore


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'sub', 'state.json')
        self.clock = Clock()
        self.store = StateStore(self.path, delay=5.0, clock=self.clock)
        self.store.load()

    def tearDown(self):
        self.dir.cleanup()

    def test_not_due_without_changes(self):
        self.assertFalse(self.store.due())
        self.assertFalse(self.store.flush())
        self.assertFalse(os.path.exists(self.path))

    def test_debounce_and_atomic_write(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.clock.now += 4.9
        self.assertFalse(self.store.due())
        self.clock.now += 0.2
        self.assertTrue(self.store.due())
        self.assertTrue(self.store.flush())
        with open(self.path) as f:
            self.assertEqual(json.load(f)['params'], {'5:piano_vol': 0.5})
        self.assertFalse(os.path.exists(self.path + '.tmp'))
        self.assertFalse(self.store.due())

    def test_value_equal_to_baseline_is_dropped(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_param(5, 'piano_vol', 0.8, baseline=0.8)
        self.assertEqual(self.store.data['params'], {})

    def test_patch_params_and_roundtrip(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        self.store.set_patch_param(0, uri, 0.3, baseline=0.72)
        self.store.flush()
        other = StateStore(self.path)
        other.load()
        self.assertEqual(other.data['patch_params'], {f'0:{uri}': 0.3})
        self.assertEqual(StateStore.split_key(f'0:{uri}'), (0, uri))

    def test_corrupt_file_loads_empty(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.store.load()['params'], {})

    def test_clear_removes_file(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.flush()
        self.store.clear()
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.store.data['params'], {})


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError: No module named 'kiwi_web.state'`.

- [ ] **Step 3: Implement** `web/kiwi_web/state.py`

```python
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
```

- [ ] **Step 4: Run tests** — Expected: all pass.

- [ ] **Step 5: Commit** — `git add web && git commit -m "Web UI: autosave store" && git push`

---

### Task 5: Request security checks

**Files:**
- Create: `web/kiwi_web/security.py`, `web/tests/test_security.py`

**Interfaces:**
- Produces: `is_private(address: str) -> bool`, `same_origin(origin: str|None, host: str|None) -> bool`.

- [ ] **Step 1: Write the failing test** `web/tests/test_security.py`

```python
import unittest

from kiwi_web.security import is_private, same_origin


class SecurityTest(unittest.TestCase):
    def test_private_addresses(self):
        for address in ['192.168.1.5', '10.0.0.2', '172.16.3.4', '127.0.0.1',
                        '::1', 'fe80::1', '::ffff:192.168.0.4', '169.254.3.3']:
            self.assertTrue(is_private(address), address)

    def test_public_addresses(self):
        for address in ['8.8.8.8', '2001:4860:4860::8888', '::ffff:8.8.8.8', 'junk', '']:
            self.assertFalse(is_private(address), address)

    def test_same_origin(self):
        self.assertTrue(same_origin('http://patchbox.local', 'patchbox.local'))
        self.assertTrue(same_origin('http://kiwi.local:8080', 'kiwi.local:8080'))
        self.assertFalse(same_origin('http://evil.example', 'patchbox.local'))
        self.assertFalse(same_origin(None, 'patchbox.local'))
        self.assertFalse(same_origin('http://patchbox.local', None))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `web/kiwi_web/security.py`

```python
"""Who may use the page: private networks only, and POSTs only from the page itself."""
import ipaddress
from urllib.parse import urlsplit


def is_private(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def same_origin(origin, host):
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()
```

- [ ] **Step 4: Run tests** — Expected: all pass.

- [ ] **Step 5: Commit** — `git add web && git commit -m "Web UI: request security checks" && git push`

---

### Task 6: Parameter metadata generator

**Files:**
- Create: `web/tools/gen_params.py`, `web/tests/test_gen_params.py`

**Interfaces:**
- Consumes: `parse_patch` (Task 1).
- Produces: `parse_lv2info(text) -> list[dict]` (control input ports: `symbol, name, min, max, default, type ('float'|'int'|'toggle'|'enum'), options?, group?`); `parse_pianoteq_ttl(text) -> list[dict]` (`uri, name, group, min, max, default`); `build(patch, lv2info, pianoteq_ttl) -> dict` → `{'plugins': [{'instance', 'title', 'kind': 'port'|'patch', 'params': [...]}]}`, Pianoteq params carry `curated: bool`. Running the script prints that JSON.

- [ ] **Step 1: Write the failing test** `web/tests/test_gen_params.py`

```python
import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location('gen_params', os.path.join(HERE, '..', 'tools', 'gen_params.py'))
gen_params = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_params)

LV2INFO = '''urn:dragonfly:plate

	Name:              Dragonfly Plate Reverb

	Port 0:
		Type:        http://lv2plug.in/ns/lv2core#AudioPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      lv2_audio_in_1
		Name:        Audio Input 1

	Port 3:
		Type:        http://lv2plug.in/ns/lv2core#InputPort
		             http://lv2plug.in/ns/lv2core#ControlPort
		Symbol:      amp_attack
		Name:        Amp Attack
		Group:       http://code.google.com/p/amsynth/amsynth#group_amp_env
		Minimum:     0.000000
		Maximum:     2.500000
		Default:     0.000000
		Properties:  http://lv2plug.in/ns/ext/port-props#hasStrictBounds

	Port 5:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      algorithm
		Name:        Algorithm
		Minimum:     0.000000
		Maximum:     2.000000
		Default:     1.000000
		Properties:  http://lv2plug.in/ns/lv2core#integer
		             http://lv2plug.in/ns/lv2core#enumeration
		Scale Points:				1 = "Nested"
			0 = "Simple"
			2 = "Tank"

	Port 6:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      truepeak
		Name:        True Peak
		Minimum:     0.000000
		Maximum:     1.000000
		Default:     0.000000
		Properties:  http://lv2plug.in/ns/lv2core#integer
		             http://lv2plug.in/ns/lv2core#toggled

	Port 7:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#OutputPort
		Symbol:      level
		Name:        Level
		Minimum:     -10.000000
		Maximum:     20.000000
'''

TTL = '''@prefix plug:  <https://www.modartt.com/lv2/Pianoteq8:> .

plug:Volume
	a lv2:Parameter ;
	rdfs:label "Volume" ;
	pg:group plug:paramgroup_main ;
	rdfs:range atom:Float ;
	lv2:default 0.727273 ;
	lv2:minimum 0 ;
	lv2:maximum 1 .

plug:Reverb_20Switch
	a lv2:Parameter ;
	rdfs:label "Reverb Switch" ;
	pg:group plug:paramgroup_reverb ;
	rdfs:range atom:Float ;
	lv2:default 1 ;
	lv2:minimum 0 ;
	lv2:maximum 1 .

plug:paramgroup_main
	a pg:Group ;
	lv2:symbol "paramgroup_main" ;		lv2:name "Main" .

plug:paramgroup_reverb
	a pg:Group ;
	lv2:symbol "paramgroup_reverb" ;		lv2:name "Reverb" .
'''


class Lv2InfoTest(unittest.TestCase):
    def test_control_inputs_only(self):
        params = gen_params.parse_lv2info(LV2INFO)
        self.assertEqual([p['symbol'] for p in params], ['amp_attack', 'algorithm', 'truepeak'])

    def test_float_with_group(self):
        attack = gen_params.parse_lv2info(LV2INFO)[0]
        self.assertEqual(attack, {'symbol': 'amp_attack', 'name': 'Amp Attack', 'min': 0.0, 'max': 2.5,
                                  'default': 0.0, 'type': 'float', 'group': 'amp env'})

    def test_enum_and_toggle(self):
        _, algorithm, truepeak = gen_params.parse_lv2info(LV2INFO)
        self.assertEqual(algorithm['type'], 'enum')
        self.assertEqual(algorithm['options'], [[0.0, 'Simple'], [1.0, 'Nested'], [2.0, 'Tank']])
        self.assertEqual(truepeak['type'], 'toggle')


class PianoteqTest(unittest.TestCase):
    def test_parameters(self):
        params = gen_params.parse_pianoteq_ttl(TTL)
        self.assertEqual(params[0], {'uri': 'https://www.modartt.com/lv2/Pianoteq8:Volume', 'name': 'Volume',
                                     'group': 'Main', 'min': 0.0, 'max': 1.0, 'default': 0.727273})
        self.assertEqual(params[1]['group'], 'Reverb')


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: `FileNotFoundError` / error loading `gen_params.py`.

- [ ] **Step 3: Implement** `web/tools/gen_params.py`

```python
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
PIANOTEQ_CURATED = ['Volume', 'Dynamics', 'Condition', 'Post Effect Gain', 'Unison',
                    'Hammer Hardness Piano', 'Hammer Hardness Mezzo', 'Hammer Hardness Forte',
                    'Direct Sound', 'Sympathetic Resonance', 'Stereo Width', 'Lid Position',
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
```

- [ ] **Step 4: Run tests** — Expected: all pass.

- [ ] **Step 5: Run the generator on the Pi** (after rsync)

```bash
ssh patch@kiwi.local 'cd ~/kiwi && python3 web/tools/gen_params.py | python3 -c "import json,sys; d=json.load(sys.stdin); [print(p[\"instance\"], p[\"title\"], len(p[\"params\"]), sum(1 for q in p[\"params\"] if q.get(\"curated\"))) for p in d[\"plugins\"]]"'
```
Expected: lines for 5 Mix 6, 3 Vocoder carrier 1, 6 Reverb 8, 7 Limiter 4, 4 Vocoder 1, 1 Sampler 6, 2 Synth 41, 0 Pianoteq 221 14; and no "curated … not found" lines on stderr. If a curated name is reported missing, find its exact label with `grep -o 'rdfs:label "[^"]*Hammer[^"]*"' "/home/patch/.vst/Pianoteq 8.lv2/dsp.ttl"` and fix `PIANOTEQ_CURATED`.

- [ ] **Step 6: Commit** — `git add web && git commit -m "Web UI: parameter metadata generator" && git push`

---

### Task 7: Server: model, host link, HTTP/SSE, restore mode

**Files:**
- Create: `web/kiwi_web/server.py`, `web/kiwi-web`, `web/tests/test_server.py`, `web/static/index.html` (placeholder `<!doctype html><title>kiwi</title>` until Task 8)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `Model` (`update(key, value)`, `get(key)`, `since(version) -> (dict, int)`, `wait(version, timeout)`); `HostLink` (`add_viewer`, `remove_viewer`, `set_value(kind, instance, name, value)`, `request_read(instance, symbol)`, `request_reset()`, `run()`, `stop()`); `App(args, units_ready=None, monitor_command=None)` with `apply_changes(list) -> bool`, `open_stream()`, `close_stream()`; `make_server(address, app)`; `restore(args) -> int`; `main(argv=None)`; `parse_args(argv)`.
- Model keys: `port:<inst>:<symbol>`, `patch:<inst>:<uri>`, `status:host` (`online|offline|starting|idle`), `status:cpu`, `status:temp`, `status:saved`, `midi:notes` (list of note numbers), `midi:last` (text), `midi:cc:<n>` (0–127).
- HTTP: `GET /` (HTML), `GET /params.json` (metadata + `baseline` per param + `cc_map`), `GET /events` (SSE, `data: {delta}`), `POST /set {"changes": [{instance, symbol|uri, value}]}` → 204, `POST /reset` → 204.

- [ ] **Step 1: Write the failing integration tests** `web/tests/test_server.py`

```python
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from kiwi_web.server import App, HostLink, Model, make_server, parse_args
from kiwi_web.state import StateStore
from tests.fakehost import FakeHost

PATCH = """add https://github.com/jonasbarsten/kiwi#carrier 3
add https://github.com/jonasbarsten/kiwi#mix 5
add https://www.modartt.com/lv2/Pianoteq8 0
param_set 5 piano_vol 0.8
midi_map 5 piano_vol 0 20 0 1
"""
PARAMS = {'plugins': [
    {'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
        {'symbol': 'piano_vol', 'name': 'Piano Volume', 'min': 0, 'max': 1, 'default': 0.5, 'type': 'float'}]},
    {'instance': 3, 'title': 'Vocoder carrier', 'kind': 'port', 'params': [
        {'symbol': 'blend', 'name': 'Blend', 'min': 0, 'max': 2, 'default': 0, 'type': 'float'}]},
    {'instance': 0, 'title': 'Pianoteq', 'kind': 'patch', 'params': [
        {'uri': 'https://www.modartt.com/lv2/Pianoteq8:Volume', 'name': 'Volume', 'group': 'Main',
         'min': 0, 'max': 1, 'default': 0.72, 'curated': True}]},
]}


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        paths = {}
        for name, content in [('kiwi.patch', PATCH), ('params.json', json.dumps(PARAMS))]:
            paths[name] = os.path.join(self.dir.name, name)
            with open(paths[name], 'w') as f:
                f.write(content)
        static = os.path.join(self.dir.name, 'static')
        os.mkdir(static)
        with open(os.path.join(static, 'index.html'), 'w') as f:
            f.write('<!doctype html><title>kiwi</title>')
        self.host = FakeHost({(5, 'piano_vol'): 0.8, (3, 'blend'): 0.0})
        self.state_path = os.path.join(self.dir.name, 'state', 'state.json')
        args = parse_args(['--port', '0', '--host-port', str(self.host.port), '--patch', paths['kiwi.patch'],
                           '--params', paths['params.json'], '--static', static,
                           '--state', self.state_path, '--save-delay', '0.2'])
        self.ready = True
        self.app = App(args, units_ready=lambda: self.ready,
                       monitor_command=[sys.executable, '-c', 'import time; time.sleep(60)'])
        self.link_thread = threading.Thread(target=self.app.link.run, daemon=True)
        self.link_thread.start()
        self.server = make_server(('127.0.0.1', 0), self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.link.stop()
        self.app.monitor.stop()
        self.server.shutdown()
        self.server.server_close()
        self.host.close()
        self.dir.cleanup()

    def request(self, method, path, body=None, origin=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def open_events(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        return conn, response

    def read_event(self, response):
        lines = []
        while True:
            line = response.fp.readline().decode()
            if line in ('\n', '\r\n', ''):
                if lines:
                    break
                continue
            lines.append(line)
        data = [l[len('data: '):] for l in lines if l.startswith('data: ')]
        return json.loads(''.join(data)) if data else {}

    def test_index_and_params(self):
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'kiwi', body)
        status, body = self.request('GET', '/params.json')
        meta = json.loads(body)
        mix = meta['plugins'][0]['params'][0]
        self.assertEqual(mix['baseline'], 0.8)      # from param_set in the patch
        self.assertEqual(meta['plugins'][2]['params'][0]['baseline'], 0.72)   # plugin default
        self.assertEqual(meta['cc_map'][0]['cc'], 20)

    def test_events_snapshot_then_set(self):
        conn, response = self.open_events()
        snapshot = {}
        while 'port:5:piano_vol' not in snapshot:
            snapshot.update(self.read_event(response))
        self.assertAlmostEqual(snapshot['port:5:piano_vol'], 0.8)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['params'], {'5:piano_vol': 0.3})
        conn.close()

    def test_set_rejects_unknown_and_clamps(self):
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 4, 'symbol': 'carrier', 'value': 1}]})
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 'nan'}]})
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 3, 'symbol': 'blend', 'value': 9}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.params[(3, 'blend')] == 2.0))

    def test_patch_param(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 0, 'uri': uri, 'value': 0.4}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.patch.get((0, uri)) == 0.4))

    def test_post_requires_origin(self):
        status, _ = self.request('POST', '/set', {'changes': []}, origin=False)
        self.assertEqual(status, 403)

    def test_reset_restores_baseline(self):
        self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        status, _ = self.request('POST', '/reset', {})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6))
        self.assertTrue(wait_for(lambda: not os.path.exists(self.state_path)))


class LinkTest(unittest.TestCase):
    def make_link(self, ready):
        meta = {'plugins': [{'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
            {'symbol': 'piano_vol', 'name': 'v', 'min': 0, 'max': 1, 'default': 0.8, 'baseline': 0.8,
             'type': 'float'}]}], 'cc_map': []}
        self.dir = tempfile.TemporaryDirectory()
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        from kiwi_web.modhost import HostClient
        self.clock = [0.0]
        link = HostLink(Model(), HostClient(('127.0.0.1', self.host.port), timeout=5), meta,
                        StateStore(os.path.join(self.dir.name, 's.json')), ready, clock=lambda: self.clock[0])
        threading.Thread(target=link.run, daemon=True).start()
        return link

    def tearDown(self):
        self.link.stop()
        self.host.close()
        self.dir.cleanup()

    def test_link_waits_for_units(self):
        self.link = self.make_link(lambda: False)
        self.link.add_viewer()
        time.sleep(0.5)
        self.assertEqual(self.host.log, [])
        self.assertEqual(self.link.model.get('status:host'), 'starting')

    def test_link_disconnects_when_idle(self):
        self.link = self.make_link(lambda: True)
        self.link.add_viewer()
        self.assertTrue(wait_for(lambda: self.link.client.connected))
        self.link.remove_viewer()
        self.clock[0] += 11.0
        self.assertTrue(wait_for(lambda: not self.link.client.connected))
        self.assertEqual(self.link.model.get('status:host'), 'idle')


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError: No module named 'kiwi_web.server'`.

- [ ] **Step 3: Implement** `web/kiwi_web/server.py`

```python
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


def systemd_units_ready():
    result = subprocess.run(['systemctl', 'is-active', '--quiet', 'kiwi-patch', 'kiwi-restore'])
    return result.returncode == 0


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
```

- [ ] **Step 4: Entry script** `web/kiwi-web`

```python
#!/usr/bin/env python3
"""Starts the kiwi web UI (see kiwi_web/server.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kiwi_web.server import main  # noqa: E402

sys.exit(main())
```

Then `chmod +x web/kiwi-web`.

- [ ] **Step 5: Run tests** — Expected: all pass (≈ 40 tests).

- [ ] **Step 6: Commit** — `git add web && git commit -m "Web UI: server, host link, restore mode" && git push`

---

### Task 8: Mobile-first kiwi page

**Files:**
- Modify: `web/static/index.html` (replace placeholder)

**Interfaces:**
- Consumes: Task 7 HTTP API and model keys; `params.json` schema (Task 6 + `baseline`, `cc_map`).

- [ ] **Step 1: Write** `web/static/index.html` — the complete page:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#5B3A1E">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="kiwi">
<title>kiwi</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='-50 -50 100 100'%3E%3Ccircle r='48' fill='%235B3A1E'/%3E%3Ccircle r='42' fill='%238DC63F'/%3E%3Ccircle r='13' fill='%23F3F0D7'/%3E%3C/svg%3E">
<style>
:root {
  --skin: #5B3A1E; --fuzz: #8A6A45; --flesh: #8DC63F; --flesh-hi: #C5E17A;
  --core: #F3F0D7; --seed: #1E1A14; --bg: #170f08; --card: #26180c; --line: #4a3420;
  --muted: #bcaa8a; --warn: #e8a33c; --bad: #e0573a;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--core);
  font: 16px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-left: env(safe-area-inset-left); padding-right: env(safe-area-inset-right);
}
header {
  position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 10px;
  padding: calc(8px + env(safe-area-inset-top)) 16px 8px; background: var(--skin);
  border-bottom: 3px solid var(--fuzz);
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 20px; }
.brand svg { width: 28px; height: 28px; }
.spacer { flex: 1; }
.stat { font-size: 14px; font-variant-numeric: tabular-nums; }
.dot { width: 12px; height: 12px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.online { background: var(--flesh); }
.dot.offline { background: var(--bad); }
.dot.starting, .dot.idle { background: var(--warn); }
.dot.pulse { background: var(--flesh-hi); box-shadow: 0 0 0 3px rgba(197,225,122,.35); }
main {
  display: grid; grid-template-columns: 1fr; gap: 12px; max-width: 1200px; margin: 0 auto;
  padding: 12px 12px calc(84px + env(safe-area-inset-bottom));
}
.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 12px 14px; min-width: 0; }
.card h2, details.card > summary {
  margin: 0 0 6px; font-size: 14px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1.5px; color: var(--flesh-hi);
}
.modes { display: grid; grid-template-columns: 1fr; gap: 10px; }
.mode { border-top: 1px solid var(--line); padding-top: 6px; }
.mode:first-child { border-top: 0; padding-top: 0; }
.mode h3 { margin: 0; font-size: 17px; }
.row { display: grid; grid-template-columns: minmax(5.5em, 8em) 1fr 3.4em; align-items: center; gap: 10px; min-height: 48px; }
.row.choice { grid-template-columns: minmax(5.5em, 8em) 1fr; }
.row label { font-size: 16px; overflow-wrap: anywhere; }
.row label small { display: block; font-size: 12px; color: var(--muted); }
.row output { text-align: right; font-size: 14px; color: var(--muted); font-variant-numeric: tabular-nums; }
.row.differs label::before { content: "● "; color: var(--flesh); font-size: 12px; }
.row.unknown output { color: var(--fuzz); }
input[type=range] {
  -webkit-appearance: none; appearance: none; width: 100%; height: 48px; margin: 0;
  background: transparent; touch-action: none; --fill: 0%;
}
input[type=range]:focus-visible { outline: 2px solid var(--flesh-hi); outline-offset: 2px; border-radius: 8px; }
input[type=range]::-webkit-slider-runnable-track {
  height: 10px; border-radius: 5px;
  background: linear-gradient(90deg, var(--flesh) var(--fill), var(--line) var(--fill));
}
input[type=range]::-moz-range-track { height: 10px; border-radius: 5px; background: var(--line); }
input[type=range]::-moz-range-progress { height: 10px; border-radius: 5px; background: var(--flesh); }
input[type=range]::-webkit-slider-thumb {
  -webkit-appearance: none; width: 24px; height: 34px; margin-top: -12px;
  border-radius: 50% / 60% 60% 40% 40%; background: var(--seed); border: 2px solid var(--core);
}
input[type=range]::-moz-range-thumb {
  width: 24px; height: 34px; border-radius: 50% / 60% 60% 40% 40%;
  background: var(--seed); border: 2px solid var(--core);
}
.unknown input[type=range]::-webkit-slider-thumb { opacity: .45; }
.unknown input[type=range]::-moz-range-thumb { opacity: .45; }
.blend-labels { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); margin: -6px 0 0; }
.seg { display: flex; flex-wrap: wrap; gap: 6px; }
button, select {
  font: inherit; font-size: 16px; min-height: 44px; min-width: 44px; border-radius: 12px;
  border: 1px solid var(--fuzz); background: var(--skin); color: var(--core); padding: 0 14px;
}
button.on { background: var(--flesh); border-color: var(--flesh); color: var(--seed); font-weight: 700; }
select { width: 100%; }
.midi { display: flex; align-items: center; gap: 14px; }
.midi svg { width: 104px; height: 104px; flex: none; }
.seed { fill: var(--seed); transition: fill 80ms; }
.seed.on { fill: var(--core); }
.last { font-size: 15px; color: var(--muted); min-height: 2.8em; }
.ccs { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; margin-top: 10px; }
.cc { font-size: 12px; text-align: center; color: var(--muted); }
.cc i { display: block; position: relative; height: 32px; border-radius: 6px; background: var(--line); overflow: hidden; margin-bottom: 2px; }
.cc i b { position: absolute; left: 0; right: 0; bottom: 0; height: 0; background: var(--flesh); }
details.card > summary {
  list-style: none; display: flex; align-items: center; min-height: 44px; margin: -4px 0; cursor: pointer;
}
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::after { content: "+"; margin-left: auto; font-size: 24px; color: var(--core); }
details.card[open] > summary::after { content: "−"; }
details.card[open] > summary { margin-bottom: 6px; }
.group { margin: 10px 0 0; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
.note { font-size: 13px; color: var(--muted); margin: 4px 0 8px; }
footer {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 2; display: flex; align-items: center; gap: 12px;
  padding: 8px 16px calc(8px + env(safe-area-inset-bottom)); background: var(--skin);
  border-top: 3px solid var(--fuzz); font-size: 14px;
}
@media (min-width: 768px) {
  main { grid-template-columns: 1fr 1fr; padding-left: 16px; padding-right: 16px; }
  .modes { grid-template-columns: repeat(3, 1fr); }
  .mode { border-top: 0; padding-top: 0; }
  .wide { grid-column: 1 / -1; }
}
@media (min-width: 1100px) { main { grid-template-columns: repeat(3, 1fr); } }
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg viewBox="-50 -50 100 100" aria-hidden="true"><circle r="48" style="fill:var(--fuzz)"/><circle r="42" style="fill:var(--flesh)"/><circle r="13" style="fill:var(--core)"/></svg>
    kiwi
  </div>
  <span class="dot" id="hostdot" title="audio host"></span>
  <span class="spacer"></span>
  <span class="stat" id="cpu">cpu –</span>
  <span class="stat" id="temp">–°C</span>
  <span class="dot" id="mididot" title="MIDI in"></span>
</header>
<main id="main">
  <section class="card wide">
    <h2>Mix</h2>
    <div class="modes" id="modes"></div>
    <div id="blend"></div>
  </section>
  <section class="card">
    <h2>MIDI in</h2>
    <div class="midi">
      <svg viewBox="-50 -50 100 100" id="slice" aria-hidden="true"></svg>
      <div class="last" id="last">no MIDI yet</div>
    </div>
    <div class="ccs" id="ccs"></div>
  </section>
</main>
<footer>
  <span id="saved">autosave on</span>
  <span class="spacer"></span>
  <button type="button" id="reset">Reset to patch</button>
</footer>
<script>
'use strict';
const $ = (id) => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const values = new Map();
const controls = new Map();
const pending = new Map();
let meta = null;
let source = null;
let dragging = null;
let flushTimer = null;
let lastTap = { key: null, time: 0 };

function keyOf(plugin, param) {
  return plugin.kind === 'port'
    ? `port:${plugin.instance}:${param.symbol}`
    : `patch:${plugin.instance}:${param.uri}`;
}

function format(param, v) {
  if (v === null || v === undefined) return '–';
  if (param.type === 'toggle') return v >= 0.5 ? 'on' : 'off';
  if (param.options) {
    const option = param.options.find(([value]) => Math.abs(value - v) < 1e-6);
    if (option) return option[1];
  }
  if (param.type === 'int') return String(Math.round(v));
  const span = param.max - param.min;
  return span <= 2 ? v.toFixed(2) : span <= 200 ? v.toFixed(1) : String(Math.round(v));
}

function ccFor(instance, symbol) {
  return meta.cc_map.find((m) => m.instance === instance && m.symbol === symbol);
}

function makeControl(plugin, param, label) {
  const key = keyOf(plugin, param);
  const row = document.createElement('div');
  row.className = 'row';
  const lab = document.createElement('label');
  lab.textContent = label || param.name;
  if (plugin.kind === 'port') {
    const cc = ccFor(plugin.instance, param.symbol);
    if (cc) {
      const small = document.createElement('small');
      small.textContent = `CC ${cc.cc}`;
      lab.appendChild(small);
    }
  }
  let input;
  let output = null;
  const choices = param.type === 'toggle'
    ? [[param.min, 'off'], [param.max, 'on']]
    : param.options;
  if (choices && choices.length <= 4) {
    row.classList.add('choice');
    input = document.createElement('div');
    input.className = 'seg';
    for (const [value, text] of choices) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      button.dataset.value = value;
      button.addEventListener('click', () => change(key, value));
      input.appendChild(button);
    }
  } else if (choices) {
    row.classList.add('choice');
    input = document.createElement('select');
    for (const [value, text] of choices) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      input.appendChild(option);
    }
    input.addEventListener('change', () => change(key, parseFloat(input.value)));
  } else {
    input = document.createElement('input');
    input.type = 'range';
    input.min = param.min;
    input.max = param.max;
    input.step = param.type === 'int' ? 1 : (param.max - param.min) / 1000;
    input.id = `c${controls.size}`;
    lab.htmlFor = input.id;
    input.addEventListener('pointerdown', () => { dragging = key; });
    const release = () => {
      if (dragging === key) dragging = null;
      const now = performance.now();
      if (lastTap.key === key && now - lastTap.time < 300) change(key, param.baseline);
      lastTap = { key, time: now };
    };
    input.addEventListener('pointerup', release);
    input.addEventListener('pointercancel', () => { dragging = null; });
    input.addEventListener('input', () => change(key, parseFloat(input.value)));
    output = document.createElement('output');
  }
  row.append(lab, input);
  if (output) row.append(output);
  const control = { key, row, input, output, param, plugin };
  controls.set(key, control);
  render(control);
  return row;
}

function render(control) {
  const { param, input, output, row } = control;
  const v = values.has(control.key) ? values.get(control.key) : null;
  row.classList.toggle('unknown', v === null);
  const span = Math.abs(param.max - param.min) || 1;
  row.classList.toggle('differs', v !== null && Math.abs(v - param.baseline) > span * 1e-4);
  if (input.tagName === 'INPUT') {
    const shown = v === null ? param.baseline : v;
    if (dragging !== control.key) input.value = shown;
    input.style.setProperty('--fill', `${((shown - param.min) / span) * 100}%`);
    output.textContent = format(param, v);
  } else if (input.tagName === 'SELECT') {
    if (v !== null) input.value = String(v);
  } else {
    for (const button of input.children) {
      button.classList.toggle('on', v !== null && Math.abs(parseFloat(button.dataset.value) - v) < 1e-6);
    }
  }
}

function setValue(key, v) {
  values.set(key, v);
  const control = controls.get(key);
  if (control) render(control);
}

function change(key, v) {
  const control = controls.get(key);
  setValue(key, v);
  const item = { instance: control.plugin.instance, value: v };
  if (control.plugin.kind === 'port') item.symbol = control.param.symbol;
  else item.uri = control.param.uri;
  pending.set(key, item);
  if (!flushTimer) flushTimer = setTimeout(flush, 50);
}

async function flush() {
  flushTimer = null;
  const changes = [...pending.values()];
  pending.clear();
  if (!changes.length) return;
  try {
    await fetch('/set', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ changes }) });
  } catch (error) {
    setHost('offline');
  }
}

function section(title, build, note) {
  const details = document.createElement('details');
  details.className = 'card';
  const summary = document.createElement('summary');
  summary.textContent = title;
  details.appendChild(summary);
  if (note) {
    const p = document.createElement('p');
    p.className = 'note';
    p.textContent = note;
    details.appendChild(p);
  }
  details.addEventListener('toggle', () => {
    if (details.open && !details.dataset.built) {
      details.dataset.built = '1';
      build(details);
    }
  });
  $('main').appendChild(details);
}

function addGrouped(container, plugin, params) {
  let group = null;
  const sorted = [...params].sort((a, b) => (a.group || '').localeCompare(b.group || ''));
  for (const param of sorted) {
    if ((param.group || '') !== group) {
      group = param.group || '';
      if (group) {
        const heading = document.createElement('div');
        heading.className = 'group';
        heading.textContent = group;
        container.appendChild(heading);
      }
    }
    container.appendChild(makeControl(plugin, param));
  }
}

function build() {
  const plugin = (instance) => meta.plugins.find((p) => p.instance === instance);
  const mix = plugin(5);
  for (const [title, prefix] of [['Piano', 'piano'], ['Sampler', 'sampler'], ['Vocoder', 'vocoder']]) {
    const card = document.createElement('div');
    card.className = 'mode';
    const heading = document.createElement('h3');
    heading.textContent = title;
    card.appendChild(heading);
    for (const [suffix, label] of [['vol', 'Volume'], ['send', 'Reverb']]) {
      const param = mix.params.find((p) => p.symbol === `${prefix}_${suffix}`);
      if (param) card.appendChild(makeControl(mix, param, label));
    }
    $('modes').appendChild(card);
  }
  const carrier = plugin(3);
  const blend = carrier.params.find((p) => p.symbol === 'blend');
  $('blend').appendChild(makeControl(carrier, blend, 'Vocoder carrier'));
  const labels = document.createElement('div');
  labels.className = 'blend-labels';
  for (const text of ['synth', 'piano', 'sampler']) {
    const span = document.createElement('span');
    span.textContent = text;
    labels.appendChild(span);
  }
  $('blend').appendChild(labels);

  for (const m of meta.cc_map) {
    const cell = document.createElement('div');
    cell.className = 'cc';
    cell.innerHTML = `<i><b id="cc${m.cc}"></b></i>${m.cc}`;
    $('ccs').appendChild(cell);
  }

  for (const p of meta.plugins) {
    if (p.instance === 5 || p.instance === 3) continue;
    if (p.kind === 'patch') {
      const note = 'Pianoteq values are shown once set from this page.';
      section('Pianoteq', (d) => addGrouped(d, p, p.params.filter((q) => q.curated)), note);
      section('Pianoteq · all parameters', (d) => addGrouped(d, p, p.params), note);
    } else {
      section(p.title, (d) => addGrouped(d, p, p.params));
    }
  }
}

function drawSlice() {
  const svg = $('slice');
  const shape = (tag, attrs) => {
    const el = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
    svg.appendChild(el);
    return el;
  };
  shape('circle', { r: 48, style: 'fill:var(--fuzz)' });
  shape('circle', { r: 43, style: 'fill:var(--flesh)' });
  shape('circle', { r: 30, style: 'fill:var(--flesh-hi);opacity:.35' });
  shape('circle', { r: 13, style: 'fill:var(--core)' });
  for (let i = 0; i < 12; i++) {
    const angle = i * 30 - 90;
    const x = 23 * Math.cos(angle * Math.PI / 180);
    const y = 23 * Math.sin(angle * Math.PI / 180);
    const seed = shape('ellipse', { cx: x, cy: y, rx: 3, ry: 6, class: 'seed', id: `seed${i}`,
                                    transform: `rotate(${angle + 90} ${x} ${y})` });
    seed.appendChild(document.createElementNS(SVG, 'title'));
  }
}

function lightSeeds(notes) {
  const classes = new Set(notes.map((n) => n % 12));
  for (let i = 0; i < 12; i++) $(`seed${i}`).classList.toggle('on', classes.has(i));
}

function setHost(state) {
  $('hostdot').className = `dot ${state}`;
  $('hostdot').title = `audio host: ${state}`;
}

let midiPulse = null;
function pulseMidi() {
  $('mididot').classList.add('pulse');
  clearTimeout(midiPulse);
  midiPulse = setTimeout(() => $('mididot').classList.remove('pulse'), 150);
}

function apply(delta) {
  for (const [key, v] of Object.entries(delta)) {
    if (key.startsWith('port:') || key.startsWith('patch:')) {
      if (key !== dragging && !pending.has(key)) setValue(key, v);
    } else if (key === 'status:host') {
      setHost(v);
    } else if (key === 'status:cpu') {
      $('cpu').textContent = v === null ? 'cpu –' : `cpu ${v}%`;
    } else if (key === 'status:temp') {
      $('temp').textContent = v === null ? '–°C' : `${v}°C`;
    } else if (key === 'status:saved') {
      $('saved').textContent = `saved ${new Date(v * 1000).toLocaleTimeString()}`;
    } else if (key === 'midi:notes') {
      lightSeeds(v);
    } else if (key === 'midi:last') {
      $('last').textContent = v;
      pulseMidi();
    } else if (key.startsWith('midi:cc:')) {
      const bar = $(`cc${key.slice(8)}`);
      if (bar) bar.style.height = `${(v / 127) * 100}%`;
    }
  }
}

function connect() {
  if (source || !meta) return;
  source = new EventSource('/events');
  source.onmessage = (event) => apply(JSON.parse(event.data));
  source.onerror = () => setHost('offline');
}

function disconnect() {
  if (source) {
    source.close();
    source = null;
  }
}

document.addEventListener('visibilitychange', () => (document.hidden ? disconnect() : connect()));

$('reset').addEventListener('click', async () => {
  if (!confirm('Reset every value to the patch defaults?')) return;
  await fetch('/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
});

fetch('/params.json')
  .then((response) => response.json())
  .then((data) => {
    meta = data;
    build();
    drawSlice();
    if (!document.hidden) connect();
  });
</script>
</body>
</html>
```

- [ ] **Step 2: Run tests** — `python3 -m unittest discover -s web/tests -t web -v` → all pass (index is served as a file; content not asserted beyond `kiwi`).

- [ ] **Step 3: Check the layout locally against a fake host**

Write a throwaway driver in the scratchpad directory (not in the repo) with the Write tool: it adds `web/` to `sys.path`, starts `tests.fakehost.FakeHost` with every port parameter of the real metadata at its default, writes nothing else, and runs `App` + `make_server(('127.0.0.1', 8080), app)` the same way `test_server.py` does (`units_ready=lambda: True`, a sleeping `monitor_command`). The real metadata comes from the Pi: copy the output of `ssh patch@kiwi.local 'cd ~/kiwi && python3 web/tools/gen_params.py'` into the scratchpad with the Write tool. Open `http://127.0.0.1:8080/` with the browser tools at 360×740, 740×360, 768×1024 and 1280×800. Expected: no horizontal scroll; slider hit areas ≥ 44 px tall; label text ≥ 16 px; sections expand; moving a slider updates its number.

- [ ] **Step 4: Commit** — `git add web/static/index.html && git commit -m "Web UI: mobile-first kiwi page" && git push`

---

### Task 9: Services, install, on-device verification

**Files:**
- Create: `system/kiwi-web.service`, `system/kiwi-restore.service`
- Modify: `install.sh` (add "Web UI" section before the final echo), `README.md` (web UI section, rsync exclude), `docs/superpowers/specs/2026-10-03-kiwi-webui-design.md` (Reset is live; params generated at install)

- [ ] **Step 1: Write** `system/kiwi-restore.service`

```ini
[Unit]
Description=Kiwi autosaved state restore
After=kiwi-patch.service
BindsTo=kiwi-host.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=patch
ExecStart=/usr/bin/python3 /home/patch/kiwi/web/kiwi-web --restore

[Install]
WantedBy=kiwi-host.service
```

- [ ] **Step 2: Write** `system/kiwi-web.service`

```ini
[Unit]
Description=Kiwi web UI
After=network.target kiwi-host.service

[Service]
User=patch
ExecStart=/usr/bin/python3 /home/patch/kiwi/web/kiwi-web
# Never compete with audio: idle CPU and I/O class, capped memory.
Nice=19
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
MemoryMax=64M
TasksMax=32
AmbientCapabilities=CAP_NET_BIND_SERVICE
ProtectSystem=strict
ReadWritePaths=/home/patch/.local/state/kiwi
PrivateTmp=yes
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Add the install section** (Edit tool, before the final `echo`)

```bash
section "Web UI"
python3 "$KIWI_DIR/web/tools/gen_params.py" > "$KIWI_DIR/web/params.json.tmp"
mv "$KIWI_DIR/web/params.json.tmp" "$KIWI_DIR/web/params.json"
mkdir -p "$HOME/.local/state/kiwi"
sudo install -m 644 "$KIWI_DIR/system/kiwi-web.service" "$KIWI_DIR/system/kiwi-restore.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiwi-restore kiwi-web
sudo systemctl start kiwi-restore
sudo systemctl restart kiwi-web
```

- [ ] **Step 4: Deploy and verify services**

```bash
rsync … (Global Constraints, includes --exclude web/params.json)
ssh patch@kiwi.local 'cd ~/kiwi && ./install.sh > /tmp/kiwi-install.log 2>&1; echo install=$?; systemctl is-active kiwi-web kiwi-restore; journalctl -u kiwi-restore -b -o cat | tail -2; ps -o pid,cls,ni,rss,cmd -C python3'
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://kiwi.local/
curl -s http://kiwi.local/params.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['plugins']))"
```
Expected: `install=0`, both `active`, restore says `nothing to restore`, python3 listed with class `IDL` and nice 19, RSS < 30 MB; `200` and a size under 40 KB; `8` plugins.

- [ ] **Step 5: Verify behaviour on device**

1. Open `http://patchbox.local/` (or `http://kiwi.local/`) on the Mac with the browser tools at 390×844. Move "Piano Volume" → `ssh … 'exec 3<>/dev/tcp/127.0.0.1/5555; printf "param_get 5 piano_vol\0" >&3; IFS= read -r -d "" r <&3; echo $r'` shows the new value.
2. Send CC 20 with `host/kiwi-stress`'s virtual port: `amidi -p hw:4,0 -S "B0 14 20"` (after `sudo modprobe snd-virmidi midi_devs=1` and `aconnect <virmidi>:0 "Midi Through":0`) → the page's Piano Volume moves within ~200 ms and the CC 20 bar shows 32/127.
3. Wait 6 s → `~/.local/state/kiwi/state.json` contains the changed values. `sudo systemctl restart kiwi-host` → after the patch loads, `journalctl -u kiwi-restore -b -o cat | tail -1` says `restored N value(s), 0 failure(s)` and `param_get 5 piano_vol` matches; the open page reconnects (green dot) without reload.
4. Reset to patch → values return to the patch, state file gone.
5. Close the page → within 15 s `ss -tnp | grep 5555` shows no python3 connection and `pgrep -x aseqdump` is empty.
6. With two pages open (two tabs), `~/kiwi/host/kiwi-stress 600` → `0 xrun/process-error lines`.
7. From outside private networks it can't be tested here; confirm the check with the unit tests.

- [ ] **Step 6: Update README** (Edit tool): add a *Web UI* section (URL, what it shows, autosave and Reset, mobile home-screen tip, resource limits, `kiwi-web.service`/`kiwi-restore.service`), and add `--exclude web/params.json` to the deploy rsync command. Update the spec's §6 Reset bullet to "re-applies the patch values live (no restart)" and §4's "generated once … committed" to "generated by install.sh on the Pi".

- [ ] **Step 7: Commit** — `git add system install.sh README.md docs web && git commit -m "Web UI: services, install and docs" && git push`

---

### Task 10: Pianoteq preset spike (phase 3 gate)

**Files:**
- Create: `docs/superpowers/specs/2026-10-03-preset-spike.md` (findings)

This task produces an answer, not shipped code.

- [ ] **Step 1: Export presets**

```bash
ssh patch@kiwi.local 'mkdir -p ~/kiwi-data && nice -n 19 ionice -c3 "/home/patch/.vst/Pianoteq 8" --export-lv2-presets ~/kiwi-data/pianoteq-presets --export-presets-filter all; ls ~/kiwi-data/pianoteq-presets | head; ls ~/kiwi-data/pianoteq-presets | wc -l'
```
Expected: one or more `.lv2` bundles with presets. Record the layout.

- [ ] **Step 2: Load one preset live and time it**

With `kiwi-web` stopped (`sudo systemctl stop kiwi-web`) to free the socket:
```bash
ssh patch@kiwi.local 'exec 3<>/dev/tcp/127.0.0.1/5555; s(){ printf "%s\0" "$1" >&3; IFS= read -r -d "" r <&3; echo "$1 -> $r"; }; s "bundle_add /home/patch/kiwi-data/pianoteq-presets/<bundle>.lv2"; t=$(date +%s%N); s "preset_load 0 <preset uri>"; echo "ms: $(( ($(date +%s%N)-t)/1000000 ))"'
```
(Fill `<bundle>` and `<preset uri>` from Step 1's manifest.) Run while `host/kiwi-stress 60` plays in a second ssh session.

- [ ] **Step 3: Record and decide**

Write `docs/superpowers/specs/2026-10-03-preset-spike.md`: export layout, `bundle_add` result, load time, xrun count during switching (with and without the mute sequence), whether Pianoteq's reverb turned back on, any crash. Recommend ship / ship-with-mute / don't ship. Commit, restart `kiwi-web`, and report to the user before any preset UI is built.

---

## Self-review notes

- Spec §1 criteria → Task 9 Step 5 (idle cost, stress with two pages, CC latency, autosave, reset); presets → Task 10 gate.
- Spec §3.3 restore → Tasks 7 (`restore`) + 9 (unit).
- Spec §4 parameter list → Task 6 `PORT_PLUGINS`, `PIANOTEQ_*`.
- Spec mobile-first → Task 8 CSS (single column default, 44–48 px targets, 16 px text, no hover) + Task 8 Step 3 layout check.
- Deviations from the spec, to be written back in Task 9 Step 6: Reset is live (no host restart, no sudo); params.json generated at install time (git-ignored) instead of committed.
