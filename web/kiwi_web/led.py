"""Blinks the Pisound LED: writing a count to its sysfs file flashes it that many times."""


class Led:
    def __init__(self, path='/sys/kernel/pisound/led'):
        self.path = path
        self._reported = False

    def blink(self, count):
        try:
            with open(self.path, 'w') as f:
                f.write(str(int(count)))
            return True
        except OSError as error:
            if not self._reported:
                print(f'kiwi-web: LED {self.path}: {error}', flush=True)
                self._reported = True
            return False
