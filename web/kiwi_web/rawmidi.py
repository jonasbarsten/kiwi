"""Sends MIDI control changes through the kernel's virtual raw MIDI device.

snd-virmidi gives a raw MIDI device (/dev/snd/midiC<n>D0) whose ALSA sequencer
port amidiminder connects to Midi Through, so bytes written here reach every
instrument like any other MIDI source. Used for parameters that a plugin only
offers as CC modulation (the sampler's envelope in kiwi.sfz).
"""
import re

_CARD = re.compile(r'^\s*(\d+)\s+\[VirMIDI\s*\]', re.M)


def find_device(cards_text=None):
    """The raw MIDI device of the VirMIDI card, from /proc/asound/cards."""
    if cards_text is None:
        try:
            with open('/proc/asound/cards') as f:
                cards_text = f.read()
        except OSError:
            return None
    match = _CARD.search(cards_text)
    return f'/dev/snd/midiC{match.group(1)}D0' if match else None


def to_7bit(value, minimum, maximum):
    span = maximum - minimum
    position = (value - minimum) / span if span else 0.0
    return max(0, min(127, round(position * 127)))


class RawMidi:
    def __init__(self, path):
        self.path = path
        self._file = None

    def _open(self):
        if self._file is None and self.path:
            self._file = open(self.path, 'wb', buffering=0)
        return self._file

    def cc(self, channel, number, value):
        """Writes one control change; returns False (and logs) if the device is unusable."""
        try:
            self._open().write(bytes([0xB0 | (channel & 0x0F), number & 0x7F, value & 0x7F]))
            return True
        except (OSError, AttributeError) as error:
            print(f'kiwi-web: MIDI device {self.path}: {error}', flush=True)
            self.close()
            return False

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
