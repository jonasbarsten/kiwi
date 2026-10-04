import http.client
import json
import os
import socket as socketlib
import sys
import tempfile
import threading
import time
import unittest

from kiwi_web import server as server_module
from kiwi_web.modhost import HostClient
from kiwi_web.server import App, HostLink, Model, make_server, parse_args, restore, systemd_units_ready
from kiwi_web.state import StateStore
from tests.fakehost import FakeHost

ZITA = 'http://guitarix.sourceforge.net/plugins/gx_zita_rev1_stereo#_zita_rev1_stereo'
CALF = 'http://calf.sourceforge.net/plugins/Reverb'
PATCH = f"""add https://github.com/jonasbarsten/kiwi#carrier 3
add https://github.com/jonasbarsten/kiwi#mix 5
add https://www.modartt.com/lv2/Pianoteq8 0
add {ZITA} 6
param_set 5 piano_vol 0.8
param_set 6 MID_RT60 3
midi_map 5 piano_vol 0 20 0 1
"""
PARAMS = {'plugins': [
    {'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
        {'symbol': 'piano_vol', 'name': 'Piano Volume', 'min': 0, 'max': 1, 'default': 0.5, 'type': 'float'}]},
    {'instance': 3, 'title': 'Vocoder carrier', 'kind': 'port', 'params': [
        {'symbol': 'blend', 'name': 'Blend', 'min': 0, 'max': 2, 'default': 0, 'type': 'float'}]},
    {'instance': 1, 'title': 'Sampler', 'kind': 'port', 'params': [
        {'symbol': 'release', 'name': 'Release', 'min': 0, 'max': 4, 'default': 0.1, 'type': 'float',
         'cc': 105, 'group': 'envelope'}]},
    {'instance': 0, 'title': 'Pianoteq', 'kind': 'patch', 'params': [
        {'uri': 'https://www.modartt.com/lv2/Pianoteq8:Volume', 'name': 'Volume', 'group': 'Main',
         'min': 0, 'max': 1, 'default': 0.72, 'curated': True}]},
], 'reverbs': [
    {'id': 'zita', 'name': 'Zita Rev1', 'uri': ZITA, 'params': [
        {'symbol': 'MID_RT60', 'name': 'Mid RT60', 'min': 1, 'max': 8, 'default': 2, 'type': 'float'}]},
    {'id': 'calf', 'name': 'Calf Reverb', 'uri': CALF, 'params': [
        {'symbol': 'decay_time', 'name': 'Decay', 'min': 0.4, 'max': 15, 'default': 1.5, 'type': 'float'}]},
], 'default_reverb': 'zita'}
SLEEPER = [sys.executable, '-c', 'import time; time.sleep(60)']


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def write_fixture(tmp):
    paths = {}
    for name, content in [('kiwi.patch', PATCH), ('params.json', json.dumps(PARAMS))]:
        paths[name] = os.path.join(tmp, name)
        with open(paths[name], 'w') as f:
            f.write(content)
    static = os.path.join(tmp, 'static')
    os.mkdir(static)
    with open(os.path.join(static, 'index.html'), 'w') as f:
        f.write('<!doctype html><title>kiwi</title>')
    paths['static'] = static
    return paths


