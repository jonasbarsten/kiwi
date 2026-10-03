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
