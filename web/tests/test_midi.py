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

    def test_restarts_child_that_exits(self):
        events = []
        twice = threading.Event()

        def on_event(event):
            events.append(event)
            if len(events) >= 2:
                twice.set()

        script = 'import sys; sys.stdout.write(%r)' % (LINES[3] + '\n')
        monitor = MidiMonitor(on_event, command=[sys.executable, '-c', script], restart_delay=0.1)
        monitor.start()
        self.assertTrue(twice.wait(5), 'monitor did not restart its child')
        monitor.stop()
        count = len(events)
        threading.Event().wait(0.5)
        self.assertEqual(len(events), count, 'monitor kept restarting after stop')

    def test_default_command_lets_amidiminder_connect(self):
        self.assertEqual(MidiMonitor(lambda e: None).command, ['aseqdump'])


if __name__ == '__main__':
    unittest.main()
