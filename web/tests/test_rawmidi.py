import os
import tempfile
import unittest

from kiwi_web.rawmidi import RawMidi, find_device, to_7bit

CARDS = """ 3 [pisound        ]: pisound - pisound
                      pisound
 4 [VirMIDI        ]: VirMIDI - VirMIDI
                      Virtual MIDI Card 1
"""


class RawMidiTest(unittest.TestCase):
    def test_find_device_by_card_id(self):
        self.assertEqual(find_device(CARDS), '/dev/snd/midiC4D0')
        self.assertIsNone(find_device(' 3 [pisound ]: pisound - pisound\n'))

    def test_to_7bit(self):
        self.assertEqual(to_7bit(0.0, 0.0, 4.0), 0)
        self.assertEqual(to_7bit(4.0, 0.0, 4.0), 127)
        self.assertEqual(to_7bit(2.0, 0.0, 4.0), 64)
        self.assertEqual(to_7bit(9.0, 0.0, 4.0), 127)
        self.assertEqual(to_7bit(-1.0, 0.0, 4.0), 0)

    def test_cc_writes_three_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'midi')
            midi = RawMidi(path)
            self.assertTrue(midi.cc(0, 102, 64))
            self.assertTrue(midi.cc(2, 105, 127))
            midi.close()
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b'\xb0\x66\x40\xb2\x69\x7f')

    def test_missing_device_is_reported_not_raised(self):
        midi = RawMidi('/nonexistent/midi')
        self.assertFalse(midi.cc(0, 102, 1))


if __name__ == '__main__':
    unittest.main()
