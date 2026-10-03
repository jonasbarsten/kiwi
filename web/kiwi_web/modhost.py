"""Lockstep client for mod-host's command socket.

mod-host serves one client at a time and treats each received chunk as one
command, so every command is sent on its own and its reply is read before the
next one is sent.
"""
import socket
import threading


class HostError(Exception):
    pass


def parse_response(raw):
    text = raw.decode('utf-8', 'replace').strip()
    if not text.startswith('resp '):
        raise HostError(f'unexpected reply: {text!r}')
    code, _, value = text[5:].partition(' ')
    return int(code), (value or None)


class HostClient:
    def __init__(self, address=('127.0.0.1', 5555), timeout=60.0):
        self.address = address
        self.timeout = timeout
        self._sock = None
        self._buffer = b''
        self._lock = threading.Lock()

    @property
    def connected(self):
        return self._sock is not None

    def connect(self):
        with self._lock:
            if self._sock is None:
                sock = socket.create_connection(self.address, timeout=5.0)
                sock.settimeout(self.timeout)
                self._sock = sock
                self._buffer = b''

    def close(self):
        # Taking the lock guarantees no reply is outstanding when the socket closes.
        with self._lock:
            self._drop()

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b''

    def command(self, line):
        if '\0' in line:
            raise ValueError('command contains NUL')
        with self._lock:
            if self._sock is None:
                raise HostError('not connected')
            try:
                self._sock.sendall(line.encode() + b'\0')
                while b'\0' not in self._buffer:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise HostError('mod-host closed the connection')
                    self._buffer += chunk
            except (OSError, HostError) as error:
                self._drop()
                raise HostError(str(error)) from error
            raw, self._buffer = self._buffer.split(b'\0', 1)
            return parse_response(raw)

    def param_get(self, instance, symbol):
        code, value = self.command(f'param_get {instance} {symbol}')
        return float(value) if code == 0 and value is not None else None

    def param_set(self, instance, symbol, value):
        code, _ = self.command(f'param_set {instance} {symbol} {value:.6f}')
        return code

    def patch_set(self, instance, uri, value):
        code, _ = self.command(f'patch_set {instance} {uri} {value:.6f}')
        return code

    def cpu_load(self):
        code, value = self.command('cpu_load')
        return float(value) if code == 0 and value is not None else None
