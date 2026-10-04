import os
import tempfile
import time
import unittest

from kiwi_web.led import Led


class LedTest(unittest.TestCase):
    def test_blink_flashes_count_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'led')
            led = Led(path)
            self.assertTrue(led.blink(3, interval=0.02))
            time.sleep(0.2)
            self.assertEqual(led.flashes, 3)
            self.assertEqual(led.last, (3, 0.02))
            with open(path) as f:
                self.assertEqual(f.read(), '1')   # a short flash, not a count

    def test_new_pattern_cancels_the_running_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Led(os.path.join(tmp, 'led'))
            led.blink(50, interval=0.02)
            time.sleep(0.05)
            led.blink(1)
            flashes = led.flashes
            time.sleep(0.15)
            self.assertEqual(led.flashes, flashes, 'the old pattern kept flashing')

    def test_unusable_path_is_reported_not_raised(self):
        led = Led('/nonexistent/led')
        self.assertFalse(led.blink(1))
        self.assertFalse(led.blink(2))


if __name__ == '__main__':
    unittest.main()
