import unittest

from kiwi_web.patchfile import parse_patch

SAMPLE = """# comment
add https://www.modartt.com/lv2/Pianoteq8 0
add https://github.com/jonasbarsten/kiwi#mix 5

bundle_add /home/patch/kiwi-data/pianoteq-presets-lv2/petrof.lv2/
preset_load 0 file:///home/patch/kiwi-data/pianoteq-presets-lv2/petrof.lv2/ant-petrof-warm.ttl
patch_set 0 https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch 0
param_set 6 decay 1.6
connect @MIDI_IN@ effect_0:in
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

    def test_preset_load(self):
        patch = parse_patch(SAMPLE)
        self.assertEqual(patch.presets,
                         {0: 'file:///home/patch/kiwi-data/pianoteq-presets-lv2/petrof.lv2/ant-petrof-warm.ttl'})
        self.assertEqual(parse_patch('connect a b\n').presets, {})

    def test_ignores_comments_and_connects(self):
        patch = parse_patch('# add x 1\nconnect a b\n')
        self.assertEqual(patch.instances, {})


if __name__ == '__main__':
    unittest.main()
