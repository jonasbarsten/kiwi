import json
import os
import tempfile
import unittest

from kiwi_web.state import StateStore


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'sub', 'state.json')
        self.clock = Clock()
        self.store = StateStore(self.path, delay=5.0, clock=self.clock)
        self.store.load()

    def tearDown(self):
        self.dir.cleanup()

    def test_not_due_without_changes(self):
        self.assertFalse(self.store.due())
        self.assertFalse(self.store.flush())
        self.assertFalse(os.path.exists(self.path))

    def test_debounce_and_atomic_write(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.clock.now += 4.9
        self.assertFalse(self.store.due())
        self.clock.now += 0.2
        self.assertTrue(self.store.due())
        self.assertTrue(self.store.flush())
        with open(self.path) as f:
            self.assertEqual(json.load(f)['params'], {'5:piano_vol': 0.5})
        self.assertFalse(os.path.exists(self.path + '.tmp'))
        self.assertFalse(self.store.due())

    def test_value_equal_to_baseline_is_dropped(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_param(5, 'piano_vol', 0.8, baseline=0.8)
        self.assertEqual(self.store.data['params'], {})

    def test_patch_params_and_roundtrip(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        self.store.set_patch_param(0, uri, 0.3, baseline=0.72)
        self.store.flush()
        other = StateStore(self.path)
        other.load()
        self.assertEqual(other.data['patch_params'], {f'0:{uri}': 0.3})
        self.assertEqual(StateStore.split_key(f'0:{uri}'), (0, uri))

    def test_corrupt_file_loads_empty(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.store.load()['params'], {})

    def test_reverb_choice_and_params_per_reverb(self):
        self.store.set_reverb('calf')
        self.store.set_reverb_param('calf', 'decay_time', 0.9, baseline=1.5)
        self.store.set_reverb_param('zita', 'MID_RT60', 2.0, baseline=2.0)   # equals baseline: dropped
        self.store.flush()
        other = StateStore(self.path)
        other.load()
        self.assertEqual(other.data['reverb'], 'calf')
        self.assertEqual(other.data['reverb_params'], {'calf': {'decay_time': 0.9}})

    def test_load_drops_non_finite_and_malformed(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{"params": {"5:piano_vol": NaN, "junk": 0.5, "3:blend": 1.0},'
                    ' "reverb": 7, "reverb_params": {"calf": {"x": Infinity, "y": 0.2}, "bad": 3}}')
        data = self.store.load()
        self.assertEqual(data['params'], {'3:blend': 1.0})
        self.assertIsNone(data['reverb'])
        self.assertEqual(data['reverb_params'], {'calf': {'y': 0.2}})

    def test_clear_removes_file(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.flush()
        self.store.clear()
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.store.data['params'], {})


if __name__ == '__main__':
    unittest.main()
