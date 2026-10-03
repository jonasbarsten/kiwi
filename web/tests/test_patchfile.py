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
