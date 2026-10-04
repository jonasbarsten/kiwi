import os
import tempfile
import unittest

from kiwi_web.presets import Favourites, encode_path, index_presets, load_commands

MANIFEST = '''@prefix pset: <http://lv2plug.in/ns/ext/presets#> .

<BANK_Electric>
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	a pset:Bank ;
	rdfs:label "Electric" .

<MKII_Spark.ttl>
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	a pset:Preset ;
	pset:bank <BANK_Electric> ;
	rdfs:seeAlso <MKII_Spark.ttl> .
<MKI_Amped.ttl>
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	a pset:Preset ;
	pset:bank <BANK_Electric> ;
	rdfs:seeAlso <MKI_Amped.ttl> .
'''
PRESET = '''<>
	a pset:Preset ;
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	rdfs:label "%s" ;
	state:state [ ] .
'''


class PresetsTest(unittest.TestCase):
    def test_encode_path(self):
        self.assertEqual(encode_path('/home/patch/kiwi-data/Pianoteq 8-factory-presets-Blüthner.lv2/A_B.ttl'),
                         'file:///home/patch/kiwi-data/Pianoteq%208-factory-presets-Bl%C3%BCthner.lv2/A_B.ttl')

    def test_index_reads_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, 'Pianoteq 8-factory-presets-Electric.lv2')
            os.mkdir(bundle)
            with open(os.path.join(bundle, 'manifest.ttl'), 'w') as f:
                f.write(MANIFEST)
            for name, label in [('MKII_Spark', 'MKII Spark'), ('MKI_Amped', 'MKI Amped')]:
                with open(os.path.join(bundle, f'{name}.ttl'), 'w') as f:
                    f.write(PRESET % label)
            os.mkdir(os.path.join(tmp, 'broken.lv2'))      # no manifest: skipped
            presets = index_presets(tmp)
            self.assertEqual([p['name'] for p in presets], ['MKI Amped', 'MKII Spark'])
            self.assertEqual(presets[0]['family'], 'Electric')
            self.assertTrue(presets[0]['uri'].startswith('file://'))
            self.assertTrue(presets[0]['uri'].endswith('Pianoteq%208-factory-presets-Electric.lv2/MKI_Amped.ttl'))
            self.assertEqual(presets[0]['bundle'], bundle)

            # With a link root, bundle_add gets a space-free symlink to the real bundle.
            links = os.path.join(tmp, 'links')
            linked = index_presets(tmp, link_root=links)
            self.assertEqual(linked[0]['bundle'], os.path.join(links, 'pianoteq-8-factory-presets-electric.lv2'))
            self.assertNotIn(' ', linked[0]['bundle'])
            self.assertEqual(os.readlink(linked[0]['bundle']), bundle)
            self.assertTrue(linked[0]['uri'].endswith('Pianoteq%208-factory-presets-Electric.lv2/MKI_Amped.ttl'),
                            'the preset URI stays the real, canonical file')
            index_presets(tmp, link_root=links)     # idempotent

    def test_missing_root_is_empty(self):
        self.assertEqual(index_presets('/nonexistent'), [])

    def test_favourites_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state', 'favourites.json')
            favourites = Favourites(path)
            self.assertEqual(favourites.load(), [])
            self.assertTrue(favourites.toggle('file:///a.ttl', True))
            self.assertTrue(favourites.toggle('file:///b.ttl', True))
            self.assertFalse(favourites.toggle('file:///a.ttl', True))    # already starred: no write
            self.assertTrue(favourites.toggle('file:///a.ttl', False))
            self.assertEqual(Favourites(path).load(), ['file:///b.ttl'])
            with open(path, 'w') as f:
                f.write('{nope')
            self.assertEqual(Favourites(path).load(), [])

    def test_load_commands(self):
        uri = 'file:///x/Pianoteq%208-factory-presets-Electric.lv2/MKI_Amped.ttl'
        bundle = '/x/links/pianoteq-8-factory-presets-electric.lv2'
        self.assertEqual(load_commands(uri, bundle), [
            'bundle_add /x/links/pianoteq-8-factory-presets-electric.lv2/',
            f'preset_load 0 {uri}',
            'patch_set 0 https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch 0.000000',
        ])


if __name__ == '__main__':
    unittest.main()
