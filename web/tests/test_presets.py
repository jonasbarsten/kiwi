import os
import re
import tempfile
import unittest

from kiwi_web.pianoteq_state import STATE_KEY, state_string
from kiwi_web.presets import Favourites, index_presets, load_commands, slug

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
<Broken.ttl>
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	a pset:Preset ;
	pset:bank <BANK_Electric> ;
	rdfs:seeAlso <Broken.ttl> .
'''
PRESET = '''<>
	a pset:Preset ;
	lv2:appliesTo <https://www.modartt.com/lv2/Pianoteq8> ;
	rdfs:label "%s" ;
	state:state [
		<urn:juce:stateBinary> """
	%s
"""^^xsd:base64Binary
] .
'''
BLOB = 'UHJWSyYAAAJYRlRQ'      # PrVK&\0\0\x02XFTP


def write_export(root):
    bundle = os.path.join(root, 'Pianoteq 8-factory-presets-Electric.lv2')
    os.mkdir(bundle)
    with open(os.path.join(bundle, 'manifest.ttl'), 'w') as f:
        f.write(MANIFEST)
    for name, label in [('MKII_Spark', 'MKII "Spark"'), ('MKI_Amped', 'MKI Amped')]:
        with open(os.path.join(bundle, f'{name}.ttl'), 'w') as f:
            f.write(PRESET % (label.replace('"', '\\"'), BLOB))
    with open(os.path.join(bundle, 'Broken.ttl'), 'w') as f:
        f.write('<> a pset:Preset .\n')            # no state: skipped
    os.mkdir(os.path.join(root, 'broken.lv2'))     # no manifest: skipped
    return bundle


class PresetsTest(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slug('Blüthner'), 'bluthner')
        self.assertEqual(slug('Bösendorfer 280VC'), 'bosendorfer-280vc')
        self.assertEqual(slug("H. Ruckers II Harpsichord 4'"), 'h-ruckers-ii-harpsichord-4')
        self.assertEqual(slug('ñ'), 'n')
        self.assertEqual(slug('日本'), 'preset')

    def test_index_rewrites_bundles_in_the_form_the_plugin_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_export(tmp)
            out = os.path.join(tmp, 'out')
            presets = index_presets(tmp, out)
            self.assertEqual([p['name'] for p in presets], ['MKI Amped', 'MKII "Spark"'])
            self.assertEqual(presets[0]['family'], 'Electric')
            bundle = os.path.join(out, 'electric.lv2')
            self.assertEqual(presets[0]['bundle'], bundle)
            self.assertEqual(presets[0]['uri'], 'file://' + os.path.join(bundle, 'mki-amped.ttl'))
            self.assertEqual(presets[1]['uri'], 'file://' + os.path.join(bundle, 'mkii-spark.ttl'))
            self.assertEqual(sorted(os.listdir(bundle)), ['manifest.ttl', 'mki-amped.ttl', 'mkii-spark.ttl'])

            with open(os.path.join(bundle, 'mki-amped.ttl')) as f:
                ttl = f.read()
            self.assertIn('rdfs:label "MKI Amped"', ttl)
            with open(os.path.join(bundle, 'mkii-spark.ttl')) as f:
                self.assertIn('rdfs:label "MKII \\"Spark\\""', f.read())
            expected = state_string(b'PrVK&\x00\x00\x02XFTP')
            self.assertIn(f'<{STATE_KEY}> "{expected}"', ttl)
            self.assertNotIn('stateBinary', ttl)
            with open(os.path.join(bundle, 'manifest.ttl')) as f:
                manifest = f.read()
            self.assertIn('rdfs:label "Electric"', manifest)
            self.assertEqual(sorted(re.findall(r'^<([^>]+\.ttl)>', manifest, re.M)), ['mki-amped.ttl', 'mkii-spark.ttl'])

    def test_index_replaces_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_export(tmp)
            out = os.path.join(tmp, 'out')
            os.makedirs(os.path.join(out, 'old.lv2'))
            index_presets(tmp, out)
            self.assertEqual(os.listdir(out), ['electric.lv2'])

    def test_colliding_names_get_distinct_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_export(tmp)
            with open(os.path.join(bundle, 'MKI_Amped.ttl'), 'w') as f:
                f.write(PRESET % ('MKII Spark', BLOB))
            presets = index_presets(tmp, os.path.join(tmp, 'out'))
            self.assertEqual(sorted(os.path.basename(p['uri']) for p in presets), ['mkii-spark-2.ttl', 'mkii-spark.ttl'])

    def test_missing_root_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(index_presets('/nonexistent', os.path.join(tmp, 'out')), [])

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
        uri = 'file:///x/out/electric.lv2/mki-amped.ttl'
        bundle = '/x/out/electric.lv2'
        self.assertEqual(load_commands(uri, bundle), [
            'bundle_add /x/out/electric.lv2/',
            f'preset_load 0 {uri}',
            'patch_set 0 https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch 0.000000',
        ])


if __name__ == '__main__':
    unittest.main()
