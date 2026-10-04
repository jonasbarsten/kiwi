import json
import os
import tempfile
import unittest

from kiwi_web.ccmap import ACTIONS, CcMap, defaults, known_targets, normalise, target_key

META = {'plugins': [
    {'instance': 5, 'kind': 'port', 'params': [{'symbol': 'piano_vol'}, {'symbol': 'piano_send'},
                                               {'symbol': 'sampler_vol'}, {'symbol': 'sampler_send'},
                                               {'symbol': 'vocoder_vol'}, {'symbol': 'vocoder_send'}]},
    {'instance': 3, 'kind': 'port', 'params': [{'symbol': 'blend'}]},
    {'instance': 1, 'kind': 'port', 'params': [{'symbol': 'release', 'cc': 105}]},
    {'instance': 0, 'kind': 'patch', 'params': [{'uri': 'urn:ptq:Volume'}]},
], 'reverbs': [{'id': 'zita', 'params': [{'symbol': 'MID_RT60'}]}]}
PIANO = {'kind': 'port', 'instance': 5, 'symbol': 'piano_vol'}
VOLUME = {'kind': 'patch', 'instance': 0, 'uri': 'urn:ptq:Volume'}
RELEASE = {'kind': 'cc', 'instance': 1, 'number': 105}
PANIC = {'kind': 'action', 'name': 'panic'}


class KnownTargetsTest(unittest.TestCase):
    def test_everything_mappable(self):
        known = known_targets(META)
        self.assertIn(target_key(PIANO), known)
        self.assertIn(target_key(VOLUME), known)
        self.assertIn(target_key(RELEASE), known)
        self.assertIn(('port', 6, 'MID_RT60'), known)
        for name in ACTIONS:
            self.assertIn(('action', name), known)
        self.assertNotIn(('port', 1, 'release'), known, 'a CC control is not a port target')

    def test_normalise(self):
        known = known_targets(META)
        self.assertEqual(normalise({'kind': 'port', 'instance': 5, 'symbol': 'piano_vol', 'junk': 1}, known), PIANO)
        self.assertIsNone(normalise({'kind': 'port', 'instance': 5, 'symbol': 'nope'}, known))
        self.assertIsNone(normalise({'kind': 'port'}, known))
        self.assertIsNone(normalise('piano', known))
        self.assertIsNone(normalise({'kind': 'action', 'name': 'format-disk'}, known))


class CcMapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'state', 'ccmap.json')
        self.map = CcMap(self.path, known_targets(META))

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_when_no_file(self):
        self.assertEqual(self.map.load(), defaults())
        self.assertEqual(self.map.targets(0, 20), [PIANO])
        self.assertEqual(self.map.targets(0, 27), [{'kind': 'action', 'name': 'morph'}])
        self.assertFalse(os.path.exists(self.path), 'defaults are not written')
        self.assertEqual(len(self.map.ports()), 7)
        self.assertFalse(self.map.forwarding())

    def test_set_replaces_the_targets_binding_and_allows_many_targets_per_cc(self):
        self.map.load()
        previous = self.map.set(0, 74, PIANO)
        self.assertEqual(previous, {'channel': 0, 'cc': 20, 'target': PIANO})
        self.assertEqual(self.map.targets(0, 20), [])
        self.assertEqual(self.map.targets(0, 74), [PIANO])
        self.assertIsNone(self.map.set(0, 74, VOLUME))
        self.assertIsNone(self.map.set(1, 74, RELEASE))
        self.assertEqual(self.map.targets(0, 74), [PIANO, VOLUME])
        self.assertEqual(self.map.targets(1, 74), [RELEASE])
        self.assertTrue(self.map.forwarding())
        self.assertEqual(self.map.mapping_for(PIANO)['cc'], 74)

    def test_remove(self):
        self.map.load()
        self.assertEqual(self.map.remove(PIANO)['cc'], 20)
        self.assertIsNone(self.map.remove(PIANO))
        self.assertEqual(self.map.targets(0, 20), [])

    def test_persists_and_reloads(self):
        self.map.load()
        self.map.set(2, 74, PANIC)
        self.map.remove({'kind': 'action', 'name': 'morph'})
        reloaded = CcMap(self.path, known_targets(META))
        reloaded.load()
        self.assertEqual(reloaded.mappings, self.map.mappings)
        self.assertEqual(reloaded.targets(2, 74), [PANIC])
        self.assertEqual(reloaded.targets(0, 27), [])
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_bad_entries_are_dropped_and_a_corrupt_file_means_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump({'mappings': [
                {'channel': 0, 'cc': 1, 'target': PIANO},
                {'channel': 16, 'cc': 1, 'target': VOLUME},            # bad channel
                {'channel': 0, 'cc': 1, 'target': {'kind': 'port', 'instance': 9, 'symbol': 'x'}},
                {'cc': 1},                                              # malformed
                'nonsense',
            ]}, f)
        self.assertEqual(self.map.load(), [{'channel': 0, 'cc': 1, 'target': PIANO}])
        with open(self.path, 'w') as f:
            f.write('{nope')
        self.assertEqual(self.map.load(), defaults())


if __name__ == '__main__':
    unittest.main()
