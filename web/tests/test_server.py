import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from kiwi_web.modhost import HostClient
from kiwi_web.server import App, HostLink, Model, make_server, parse_args
from kiwi_web.state import StateStore
from tests.fakehost import FakeHost

PATCH = """add https://github.com/jonasbarsten/kiwi#carrier 3
add https://github.com/jonasbarsten/kiwi#mix 5
add https://www.modartt.com/lv2/Pianoteq8 0
param_set 5 piano_vol 0.8
midi_map 5 piano_vol 0 20 0 1
"""
PARAMS = {'plugins': [
    {'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
        {'symbol': 'piano_vol', 'name': 'Piano Volume', 'min': 0, 'max': 1, 'default': 0.5, 'type': 'float'}]},
    {'instance': 3, 'title': 'Vocoder carrier', 'kind': 'port', 'params': [
        {'symbol': 'blend', 'name': 'Blend', 'min': 0, 'max': 2, 'default': 0, 'type': 'float'}]},
    {'instance': 0, 'title': 'Pianoteq', 'kind': 'patch', 'params': [
        {'uri': 'https://www.modartt.com/lv2/Pianoteq8:Volume', 'name': 'Volume', 'group': 'Main',
         'min': 0, 'max': 1, 'default': 0.72, 'curated': True}]},
]}


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
        self.host = FakeHost({(5, 'piano_vol'): 0.8, (3, 'blend'): 0.0})
        self.state_path = os.path.join(self.dir.name, 'state', 'state.json')
        args = parse_args(['--port', '0', '--host-port', str(self.host.port), '--patch', paths['kiwi.patch'],
                           '--params', paths['params.json'], '--static', static,
                           '--state', self.state_path, '--save-delay', '0.2'])
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
        self.assertEqual(meta['plugins'][2]['params'][0]['baseline'], 0.72)
        self.assertEqual(meta['cc_map'][0]['cc'], 20)

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

    def test_post_requires_origin(self):
        status, _ = self.request('POST', '/set', {'changes': []}, origin=False)
        self.assertEqual(status, 403)

    def test_reset_restores_baseline(self):
        self.request('POST', '/set', {'changes': [{'instance': 5, 'symbol': 'piano_vol', 'value': 0.3}]})
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.3) < 1e-6))
        status, _ = self.request('POST', '/reset', {})
        self.assertEqual(status, 204)
        self.assertTrue(wait_for(lambda: abs(self.host.params[(5, 'piano_vol')] - 0.8) < 1e-6))
        self.assertTrue(wait_for(lambda: not os.path.exists(self.state_path)))


class LinkTest(unittest.TestCase):
    def make_link(self, ready):
        meta = {'plugins': [{'instance': 5, 'title': 'Mix', 'kind': 'port', 'params': [
            {'symbol': 'piano_vol', 'name': 'v', 'min': 0, 'max': 1, 'default': 0.8, 'baseline': 0.8,
             'type': 'float'}]}], 'cc_map': []}
        self.dir = tempfile.TemporaryDirectory()
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        self.clock = [0.0]
        link = HostLink(Model(), HostClient(('127.0.0.1', self.host.port), timeout=5), meta,
                        StateStore(os.path.join(self.dir.name, 's.json')), ready, clock=lambda: self.clock[0])
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


if __name__ == '__main__':
    unittest.main()
