import unittest

from kiwi_web.modhost import HostClient, HostError, parse_response
from tests.fakehost import FakeHost


class ParseResponseTest(unittest.TestCase):
    def test_value(self):
        self.assertEqual(parse_response(b'resp 0 0.8000'), (0, '0.8000'))

    def test_error_code(self):
        self.assertEqual(parse_response(b'resp -103'), (-103, None))

    def test_garbage(self):
        with self.assertRaises(HostError):
            parse_response(b'hello')


class HostClientTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost({(5, 'piano_vol'): 0.8})
        self.client = HostClient(('127.0.0.1', self.host.port), timeout=5.0)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.host.close()

    def test_param_roundtrip(self):
        self.assertAlmostEqual(self.client.param_get(5, 'piano_vol'), 0.8)
        self.assertEqual(self.client.param_set(5, 'piano_vol', 0.5), 0)
        self.assertAlmostEqual(self.host.params[(5, 'piano_vol')], 0.5)

    def test_unknown_param(self):
        self.assertIsNone(self.client.param_get(5, 'nope'))
        self.assertEqual(self.client.param_set(5, 'nope', 1.0), -103)

    def test_one_command_per_send(self):
        for _ in range(5):
            self.client.cpu_load()
        self.assertTrue(all(chunk.count(b'\0') == 1 for chunk in self.host.chunks))

    def test_rejects_nul(self):
        with self.assertRaises(ValueError):
            self.client.command('cpu_load\0cpu_load')


class DropTest(unittest.TestCase):
    def test_client_drops_on_eof(self):
        host = FakeHost({(5, 'piano_vol'): 0.8}, close_after=1)
        client = HostClient(('127.0.0.1', host.port), timeout=5.0)
        client.connect()
        client.cpu_load()
        with self.assertRaises(HostError):
            client.cpu_load()
        self.assertFalse(client.connected)
        host.close()


if __name__ == '__main__':
    unittest.main()
