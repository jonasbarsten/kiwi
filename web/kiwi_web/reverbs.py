"""The reverbs that can occupy the shared reverb slot (mod-host instance 6).

Each entry names the plugin, its stereo audio ports, and the settings that keep
it fully wet on the send bus (those are applied on every load and hidden from
the page). All of them ran clean under host/kiwi-stress at 128 frames.
"""

REVERB_INSTANCE = 6
SEND_PORTS = ('effect_5:send_l', 'effect_5:send_r')
OUTPUT_PORTS = ('system:playback_1', 'system:playback_2')

REVERBS = [
    {
        'id': 'zita',
        'name': 'Zita Rev1',
        'uri': 'http://guitarix.sourceforge.net/plugins/gx_zita_rev1_stereo#_zita_rev1_stereo',
        'inputs': ('in', 'in1'),
        'outputs': ('out', 'out1'),
        'locked': (('DRY_WET_MIX', 1.0),),
    },
    {
        'id': 'calf',
        'name': 'Calf Reverb',
        'uri': 'http://calf.sourceforge.net/plugins/Reverb',
        'inputs': ('in_l', 'in_r'),
        'outputs': ('out_l', 'out_r'),
        'locked': (('dry', 0.0), ('on', 1.0)),
    },
    {
        'id': 'ambience',
        'name': 'MDA Ambience',
        'uri': 'http://drobilla.net/plugins/mda/Ambience',
        'inputs': ('left_in', 'right_in'),
        'outputs': ('left_out', 'right_out'),
        'locked': (('mix', 1.0),),
    },
    {
        'id': 'gxreverb',
        'name': 'Guitarix Reverb',
        'uri': 'http://guitarix.sourceforge.net/plugins/gx_reverb_stereo#_reverb_stereo',
        'inputs': ('in', 'in1'),
        'outputs': ('out', 'out1'),
        'locked': (('dry_wet', 100.0),),
    },
    {
        'id': 'plate',
        'name': 'Dragonfly Plate',
        'uri': 'urn:dragonfly:plate',
        'inputs': ('lv2_audio_in_1', 'lv2_audio_in_2'),
        'outputs': ('lv2_audio_out_1', 'lv2_audio_out_2'),
        'locked': (('dry_level', 0.0),),
    },
]


def find(reverb_id):
    for entry in REVERBS:
        if entry['id'] == reverb_id:
            return entry
    return None


def find_by_uri(uri):
    for entry in REVERBS:
        if entry['uri'] == uri:
            return entry
    return None


def switch_commands(entry, settings):
    """The mod-host commands that put `entry` into the reverb slot, fully wet,
    with `settings` ({symbol: value}) applied, wired to the send bus and outputs."""
    commands = [f'remove {REVERB_INSTANCE}', f"add {entry['uri']} {REVERB_INSTANCE}"]
    for symbol, value in entry['locked']:
        commands.append(f'param_set {REVERB_INSTANCE} {symbol} {value:.6f}')
    for symbol, value in settings.items():
        commands.append(f'param_set {REVERB_INSTANCE} {symbol} {value:.6f}')
    for send, port in zip(SEND_PORTS, entry['inputs']):
        commands.append(f'connect {send} effect_{REVERB_INSTANCE}:{port}')
    for port, out in zip(entry['outputs'], OUTPUT_PORTS):
        commands.append(f'connect effect_{REVERB_INSTANCE}:{port} {out}')
    return commands
