import os
import tempfile
import unittest

from kiwi_web.led import Led


class LedTest(unittest.TestCase):
    def test_blink_writes_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'led')
            led = Led(path)
            self.assertTrue(led.blink(3))
            with open(path) as f:
                self.assertEqual(f.read(), '3')

    def test_unusable_path_is_reported_not_raised(self):
        led = Led('/nonexistent/led')
        self.assertFalse(led.blink(1))
        self.assertFalse(led.blink(2))


if __name__ == '__main__':
    unittest.main()
