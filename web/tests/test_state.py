import json
import os
import tempfile
import unittest

from kiwi_web.state import StateStore


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, 'kiwi')
        self.store = StateStore(self.root)
        self.store.load()

    def tearDown(self):
        self.dir.cleanup()

    def slot_path(self, n):
        return os.path.join(self.root, 'presets', f'{n}.json')

    def listing(self):
        found = []
        for base, _, files in os.walk(self.dir.name):
            found.extend(os.path.join(base, f) for f in files)
        return sorted(found)

    # --- RAM edits ---------------------------------------------------------

    def test_value_equal_to_baseline_is_dropped(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_param(5, 'piano_vol', 0.8, baseline=0.8)
        self.assertEqual(self.store.data['params'], {})

    def test_reverb_and_cc_values(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        self.store.set_patch_param(0, uri, 0.3, baseline=0.72)
        self.store.set_reverb('calf')
        self.store.set_reverb_param('calf', 'decay_time', 0.9, baseline=1.5)
        self.store.set_reverb_param('zita', 'MID_RT60', 2.0, baseline=2.0)   # equals baseline: dropped
        self.store.set_cc(105, 1.2, baseline=0.1)
        self.assertEqual(self.store.data['patch_params'], {f'0:{uri}': 0.3})
        self.assertEqual(self.store.data['reverb'], 'calf')
        self.assertEqual(self.store.data['reverb_params'], {'calf': {'decay_time': 0.9}})
        self.assertEqual(self.store.data['cc'], {'105': 1.2})
        self.assertEqual(StateStore.split_key(f'0:{uri}'), (0, uri))

    def test_no_writes_outside_save_and_select(self):
        before = self.listing()
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_cc(105, 1.2, baseline=0.1)
        self.store.set_name('Loud')
        self.store.clear()
        self.assertEqual(self.listing(), before)

    def test_dirty_flag(self):
        self.assertFalse(self.store.dirty)
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.assertTrue(self.store.dirty)
        self.store.save()
        self.assertFalse(self.store.dirty)
        self.store.set_name('Soft')
        self.assertTrue(self.store.dirty)

    # --- slots ---------------------------------------------------------------

    def test_fresh_store_is_slot_1_with_default_names(self):
        self.assertEqual(self.store.slot, 1)
        self.assertEqual(self.store.data['name'], 'Preset 1')
        self.assertEqual(self.store.names(), [f'Preset {n}' for n in range(1, 9)])

    def test_save_writes_slot_atomically(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_name('Warm')
        self.store.save()
        with open(self.slot_path(1)) as f:
            saved = json.load(f)
        self.assertEqual(saved['params'], {'5:piano_vol': 0.5})
        self.assertEqual(saved['name'], 'Warm')
        self.assertFalse(os.path.exists(self.slot_path(1) + '.tmp'))
        self.assertEqual(self.store.names()[0], 'Warm')

    def test_migrate_presets_rewrites_only_slots_whose_uri_changes(self):
        self.store.set_preset('file:///old/A.ttl')
        self.store.save()
        self.store.select(2)
        self.store.set_preset('file:///new/b.ttl')
        self.store.save()
        self.store.select(3)
        self.store.set_preset('file:///old/C.ttl')          # RAM only, current slot
        replace = {'file:///old/A.ttl': 'file:///new/a.ttl', 'file:///old/C.ttl': 'file:///new/c.ttl'}
        changed = self.store.migrate_presets(lambda uri: replace.get(uri, uri))
        self.assertEqual(changed, [1, 3])
        self.assertEqual(self.store.read_slot(1)['preset'], 'file:///new/a.ttl')
        self.assertEqual(self.store.read_slot(2)['preset'], 'file:///new/b.ttl')
        self.assertEqual(self.store.data['preset'], 'file:///new/c.ttl')
        self.assertEqual(self.store.summaries()[0]['preset'], 'file:///new/a.ttl')

    def test_summaries_show_preset_and_reverb_per_slot(self):
        self.store.set_preset('file:///x/a.ttl')
        self.store.set_reverb('calf')
        self.store.set_name('Warm')
        self.store.save()
        self.store.select(2)
        self.store.set_reverb('plate')             # RAM only: still reported for the current slot
        summaries = self.store.summaries()
        self.assertEqual(summaries[0], {'name': 'Warm', 'preset': 'file:///x/a.ttl', 'reverb': 'calf'})
        self.assertEqual(summaries[1], {'name': 'Preset 2', 'preset': None, 'reverb': 'plate'})
        self.assertEqual(len(summaries), 8)

    def test_select_writes_current_and_loads_slot(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.save()
        self.store.select(3)
        self.store.set_param(3, 'blend', 1.5, baseline=0.0)
        self.store.save()
        with open(os.path.join(self.root, 'current')) as f:
            self.assertEqual(f.read().strip(), '3')
        self.store.select(1)
        self.assertEqual(self.store.slot, 1)
        self.assertEqual(self.store.data['params'], {'5:piano_vol': 0.5})
        self.assertFalse(self.store.dirty)
        other = StateStore(self.root)
        other.load()
        self.assertEqual(other.slot, 1)
        self.assertEqual(other.read_slot(3)['params'], {'3:blend': 1.5})

    def test_select_discards_unsaved_edits(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.select(2)
        self.assertEqual(self.store.data['params'], {})
        self.store.select(1)
        self.assertEqual(self.store.data['params'], {})

    def test_missing_and_corrupt_slots_are_empty(self):
        self.assertEqual(self.store.read_slot(7)['params'], {})
        os.makedirs(os.path.join(self.root, 'presets'), exist_ok=True)
        with open(self.slot_path(4), 'w') as f:
            f.write('{not json')
        self.assertEqual(self.store.read_slot(4)['params'], {})
        self.assertEqual(self.store.read_slot(4)['name'], 'Preset 4')

    def test_slot_numbers_are_validated(self):
        with self.assertRaises(ValueError):
            self.store.select(0)
        with self.assertRaises(ValueError):
            self.store.select(9)

    def test_load_drops_non_finite_and_malformed(self):
        os.makedirs(os.path.join(self.root, 'presets'))
        with open(self.slot_path(1), 'w') as f:
            f.write('{"params": {"5:piano_vol": NaN, "junk": 0.5, "3:blend": 1.0},'
                    ' "reverb": 7, "reverb_params": {"calf": {"x": Infinity, "y": 0.2}, "bad": 3},'
                    ' "cc": {"102": 0.5, "x": 1, "103": "no"}, "name": 12}')
        data = self.store.load()
        self.assertEqual(data['params'], {'3:blend': 1.0})
        self.assertEqual(data['cc'], {'102': 0.5})
        self.assertIsNone(data['reverb'])
        self.assertEqual(data['reverb_params'], {'calf': {'y': 0.2}})
        self.assertEqual(data['name'], 'Preset 1')

    def test_migrates_state_json_into_slot_1(self):
        os.makedirs(self.root, exist_ok=True)
        legacy = os.path.join(self.root, 'state.json')
        with open(legacy, 'w') as f:
            json.dump({'params': {'5:piano_vol': 0.4}, 'reverb': 'calf'}, f)
        with open(os.path.join(self.root, 'current'), 'w') as f:
            f.write('2\n')
        store = StateStore(self.root)
        store.load()
        self.assertEqual(store.slot, 2)
        self.assertEqual(store.read_slot(1)['params'], {'5:piano_vol': 0.4})
        self.assertEqual(store.read_slot(1)['reverb'], 'calf')
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + '.migrated'))

    def test_clear_keeps_slot_and_name(self):
        self.store.set_param(5, 'piano_vol', 0.5, baseline=0.8)
        self.store.set_name('Keep')
        self.store.clear()
        self.assertEqual(self.store.data['params'], {})
        self.assertEqual(self.store.data['name'], 'Keep')
        self.assertEqual(self.store.slot, 1)
        self.assertTrue(self.store.dirty)


if __name__ == '__main__':
    unittest.main()
