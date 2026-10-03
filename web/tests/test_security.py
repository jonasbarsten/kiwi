import unittest

from kiwi_web.security import is_private, same_origin


class SecurityTest(unittest.TestCase):
    def test_private_addresses(self):
        for address in ['192.168.1.5', '10.0.0.2', '172.16.3.4', '127.0.0.1',
                        '::1', 'fe80::1', '::ffff:192.168.0.4', '169.254.3.3']:
            self.assertTrue(is_private(address), address)

    def test_public_addresses(self):
        for address in ['8.8.8.8', '2001:4860:4860::8888', '::ffff:8.8.8.8', 'junk', '']:
            self.assertFalse(is_private(address), address)

    def test_same_origin(self):
        self.assertTrue(same_origin('http://patchbox.local', 'patchbox.local'))
        self.assertTrue(same_origin('http://192.168.1.126:8080', '192.168.1.126:8080'))
        self.assertFalse(same_origin('http://evil.example', 'patchbox.local'))
        self.assertFalse(same_origin(None, 'patchbox.local'))
        self.assertFalse(same_origin('http://patchbox.local', None))


if __name__ == '__main__':
    unittest.main()
