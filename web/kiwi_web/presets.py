"""Pianoteq presets, exported by the standalone as LV2 preset bundles.

One bundle per instrument family: `<root>/Pianoteq 8-factory-presets-<Family>.lv2/`
with a `manifest.ttl` (bank label + one entry per preset file) and one `.ttl` per
preset (its `rdfs:label` is the display name).

mod-host's `bundle_add` takes a plain filesystem path (it percent-encodes it
itself, so an encoded path loads nothing) and splits commands on spaces, so the
bundles are reached through space-free symlinks (`<link_root>/<family>.lv2`).
lilv canonicalises the symlink, so `preset_load` then takes the `file://` URI of
the real preset file, percent-encoded.
"""
import json
import os
import re
from urllib.parse import quote

from . import reverbs

PIANOTEQ_INSTANCE = 0
REVERB_SWITCH = 'https://www.modartt.com/lv2/Pianoteq8:Reverb_20Switch'

_BANK = re.compile(r'a pset:Bank\s*;\s*rdfs:label "([^"]*)"')
_ENTRY = re.compile(r'^<([^>]+\.ttl)>\s*\n(?:[^\n]*\n)*?[^\n]*a pset:Preset', re.M)
_LABEL = re.compile(r'rdfs:label "([^"]*)"')


def encode_path(path):
    """A file:// URI for a filesystem path, as lilv resolves it (spaces and
    non-ASCII percent-encoded, slashes kept)."""
    return 'file://' + quote(path, safe='/')


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'bundle'


def index_presets(root, link_root=None):
    """[{'uri', 'name', 'family', 'bundle'}] for every preset under `root`,
    ordered by family then name. Missing or unreadable bundles are skipped.

    `bundle` is the plain path to give `bundle_add`: a space-free symlink under
    `link_root` (created here) when given, else the real directory.
    """
    presets = []
    try:
        bundles = sorted(d for d in os.listdir(root) if d.endswith('.lv2'))
    except OSError:
        return presets
    if link_root:
        os.makedirs(link_root, exist_ok=True)
    for bundle in bundles:
        directory = os.path.join(root, bundle)
        try:
            with open(os.path.join(directory, 'manifest.ttl')) as f:
                manifest = f.read()
        except OSError:
            continue
        bank = _BANK.search(manifest)
        family = bank.group(1) if bank else bundle
        bundle_path = directory
        if link_root:
            bundle_path = os.path.join(link_root, slug(bundle[:-4]) + '.lv2')
            if os.path.islink(bundle_path) and os.readlink(bundle_path) != directory:
                os.remove(bundle_path)
            if not os.path.lexists(bundle_path):
                os.symlink(directory, bundle_path)
        for filename in _ENTRY.findall(manifest):
            path = os.path.join(directory, filename)
            name = filename[:-4].replace('_', ' ')
            try:
                with open(path) as f:
                    label = _LABEL.search(f.read())
                if label:
                    name = label.group(1)
            except OSError:
                continue
            presets.append({'uri': encode_path(path), 'name': name, 'family': family,
                            'bundle': bundle_path})
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