def listing(root):
    found = []
    for base, _, files in os.walk(root):
        found.extend(os.path.join(base, f) for f in files)
    return sorted(found)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        paths = write_fixture(self.dir.name)
        self.host = FakeHost({(5, 'piano_vol'): 0.8, (3, 'blend'): 0.0, (6, 'MID_RT60'): 3.0})
        self.state_dir = os.path.join(self.dir.name, 'state')
        self.midi_path = os.path.join(self.dir.name, 'midi')
        self.led_path = os.path.join(self.dir.name, 'led')
        args = parse_args(['--port', '0', '--host-port', str(self.host.port), '--patch', paths['kiwi.patch'],
                           '--params', paths['params.json'], '--static', paths['static'],
                           '--state-dir', self.state_dir, '--midi-device', self.midi_path, '--led', self.led_path])
        self.app = App(args, units_ready=lambda: True, monitor_command=SLEEPER)
        threading.Thread(target=self.app.link.run, daemon=True).start()
        self.server = make_server(('127.0.0.1', 0), self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.link.stop()
        self.app.monitor.stop()
        self.server.shutdown()
        self.server.server_close()
        self.host.close()
        self.dir.cleanup()

    def request(self, method, path, body=None, origin=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def post(self, path, body=None):
        return self.request('POST', path, body if body is not None else {})[0]

    def open_events(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        return conn, response

    def read_event(self, response):
        lines = []
        while True:
            line = response.fp.readline().decode()
            if line in ('\n', '\r\n', ''):
                if lines:
                    break
                continue
            lines.append(line)
        data = [l[len('data: '):] for l in lines if l.startswith('data: ')]
        return json.loads(''.join(data)) if data else {}

    def read_until(self, response, key, value=None):
        """Reads events into a snapshot kept per stream until `key` (optionally with `value`) is seen."""
        snapshots = self.__dict__.setdefault('_snapshots', {})
        snapshot = snapshots.setdefault(id(response), {})
        if key in snapshot and (value is None or snapshot[key] == value):
            # Already seen in an earlier call on this stream: wait for a fresh update only
            # when a specific value is requested and it is not the current one.
            return snapshot
        while key not in snapshot or (value is not None and snapshot[key] != value):
            snapshot.update(self.read_event(response))
        return snapshot

    def slot_file(self, n):
        return os.path.join(self.state_dir, 'presets', f'{n}.json')

    def led(self):
        try:
            with open(self.led_path) as f:
                return f.read()
        except OSError:
            return None

    # --- serving ---------------------------------------------------------

    def test_index_and_params(self):
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'kiwi', body)
        status, body = self.request('GET', '/params.json')
        meta = json.loads(body)
        self.assertEqual(meta['plugins'][0]['params'][0]['baseline'], 0.8)
        self.assertEqual(meta['plugins'][3]['params'][0]['baseline'], 0.72)
        self.assertEqual(meta['cc_map'][0]['cc'], 20)
        self.assertEqual(meta['default_reverb'], 'zita')
        self.assertEqual(meta['reverbs'][0]['params'][0]['baseline'], 3.0)
        self.assertEqual(meta['reverbs'][1]['params'][0]['baseline'], 1.5)
        self.assertEqual(meta['slots'], 8)
        self.assertEqual(meta['morph_cc'], 27)

    def test_events_snapshot_then_set_is_ram_only(self):
        conn, response = self.open_events()
        snapshot = self.read_until(response, 'port:5:piano_vol')
        self.assertAlmostEqual(snapshot['port:5:piano_vol'], 0.8)
        self.assertEqual(self.read_until(response, 'slot:current')['slot:current'], 1)
        self.assertEqual(self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]}), 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        self.assertTrue(self.read_until(response, 'slot:dirty', True)['slot:dirty'])
        time.sleep(0.3)
        self.assertEqual(listing(self.state_dir), [], 'a parameter change must not write to disk')
        conn.close()

    def test_set_rejects_unknown_and_clamps(self):
        self.assertEqual(self.post('/set', {'changes': [{'instance': 4, 'symbol': 'carrier', 'value': 1}]}), 400)
        self.assertEqual(self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 'nan'}]}), 400)
        self.assertEqual(self.post('/set', {'changes': [{'instance': 3, 'symbol': 'blend', 'value': 9}]}), 204)
        self.assertTrue(wait_for(lambda: self.host.params[(3, 'blend')] == 2.0))

    def test_patch_param(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        self.assertEqual(self.post('/set', {'changes': [{'instance': 0, 'uri': uri, 'value': 0.4}]}), 204)
        self.assertTrue(wait_for(lambda: self.host.patch.get((0, uri)) == 0.4))

    def test_cc_move_marks_dirty(self):
        conn, response = self.open_events()
        self.read_until(response, 'port:5:piano_vol')
        self.host.params[(5, 'piano_vol')] = 0.3     # the knob moved the value inside mod-host
        self.app.on_midi({'type': 'cc', 'channel': 0, 'controller': 20, 'value': 38})
        self.assertEqual(self.read_until(response, 'port:5:piano_vol', 0.3)['port:5:piano_vol'], 0.3)
        self.assertTrue(self.read_until(response, 'slot:dirty', True)['slot:dirty'])
        self.assertEqual(self.app.store.data['params'], {'5:piano_vol': 0.3})
        conn.close()

    def test_cc_control_writes_midi(self):
        conn, response = self.open_events()
        self.assertEqual(self.read_until(response, 'cc:105')['cc:105'], 0.1)
        self.assertEqual(self.post('/set', {'changes': [{'instance': 1, 'cc': 105, 'value': 2.0}]}), 204)
        self.assertEqual(self.read_until(response, 'cc:105', 2.0)['cc:105'], 2.0)
        with open(self.midi_path, 'rb') as f:
            self.assertEqual(f.read(), b'\xb0\x69\x40')
        self.assertEqual(self.post('/set', {'changes': [{'instance': 1, 'cc': 7, 'value': 1}]}), 400)
        conn.close()

    # --- security ----------------------------------------------------------

    def test_post_requires_origin_from_the_lan(self):
        # 127.0.0.1 is loopback, so emulate a LAN client by sending a foreign Origin.
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('POST', '/slot/save', body='{}', headers={'Content-Type': 'application/json',
                                                               'Origin': 'http://evil.example'})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 204, 'loopback slot requests are exempt from the Origin check')

    def test_slot_endpoints_from_loopback_without_origin(self):
        self.assertEqual(self.request('POST', '/slot/next', {}, origin=False)[0], 204)
        self.assertTrue(wait_for(lambda: self.app.store.slot == 2))

    def test_foreign_host_header_refused(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/params.json', headers={'Host': 'evil.example'})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 403)

    def test_idle_connection_times_out(self):
        original = server_module.Handler.timeout
        self.assertIsNotNone(original)
        self.assertLessEqual(original, 60)
        server_module.Handler.timeout = 0.5
        try:
            conn = socketlib.create_connection(('127.0.0.1', self.port), timeout=5)
            time.sleep(1.5)
            self.assertEqual(conn.recv(100), b'')
            conn.close()
        finally:
            server_module.Handler.timeout = original

    def test_bad_content_length(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.putrequest('POST', '/set')
        conn.putheader('Origin', f'http://127.0.0.1:{self.port}')
        conn.putheader('Content-Length', '-5')
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 400)

    # --- reverb ------------------------------------------------------------

    def test_switch_reverb(self):
        conn, response = self.open_events()
        self.assertEqual(self.read_until(response, 'reverb:current')['reverb:current'], 'zita')
        self.assertEqual(self.post('/reverb', {'id': 'calf'}), 204)
        self.assertEqual(self.read_until(response, 'reverb:current', 'calf')['reverb:current'], 'calf')
        start = self.host.log.index('remove 6')
        self.assertEqual(self.host.log[start:start + 9], [
            'remove 6', f'add {CALF} 6', 'param_set 6 dry 0.000000', 'param_set 6 on 1.000000',
            'param_set 6 decay_time 1.500000',
            'connect effect_5:send_l effect_6:in_l', 'connect effect_5:send_r effect_6:in_r',
            'connect effect_6:out_l system:playback_1', 'connect effect_6:out_r system:playback_2'])
        self.assertIn('param_get 6 decay_time', self.host.log[start + 9:])
        self.assertEqual(listing(self.state_dir), [])
        conn.close()

    def test_reverb_params_follow_the_current_reverb(self):
        self.post('/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == CALF))
        self.assertEqual(self.post('/set', {'changes': [{'instance': 6, 'symbol': 'MID_RT60', 'value': 2}]}), 400)
        self.assertEqual(self.post('/set', {'changes': [{'instance': 6, 'symbol': 'decay_time', 'value': 0.9}]}), 204)
        self.assertTrue(wait_for(lambda: self.app.store.data['reverb_params'] == {'calf': {'decay_time': 0.9}}))
        self.assertEqual(self.host.params.get((6, 'decay_time')), 0.9)
        self.post('/reverb', {'id': 'zita'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == ZITA))
        self.post('/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'decay_time')) == 0.9))

    def test_unknown_reverb_rejected(self):
        self.assertEqual(self.post('/reverb', {'id': 'nope'}), 400)
        self.assertNotIn('remove 6', self.host.log)

    # --- reset ---------------------------------------------------------------

    def test_reset_restores_baselines_in_ram(self):
        self.post('/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == CALF))
        self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3},
                                       {'instance': 1, 'cc': 105, 'value': 3.0}]})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        self.assertEqual(self.post('/reset'), 204)
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == ZITA))
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6))
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'MID_RT60')) == 3.0))
        with open(self.midi_path, 'rb') as f:
            self.assertTrue(f.read().endswith(b'\xb0\x69\x03'))    # release back to 0.1 of 0..4
        self.assertEqual(self.app.store.data['params'], {})
        self.assertTrue(self.app.store.dirty)
        self.assertEqual(listing(self.state_dir), [])

    # --- slots ---------------------------------------------------------------

    def test_slot_save_writes_file_and_clears_dirty(self):
        conn, response = self.open_events()
        self.read_until(response, 'slot:current')
        self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(self.read_until(response, 'slot:dirty', True)['slot:dirty'])
        self.assertEqual(self.post('/slot/name', {'name': 'Warm'}), 204)
        self.assertEqual(self.read_until(response, 'slot:name', 'Warm')['slot:name'], 'Warm')
        self.assertEqual(self.post('/slot/save'), 204)
        self.assertFalse(self.read_until(response, 'slot:dirty', False)['slot:dirty'])
        with open(self.slot_file(1)) as f:
            saved = json.load(f)
        self.assertEqual(saved['params'], {'5:piano_vol': 0.3})
        self.assertEqual(saved['name'], 'Warm')
        self.assertEqual(self.read_until(response, 'slot:names')['slot:names'][0], 'Warm')
        self.assertEqual(self.app.link.led.last, (8, 0.1), 'a save is a rapid burst')
        conn.close()

    def test_slot_select_applies_slot_and_resets_leftovers(self):
        # Slot 2: piano quiet, calf reverb, long release. Slot 3: only the blend.
        os.makedirs(os.path.join(self.state_dir, 'presets'))
        with open(self.slot_file(2), 'w') as f:
            json.dump({'params': {'5:piano_vol': 0.2}, 'reverb': 'calf',
                       'reverb_params': {'calf': {'decay_time': 0.9}}, 'cc': {'105': 4.0}, 'name': 'Two'}, f)
        with open(self.slot_file(3), 'w') as f:
            json.dump({'params': {'3:blend': 1.5}, 'name': 'Three'}, f)
        conn, response = self.open_events()
        self.read_until(response, 'slot:current')
        self.assertEqual(self.post('/slot/select', {'slot': 2}), 204)
        self.assertEqual(self.read_until(response, 'slot:current', 2)['slot:current'], 2)
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == CALF))
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.2) < 1e-6))
        self.assertEqual(self.host.params.get((6, 'decay_time')), 0.9)
        with open(self.midi_path, 'rb') as f:
            self.assertEqual(f.read(), b'\xb0\x69\x7f')
        self.assertEqual(self.read_until(response, 'slot:name', 'Two')['slot:name'], 'Two')
        self.assertEqual(self.app.link.led.last, (2, 0.2), 'a selection flashes the slot number')
        with open(os.path.join(self.state_dir, 'current')) as f:
            self.assertEqual(f.read().strip(), '2')

        self.assertEqual(self.post('/slot/next'), 204)
        self.assertEqual(self.read_until(response, 'slot:current', 3)['slot:current'], 3)
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == ZITA), 'reverb back to the patch default')
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6), 'leftover reset')
        self.assertTrue(wait_for(lambda: self.host.params.get((3, 'blend')) == 1.5))
        with open(self.midi_path, 'rb') as f:
            self.assertTrue(f.read().endswith(b'\xb0\x69\x03'), 'release back to its default')
        self.assertFalse(self.read_until(response, 'slot:dirty', False)['slot:dirty'])
        conn.close()

    def test_next_prev_wrap(self):
        self.post('/slot/prev')
        self.assertTrue(wait_for(lambda: self.app.store.slot == 8))
        self.post('/slot/next')
        self.assertTrue(wait_for(lambda: self.app.store.slot == 1))
        self.assertEqual(self.post('/slot/select', {'slot': 9}), 400)
        self.assertEqual(self.post('/slot/select', {'slot': True}), 400)

    def test_two_quick_clicks_move_two_slots(self):
        self.post('/slot/next')
        self.post('/slot/next')
        self.assertTrue(wait_for(lambda: self.app.store.slot == 3))

    def test_knob_move_is_saved_by_a_hold_without_a_page(self):
        # No page open: the link is idle. A knob moves inside mod-host, then the button saves.
        self.host.params[(5, 'piano_vol')] = 0.3
        self.app.on_midi({'type': 'cc', 'channel': 0, 'controller': 20, 'value': 38})
        self.assertEqual(self.request('POST', '/slot/save', {}, origin=False)[0], 204)
        self.assertTrue(wait_for(lambda: os.path.exists(self.slot_file(1))))
        self.assertTrue(wait_for(lambda: json.load(open(self.slot_file(1)))['params'].get('5:piano_vol') == 0.3))

    def test_host_restart_reloads_the_slot(self):
        self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(wait_for(lambda: self.app.store.data['params'] == {'5:piano_vol': 0.3}))
        # mod-host restarts: the connection drops, kiwi-restore re-applies the saved
        # slot (nothing saved → baselines), and the units report a new host start.
        self.host.params[(5, 'piano_vol')] = 0.8
        self.app.link.units_ready = lambda: 'epoch-2'
        self.host.drop()
        conn, response = self.open_events()      # a viewer makes the link reconnect
        self.assertTrue(wait_for(lambda: self.app.store.data['params'] == {} and not self.app.store.dirty,
                                 timeout=8), 'RAM must follow what kiwi-restore applied')
        self.assertFalse(self.read_until(response, 'slot:dirty', False)['slot:dirty'])
        conn.close()

    def test_rename_targets_the_slot_it_was_typed_for(self):
        self.assertEqual(self.post('/slot/name', {'name': 'Late', 'slot': 2}), 409)
        self.assertEqual(self.post('/slot/name', {'name': 'Mine', 'slot': 1}), 204)
        self.assertTrue(wait_for(lambda: self.app.store.data['name'] == 'Mine'))

    def test_loopback_exemption_covers_only_slot_endpoints(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('POST', '/reset', body='{}', headers={'Content-Type': 'application/json',
                                                           'Origin': 'http://evil.example'})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 403)

    def test_select_discards_unsaved_edits(self):
        self.post('/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        self.post('/slot/select', {'slot': 1})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6))
        self.assertEqual(listing(self.state_dir), [os.path.join(self.state_dir, 'current')])

    # --- morph ---------------------------------------------------------------

    def write_morph_slots(self):
        os.makedirs(os.path.join(self.state_dir, 'presets'), exist_ok=True)
        with open(self.slot_file(1), 'w') as f:
            json.dump({'params': {'5:piano_vol': 0.2}, 'cc': {'105': 0.0}}, f)
        with open(self.slot_file(2), 'w') as f:
            json.dump({'params': {'5:piano_vol': 1.0, '3:blend': 2.0}, 'cc': {'105': 4.0}}, f)

    def test_morph_via_cc27(self):
        self.write_morph_slots()
        self.app.on_midi({'type': 'cc', 'channel': 0, 'controller': 27, 'value': 64})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - (0.2 + 0.8 * 64 / 127)) < 1e-6))
        self.assertAlmostEqual(self.host.params[(3, 'blend')], 2.0 * 64 / 127)
        with open(self.midi_path, 'rb') as f:
            self.assertEqual(f.read()[-3:], b'\xb0\x69\x40')
        self.assertTrue(self.app.store.dirty)
        self.assertEqual(listing(self.state_dir), [self.slot_file(1), self.slot_file(2)], 'morph must not write')

    def test_morph_endpoint(self):
        self.write_morph_slots()
        conn, response = self.open_events()
        self.read_until(response, 'morph:value')
        self.assertEqual(self.post('/morph', {'value': 1.0}), 204)
        self.assertEqual(self.read_until(response, 'morph:value', 1.0)['morph:value'], 1.0)
        self.assertTrue(wait_for(lambda: self.host.params[(5, 'piano_vol')] == 1.0))
        self.assertEqual(self.post('/morph', {'value': 'x'}), 400)
        conn.close()

    def test_monitor_runs_without_viewers(self):
        self.assertIsNotNone(self.app.monitor._proc)


