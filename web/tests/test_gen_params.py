import importlib.util
import os
import subprocess
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location('gen_params', os.path.join(HERE, '..', 'tools', 'gen_params.py'))
gen_params = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_params)

LV2INFO = '''urn:dragonfly:plate

	Name:              Dragonfly Plate Reverb

	Port 0:
		Type:        http://lv2plug.in/ns/lv2core#AudioPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      lv2_audio_in_1
		Name:        Audio Input 1

	Port 3:
		Type:        http://lv2plug.in/ns/lv2core#InputPort
		             http://lv2plug.in/ns/lv2core#ControlPort
		Symbol:      amp_attack
		Name:        Amp Attack
		Group:       http://code.google.com/p/amsynth/amsynth#group_amp_env
		Minimum:     0.000000
		Maximum:     2.500000
		Default:     0.000000
		Properties:  http://lv2plug.in/ns/ext/port-props#hasStrictBounds

	Port 5:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      algorithm
		Name:        Algorithm
		Minimum:     0.000000
		Maximum:     2.000000
		Default:     1.000000
		Properties:  http://lv2plug.in/ns/lv2core#integer
		             http://lv2plug.in/ns/lv2core#enumeration
		Scale Points:				1 = "Nested"
			0 = "Simple"
			2 = "Tank"

	Port 6:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#InputPort
		Symbol:      truepeak
		Name:        True Peak
		Minimum:     0.000000
		Maximum:     1.000000
		Default:     0.000000
		Properties:  http://lv2plug.in/ns/lv2core#integer
		             http://lv2plug.in/ns/lv2core#toggled

	Port 7:
		Type:        http://lv2plug.in/ns/lv2core#ControlPort
		             http://lv2plug.in/ns/lv2core#OutputPort
		Symbol:      level
		Name:        Level
		Minimum:     -10.000000
		Maximum:     20.000000
'''

TTL = '''@prefix plug:  <https://www.modartt.com/lv2/Pianoteq8:> .

plug:Volume
	a lv2:Parameter ;
	rdfs:label "Volume" ;
	pg:group plug:paramgroup_main ;
	rdfs:range atom:Float ;
	lv2:default 0.727273 ;
	lv2:minimum 0 ;
	lv2:maximum 1 .

plug:Reverb_20Switch
	a lv2:Parameter ;
	rdfs:label "Reverb Switch" ;
	pg:group plug:paramgroup_reverb ;
	rdfs:range atom:Float ;
	lv2:default 1 ;
	lv2:minimum 0 ;
	lv2:maximum 1 .

plug:paramgroup_main
	a pg:Group ;
	lv2:symbol "paramgroup_main" ;		lv2:name "Main" .

plug:paramgroup_reverb
	a pg:Group ;
	lv2:symbol "paramgroup_reverb" ;		lv2:name "Reverb" .
'''


class Lv2InfoTest(unittest.TestCase):
    def test_control_inputs_only(self):
        params = gen_params.parse_lv2info(LV2INFO)
        self.assertEqual([p['symbol'] for p in params], ['amp_attack', 'algorithm', 'truepeak'])

    def test_float_with_group(self):
        attack = gen_params.parse_lv2info(LV2INFO)[0]
        self.assertEqual(attack, {'symbol': 'amp_attack', 'name': 'Amp Attack', 'min': 0.0, 'max': 2.5,
                                  'default': 0.0, 'type': 'float', 'group': 'amp env'})

    def test_enum_and_toggle(self):
        _, algorithm, truepeak = gen_params.parse_lv2info(LV2INFO)
        self.assertEqual(algorithm['type'], 'enum')
        self.assertEqual(algorithm['options'], [[0.0, 'Simple'], [1.0, 'Nested'], [2.0, 'Tank']])
        self.assertEqual(truepeak['type'], 'toggle')


class BuildTest(unittest.TestCase):
    def test_reverbs_listed_with_locked_settings_hidden(self):
        from kiwi_web.patchfile import parse_patch
        patch = parse_patch('add http://calf.sourceforge.net/plugins/Reverb 6\n')

        def lv2info(uri):
            if uri == 'http://calf.sourceforge.net/plugins/Reverb':
                return LV2INFO.replace('amp_attack', 'decay_time').replace('algorithm', 'dry')
            if uri == 'urn:dragonfly:plate':
                return LV2INFO
            raise subprocess.CalledProcessError(1, 'lv2info')   # the others are not installed

        meta = gen_params.build(patch, lv2info, TTL, port_plugins=[])
        self.assertEqual(meta['default_reverb'], 'calf')
        ids = [r['id'] for r in meta['reverbs']]
        self.assertEqual(ids, ['calf', 'plate'])
        calf = meta['reverbs'][0]
        self.assertEqual([p['symbol'] for p in calf['params']], ['decay_time', 'truepeak'])  # dry is locked
        self.assertNotIn(6, [p['instance'] for p in meta['plugins']])

    def test_default_reverb_must_be_known(self):
        from kiwi_web.patchfile import parse_patch
        patch = parse_patch('add urn:unknown:reverb 6\n')
        with self.assertRaises(SystemExit):
            gen_params.build(patch, lambda uri: LV2INFO, TTL, port_plugins=[])


class PianoteqTest(unittest.TestCase):
    def test_parameters(self):
        params = gen_params.parse_pianoteq_ttl(TTL)
        self.assertEqual(params[0], {'uri': 'https://www.modartt.com/lv2/Pianoteq8:Volume', 'name': 'Volume',
                                     'group': 'Main', 'min': 0.0, 'max': 1.0, 'default': 0.727273})
        self.assertEqual(params[1]['group'], 'Reverb')


if __name__ == '__main__':
    unittest.main()
