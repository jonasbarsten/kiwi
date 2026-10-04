"""Blinks the Pisound LED.

The sysfs file takes a flash *duration* (0-255 firmware units), one flash per
write, so patterns are made here: `count` flashes spaced `interval` seconds
apart, run on a background thread. A new pattern cancels a running one.
"""
import threading

FLASH = 1   # one short flash (the duration Patchbox's own scripts use)


class Led:
    def __init__(self, path='/sys/kernel/pisound/led'):
        self.path = path
        self.last = None
        self.flashes = 0
        self._reported = False
        self._lock = threading.Lock()
        self._generation = 0

    def _flash(self, duration):
        try:
            with open(self.path, 'w') as f:
                f.write(str(int(duration)))
            self.flashes += 1
            return True
        except OSError as error:
            if not self._reported:
                print(f'kiwi-web: LED {self.path}: {error}', flush=True)
                self._reported = True
            return False

    def blink(self, count, interval=0.35, duration=FLASH):
        """Starts `count` flashes, `interval` seconds apart. Returns False if the LED is unusable."""
        with self._lock:
            self._generation += 1
            generation = self._generation
        self.last = (count, interval)
        if not self._flash(duration):
            return False

        def rest():
            for _ in range(count - 1):
                if threading.Event().wait(interval) or generation != self._generation:
                    return
                self._flash(duration)

        if count > 1:
            threading.Thread(target=rest, daemon=True).start()
        return True
