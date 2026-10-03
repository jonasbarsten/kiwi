import unittest

from kiwi_web import reverbs


class RegistryTest(unittest.TestCase):
    def test_ids_unique_and_complete(self):
        ids = [r['id'] for r in reverbs.REVERBS]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in reverbs.REVERBS:
            self.assertEqual(len(entry['inputs']), 2, entry['id'])
            self.assertEqual(len(entry['outputs']), 2, entry['id'])
            self.assertTrue(entry['locked'], f"{entry['id']} has no fully-wet setting")

    def test_find(self):
        self.assertEqual(reverbs.find('zita')['name'], 'Zita Rev1')
        self.assertIsNone(reverbs.find('nope'))
        self.assertEqual(reverbs.find_by_uri('urn:dragonfly:plate')['id'], 'plate')

    def test_switch_commands_order(self):
        commands = reverbs.switch_commands(reverbs.find('calf'), {'decay_time': 0.9})
        self.assertEqual(commands, [
            'remove 6',
            'add http://calf.sourceforge.net/plugins/Reverb 6',
            'param_set 6 dry 0.000000',
            'param_set 6 on 1.000000',
            'param_set 6 decay_time 0.900000',
            'connect effect_5:send_l effect_6:in_l',
            'connect effect_5:send_r effect_6:in_r',
            'connect effect_6:out_l system:playback_1',
            'connect effect_6:out_r system:playback_2',
        ])


if __name__ == '__main__':
    unittest.main()
