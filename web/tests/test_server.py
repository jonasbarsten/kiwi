import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

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


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        paths = {}
        for name, content in [('kiwi.patch', PATCH), ('params.json', json.dumps(PARAMS))]:
            paths[name] = os.path.join(self.dir.name, name)
            with open(paths[name], 'w') as f:
                f.write(content)
        static = os.path.join(self.dir.name, 'static')
        os.mkdir(static)
        with open(os.path.join(static, 'index.html'), 'w') as f:
            f.write('<!doctype html><title>kiwi</title>')
        self.host = FakeHost({(5, 'piano_vol'): 0.8, (3, 'blend'): 0.0, (6, 'MID_RT60'): 3.0})
        self.state_path = os.path.join(self.dir.name, 'state', 'state.json')
        self.midi_path = os.path.join(self.dir.name, 'midi')
        args = parse_args(['--port', '0', '--host-port', str(self.host.port), '--patch', paths['kiwi.patch'],
                           '--params', paths['params.json'], '--static', static,
                           '--state', self.state_path, '--save-delay', '0.2', '--midi-device', self.midi_path])
        self.app = App(args, units_ready=lambda: True,
                       monitor_command=[sys.executable, '-c', 'import time; time.sleep(60)'])
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

    def test_index_and_params(self):
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'kiwi', body)
        status, body = self.request('GET', '/params.json')
        meta = json.loads(body)
        mix = meta['plugins'][0]['params'][0]
        self.assertEqual(mix['baseline'], 0.8)
        self.assertEqual(meta['plugins'][3]['params'][0]['baseline'], 0.72)
        self.assertEqual(meta['cc_map'][0]['cc'], 20)
        self.assertEqual(meta['default_reverb'], 'zita')
        self.assertEqual(meta['reverbs'][0]['params'][0]['baseline'], 3.0)   # patch value for the default reverb
        self.assertEqual(meta['reverbs'][1]['params'][0]['baseline'], 1.5)   # plugin default for the others

    def read_until(self, response, key):
        snapshot = {}
        while key not in snapshot:
            snapshot.update(self.read_event(response))
        return snapshot

    def test_switch_reverb(self):
        conn, response = self.open_events()
        self.assertEqual(self.read_until(response, 'reverb:current')['reverb:current'], 'zita')
        status, _ = self.request('POST', '/reverb', {'id': 'calf'})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_until(response, 'reverb:current')['reverb:current'], 'calf')
        start = self.host.log.index('remove 6')
        self.assertEqual(self.host.log[start:start + 8], [
            'remove 6', f'add {CALF} 6', 'param_set 6 dry 0.000000', 'param_set 6 on 1.000000',
            'connect effect_5:send_l effect_6:in_l', 'connect effect_5:send_r effect_6:in_r',
            'connect effect_6:out_l system:playback_1', 'connect effect_6:out_r system:playback_2'])
        self.assertIn('param_get 6 decay_time', self.host.log[start + 8:])
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['reverb'], 'calf')
        conn.close()

    def test_reverb_params_follow_the_current_reverb(self):
        self.request('POST', '/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == CALF))
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 6, 'symbol': 'MID_RT60', 'value': 2}]})
        self.assertEqual(status, 400, 'a zita parameter must be refused while calf is loaded')
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 6, 'symbol': 'decay_time', 'value': 0.9}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'decay_time')) == 0.9))
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['reverb_params'], {'calf': {'decay_time': 0.9}})
        # Switching back and forth brings the tuned setting back with its reverb.
        self.request('POST', '/reverb', {'id': 'zita'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == ZITA))
        self.request('POST', '/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'decay_time')) == 0.9))

    def test_cc_control_writes_midi_and_saves(self):
        conn, response = self.open_events()
        self.assertEqual(self.read_until(response, 'cc:105')['cc:105'], 0.1)   # the default
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 1, 'cc': 105, 'value': 2.0}]})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_until(response, 'cc:105')['cc:105'], 2.0)
        self.assertTrue(wait_for(lambda: os.path.exists(self.midi_path) and os.path.getsize(self.midi_path) >= 3))
        with open(self.midi_path, 'rb') as f:
            self.assertEqual(f.read(), b'\xb0\x69\x40')   # channel 1, CC 105, 2.0 of 0..4 → 64
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['cc'], {'105': 2.0})
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 1, 'cc': 7, 'value': 1}]})
        self.assertEqual(status, 400, 'only the listed CCs may be sent')
        conn.close()

    def test_unknown_reverb_rejected(self):
        status, _ = self.request('POST', '/reverb', {'id': 'nope'})
        self.assertEqual(status, 400)
        self.assertNotIn('remove 6', self.host.log)

    def test_reset_returns_to_default_reverb(self):
        self.request('POST', '/reverb', {'id': 'calf'})
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == CALF))
        self.request('POST', '/set', {'changes': [{'instance': 6, 'symbol': 'decay_time', 'value': 0.9}]})
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'decay_time')) == 0.9))
        status, _ = self.request('POST', '/reset', {})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.instances.get(6) == ZITA))
        self.assertTrue(wait_for(lambda: self.host.params.get((6, 'MID_RT60')) == 3.0))
        self.assertTrue(wait_for(lambda: not os.path.exists(self.state_path)))

    def test_events_snapshot_then_set(self):
        conn, response = self.open_events()
        snapshot = {}
        while 'port:5:piano_vol' not in snapshot:
            snapshot.update(self.read_event(response))
        self.assertAlmostEqual(snapshot['port:5:piano_vol'], 0.8)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['params'], {'5:piano_vol': 0.3})
        conn.close()

    def test_set_rejects_unknown_and_clamps(self):
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 4, 'symbol': 'carrier', 'value': 1}]})
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 'nan'}]})
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 3, 'symbol': 'blend', 'value': 9}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.params[(3, 'blend')] == 2.0))

    def test_patch_param(self):
        uri = 'https://www.modartt.com/lv2/Pianoteq8:Volume'
        status, _ = self.request('POST', '/set', {'changes': [{'instance': 0, 'uri': uri, 'value': 0.4}]})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: self.host.patch.get((0, uri)) == 0.4))

    def test_idle_connection_times_out(self):
        import socket as socketlib
        from kiwi_web import server as server_module
        original = server_module.Handler.timeout
        self.assertIsNotNone(original, 'connections must not be allowed to idle forever')
        self.assertLessEqual(original, 60)
        server_module.Handler.timeout = 0.5
        try:
            conn = socketlib.create_connection(('127.0.0.1', self.port), timeout=5)
            time.sleep(1.5)
            self.assertEqual(conn.recv(100), b'', 'idle connection still open')
            conn.close()
        finally:
            server_module.Handler.timeout = original

    def test_foreign_host_header_refused(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/params.json', headers={'Host': 'evil.example'})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 403)

    def test_post_requires_origin(self):
        status, _ = self.request('POST', '/set', {'changes': []}, origin=False)
        self.assertEqual(status, 403)

    def test_cc_move_is_autosaved(self):
        conn, response = self.open_events()
        snapshot = {}
        while 'port:5:piano_vol' not in snapshot:
            snapshot.update(self.read_event(response))
        self.host.params[(5, 'piano_vol')] = 0.3     # the knob moved the value inside mod-host
        self.app.on_midi({'type': 'cc', 'channel': 0, 'controller': 20, 'value': 38})
        self.assertTrue(wait_for(lambda: os.path.exists(self.state_path)))
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)['params'], {'5:piano_vol': 0.3})
        conn.close()

    def test_reset_restores_baseline(self):
        self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        status, _ = self.request('POST', '/reset', {})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6))
        self.assertTrue(wait_for(lambda: not os.path.exists(self.state_path)))


