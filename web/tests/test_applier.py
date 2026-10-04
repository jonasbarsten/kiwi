import unittest

from kiwi_web.applier import Applier
from kiwi_web.state import StateStore

META = {
    'plugins': [
        {'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
            {'symbol': 'piano_vol', 'min': 0, 'max': 1, 'default': 0.5, 'baseline': 0.8, 'type': 'float'},
            {'symbol': 'piano_send', 'min': 0, 'max': 1, 'default': 0.2, 'baseline': 0.2, 'type': 'float'}]},
        {'instance': 7, 'title': 'Limiter', 'kind': 'port', 'params': [
            {'symbol': 'truepeak', 'min': 0, 'max': 1, 'default': 0, 'baseline': 0, 'type': 'toggle'},
            {'symbol': 'threshold', 'min': -10, 'max': 0, 'default': -1, 'baseline': -1, 'type': 'float'}]},
        {'instance': 1, 'title': 'Sampler', 'kind': 'port', 'params': [
            {'symbol': 'release', 'cc': 105, 'min': 0, 'max': 4, 'default': 0.1, 'baseline': 0.1, 'type': 'float'},
            {'symbol': 'sample_quality', 'min': 0, 'max': 10, 'default': 2, 'baseline': 2, 'type': 'int'}]},
        {'instance': 0, 'title': 'Pianoteq', 'kind': 'patch', 'params': [
            {'uri': 'ptq:Volume', 'min': 0, 'max': 1, 'default': 0.72, 'baseline': 0.72}]},
    ],
    'reverbs': [
        {'id': 'zita', 'params': [{'symbol': 'MID_RT60', 'min': 1, 'max': 8, 'default': 2, 'baseline': 3, 'type': 'float'}]},
        {'id': 'calf', 'params': [{'symbol': 'decay_time', 'min': 0.4, 'max': 15, 'default': 1.5, 'baseline': 1.5, 'type': 'float'}]},
    ],
    'default_reverb': 'zita',
}


def doc(**fields):
    document = StateStore.empty()
    document.update(fields)
    return document


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.applier = Applier(META)

    def test_ops_sets_target_values(self):
        target = doc(params={'5:piano_vol': 0.3}, patch_params={'0:ptq:Volume': 0.5}, cc={'105': 2.0})
        self.assertEqual(self.applier.ops(doc(), target, replace=True), [
            ('port', 5, 'piano_vol', 0.3), ('patch', 0, 'ptq:Volume', 0.5), ('cc', 105, 2.0)])

    def test_select_resets_leftovers(self):
        current = doc(params={'5:piano_vol': 0.3, '7:threshold': -4}, cc={'105': 2.0},
                      reverb_params={'zita': {'MID_RT60': 6}})
        target = doc(params={'7:threshold': -4})
        self.assertEqual(self.applier.ops(current, target, replace=True), [
            ('port', 5, 'piano_vol', 0.8), ('port', 6, 'MID_RT60', 3), ('cc', 105, 0.1)])

    def test_morph_does_not_reset_leftovers(self):
        current = doc(params={'5:piano_vol': 0.3})
        self.assertEqual(self.applier.ops(current, doc(), replace=False), [])

    def test_unknown_keys_are_ignored(self):
        target = doc(params={'9:nope': 1.0, '5:piano_vol': 0.3}, cc={'7': 1.0})
        self.assertEqual(self.applier.ops(doc(), target, replace=True), [('port', 5, 'piano_vol', 0.3)])

    def test_reverb_switch_op_when_reverb_differs(self):
        current = doc(reverb_params={'zita': {'MID_RT60': 6}})
        target = doc(reverb='calf', reverb_params={'calf': {'decay_time': 0.9}})
        ops = self.applier.ops(current, target, replace=True)
        self.assertEqual(ops[0], ('reverb', 'calf', {'decay_time': 0.9}))
        self.assertNotIn(('port', 6, 'MID_RT60', 3), ops)   # the old reverb is gone, no reset needed

    def test_back_to_default_reverb_sends_patch_baselines(self):
        current = doc(reverb='calf')
        self.assertEqual(self.applier.ops(current, doc(), replace=True), [('reverb', 'zita', {'MID_RT60': 3})])

    def test_reverb_param_diff_when_same_reverb(self):
        current = doc(reverb='calf', reverb_params={'calf': {'decay_time': 0.9}})
        target = doc(reverb='calf', reverb_params={'calf': {'decay_time': 3.0}})
        self.assertEqual(self.applier.ops(current, target, replace=True), [('port', 6, 'decay_time', 3.0)])
        self.assertEqual(self.applier.ops(target, doc(reverb='calf'), replace=True), [('port', 6, 'decay_time', 1.5)])

    def test_preset_op_precedes_patch_params(self):
        target = doc(preset='file:///p/A.ttl', patch_params={'0:ptq:Volume': 0.5}, reverb='calf')
        ops = self.applier.ops(doc(), target, replace=True)
        kinds = [op[0] for op in ops]
        self.assertEqual(kinds, ['reverb', 'preset', 'patch'])
        self.assertEqual(ops[1], ('preset', 'file:///p/A.ttl'))

    def test_no_preset_leaves_the_piano_alone_without_a_patch_default(self):
        current = doc(preset='file:///p/A.ttl')
        self.assertEqual(self.applier.ops(current, doc(), replace=True), [])
        self.assertEqual(self.applier.ops(current, doc(preset='file:///p/A.ttl'), replace=True), [])

    def test_patch_default_preset_is_the_baseline(self):
        applier = Applier(dict(META, default_preset='file:///p/Default.ttl'))
        current = doc(preset='file:///p/A.ttl')
        self.assertEqual(applier.ops(current, doc(), replace=True), [('preset', 'file:///p/Default.ttl')])
        self.assertEqual(applier.ops(doc(), doc(), replace=True), [])

    def test_morph_carries_the_preset(self):
        target = self.applier.morph(doc(), doc(), 0.5, 'zita', preset='file:///p/A.ttl')
        self.assertEqual(target['preset'], 'file:///p/A.ttl')
        self.assertEqual(self.applier.ops(doc(preset='file:///p/A.ttl'), target, replace=False), [])

    def test_unchanged_values_produce_no_ops(self):
        current = doc(params={'5:piano_vol': 0.3})
        self.assertEqual(self.applier.ops(current, doc(params={'5:piano_vol': 0.3}), replace=True), [])


