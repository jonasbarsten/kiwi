import struct
import unittest

from kiwi_web.pianoteq_state import (STATE_KEY, exported_blob, juce_base64, state_string, version_warning,
                                     wrap_state)

# The first 16 bytes of a state the plugin saved, and how it encoded them
# (the plugin's string starts "12965.fh4D9K.....R3HA.UJC..D"; 21 characters
# cover 126 bits, so they are the same for any longer input).
PLUGIN_HEADER = bytes.fromhex('a09813fe' '02000000' '48381200' '95320000')
PLUGIN_ENCODED_PREFIX = 'fh4D9K.....R3HA.UJC..'

EXPORTED = '''<>
	a pset:Preset ;
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	rdfs:label "NY Steinway D Bowed" ;
	state:state [
		<urn:juce:stateBinary> """
	UHJWSyYAAAJYRlRQ
	AQIDBA==
"""^^xsd:base64Binary
] .
'''


class JuceBase64Test(unittest.TestCase):
    def test_matches_the_plugin(self):
        encoded = juce_base64(PLUGIN_HEADER)
        self.assertTrue(encoded.startswith('16.'))
        self.assertEqual(encoded[3:24], PLUGIN_ENCODED_PREFIX)

    def test_size_prefix_and_length(self):
        self.assertEqual(juce_base64(b''), '0.')
        self.assertEqual(juce_base64(b'\x00'), '1...')          # 8 bits -> 2 groups
        self.assertEqual(juce_base64(b'\x3f'), '1.+.')          # low 6 bits first
        self.assertEqual(len(juce_base64(b'x' * 300)), len('300.') + 400)


class WrapStateTest(unittest.TestCase):
    def test_headers_as_the_plugin_writes_them(self):
        blob = b'PrVK&\x00\x00\x02XFTP'
        wrapped = wrap_state(blob)
        magic, version, word, length = struct.unpack('<IIII', wrapped[:16])
        self.assertEqual((magic, version, word), (0xFE1398A0, 2, 0x123848))
        self.assertEqual(length, len(wrapped) - 16)
        count, tag, inner_length = struct.unpack('<III', wrapped[16:28])
        self.assertEqual((count, tag, inner_length), (1, 0x38563811, len(blob)))
        self.assertEqual(wrapped[28:], blob)
        # The plugin's own 12965-byte state has these lengths.
        self.assertEqual(wrap_state(b'x' * 12937)[12:16], (12949).to_bytes(4, 'little'))

    def test_state_string_is_the_wrapped_blob(self):
        self.assertEqual(state_string(b'abc'), juce_base64(wrap_state(b'abc')))
        self.assertIn(':StateString', STATE_KEY)


class VersionWarningTest(unittest.TestCase):
    def test_verified_version_is_quiet(self):
        self.assertIsNone(version_warning('Pianoteq version 8.3.2/20240923 -- http://www.modartt.com/pianoteq\n'))

    def test_other_versions_warn(self):
        self.assertIn('8.4.0', version_warning('Pianoteq version 8.4.0/20250101'))
        self.assertIn('unknown', version_warning(''))


class ExportedBlobTest(unittest.TestCase):
    def test_decodes_across_whitespace(self):
        self.assertEqual(exported_blob(EXPORTED), b'PrVK&\x00\x00\x02XFTP\x01\x02\x03\x04')

    def test_missing(self):
        self.assertIsNone(exported_blob('<> a pset:Preset .'))


if __name__ == '__main__':
    unittest.main()