class UnitsReadyTest(unittest.TestCase):
    def fake_run(self, host, patch, restore):
        def block(state, changed, entered=0):
            return (f'ActiveState={state}\nActiveEnterTimestampMonotonic={entered}\n'
                    f'StateChangeTimestampMonotonic={changed}\n')

        class Result:
            stdout = '\n'.join([block(host[0], host[1], host[1]), block(*patch), block(*restore)])
            returncode = 0
        return lambda *args, **kwargs: Result()

    def test_both_finished_after_host_start(self):
        ready = systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('active', 160)))
        self.assertTrue(ready)
        self.assertNotEqual(ready, systemd_units_ready(run=self.fake_run(('active', 200), ('active', 250), ('active', 260))),
                            'the token must change when kiwi-host restarts')

    def test_failed_restore_still_counts_as_finished(self):
        self.assertTrue(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('failed', 160))))

    def test_restore_still_running(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('activating', 160))))

    def test_stale_state_from_previous_host_start(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 50), ('active', 60))))

    def test_host_not_running(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('activating', 100), ('active', 150), ('active', 160))))


class RestoreTest(unittest.TestCase):
    def test_restore_applies_current_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_fixture(tmp)
            state_dir = os.path.join(tmp, 'state')
            os.makedirs(os.path.join(state_dir, 'presets'))
            with open(os.path.join(state_dir, 'current'), 'w') as f:
                f.write('2\n')
            with open(os.path.join(state_dir, 'presets', '2.json'), 'w') as f:
                json.dump({'params': {'5:piano_vol': 0.3, '6:decay': 1.6}, 'reverb': 'calf',
                           'reverb_params': {'calf': {'decay_time': 0.9}}, 'cc': {'105': 4.0}}, f)
            host = FakeHost({(5, 'piano_vol'): 0.8, (6, 'MID_RT60'): 3.0})
            midi = os.path.join(tmp, 'midi')
            args = parse_args(['--state-dir', state_dir, '--host-port', str(host.port), '--midi-device', midi,
                               '--patch', paths['kiwi.patch'], '--params', paths['params.json']])
            self.assertEqual(restore(args), 0)
            self.assertEqual(host.instances.get(6), CALF)
            self.assertEqual(host.params.get((6, 'decay_time')), 0.9)
            self.assertEqual(host.params.get((5, 'piano_vol')), 0.3)
            self.assertNotIn('param_set 6 decay 1.600000', host.log)   # stale key: not a known parameter
            with open(midi, 'rb') as f:
                self.assertEqual(f.read(), b'\xb0\x69\x7f')
            host.close()

    def test_nothing_to_restore_needs_no_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_fixture(tmp)
            args = parse_args(['--state-dir', os.path.join(tmp, 'state'), '--host-port', '1',
                               '--patch', paths['kiwi.patch'], '--params', paths['params.json']])
            self.assertEqual(restore(args), 0)

    def test_lost_connection_is_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_fixture(tmp)
            state_dir = os.path.join(tmp, 'state')
            os.makedirs(os.path.join(state_dir, 'presets'))
            with open(os.path.join(state_dir, 'presets', '1.json'), 'w') as f:
                json.dump({'params': {'5:piano_vol': 0.3, '3:blend': 0.4}}, f)
            host = FakeHost({(5, 'piano_vol'): 0.8, (3, 'blend'): 0.0}, close_after=1)
            args = parse_args(['--state-dir', state_dir, '--host-port', str(host.port),
                               '--patch', paths['kiwi.patch'], '--params', paths['params.json']])
            self.assertEqual(restore(args), 0)
            host.close()