class MorphTest(unittest.TestCase):
    def setUp(self):
        self.applier = Applier(META)
        self.a = doc(params={'5:piano_vol': 0.2, '7:truepeak': 1, '1:sample_quality': 8},
                     patch_params={'0:ptq:Volume': 0.1}, cc={'105': 0.0},
                     reverb_params={'zita': {'MID_RT60': 2}})
        self.b = doc(params={'5:piano_send': 1.0, '7:threshold': -9},
                     patch_params={'0:ptq:Volume': 0.9}, cc={'105': 4.0},
                     reverb_params={'zita': {'MID_RT60': 8}})

    def test_endpoints_and_midpoint(self):
        at0 = self.applier.morph(self.a, self.b, 0.0, 'zita')
        self.assertAlmostEqual(at0['params']['5:piano_vol'], 0.2)
        self.assertAlmostEqual(at0['params']['5:piano_send'], 0.2)      # A has no value: baseline
        at1 = self.applier.morph(self.a, self.b, 1.0, 'zita')
        self.assertAlmostEqual(at1['params']['5:piano_vol'], 0.8)      # B has no value: baseline
        self.assertAlmostEqual(at1['params']['5:piano_send'], 1.0)
        self.assertAlmostEqual(at1['params']['7:threshold'], -9)
        mid = self.applier.morph(self.a, self.b, 0.5, 'zita')
        self.assertAlmostEqual(mid['params']['5:piano_vol'], 0.5)
        self.assertAlmostEqual(mid['cc']['105'], 2.0)
        self.assertAlmostEqual(mid['reverb_params']['zita']['MID_RT60'], 5)

    def test_morph_excludes_non_continuous(self):
        mid = self.applier.morph(self.a, self.b, 0.5, 'zita')
        self.assertNotIn('7:truepeak', mid['params'])
        self.assertNotIn('1:sample_quality', mid['params'])
        self.assertEqual(mid['patch_params'], {})
        self.assertEqual(mid['reverb'], 'zita', 'the reverb choice is carried over, never morphed')

    def test_morph_reverb_params_only_when_same_reverb(self):
        b = dict(self.b, reverb='calf')
        self.assertEqual(self.applier.morph(self.a, b, 0.5, 'zita')['reverb_params'], {})
        self.assertEqual(self.applier.morph(self.a, self.b, 0.5, 'calf')['reverb_params'], {})

    def test_morph_with_non_default_reverb_does_not_switch_it(self):
        a = dict(self.a, reverb='calf', reverb_params={'calf': {'decay_time': 1.0}})
        b = dict(self.b, reverb='calf', reverb_params={'calf': {'decay_time': 3.0}})
        target = self.applier.morph(a, b, 0.5, 'calf')
        self.assertEqual(target['reverb'], 'calf')
        ops = self.applier.ops(dict(a), target, replace=False)
        self.assertFalse([op for op in ops if op[0] == 'reverb'], ops)
        self.assertIn(('port', 6, 'decay_time', 2.0), ops)

    def test_effective_reverb(self):
        self.assertEqual(self.applier.effective_reverb(doc()), 'zita')
        self.assertEqual(self.applier.effective_reverb(doc(reverb='calf')), 'calf')
        self.assertEqual(self.applier.effective_reverb(doc(reverb='nope')), 'zita')


if __name__ == '__main__':
    unittest.main()