class UnitsReadyTest(unittest.TestCase):
    """Ready once kiwi-patch and kiwi-restore finished (active or failed) since kiwi-host started."""

    def fake_run(self, host, patch, restore):
        def block(state, changed, entered=0):
            return (f'ActiveState={state}\nActiveEnterTimestampMonotonic={entered}\n'
                    f'StateChangeTimestampMonotonic={changed}\n')

        class Result:
            stdout = '\n'.join([block(host[0], host[1], host[1]), block(*patch), block(*restore)])
            returncode = 0
        return lambda *args, **kwargs: Result()

    def test_both_finished_after_host_start(self):
        self.assertTrue(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('active', 160))))

    def test_failed_restore_still_counts_as_finished(self):
        self.assertTrue(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('failed', 160))))

    def test_restore_still_running(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 150), ('activating', 160))))

    def test_stale_state_from_previous_host_start(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('active', 100), ('active', 50), ('active', 60))))

    def test_host_not_running(self):
        self.assertFalse(systemd_units_ready(run=self.fake_run(('activating', 100), ('active', 150), ('active', 160))))


class RestoreTest(unittest.TestCase):
    def test_restores_reverb_choice_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name, content in [('kiwi.patch', PATCH), ('params.json', json.dumps(PARAMS))]:
                paths[name] = os.path.join(tmp, name)
                with open(paths[name], 'w') as f:
                    f.write(content)
            state = os.path.join(tmp, 'state.json')
            with open(state, 'w') as f:
                # '6:decay' is a stale entry from before per-reverb settings: it must be ignored.
                json.dump({'params': {'5:piano_vol': 0.3, '6:decay': 1.6}, 'reverb': 'calf',
                           'reverb_params': {'calf': {'decay_time': 0.9}}, 'cc': {'105': 4.0}}, f)
            host = FakeHost({(5, 'piano_vol'): 0.8, (6, 'MID_RT60'): 3.0})
            midi = os.path.join(tmp, 'midi')
            args = parse_args(['--state', state, '--host-port', str(host.port), '--midi-device', midi,
                               '--patch', paths['kiwi.patch'], '--params', paths['params.json']])
            self.assertEqual(restore(args), 0)
            with open(midi, 'rb') as f:
                self.assertEqual(f.read(), b'\xb0\x69\x7f')
            self.assertEqual(host.instances.get(6), CALF)
            self.assertEqual(host.params.get((6, 'decay_time')), 0.9)
            self.assertEqual(host.params.get((5, 'piano_vol')), 0.3)
            self.assertNotIn('param_set 6 decay 1.600000', host.log)
            host.close()

    def test_lost_connection_is_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, 'state.json')
            with open(state, 'w') as f:
                json.dump({'params': {'5:piano_vol': 0.3, '5:piano_send': 0.4}}, f)
            host = FakeHost({(5, 'piano_vol'): 0.8, (5, 'piano_send'): 0.2}, close_after=1)
            args = parse_args(['--state', state, '--host-port', str(host.port)])
            self.assertEqual(restore(args), 0)
            host.close()