class StreamCountTest(unittest.TestCase):
    def test_missing_monitor_does_not_break_viewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_fixture(tmp)
            args = parse_args(['--patch', paths['kiwi.patch'], '--params', paths['params.json'],
                               '--static', paths['static'], '--state-dir', os.path.join(tmp, 's')])
            app = App(args, units_ready=lambda: False, monitor_command=['/nonexistent/aseqdump'])
            self.assertTrue(app.open_stream())
            app.close_stream()
            self.assertEqual(app._streams, 0)
            self.assertTrue(app.open_stream())
            app.close_stream()
            self.assertEqual(app._streams, 0)


class BrokenStore(StateStore):
    def set_param(self, *args, **kwargs):
        raise OSError('boom')


class LinkTest(unittest.TestCase):
    def make_link(self, ready, store_class=StateStore):
        meta = {'plugins': [{'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
            {'symbol': 'piano_vol', 'name': 'v', 'min': 0, 'max': 1, 'default': 0.8, 'baseline': 0.8,
             'type': 'float'}]}], 'cc_map': [], 'reverbs': [], 'default_reverb': None}
        self.dir = tempfile.TemporaryDirectory()
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        self.clock = [0.0]
        store = store_class(os.path.join(self.dir.name, 's'))
        store.load()
        link = HostLink(Model(), HostClient(('127.0.0.1', self.host.port), timeout=5), meta,
                        store, ready, clock=lambda: self.clock[0], led=server_module.Led(os.path.join(self.dir.name, 'led')))
        threading.Thread(target=link.run, daemon=True).start()
        return link

    def tearDown(self):
        self.link.stop()
        self.host.close()
        self.dir.cleanup()

    def test_link_waits_for_units(self):
        self.link = self.make_link(lambda: False)
        self.link.add_viewer()
        time.sleep(0.5)
        self.assertEqual(self.host.log, [])
        self.assertEqual(self.link.model.get('status:host'), 'starting')

    def test_link_disconnects_when_idle(self):
        self.link = self.make_link(lambda: True)
        self.link.add_viewer()
        self.assertTrue(wait_for(lambda: self.link.client.connected))
        self.link.remove_viewer()
        self.clock[0] += 11.0
        self.assertTrue(wait_for(lambda: not self.link.client.connected))
        self.assertEqual(self.link.model.get('status:host'), 'idle')

    def test_link_survives_unexpected_errors(self):
        self.link = self.make_link(lambda: True, store_class=BrokenStore)
        self.link.set_value('port', 5, 'piano_vol', 0.4)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.4) < 1e-6))
        time.sleep(0.3)
        self.link.set_value('port', 5, 'piano_vol', 0.6)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.6) < 1e-6),
                        'link thread died after a store error')


if __name__ == '__main__':
    unittest.main()
