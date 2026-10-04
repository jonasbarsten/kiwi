"""The state format Pianoteq 8's LV2 plugin actually restores.

Pianoteq 8.3.2 exports its presets (`--export-lv2-presets`) as
`<urn:juce:stateBinary> <base64>^^xsd:base64Binary`, a small "LEAN-PRESET"
blob that names the preset. The 8.3.2 plugin, however, only looks for the key
`<https://www.modartt.com/lv2/Pianoteq8:StateString>`, a JUCE-base64 string
(this is the one key it writes when a host asks it to save). Loading an exported
preset therefore succeeds in every host and changes nothing.

What the plugin saves, decoded, is a 16-byte header, a 12-byte inner header and
the same kind of PrVK blob. The two unexplained words are constants: they were
identical for states with different contents. An exported blob wrapped the same
way loads (verified on the device: the plugin then reports the new preset name).
"""
import base64
import re
import struct

STATE_KEY = 'https://www.modartt.com/lv2/Pianoteq8:StateString'
EXPORTED_KEY = 'urn:juce:stateBinary'

_MAGIC = 0xFE1398A0
_VERSION = 2
_HEADER_WORD = 0x123848
_INNER_COUNT = 1
_INNER_TAG = 0x38563811

# JUCE MemoryBlock::toBase64Encoding: "<size>." then 6-bit groups taken LSB-first.
_TABLE = '.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+'

_EXPORTED = re.compile(r'<' + re.escape(EXPORTED_KEY) + r'>\s*"""(.*?)"""\^\^xsd:base64Binary', re.S)


def juce_base64(data):
    """`data` as JUCE's MemoryBlock::toBase64Encoding() writes it."""
    bits = int.from_bytes(data, 'little')
    chars = (len(data) * 8 + 5) // 6
    return f'{len(data)}.' + ''.join(_TABLE[(bits >> (6 * i)) & 63] for i in range(chars))


def wrap_state(blob):
    """The exported PrVK blob inside the headers the plugin puts around its own state."""
    inner = struct.pack('<III', _INNER_COUNT, _INNER_TAG, len(blob)) + blob
    return struct.pack('<IIII', _MAGIC, _VERSION, _HEADER_WORD, len(inner)) + inner


def state_string(blob):
    return juce_base64(wrap_state(blob))


def exported_blob(ttl):
    """The PrVK blob of an exported preset file's text; None if it has none."""
    match = _EXPORTED.search(ttl)
    if not match:
        return None
    return base64.b64decode(re.sub(r'\s', '', match.group(1)))