class StreamCountTest(unittest.TestCase):
    def test_missing_monitor_does_not_leak_viewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name, content in [('kiwi.patch', PATCH), ('params.json', json.dumps(PARAMS))]:
                paths[name] = os.path.join(tmp, name)
                with open(paths[name], 'w') as f:
                    f.write(content)
            os.mkdir(os.path.join(tmp, 'static'))
            with open(os.path.join(tmp, 'static', 'index.html'), 'w') as f:
                f.write('kiwi')
            args = parse_args(['--patch', paths['kiwi.patch'], '--params', paths['params.json'],
                               '--static', os.path.join(tmp, 'static'), '--state', os.path.join(tmp, 's.json')])
            app = App(args, units_ready=lambda: False, monitor_command=['/nonexistent/aseqdump'])
            self.assertTrue(app.open_stream())
            app.close_stream()
            self.assertEqual(app._streams, 0)
            self.assertTrue(app.open_stream())
            app.close_stream()
            self.assertEqual(app._streams, 0)


class BrokenStore(StateStore):
    def due(self):
        return True

    def flush(self):
        raise OSError('disk full')


class LinkTest(unittest.TestCase):
    def make_link(self, ready, store_class=StateStore):
        meta = {'plugins': [{'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
            {'symbol': 'piano_vol', 'name': 'v', 'min': 0, 'max': 1, 'default': 0.8, 'baseline': 0.8,
             'type': 'float'}]}], 'cc_map': []}
        self.dir = tempfile.TemporaryDirectory()
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        self.clock = [0.0]
        link = HostLink(Model(), HostClient(('127.0.0.1', self.host.port), timeout=5), meta,
                        store_class(os.path.join(self.dir.name, 's.json')), ready, clock=lambda: self.clock[0])
        threading.Thread(target=link.run, daemon=True).start()
        return link

    def test_link_survives_unexpected_errors(self):
        self.link = self.make_link(lambda: True, store_class=BrokenStore)
        time.sleep(0.5)
        self.link.set_value('port', 5, 'piano_vol', 0.4)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.4) < 1e-6),
                        'link thread died after a store error')

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


if __name__ == '__main__':
    unittest.main()
