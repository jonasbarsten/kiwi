"""Pianoteq presets: the standalone's LV2 export, rewritten into bundles the plugin
actually loads.

The export (`<root>/Pianoteq 8-factory-presets-<Family>.lv2/`) has a `manifest.ttl`
(bank label + one entry per preset file) and one `.ttl` per preset with its
`rdfs:label` and the state blob under a key the plugin does not read (see
`pianoteq_state`). `index_presets` writes each bundle again under `<out_root>/
<family-slug>.lv2/<preset-slug>.ttl` with the state in the form the plugin
restores. The new paths are ASCII without spaces, so mod-host's `bundle_add`
(which splits commands on spaces and percent-encodes the path itself) takes
them as they are and the `preset_load` URI is simply `file://` + path.
"""
import json
import os
import re
import shutil
import unicodedata

from . import pianoteq_state, reverbs

PIANOTEQ_INSTANCE = 0
PIANOTEQ_URI = 'https://www.modartt.com/lv2/Pianoteq8'
REVERB_SWITCH = 'https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch'

_BANK = re.compile(r'a pset:Bank\s*;\s*rdfs:label "([^"]*)"')
_ENTRY = re.compile(r'^<([^>]+\.ttl)>\s*\n(?:[^\n]*\n)*?[^\n]*a pset:Preset', re.M)
_LABEL = re.compile(r'rdfs:label "((?:[^"\\]|\\.)*)"')

_PREFIXES = '''@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix pset: <http://lv2plug.in/ns/ext/presets#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix state: <http://lv2plug.in/ns/ext/state#> .

'''


def slug(text):
    """ASCII file-name form of a label: 'Blüthner' -> 'bluthner'."""
    ascii_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-') or 'preset'


def _literal(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _unescape(literal):
    return re.sub(r'\\(.)', r'\1', literal)


def _preset_ttl(label, blob):
    return (_PREFIXES + '<>\n\ta pset:Preset ;\n'
            f'\tlv2:appliesTo <{PIANOTEQ_URI}> ;\n'
            f'\trdfs:label {_literal(label)} ;\n'
            '\tstate:state [\n'
            f'\t\t<{pianoteq_state.STATE_KEY}> "{pianoteq_state.state_string(blob)}"\n'
            '\t] .\n')


def _manifest_ttl(family, filenames):
    lines = [_PREFIXES, f'<bank>\n\tlv2:appliesTo <{PIANOTEQ_URI}> ;\n\ta pset:Bank ;\n'
                        f'\trdfs:label {_literal(family)} .\n']
    for filename in filenames:
        lines.append(f'\n<{filename}>\n\tlv2:appliesTo <{PIANOTEQ_URI}> ;\n\ta pset:Preset ;\n'
                     f'\tpset:bank <bank> ;\n\trdfs:seeAlso <{filename}> .\n')
    return ''.join(lines)


def _read_exported(root):
    """[(family, [(label, blob), ...]), ...] from the standalone's export; bundles
    without a manifest and presets without a readable state are skipped."""
    try:
        bundles = sorted(d for d in os.listdir(root) if d.endswith('.lv2'))
    except OSError:
        return []
    families = []
    for bundle in bundles:
        directory = os.path.join(root, bundle)
        try:
            with open(os.path.join(directory, 'manifest.ttl')) as f:
                manifest = f.read()
        except OSError:
            continue
        bank = _BANK.search(manifest)
        family = bank.group(1) if bank else bundle[:-4]
        entries = []
        for filename in _ENTRY.findall(manifest):
            try:
                with open(os.path.join(directory, filename)) as f:
                    ttl = f.read()
            except OSError:
                continue
            blob = pianoteq_state.exported_blob(ttl)
            if blob is None:
                continue
            label = _LABEL.search(ttl)
            name = _unescape(label.group(1)) if label else filename[:-4].replace('_', ' ')
            entries.append((name, blob))
        families.append((family, entries))
    return families


def index_presets(root, out_root):
    """Rewrites the export under `root` into `out_root` and returns
    [{'uri', 'name', 'family', 'bundle'}] for every preset, ordered by family
    then name. `out_root` is replaced wholesale."""
    families = _read_exported(root)
    if os.path.isdir(out_root):
        shutil.rmtree(out_root)
    presets = []
    for family, entries in families:
        if not entries:
            continue
        bundle = os.path.join(out_root, slug(family) + '.lv2')
        os.makedirs(bundle)
        filenames = []
        for label, blob in entries:
            filename = slug(label) + '.ttl'
            while filename in filenames:
                filename = filename[:-4] + '-2.ttl'
            filenames.append(filename)
            with open(os.path.join(bundle, filename), 'w') as f:
                f.write(_preset_ttl(label, blob))
            presets.append({'uri': 'file://' + os.path.join(bundle, filename), 'name': label,
                            'family': family, 'bundle': bundle})
        with open(os.path.join(bundle, 'manifest.ttl'), 'w') as f:
            f.write(_manifest_ttl(family, filenames))
    presets.sort(key=lambda p: (p['family'].lower(), p['name'].lower()))
    return presets


def load_commands(uri, bundle):
    """mod-host commands that load a preset into Pianoteq and keep its own
    reverb off (presets carry their own reverb setting). `bundle` is a plain,
    space-free directory path."""
    return [
        f"bundle_add {bundle.rstrip('/')}/",
        f'preset_load {PIANOTEQ_INSTANCE} {uri}',
        f'patch_set {PIANOTEQ_INSTANCE} {REVERB_SWITCH} 0.000000',
    ]


class Favourites:
    """Starred presets, global (not per slot). Written only when a star is toggled."""

    def __init__(self, path):
        self.path = path
        self.uris = []

    def load(self):
        try:
            with open(self.path) as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = []
        self.uris = [u for u in loaded if isinstance(u, str)] if isinstance(loaded, list) else []
        return self.uris

    def toggle(self, uri, on):
        """Returns True if the list changed (and was written)."""
        if on and uri not in self.uris:
            self.uris.append(uri)
        elif not on and uri in self.uris:
            self.uris.remove(uri)
        else:
            return False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.uris, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        return True


assert reverbs.REVERB_INSTANCE != PIANOTEQ_INSTANCE
